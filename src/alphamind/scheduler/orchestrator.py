"""End-to-end phase orchestrator — ``run_invocation`` (ALP-449 three-tx model).

Single async entrypoint the APScheduler driver (story 04a), the emergency
receiver (story 04b), and the CLI ``--once`` path call. Per the design's
snapshot-isolation contract
(``docs/design/05-execution-layer/state-persistence.md`` § Snapshot isolation),
it runs three separate transactions per invocation:

  1. Row insert: ``insert_invocation_row`` commits the ``invocations`` row in
     its own short transaction so the row is durable + visible to fresh-session
     reads from the moment Phase 1 starts.
  2. Phase 1: one transaction wrapping fill integration + activity-log writes
     + the ``phase1_completed_at`` stamp. Commits at the close of the phase.
  3. Snapshot read between phases: ``assemble_snapshot`` uses fresh sessions
     via the repository factory; it now correctly sees the committed
     ``phase1_completed_at`` and produces a real :class:`AssembledSnapshot`.
     The snapshot feeds :class:`SnapshotBackedSynthesizerReader` (consumed by
     the analysis pipeline) and the decision pipeline. The analysis subtree
     opens its own sync ``Session`` (the distillation orchestrator threads it
     through ``asyncio.to_thread`` into sync SQLAlchemy callsites) from the
     context's ``sync_session_factory``.
  4. Phase 2: one transaction wrapping envelope dispatch + the
     ``phase2_completed_at`` stamp + row summary writeback.

Per ``docs/design/mid-pipeline-failure-handling.md``:
  * A Phase 1 abort rolls back Phase 1's writes; the invocation row stays
    with ``phase1_completed_at IS NULL``, and the repository's consistency
    guard refuses snapshot reads against it — next invocation retries fills.
  * Between-phase aborts (snapshot assembly, analysis, decision) leave Phase
    1 committed and skip Phase 2; the next scheduled invocation regenerates
    briefs from current state.
  * Phase 2 aborts roll back the in-flight envelope; already-committed work
    from prior envelopes remains durable.

The function composes existing layer primitives without inventing new
submission paths: ``gather_phase1_inputs`` (story 03b / ALP-445),
``process_unprocessed_fills`` (story 07 / ALP-365), ``run_analysis_pipeline``
(ALP-276), ``run_decision_pipeline`` (ALP-403), and ``dispatch_phase2``
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.mode import PipelineMode
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
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
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.config.models.main import MainConfig
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.execution.write_paths.phase1 import (
    Phase1Summary,
    process_unprocessed_fills,
)
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
from alphamind.scheduler.invocation import insert_invocation_record
from alphamind.scheduler.phase1_inputs import gather_phase1_inputs
from alphamind.scheduler.phase2_dispatch import (
    Phase2Summary,
    dispatch_phase2,
)
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
    phase1_summary: Phase1Summary
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


async def _update_row_phase1(
    handle: InvocationHandle,
    *,
    phase1_summary: Phase1Summary,
    staleness_flag: bool,
) -> None:
    """Persist Phase 1 outcomes onto the bound invocation row."""
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared mid-Phase-1 update; "
            "insert_invocation_record should have committed it before this phase opened"
        )
        raise RuntimeError(msg)
    row.fill_collection_summary_json = json.dumps(
        {
            "fills_processed": phase1_summary.fills_processed,
            "fills_quarantined": phase1_summary.fills_quarantined,
            "ca_activities_processed": phase1_summary.ca_activities_processed,
            "reconciliation_alerts": phase1_summary.reconciliation_alerts,
        },
        sort_keys=True,
    )
    row.staleness_flag = 1 if staleness_flag else 0


async def _update_row_phase2(
    handle: InvocationHandle,
    *,
    phase2_summary: Phase2Summary,
) -> None:
    """Persist Phase 2 outcomes onto the bound invocation row."""
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared mid-Phase-2 update; "
            "insert_invocation_record should have committed it before this phase opened"
        )
        raise RuntimeError(msg)
    row.command_execution_summary_json = json.dumps(
        {
            "commands_submitted": phase2_summary.commands_submitted,
            "commands_rejected": phase2_summary.commands_rejected,
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
    phase1_market_inputs: MarketInputs,
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
        "library_market": phase1_market_inputs,
        "profile_feature_flags": library_config.feature_flags,
        "state_delivery_config": state_delivery_config,
        "state_persistence_config": state_persistence_config,
        "options_enabled": library_config.feature_flags.options_enabled,
        "short_selling_enabled": library_config.feature_flags.short_selling_enabled,
        "active_sectors": frozenset(library_config.active_sectors),
        "invocation_id": invocation_id,
        "timestamp": now,
        "archive_root": archive_root,
    }


def _account_queries_factory_from_debug_e2e(
    context: RunInvocationContext,
) -> Any:
    """``AccountStateQueriesP`` factory derived from ``context.debug_e2e``.

    Returns ``None`` on the production daemon path so
    ``gather_phase1_inputs`` falls back to its inline Alpaca-backed
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


def _price_provider_from_phase1(
    market_inputs: MarketInputs,
) -> StubCurrentPriceProvider:
    """Wrap Phase 1's underlying-price map in the canonical stub price provider."""
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


async def run_invocation(  # noqa: PLR0915 — composition root sequences every phase in one frame; per-phase extraction would multiply the call-site surface without simplifying any single concern.
    *,
    context: RunInvocationContext,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    now: datetime,
) -> InvocationSummary:
    """Drive one pipeline invocation through the design's three-transaction model.

    See the module docstring for the per-phase transaction boundaries and
    failure semantics. The sequence in this function: resolve runtime
    dimensions → ``insert_invocation_record`` → Phase 1 (one session) →
    snapshot assembly (fresh sessions) → analysis + decision (read-only) →
    Phase 2 (per-envelope sessions + final row stamp) → return
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
    progress: ProgressEmitter = (
        context.debug_e2e.emitter_factory(invocation_id)
        if context.debug_e2e is not None
        else NOOP_PROGRESS_EMITTER
    )

    # Compose the current invocation's active_risk_parameters from the resolved fold.
    active_risk_parameters = build_active_risk_parameters(
        rule_values=pipeline_config.resolved.rule_values,
        regime=runtime.active_regime,
    )

    # Step 3 — Phase 1 transaction.
    progress.phase_start("phase1")
    async with session_factory() as session:
        phase1_handle = InvocationHandle(session=session, invocation_id=invocation_id)
        await emit_baseline_config_change_entry(
            handle=phase1_handle,
            config_dir=config_dir,
            now=now,
        )
        # Story ALP-501 — ``context.debug_e2e`` is the SOLE signal the
        # orchestrator is in debug-e2e mode (P3 — no parallel boolean
        # flag). The helpers resolve to ``None`` on the production path
        # so ``gather_phase1_inputs`` falls through to its inline
        # Alpaca-backed defaults.
        phase1_inputs = await gather_phase1_inputs(
            handle=phase1_handle,
            venue_config=venue_config,
            execution_mode=execution_mode,
            as_of=now,
            sync_session_factory=context.sync_session_factory,
            account_queries_factory=_account_queries_factory_from_debug_e2e(context),
            ca_queries_factory=_ca_queries_factory_from_debug_e2e(context),
        )
        phase1_summary = await process_unprocessed_fills(
            phase1_handle,
            phase1_inputs.ca_activities,
            phase1_inputs.alpaca_positions,
            phase1_inputs.alpaca_account,
            market_inputs=phase1_inputs.market_inputs,
            config=state_persistence_config,
        )
        await _update_row_phase1(
            phase1_handle,
            phase1_summary=phase1_summary,
            staleness_flag=phase1_inputs.staleness_flag,
        )
        await session.commit()
    progress.phase_done("phase1", fills_processed=phase1_summary.fills_processed)

    # Step 4 — Between-phase snapshot read.
    progress.phase_start("snapshot_assembly")
    sector_resolver = build_sector_resolver(pipeline_config.resolved)
    assembled, snapshot_repository = _assemble_phase1_snapshot(
        session_factory=session_factory,
        invocation_id=invocation_id,
        state_persistence_config=state_persistence_config,
        active_risk_parameters=active_risk_parameters,
        phase1_market_inputs=phase1_inputs.market_inputs,
        sector_resolver=sector_resolver,
    )
    portfolio_reader = SnapshotBackedSynthesizerReader(
        assembled.snapshot,
        sector_resolver=adapt_ticker_sector_resolver(sector_resolver),
    )
    progress.phase_done("snapshot_assembly")

    # Step 5 — Read-only analysis + decision pipelines.
    analysis_result = await _run_analysis(
        invocation_id=invocation_id,
        sync_session_factory=context.sync_session_factory,
        pipeline_config=pipeline_config,
        config_dir=config_dir,
        archive_root=archive_root,
        now=now,
        portfolio_reader=portfolio_reader,
        progress=progress,
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
    # Snapshot-backed borrow-cost resolver: read the latest borrow_cost_daily
    # fee per ticker once so the decision layer can price short-equity borrow
    # accrual (ALP-586). Pure + total for the rest of the invocation.
    with context.sync_session_factory() as borrow_session:
        borrow_cost_resolver = build_borrow_cost_resolver(borrow_session)
    decision_kwargs = _build_decision_kwargs(
        invocation_id=invocation_id,
        pipeline_config=pipeline_config,
        analysis_result=analysis_result,
        phase1_market_inputs=phase1_inputs.market_inputs,
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
    )
    decision_result = await run_decision_pipeline(**decision_kwargs, progress=progress)

    # Step 6 — Phase 2.
    progress.phase_start("phase2")
    phase2_summary = await dispatch_phase2(
        session_factory=session_factory,
        invocation_id=invocation_id,
        pm_result=decision_result.pm_result,
        state_persistence_config=state_persistence_config,
    )
    async with session_factory() as session:
        phase2_handle = InvocationHandle(session=session, invocation_id=invocation_id)
        await _update_row_phase2(phase2_handle, phase2_summary=phase2_summary)
        await stamp_phase_completion(phase2_handle, column="phase2_completed_at")
        await session.commit()
    progress.phase_done("phase2", commands_submitted=phase2_summary.commands_submitted)

    duration = time.monotonic() - start_perf
    return InvocationSummary(
        invocation_id=invocation_id,
        trigger_type=trigger_type,
        trigger_source=trigger_source,
        firing_run_type=firing_run_type,
        phase1_summary=phase1_summary,
        commands_submitted=phase2_summary.commands_submitted,
        commands_rejected=phase2_summary.commands_rejected,
        staleness_flag=phase1_inputs.staleness_flag,
        duration_seconds=duration,
    )


def _assemble_phase1_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    state_persistence_config: StatePersistenceConfig,
    active_risk_parameters: ActiveRiskParameterSet,
    phase1_market_inputs: MarketInputs,
    sector_resolver: Callable[[str], str],
) -> tuple[AssembledSnapshot, Any]:
    """Build the post-Phase-1 portfolio snapshot once per invocation.

    Opens fresh sessions through the repository factory; relies on Phase 1
    having already committed so the repository's
    ``phase1_completed_at IS NULL → RepositoryConsistencyError`` guard
    sees a satisfied row. The same ``AssembledSnapshot`` feeds the
    synthesizer reader and the decision pipeline.

    Returns the ``(AssembledSnapshot, repository)`` pair so the decision
    pipeline's Phase 1 enforcement composition (story ALP-433) can read
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
    )
    price_provider = _price_provider_from_phase1(phase1_market_inputs)
    option_price_provider = SqlOptionPriceProvider(session_factory=session_factory)
    # ``snapshot_assembled_at`` must be >= ``phase1_committed_at`` per
    # ``PortfolioStateSnapshot``'s ordering validator. Phase 1 stamps the row
    # with wall-clock-at-stamp-time; using a fresh ``datetime.now(UTC)`` here
    # guarantees the snapshot reflects post-Phase-1 reality even when the
    # orchestrator's logical ``now`` predates Phase 1's actual completion
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
    The filter ``phase2_completed_at IS NOT NULL`` mirrors
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
            InvocationRow.phase2_completed_at.is_not(None),
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


async def _run_analysis(
    *,
    invocation_id: str,
    sync_session_factory: sessionmaker[Session],
    pipeline_config: PipelineConfig,
    config_dir: Path,
    archive_root: Path,
    now: datetime,
    portfolio_reader: SynthesizerPortfolioStateReader,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
) -> Any:
    """Compose ``run_analysis_pipeline`` inputs from the loaded config + factory.

    The reader is built upstream by ``run_invocation`` (after the snapshot
    assembly) so the synthesizer projects the same post-Phase-1 snapshot the
    decision pipeline consumes. The analysis pipeline's distillation orchestrator
    threads the session through ``asyncio.to_thread`` into sync SQLAlchemy
    callsites, so a fresh sync ``Session`` is opened here rather than reusing
    the async Phase 1 handle.
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
        )
