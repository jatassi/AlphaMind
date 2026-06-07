"""End-to-end phase orchestrator — ``run_invocation``.

Single async entrypoint the APScheduler driver (story 04a), the emergency
receiver (story 04b), and the CLI ``--once`` path call. Per the design's
snapshot-isolation contract
(``docs/design/05-execution-layer/state-persistence.md`` § Snapshot isolation),
it runs three separate transactions per invocation:

  1. Row insert: ``insert_invocation_row`` commits the ``invocations`` row in
     its own short transaction so the row is durable + visible to fresh-session
     reads from the moment fill collection starts.
  2. Fill collection: one transaction wrapping fill integration + activity-log writes
     + the ``fill_collection_completed_at`` stamp. Commits at the close of the phase.
  3. Snapshot read between phases: ``assemble_snapshot`` uses fresh sessions
     via the repository factory; it now correctly sees the committed
     ``fill_collection_completed_at`` and produces a real :class:`AssembledSnapshot`.
     The snapshot feeds :class:`SnapshotBackedSynthesizerReader` (consumed by
     the analysis pipeline) and the decision pipeline. The analysis subtree
     opens its own sync ``Session`` (the distillation orchestrator threads it
     through ``asyncio.to_thread`` into sync SQLAlchemy callsites) from the
     context's ``sync_session_factory``.
  4. command execution: one transaction wrapping envelope dispatch + the
     ``command_execution_completed_at`` stamp + row summary writeback.

Per ``docs/design/mid-pipeline-failure-handling.md``:
  * A fill-collection abort rolls back fill collection's writes; the invocation row stays
    with ``fill_collection_completed_at IS NULL``, and the repository's consistency
    guard refuses snapshot reads against it — next invocation retries fills.
  * Between-phase aborts (snapshot assembly, analysis, decision) leave Phase
    1 committed and skip command execution; the next scheduled invocation regenerates
    briefs from current state.
  * command execution aborts roll back the in-flight envelope; already-committed work
    from prior envelopes remains durable.

The function composes existing layer primitives without inventing new
submission paths: ``gather_fill_collection_inputs`` (story 03b / ALP-445),
``process_unprocessed_fills`` (story 07 / ALP-365), ``run_analysis_pipeline``
(ALP-276), ``run_decision_pipeline`` (ALP-403), and ``dispatch_command_execution``
(story 03b).

ALP-472 lifted the layer-spanning helpers out of this file into their
feature packages: the regime/risk-parameter shims live in
``risk_guardrails/regime_adaptation/`` (``build_active_risk_parameters``,
``build_synthetic_regime_output``, ``load_prior_active_risk_parameters``,
``make_repository_providers``, ``evaluate_pre_event_decision``,
``evaluate_stress_decision``); the sector / ticker-scope views live in
``config/assets_views.py``. The mode-translation dispatch tables consolidated
onto :class:`alphamind._kernel.mode.PipelineMode`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import exchange_calendars
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.mode import PipelineMode
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis.thesis_resolution import (
    persist_thesis_resolutions,
    prepare_closed_position_resolutions,
)
from alphamind.config.assets_views import (
    build_sector_resolver,
    sectors_config_from_assets,
    ticker_scope_from_assets,
)
from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.config.load import PipelineConfig
from alphamind.config.loaders import load_overlays, read_yaml_file
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.config.models.main import ExecutionMode, MainConfig
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.execution.write_paths.command_execution.atomic import (
    invocation_has_pending_submit_strand,
)
from alphamind.execution.write_paths.fill_collection import (
    FillCollectionSummary,
    process_unprocessed_fills,
    rederive_thesis_ledgers,
)
from alphamind.persistence.retry import run_with_sqlite_busy_retry
from alphamind.persistence.session import begin_write_immediate
from alphamind.pipeline.analysis import run_analysis_pipeline
from alphamind.pipeline.decision import run_decision_pipeline
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.consumers.synthesizer import (
    SnapshotBackedSynthesizerReader,
    SynthesizerPortfolioStateReader,
    adapt_ticker_sector_resolver,
)
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.risk_guardrails.borrow_cost import build_borrow_cost_resolver
from alphamind.risk_guardrails.breach_behavior.halt_state import compute_halt_state
from alphamind.risk_guardrails.breach_behavior.types import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    MarketInputs,
    from_resolved_config,
)
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    build_active_risk_parameters,
    build_inputs_from_distillation_outputs,
    evaluate_pre_event_decision,
    evaluate_stress_decision,
    load_config_fan,
    make_repository_providers,
    resolve_regime_adaptation,
)
from alphamind.risk_guardrails.regime_adaptation.persistence import insert_state
from alphamind.risk_guardrails.regime_adaptation.stress_activator import (
    fetch_composite_alert_state,
)
from alphamind.risk_guardrails.state_delivery.config import (
    StateDeliveryConfig,
    load_state_delivery_config,
)
from alphamind.scheduler.account_activities_poll import run_account_activities_poll
from alphamind.scheduler.borrow_accrual import run_borrow_accrual
from alphamind.scheduler.command_execution_dispatch import (
    CommandExecutionSummary,
    dispatch_command_execution,
)
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.control.models import (
    InvocationEndedEvent,
    InvocationStartedEvent,
    PhaseTransitionEvent,
    _RunType,
)
from alphamind.scheduler.control.sse_progress_bridge import (
    PipelineSSEProgressBridge,
    make_latency_budget_lookup,
)
from alphamind.scheduler.fill_collection_inputs import gather_fill_collection_inputs
from alphamind.scheduler.invocation import insert_invocation_record
from alphamind.scheduler.run_context import RunInvocationContext
from alphamind.scheduler.runtime import resolve_runtime_dimensions
from alphamind.scripts._common import load_distillation_config
from alphamind.state.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)
from alphamind.state.drawdown_reader import read_drawdown_state
from alphamind.state.invocation_context.config_change import (
    emit_baseline_config_change_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)
from alphamind.state.invocation_context.records import (
    TriggerType,
)
from alphamind.state.repository import (
    SqlOptionPriceProvider,
    build_sql_portfolio_state_repository,
)
from alphamind.state.tables.invocations import InvocationRow

__all__ = ["InvocationSummary", "run_invocation"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationSummary:
    """Typed return value of :func:`run_invocation`.

    Returned on success; on exception the caller observes the raised
    exception and no summary is produced.
    """

    invocation_id: str
    trigger_type: TriggerType
    trigger_source: str
    firing_run_type: RunType
    fill_collection_summary: FillCollectionSummary
    commands_submitted: int
    commands_rejected: int
    staleness_flag: bool
    duration_seconds: float


_PORTFOLIO_STATE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "portfolio_state.yaml"
)


def _resolve_regime_adaptation_for_invocation(
    *,
    sync_session_factory: sessionmaker[Session],
    config_dir: Path,
    pipeline_config: PipelineConfig,
    analysis_result: Any,
    assembled: AssembledSnapshot,
    prior_parameter_set: ActiveRiskParameterSet | None,
    invocation_id: str,
    now: datetime,
) -> RegimeAdaptationOutput:
    """Invoke :func:`resolve_regime_adaptation` for the current invocation.

    Wires the regime resolver into the scheduler's per-invocation flow:
    parses the fresh distillation classification out of
    :class:`AnalysisPipelineResult`, reads the composite-alert state, and
    runs the resolver in a sync session. The new persisted state is
    appended to ``regime_adaptation_state`` so the monitor sees a populated
    row on its next tick. ``prior_parameter_set`` flows in from
    :meth:`PortfolioStateRepository.get_prior_invocation_context` so the
    resolver's parameter-change-flag computation compares the prior
    invocation's committed parameters against the current resolution.
    """
    fan = load_config_fan(
        config_dir=config_dir,
        loaded_config=pipeline_config.loaded,
        distillation_config=load_distillation_config(config_dir / "distillation.yaml"),
    )

    universal_payload = analysis_result.distillation_outputs.universal_regime_label
    distillation_regime_label = DistillationRegimeLabel(universal_payload["regime_label"])
    distillation_vix_level = float(universal_payload["vix_level"])
    distillation_regime_skip_emergency = bool(universal_payload["regime_skip_emergency"])

    with sync_session_factory() as session:
        composite_alert_state = fetch_composite_alert_state(session)
        inputs = build_inputs_from_distillation_outputs(
            fan=fan,
            distillation_regime_label=distillation_regime_label,
            distillation_vix_level=distillation_vix_level,
            distillation_regime_skip_emergency=distillation_regime_skip_emergency,
            held_positions=assembled.snapshot.open_positions,
            risk_budget=assembled.snapshot.risk_budget,
            prior_parameter_set=prior_parameter_set,
            composite_alert_state=composite_alert_state,
        )
        output = resolve_regime_adaptation(
            invocation_id=invocation_id,
            now_utc=now,
            inputs=inputs,
            session=session,
        )
        insert_state(session, output.new_persisted_state, ingested_at=now.isoformat())
    return output


def _load_base_profile_rule_values(config_dir: Path) -> dict[str, float]:
    """Read ``main.yaml`` + the active profile's rule_values, no fold applied.

    Used for the halt-state computation that happens *before* runtime
    dimensions are resolved (because halt-state feeds into the resolver).
    The base profile carries the ``daily_drawdown_pct`` rule required by
    ``compute_halt_state``; the regime / overlay multipliers don't change
    which rules exist, only the values, so the base rule_values are
    sufficient for halt detection on the first iteration.
    """
    main_config = MainConfig.model_validate(read_yaml_file(config_dir / "main.yaml"))
    active_profile = main_config.active_profile
    profile_config = ProfileConfig.model_validate(
        read_yaml_file(config_dir / "profiles" / f"{active_profile.value}.yaml")
    )
    return dict(profile_config.rule_values)


# ---------------------------------------------------------------------------
# Row-update helpers
# ---------------------------------------------------------------------------


async def _update_row_fill_collection(
    handle: InvocationHandle,
    *,
    fill_collection_summary: FillCollectionSummary,
    staleness_flag: bool,
) -> None:
    """Persist fill-collection outcomes onto the bound invocation row."""
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared mid-fill-collection update; "
            "insert_invocation_record should have committed it before this phase opened"
        )
        raise RuntimeError(msg)
    row.fill_collection_summary_json = json.dumps(
        {
            "fills_processed": fill_collection_summary.fills_processed,
            "fills_quarantined": fill_collection_summary.fills_quarantined,
            "ca_activities_processed": fill_collection_summary.ca_activities_processed,
            "reconciliation_alerts": fill_collection_summary.reconciliation_alerts,
        },
        sort_keys=True,
    )
    row.staleness_flag = 1 if staleness_flag else 0


async def _update_row_command_execution(
    handle: InvocationHandle,
    *,
    command_execution_summary: CommandExecutionSummary,
) -> None:
    """Persist command execution outcomes onto the bound invocation row."""
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared mid-command-execution update; "
            "insert_invocation_record should have committed it before this phase opened"
        )
        raise RuntimeError(msg)
    row.command_execution_summary_json = json.dumps(
        {
            "commands_submitted": command_execution_summary.commands_submitted,
            "commands_rejected": command_execution_summary.commands_rejected,
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Decision-pipeline kwarg builder
# ---------------------------------------------------------------------------


def _build_decision_kwargs(  # noqa: PLR0913 — composition surface threads each layer's inputs through one builder.
    *,
    invocation_id: str,
    pipeline_config: PipelineConfig,
    analysis_result: Any,
    fill_collection_market_inputs: MarketInputs,
    pipeline_mode: PipelineMode,
    halt_state: HaltState | None,
    now: datetime,
    archive_root: Path,
    state_delivery_config: StateDeliveryConfig,
    state_persistence_config: StatePersistenceConfig,
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float | None],
    assembled_snapshot: AssembledSnapshot,
    repository: Any,
    regime_output: RegimeAdaptationOutput,
    progressive_tiers: tuple[ProgressiveTier, ...],
    debug_e2e: Any = None,
    resume_context: Any = None,
    venue_config: VenueConfig | None = None,
    execution_mode: ExecutionMode | None = None,
    execution_config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    """Assemble the kwargs ``run_decision_pipeline`` requires."""
    resolved = pipeline_config.resolved

    library_config = from_resolved_config(resolved)

    return {
        "assembled_snapshot": assembled_snapshot,
        "repository": repository,
        "regime_output": regime_output,
        "progressive_tiers": progressive_tiers,
        "synthesizer_text": analysis_result.synthesizer_result.synthesis_text,
        "retrieval_store": analysis_result.synthesizer_result.retrieval_store,
        "mode": pipeline_mode.to_decision_literal(),
        "halt_state": halt_state,
        "agents_config": dict(resolved.agents.agents),
        "agent_overrides": dict(resolved.agent_overrides),
        "sector_resolver": sector_resolver,
        "borrow_cost_resolver": borrow_cost_resolver,
        "library_config": library_config,
        "library_market": fill_collection_market_inputs,
        "profile_feature_flags": library_config.feature_flags,
        "state_delivery_config": state_delivery_config,
        "state_persistence_config": state_persistence_config,
        "options_enabled": library_config.feature_flags.options_enabled,
        "short_selling_enabled": library_config.feature_flags.short_selling_enabled,
        "active_sectors": frozenset(library_config.active_sectors),
        "invocation_id": invocation_id,
        "timestamp": now,
        "archive_root": archive_root,
        "debug_e2e": debug_e2e,
        "resume_context": resume_context,
        # ALP-711 — broker-routing inputs (all picklable; the PM subprocess
        # worker reconstructs the live ``TradingClient`` from them). All
        # three are ``None`` on the debug-e2e / non-prod path so the
        # submit_envelope wrapper's broker-routing gate stays False and the
        # log-only / synthetic-id placeholder path persists.
        "venue_config": venue_config,
        "execution_mode": execution_mode,
        "execution_config": execution_config,
    }


def _account_queries_factory_from_debug_e2e(
    context: RunInvocationContext,
) -> Any:
    """``AccountStateQueriesP`` factory derived from ``context.debug_e2e``.

    Returns ``None`` on the production daemon path so
    ``gather_fill_collection_inputs`` falls back to its inline Alpaca-backed
    default. Returns a closure over the bundle's log-only queries when
    debug-e2e is active (story ALP-501).
    """
    debug_settings = context.debug_e2e
    if debug_settings is None:
        return None
    return lambda _venue, _mode: debug_settings.account_queries


def _ca_queries_factory_from_debug_e2e(
    context: RunInvocationContext,
) -> Any:
    """``CorporateActionsQueriesP`` factory derived from ``context.debug_e2e``.

    Mirrors :func:`_account_queries_factory_from_debug_e2e`; ``None`` on
    the production path, the bundle's log-only queries on debug-e2e.
    """
    debug_settings = context.debug_e2e
    if debug_settings is None:
        return None
    return lambda _venue, _mode: debug_settings.ca_queries


def _activities_source_factory_from_debug_e2e(
    context: RunInvocationContext,
) -> Any:
    """``AccountActivitiesSource`` factory derived from ``context.debug_e2e`` (ALP-846).

    Mirrors :func:`_account_queries_factory_from_debug_e2e`; ``None`` on the
    production path (``run_account_activities_poll`` builds the Alpaca-backed
    ``AccountStateQueries``), the bundle's log-only account queries on debug-e2e
    so the harness stays offline. The log-only stand-in's
    ``get_account_activities`` yields nothing — the synthetic portfolio carries
    no option-lifecycle events.
    """
    debug_settings = context.debug_e2e
    if debug_settings is None:
        return None
    return lambda _venue, _mode: debug_settings.account_queries


def _quote_source_factory_from_debug_e2e(
    context: RunInvocationContext,
) -> Any:
    """``BatchQuoteSource`` factory derived from ``context.debug_e2e`` (ALP-753).

    Mirrors :func:`_account_queries_factory_from_debug_e2e`; ``None`` on the
    production path (``gather_fill_collection_inputs`` builds the Alpaca-backed batch
    quote source), the bundle's log-only quote source on debug-e2e. Without this
    the debug-e2e run would fall through to the default factory and fire a live
    Alpaca quote request for the whole active universe — breaking the harness's
    offline, deterministic contract.
    """
    debug_settings = context.debug_e2e
    if debug_settings is None:
        return None
    return lambda _venue, _mode: debug_settings.quote_source


def _price_provider_from_fill_collection(
    market_inputs: MarketInputs,
) -> StubCurrentPriceProvider:
    """Wrap fill collection's underlying-price map in the canonical stub price provider."""
    quotes = {
        ticker: PriceQuote(
            ticker=ticker,
            price_usd=price,
            as_of_timestamp=market_inputs.as_of,
            source=PriceSource.INTRADAY_QUOTE,
            is_stale=False,
        )
        for ticker, price in market_inputs.underlying_prices.items()
    }
    return StubCurrentPriceProvider(quotes, market_inputs.as_of)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


# Firing ``RunType`` → schema ``run_type`` wire string for the
# ``invocation_started`` SSE frame. Typed against ``_RunType`` (the schema's
# closed literal) so a value outside the wire vocabulary fails mypy, and kept
# exhaustive over every ``RunType`` member so a future enum addition can't
# silently drop a start frame (the missing weekend keys did exactly that —
# ALP-755). ``tests/scheduler/test_orchestrator.py::TestSchemaRunTypeMapping``
# guards the exhaustiveness invariant.
_SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE: dict[RunType, _RunType] = {
    RunType.market_hours_rolling: "market_hours_rolling",
    RunType.off_hours_rolling: "off_hours_rolling",
    RunType.market_open: "market_open",
    RunType.pre_close: "pre_close",
    RunType.weekend_saturday: "weekend_saturday",
    RunType.weekend_sunday: "weekend_sunday",
    RunType.emergency: "emergency",
}


def _emit_phase_transition(
    emitter: SSEEventEmitter | None,
    *,
    invocation_id: str,
    phase: str,
) -> None:
    """Best-effort SSE phase-transition emit (ALP-720).

    No-op when ``emitter`` is ``None`` (test path / debug-e2e harness).
    Reads wall-clock at the emit boundary so ``phase_started_at`` reflects
    when the orchestrator actually transitioned into the phase, not the
    invocation's start ``now`` (a long invocation would otherwise stamp
    every phase with the same timestamp).  Errors are logged + swallowed
    so a broken emit never crashes the invocation hot path.
    """
    if emitter is None:
        return
    try:
        emitter.emit(
            PhaseTransitionEvent(
                invocation_id=invocation_id,
                phase=phase,  # type: ignore[arg-type]
                phase_started_at=datetime.now(UTC),
            )
        )
    except Exception:
        log.exception("SSE phase_transition emit failed phase=%s", phase)


async def run_invocation(  # noqa: PLR0915 — composition root sequences every phase in one frame; per-phase extraction would multiply the call-site surface without simplifying any single concern.
    *,
    context: RunInvocationContext,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    now: datetime,
    sse_emitter: SSEEventEmitter | None = None,
) -> InvocationSummary:
    """Drive one pipeline invocation through the design's three-transaction model.

    See the module docstring for the per-phase transaction boundaries and
    failure semantics. The sequence in this function: resolve runtime
    dimensions → ``insert_invocation_record`` → fill collection (one session) →
    snapshot assembly (fresh sessions) → analysis + decision (read-only) →
    command execution (per-envelope sessions + final row stamp) → return
    :class:`InvocationSummary`.
    """
    session_factory = context.session_factory
    process_lifetime_id = context.process_lifetime_id
    archive_root = context.archive_root
    config_dir = context.config_dir
    env_path = context.env_path
    venue_config = context.venue_config
    execution_mode = context.execution_mode

    start_perf = time.monotonic()
    state_persistence_config = load_state_persistence_config(
        read_yaml_file(config_dir / "main.yaml")
    )
    state_delivery_config = load_state_delivery_config(config_dir / "state_delivery.yaml")
    scheduler_config = SchedulerConfig.model_validate(read_yaml_file(config_dir / "scheduler.yaml"))
    overlays_map = load_overlays(config_dir)

    # ALP-774 — warn early when this pre_close invocation is projected to
    # submit orders after the session close based on recent pipeline durations.
    _warn_if_pre_close_projected_late(
        firing_run_type=firing_run_type,
        now=now,
        sync_session_factory=context.sync_session_factory,
        scheduler_config=scheduler_config,
    )

    # Step 1a: read drawdown state + compose a base ActiveRiskParameterSet
    # so we can compute halt_state before resolving runtime dimensions.
    drawdown_state = await read_drawdown_state(session_factory)
    base_active_risk_parameters = build_active_risk_parameters(
        rule_values=_load_base_profile_rule_values(config_dir),
        regime=Regime.normal,
    )
    halt_state = compute_halt_state(
        drawdown_state=drawdown_state,
        active_risk_parameters=base_active_risk_parameters,
    )

    # Step 1b: evaluate the two overlay activators against the freshly-loaded
    # event calendar (pre-event) and the most-recent composite-alert rows
    # (stress).
    pre_event_decision = evaluate_pre_event_decision(
        now=now,
        config_dir=config_dir,
        overlays_map=overlays_map,
        scheduler_config=scheduler_config,
    )
    stress_decision = await evaluate_stress_decision(
        session_factory=session_factory,
        overlays_map=overlays_map,
    )

    # Step 1c: resolve runtime dimensions with the real halt + overlay inputs.
    async with session_factory() as short_session:
        runtime = await resolve_runtime_dimensions(
            short_session,
            firing_trigger=firing_run_type,
            halt_state=halt_state,
            pre_event_decision=pre_event_decision,
            stress_decision=stress_decision,
        )

    # Step 2: insert the invocation row in its own short transaction.
    invocation_id, pipeline_config = await insert_invocation_record(
        session_factory=session_factory,
        process_lifetime_id=process_lifetime_id,
        trigger_type=trigger_type,
        trigger_source=trigger_source,
        trigger_reason=trigger_reason,
        firing_run_type=firing_run_type,
        runtime=runtime,
        archive_root=archive_root,
        config_dir=config_dir,
        env_path=env_path,
        now=now,
    )

    # Story ALP-497 — single read of the per-invocation progress emitter.
    # Production invocations leave ``context.debug_e2e`` at ``None`` and
    # fall through to the no-op singleton; debug-e2e callers (story 04,
    # ALP-501) populate ``debug_e2e.emitter_factory`` so the JSONL
    # emitter (story 02c / ALP-499) opens a fresh log under the
    # invocation's archive directory.
    inner_progress: ProgressEmitter = (
        context.debug_e2e.emitter_factory(invocation_id, now)
        if context.debug_e2e is not None
        else NOOP_PROGRESS_EMITTER
    )
    # ALP-720 — when an SSE emitter is supplied (production daemon path),
    # wrap the inner ProgressEmitter in :class:`PipelineSSEProgressBridge`
    # so the harness's ``agent_request`` / ``agent_response`` callbacks
    # also emit schema-shaped ``agent_started`` / ``agent_succeeded`` SSE
    # events. The bridge defers to the inner emitter for the existing
    # debug-e2e JSONL write so both observability paths stay live.
    progress: ProgressEmitter
    if sse_emitter is not None:
        # The resolved agents map is keyed by the ``AgentName`` enum; the
        # bridge's lookup operates on string keys (the harness emits the
        # agent name as the enum's string value), so normalize at the seam.
        agents_by_name = {
            name.value: cfg for name, cfg in pipeline_config.resolved.agents.agents.items()
        }
        progress = PipelineSSEProgressBridge(
            emitter=sse_emitter,
            invocation_id=invocation_id,
            latency_budget_lookup=make_latency_budget_lookup(agents_by_name),
            inner=inner_progress,
        )
    else:
        progress = inner_progress

    # ALP-720 — operator-console observability of the invocation lifecycle.
    # The schema's invocation_started event fires first; phase transitions
    # follow as we enter each phase; invocation_ended fires at the bottom
    # (or in the exception handler on failure paths).
    if sse_emitter is not None:
        try:
            sse_emitter.emit(
                InvocationStartedEvent(
                    invocation_id=invocation_id,
                    run_type=_SCHEMA_RUN_TYPE_BY_FIRING_RUN_TYPE[firing_run_type],
                    started_at=now,
                )
            )
        except Exception:
            log.exception("SSE invocation_started emit failed")

    # Compose the current invocation's active_risk_parameters from the resolved fold.
    active_risk_parameters = build_active_risk_parameters(
        rule_values=pipeline_config.resolved.rule_values,
        regime=runtime.active_regime,
    )

    # Step 3 — fill-collection transaction.
    _emit_phase_transition(sse_emitter, invocation_id=invocation_id, phase="collect")
    progress.phase_start("fill_collection")
    # ALP-824 — fill collection and the continuous monitor are two writers on one WAL DB.
    # Gather inputs FIRST in a read-only (deferred) session so no write lock is
    # held across the Alpaca network fetch; then run the write unit under an
    # up-front ``BEGIN IMMEDIATE`` (write lock taken eagerly, so ``busy_timeout``
    # governs contention with the monitor) wrapped in a bounded retry. A transient
    # cross-writer collision then makes fill collection wait/retry rather than aborting the
    # whole invocation on an immediate ``SQLITE_BUSY_SNAPSHOT``.
    #
    # Story ALP-501 — ``context.debug_e2e`` is the SOLE signal the orchestrator is
    # in debug-e2e mode (P3 — no parallel boolean flag). The helpers resolve to
    # ``None`` on the production path so ``gather_fill_collection_inputs`` falls through to
    # its inline Alpaca-backed defaults.
    async with session_factory() as read_session:
        fill_collection_inputs = await gather_fill_collection_inputs(
            handle=InvocationHandle(session=read_session, invocation_id=invocation_id),
            venue_config=venue_config,
            execution_mode=execution_mode,
            as_of=now,
            sync_session_factory=context.sync_session_factory,
            account_queries_factory=_account_queries_factory_from_debug_e2e(context),
            ca_queries_factory=_ca_queries_factory_from_debug_e2e(context),
            quote_source_factory=_quote_source_factory_from_debug_e2e(context),
        )

    # ALP-717 — SHORT-equity entry fills consult the borrow-cost resolver to stamp
    # borrow_rate_pct on the OPEN position. The resolver is also reused by the
    # decision pipeline below; build it once (pure for the rest of the invocation)
    # from a sync read session, outside the async write lock.
    with context.sync_session_factory() as borrow_session:
        borrow_cost_resolver = build_borrow_cost_resolver(borrow_session)

    async def _run_fill_collection_write_unit() -> FillCollectionSummary:
        # Fresh session per attempt so the identity map is clean on retry;
        # ``begin_write_immediate`` takes the SQLite write lock before the first
        # read/write, and rollback on failure leaves the fills ``unprocessed`` so
        # re-running the unit is idempotent.
        async with session_factory() as write_session:
            await begin_write_immediate(write_session)
            write_handle = InvocationHandle(session=write_session, invocation_id=invocation_id)
            await emit_baseline_config_change_entry(
                handle=write_handle,
                config_dir=config_dir,
                now=now,
            )
            summary = await process_unprocessed_fills(
                write_handle,
                fill_collection_inputs.ca_activities,
                fill_collection_inputs.alpaca_positions,
                fill_collection_inputs.alpaca_account,
                market_inputs=fill_collection_inputs.market_inputs,
                config=state_persistence_config,
                borrow_cost_resolver=borrow_cost_resolver,
            )
            # ALP-846 / W1b — option-lifecycle account-activities poll. A
            # pipeline-cadence task (ADR-0004 evicts it from the always-on
            # monitor): it appends OPEXP/OPEXC/OPASN/OPTRD events to the
            # broker-event log and books realized PnL inside this same write
            # transaction (single writer = pipeline). A booking error (a missing
            # local position, or the surfacing condition where an assignment is
            # not fully described by the paired OPTRD) propagates to abort the
            # write unit — it is a real inconsistency, not a transient. The
            # already-built ``borrow_cost_resolver`` (ALP-862) stamps the
            # short-only fields on a SHORT equity leg an assignment delivers —
            # the same single resolver instance ``process_unprocessed_fills``
            # uses, never a second one.
            await run_account_activities_poll(
                write_handle,
                venue_config=venue_config,
                execution_mode=execution_mode,
                borrow_cost_resolver=borrow_cost_resolver,
                activities_source_factory=_activities_source_factory_from_debug_e2e(context),
            )
            # ALP-855 / W4a — daily SHORT-equity borrow accrual, relocated out of
            # the always-on monitor (ADR-0004) into this pipeline write unit
            # (single writer = pipeline, ADR-0005). The once-per-trading-day guard
            # makes the few-times-per-day pipeline cadence book the accrual exactly
            # once; it reuses the invocation's session + the already-built
            # ``borrow_cost_resolver`` (no per-tick InvocationRow, no daily timer).
            await run_borrow_accrual(
                write_handle,
                borrow_cost_resolver=borrow_cost_resolver,
                now=now,
            )
            # CR1 — re-derive the per-thesis PnL ledgers AFTER the activities poll
            # (and borrow accrual) have appended this invocation's
            # OPEXP/OPEXC/OPASN/OPTRD option-lifecycle events to the broker-event
            # log. ``process_unprocessed_fills`` projects order status + classifies
            # broker facts from the *fill* log, but a thesis-ledger derived there
            # would miss a same-invocation lifecycle event (it is not on the log
            # until the poll runs). Re-deriving here, still inside the single
            # fill-collection write transaction, folds the complete log into the ledger.
            await rederive_thesis_ledgers(write_handle)
            await _update_row_fill_collection(
                write_handle,
                fill_collection_summary=summary,
                staleness_flag=fill_collection_inputs.staleness_flag,
            )
            await write_session.commit()
            return summary

    fill_collection_summary = await run_with_sqlite_busy_retry(_run_fill_collection_write_unit)
    progress.phase_done("fill_collection", fills_processed=fill_collection_summary.fills_processed)

    # Step 3b — Thesis resolution (ALP-834 / ALP-899). Fill collection (or the
    # continuous monitor) left closed-position theses ACTIVE; author ACTIVE →
    # RESOLVED here, in its OWN transaction — a slow LLM component-evaluation
    # must not sit inside the fill-collection write lock — and BEFORE snapshot
    # assembly, so a thesis whose position closed this invocation feeds the same
    # invocation's snapshot (the recent-resolutions feed + thesis_quality
    # aggregates) and ``get_recent_thesis_resolutions`` no longer raises on a
    # half-written RESOLVED row.
    await _run_thesis_resolution_step(
        session_factory=session_factory,
        invocation_id=invocation_id,
        archive_root=archive_root,
        progress=progress,
        now=now,
        # ALP-914 finding 5 — the invocation's resolution-time underlying prices
        # (a primitive Mapping[str, float], not the MarketInputs type — the
        # analysis-layer resolver must not import a risk_guardrails type) feed
        # the LLM evaluator's entry-vs-resolution market-data slice.
        underlying_prices=fill_collection_inputs.market_inputs.underlying_prices,
    )

    # Step 4 — Between-phase snapshot read.
    _emit_phase_transition(sse_emitter, invocation_id=invocation_id, phase="distill")
    progress.phase_start("snapshot_assembly")
    sector_resolver = build_sector_resolver(pipeline_config.resolved)
    assembled, snapshot_repository = _assemble_fill_collection_snapshot(
        session_factory=session_factory,
        invocation_id=invocation_id,
        state_persistence_config=state_persistence_config,
        active_risk_parameters=active_risk_parameters,
        fill_collection_market_inputs=fill_collection_inputs.market_inputs,
        sector_resolver=sector_resolver,
    )
    portfolio_reader = SnapshotBackedSynthesizerReader(
        assembled.snapshot,
        sector_resolver=adapt_ticker_sector_resolver(sector_resolver),
    )
    progress.phase_done("snapshot_assembly")

    # Step 5 — Read-only analysis + decision pipelines.
    _emit_phase_transition(sse_emitter, invocation_id=invocation_id, phase="analyze")
    analysis_result = await _run_analysis(
        invocation_id=invocation_id,
        sync_session_factory=context.sync_session_factory,
        pipeline_config=pipeline_config,
        config_dir=config_dir,
        archive_root=archive_root,
        now=now,
        portfolio_reader=portfolio_reader,
        progress=progress,
        debug_e2e=context.debug_e2e,
    )

    prior_context = snapshot_repository.get_prior_invocation_context()
    regime_output = _resolve_regime_adaptation_for_invocation(
        sync_session_factory=context.sync_session_factory,
        config_dir=config_dir,
        pipeline_config=pipeline_config,
        analysis_result=analysis_result,
        assembled=assembled,
        prior_parameter_set=prior_context.prior_active_risk_parameters,
        invocation_id=invocation_id,
        now=now,
    )

    pipeline_mode = PipelineMode.from_config_mode(runtime.active_mode)
    # ``borrow_cost_resolver`` was built above (before fill collection) and is reused
    # here — the latest-fee-per-ticker mapping is pure + total for the rest of
    # the invocation (ALP-586 / ALP-717).
    # ALP-711 — broker_dispatch inputs the PM submit_envelope wrapper needs to
    # dispatch accepted commands to Alpaca (see
    # ``execution.oms.broker_dispatch.dispatch_command_to_broker``). Production
    # runs (``context.debug_e2e is None``) thread the picklable triple
    # ``(venue_config, execution_mode, execution_config)`` through the
    # decision pipeline → PM runner → subprocess worker, which reconstructs
    # the live ``TradingClient`` on its own side (alpaca-py is not picklable
    # across the subprocess boundary). Debug-e2e / log-only runs pass
    # ``None`` so the submit_envelope wrapper's broker-routing gate stays
    # False and orders persist with NO broker id (NULL, ALP-847 — never a
    # synthetic placeholder). The
    # ``ExecutionConfig`` is reused from ``pipeline_config.loaded.execution``
    # rather than re-parsing ``execution.yaml`` — ``parse_loaded_config``
    # already did the work inside ``insert_invocation_record``.
    broker_routing_active = context.debug_e2e is None
    decision_kwargs = _build_decision_kwargs(
        invocation_id=invocation_id,
        pipeline_config=pipeline_config,
        analysis_result=analysis_result,
        fill_collection_market_inputs=fill_collection_inputs.market_inputs,
        pipeline_mode=pipeline_mode,
        halt_state=halt_state,
        now=now,
        archive_root=archive_root,
        state_delivery_config=state_delivery_config,
        state_persistence_config=state_persistence_config,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
        assembled_snapshot=assembled,
        repository=snapshot_repository,
        regime_output=regime_output,
        progressive_tiers=load_cumulative_drawdown_progressive_tiers(),
        debug_e2e=context.debug_e2e,
        resume_context=(
            context.debug_e2e.resume_context if context.debug_e2e is not None else None
        ),
        venue_config=venue_config if broker_routing_active else None,
        execution_mode=execution_mode if broker_routing_active else None,
        execution_config=pipeline_config.loaded.execution if broker_routing_active else None,
    )
    _emit_phase_transition(sse_emitter, invocation_id=invocation_id, phase="decide")
    decision_result = await run_decision_pipeline(**decision_kwargs, progress=progress)

    # Step 6 — command execution.
    _emit_phase_transition(sse_emitter, invocation_id=invocation_id, phase="execute")
    progress.phase_start("command_execution")
    command_execution_summary = await dispatch_command_execution(
        session_factory=session_factory,
        invocation_id=invocation_id,
        pm_result=decision_result.pm_result,
        state_persistence_config=state_persistence_config,
    )
    async with session_factory() as session:
        command_execution_handle = InvocationHandle(session=session, invocation_id=invocation_id)
        await _update_row_command_execution(
            command_execution_handle,
            command_execution_summary=command_execution_summary,
        )
        # ALP-836 integrity guard — the single authoritative command-execution stamp. Withhold
        # it (leaving command_execution_completed_at NULL) when any order is stuck in
        # PENDING_SUBMIT for this invocation: a lost post-submit backfill behind a
        # live broker order. The invocation reads as incomplete rather than papering
        # over the strand by marking the phase done. The normal abandon path drives
        # such a row to a terminal status (FL3); a residual strand here is the rare
        # lost-commit case (process death between the broker submit and the backfill
        # commit), so it is surfaced as an operator-visible warning rather than
        # silently withheld — there is no order-backfill that self-heals it next run.
        if await invocation_has_pending_submit_strand(session, invocation_id=invocation_id):
            log.warning(
                "command_execution_completed_at withheld for invocation %s — an unresolved "
                "PENDING_SUBMIT order strand (a lost post-submit backfill behind a "
                "live broker order) remains; operator follow-up required, there is "
                "no automatic recovery sweep",
                invocation_id,
            )
        else:
            await stamp_phase_completion(
                command_execution_handle, column="command_execution_completed_at"
            )
        await session.commit()
    progress.phase_done(
        "command_execution",
        commands_submitted=command_execution_summary.commands_submitted,
    )

    if sse_emitter is not None:
        try:
            sse_emitter.emit(
                InvocationEndedEvent(
                    invocation_id=invocation_id,
                    status="completed",
                    commands_issued=command_execution_summary.commands_submitted,
                )
            )
        except Exception:
            log.exception("SSE invocation_ended emit failed")

    duration = time.monotonic() - start_perf
    return InvocationSummary(
        invocation_id=invocation_id,
        trigger_type=trigger_type,
        trigger_source=trigger_source,
        firing_run_type=firing_run_type,
        fill_collection_summary=fill_collection_summary,
        commands_submitted=command_execution_summary.commands_submitted,
        commands_rejected=command_execution_summary.commands_rejected,
        staleness_flag=fill_collection_inputs.staleness_flag,
        duration_seconds=duration,
    )


# The targeted thesis-component evaluator (story 04d) is a non-roster agent —
# no ``AgentName`` slot, no ``agents.yaml`` entry. The resolver supplies a
# ``BaseAgentConfig`` on demand pointing at the committed minimal-eval prompt;
# the analysis-layer model + an LLM-call budget match the analysis researchers.
_THESIS_EVALUATOR_PROMPT = "prompts/analysis/thesis_component_evaluator.md"


def _build_thesis_evaluator_config() -> BaseAgentConfig:
    """The non-roster config the resolver hands the LLM component-evaluator."""
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=_THESIS_EVALUATOR_PROMPT,
        latency_budget_seconds=120,
        context_token_budget=4000,
        output_token_budget=2000,
        tools=[],
    )


async def _run_thesis_resolution_step(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    archive_root: Path,
    progress: ProgressEmitter,
    now: datetime,
    underlying_prices: Mapping[str, float],
) -> None:
    """Resolve closed-position theses in two phases (ALP-899 / ALP-914 finding 2).

    Phase 1 (no write lock): on a fresh read session, bulk-fetch + assess the
    eligible theses — the slow LLM component evaluation and the
    exit-method/ledger reads run here, OUTSIDE any write transaction, so they
    never sit inside the SQLite write lock that races the continuous monitor.
    A per-thesis data gap logs a WARNING and skips that thesis (finding 1)
    rather than aborting the invocation. The evaluator config is built lazily,
    only if a component needs the LLM fallback (finding 4).

    Phase 2 (short IMMEDIATE write transaction, busy-retry): if anything was
    prepared, persist the ACTIVE → RESOLVED rows + ``THESIS_RESOLVED`` entries
    through ``run_with_sqlite_busy_retry`` — a write unit that opens a fresh
    session, calls ``begin_write_immediate`` first (so ``busy_timeout`` governs
    monitor contention), persists, and commits. No LLM I/O or POSITION_CLOSED /
    ledger read occurs inside it, so a busy-retry re-runs only the persist.
    """
    progress.phase_start("thesis_resolution")
    async with session_factory() as read_session:
        prepared = await prepare_closed_position_resolutions(
            read_session,
            invocation_id=invocation_id,
            evaluator_config_factory=_build_thesis_evaluator_config,
            now=now,
            underlying_prices=underlying_prices,
            archive_root=archive_root,
            progress=progress,
        )

    if not prepared:
        progress.phase_done("thesis_resolution", theses_resolved=0)
        return

    async def _run_resolution_write_unit() -> int:
        # Fresh session per attempt so the identity map is clean on retry;
        # ``begin_write_immediate`` takes the SQLite write lock before the first
        # write, mirroring the fill-collection write unit (ALP-824).
        async with session_factory() as write_session:
            await begin_write_immediate(write_session)
            write_handle = InvocationHandle(session=write_session, invocation_id=invocation_id)
            resolved = await persist_thesis_resolutions(write_handle, prepared, now=now)
            await write_session.commit()
            return len(resolved)

    theses_resolved = await run_with_sqlite_busy_retry(_run_resolution_write_unit)
    progress.phase_done("thesis_resolution", theses_resolved=theses_resolved)


def _assemble_fill_collection_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    state_persistence_config: StatePersistenceConfig,
    active_risk_parameters: ActiveRiskParameterSet,
    fill_collection_market_inputs: MarketInputs,
    sector_resolver: Callable[[str], str],
) -> tuple[AssembledSnapshot, Any]:
    """Build the post-fill-collection portfolio snapshot once per invocation.

    Opens fresh sessions through the repository factory; relies on fill collection
    having already committed so the repository's bound invocation row has
    a non-NULL ``fill_collection_completed_at`` (the fallback-to-prior-invocation
    path in ``get_current_invocation_metadata`` is never reached here).
    The same ``AssembledSnapshot`` feeds the synthesizer reader and the
    decision pipeline.

    Returns the ``(AssembledSnapshot, repository)`` pair so the decision
    pipeline's active-guardrails composition (story ALP-433) can read
    ``DrawdownState`` from the same repository — one canonical view per
    invocation.
    """
    portfolio_state_config = load_portfolio_state_config(_PORTFOLIO_STATE_CONFIG_PATH)
    active_provider, prior_provider = make_repository_providers(active_risk_parameters)
    repository = build_sql_portfolio_state_repository(
        session_factory=session_factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=active_provider,
        prior_active_risk_parameters_provider=prior_provider,
        config=state_persistence_config,
        thesis_quality_aggregates_trailing_windows_days=(
            portfolio_state_config.thesis_quality_aggregates_trailing_windows_days
        ),
    )
    price_provider = _price_provider_from_fill_collection(fill_collection_market_inputs)
    option_price_provider = SqlOptionPriceProvider(session_factory=session_factory)
    # ``snapshot_assembled_at`` must be >= ``fill_collection_committed_at`` per
    # ``PortfolioStateSnapshot``'s ordering validator. Fill collection stamps the row
    # with wall-clock-at-stamp-time; using a fresh ``datetime.now(UTC)`` here
    # guarantees the snapshot reflects post-fill-collection reality even when the
    # orchestrator's logical ``now`` predates fill collection's actual completion
    # (the common case under test fixtures with a frozen ``now``).
    assembled = assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        option_price_provider=option_price_provider,
        sector_resolver=adapt_ticker_sector_resolver(sector_resolver),
        config=portfolio_state_config,
        now=datetime.now(UTC),
    )
    return assembled, repository


_PRE_CLOSE_TIMING_RECENT_N = 5


def _parse_invocation_timestamp(raw: str) -> datetime:
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _warn_if_pre_close_projected_late(
    *,
    firing_run_type: RunType,
    now: datetime,
    sync_session_factory: sessionmaker[Session],
    scheduler_config: SchedulerConfig,
) -> None:
    """Log a WARNING when a pre_close run is projected to miss the session close.

    Computes the rolling average wall-clock of the most recent completed
    invocations to project when command_execution will complete. Non-pre_close fires and
    non-trading days are silently skipped.
    """
    if firing_run_type is not RunType.pre_close:
        return

    with sync_session_factory() as session:
        rows = session.execute(
            select(InvocationRow.start_at, InvocationRow.command_execution_completed_at)
            .where(InvocationRow.command_execution_completed_at.is_not(None))
            .where(InvocationRow.trigger_source == "pre_close")
            .order_by(InvocationRow.start_at.desc())
            .limit(_PRE_CLOSE_TIMING_RECENT_N)
        ).all()

    durations: list[float] = []
    for start_raw, end_raw in rows:
        try:
            start_ts = _parse_invocation_timestamp(start_raw)
            end_ts = _parse_invocation_timestamp(end_raw)
            durations.append((end_ts - start_ts).total_seconds())
        except (ValueError, TypeError):
            continue

    if not durations:
        log.debug("pre_close timing guard: no prior completed invocations to project from")
        return

    avg_secs = sum(durations) / len(durations)
    projected_completion = now + timedelta(seconds=avg_secs)

    try:
        calendar = exchange_calendars.get_calendar(scheduler_config.market_calendar_exchange)
        pd_date = pd.Timestamp(now.date())
        if not calendar.is_session(pd_date):
            log.debug("pre_close timing guard: %s is not a trading session", now.date())
            return
        close_ts = calendar.session_close(pd_date)
        session_close = close_ts.to_pydatetime()
        if session_close.tzinfo is None:
            session_close = session_close.replace(tzinfo=UTC)
    except Exception:
        # Warranted broad-except (ALP-480 third-party-boundary residue): this is a
        # purely advisory timing guard — it only logs and never mutates state or
        # affects the trading decision. It wraps the exchange_calendars + pandas
        # calls above (get_calendar / is_session / session_close / to_pydatetime),
        # whose failure surface is diverse and version-dependent (invalid-calendar,
        # out-of-bounds date, Timestamp conversion). Degrade-don't-crash: any
        # failure logs with exc_info and skips the projection rather than aborting
        # the invocation over a non-critical log line.
        log.warning("pre_close timing guard: could not resolve session close", exc_info=True)
        return

    if projected_completion > session_close:
        overshoot_secs = (projected_completion - session_close).total_seconds()
        log.warning(
            "pre_close timing: projected completion %s is %.0fs after session close %s "
            "(rolling avg of %d recent invocations = %.0fs); "
            "orders may submit post-close",
            projected_completion.isoformat(),
            overshoot_secs,
            session_close.isoformat(),
            len(durations),
            avg_secs,
        )
    else:
        margin_secs = (session_close - projected_completion).total_seconds()
        log.debug(
            "pre_close timing: projected completion %s; session close %s; margin=%.0fs",
            projected_completion.isoformat(),
            session_close.isoformat(),
            margin_secs,
        )


_LAST_INVOCATION_FALLBACK = timedelta(hours=24)


def _resolve_last_invocation_time(
    session: Session,
    *,
    current_invocation_id: str,
    now: datetime,
) -> datetime:
    """Return the most recent successful prior invocation's ``start_at``.

    The qualitative researcher's news-digest window covers
    ``[last_invocation_time, as_of]`` per
    ``docs/design/03-analysis-layer/qualitative-research.md`` § News digest.
    The filter ``command_execution_completed_at IS NOT NULL`` mirrors
    :func:`alphamind.scheduler.runtime._resolve_active_regime`: an aborted
    prior invocation's ``start_at`` would otherwise truncate the next
    invocation's digest window, hiding headlines published between the last
    successful invocation and the abort.

    Falls back to ``now - 24h`` when no successful prior row exists (first
    invocation, pristine debug-e2e DB). The 24h horizon matches the domain
    researchers' fixed ``lookback_window_hours`` in
    ``analysis/domain_researchers/runner.py`` so the cross-sector digest
    covers the same corpus the sector bundles do (ALP-535).
    """
    stmt = (
        select(InvocationRow.start_at)
        .where(
            InvocationRow.invocation_id != current_invocation_id,
            InvocationRow.command_execution_completed_at.is_not(None),
        )
        .order_by(InvocationRow.start_at.desc())
        .limit(1)
    )
    raw = session.execute(stmt).scalar_one_or_none()
    if raw is None:
        return now - _LAST_INVOCATION_FALLBACK
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _run_analysis(  # noqa: PLR0913 — composition surface threads orchestrator state into the analysis pipeline; the alternative (a kwargs dict) loses the typed signature.
    *,
    invocation_id: str,
    sync_session_factory: sessionmaker[Session],
    pipeline_config: PipelineConfig,
    config_dir: Path,
    archive_root: Path,
    now: datetime,
    portfolio_reader: SynthesizerPortfolioStateReader,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    debug_e2e: Any = None,
) -> Any:
    """Compose ``run_analysis_pipeline`` inputs from the loaded config + factory.

    The reader is built upstream by ``run_invocation`` (after the snapshot
    assembly) so the synthesizer projects the same post-fill-collection snapshot the
    decision pipeline consumes. The analysis pipeline's distillation orchestrator
    threads the session through ``asyncio.to_thread`` into sync SQLAlchemy
    callsites, so a fresh sync ``Session`` is opened here rather than reusing
    the async fill-collection handle.
    """
    from alphamind.scripts._common import load_distillation_config

    resolved = pipeline_config.resolved
    ticker_scope = ticker_scope_from_assets(resolved)
    with sync_session_factory() as session:
        last_invocation_time = _resolve_last_invocation_time(
            session,
            current_invocation_id=invocation_id,
            now=now,
        )
        return await run_analysis_pipeline(
            session=session,
            invocation_id=invocation_id,
            as_of=now,
            last_invocation_time=last_invocation_time,
            distillation_config=load_distillation_config(config_dir / "distillation.yaml"),
            ticker_scope=ticker_scope,
            universe=frozenset(ticker_scope),
            agents_config={name.value: cfg for name, cfg in resolved.agents.agents.items()},
            sectors_config=sectors_config_from_assets(resolved),
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            progress=progress,
            debug_e2e=debug_e2e,
        )
