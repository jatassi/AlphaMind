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
     the analysis pipeline) and the decision pipeline.
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
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.load import PipelineConfig
from alphamind.config.loaders import load_overlays, read_yaml_file
from alphamind.config.models.main import ExecutionMode, MainConfig
from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay, PreEventOverlay, StressOverlay
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.state_persistence.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)
from alphamind.execution.state_persistence.invocation_context.config_change import (
    emit_distillation_config_change_entry,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)
from alphamind.execution.state_persistence.invocation_context.records import (
    TriggerType,
)
from alphamind.execution.state_persistence.repository import (
    build_sql_portfolio_state_repository,
)
from alphamind.execution.state_persistence.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
    drawdown_state_record_from_row,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.write_paths.phase1 import (
    Phase1Summary,
    process_unprocessed_fills,
)
from alphamind.pipeline.analysis import run_analysis_pipeline
from alphamind.pipeline.decision import run_decision_pipeline
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.consumers.synthesizer import (
    SnapshotBackedSynthesizerReader,
)
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.risk_guardrails.breach_behavior.halt_state import compute_halt_state
from alphamind.risk_guardrails.breach_behavior.types import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone
from alphamind.risk_guardrails.regime_adaptation.event_calendar import (
    load_event_calendar,
)
from alphamind.risk_guardrails.regime_adaptation.pre_event_activator import (
    evaluate_pre_event_overlay,
)
from alphamind.risk_guardrails.regime_adaptation.stress_activator import (
    evaluate_stress_overlay,
    fetch_composite_alert_state,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    OverlayActivationDecision,
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
from alphamind.scheduler.runtime import resolve_runtime_dimensions

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
    firing_run_type: RunType
    phase1_summary: Phase1Summary
    commands_submitted: int
    commands_rejected: int
    staleness_flag: bool
    duration_seconds: float


# ---------------------------------------------------------------------------
# Regime → RegimeLabel mapping (inlined to avoid pulling parameter_set's
# private constant)
# ---------------------------------------------------------------------------


_REGIME_TO_LABEL: dict[Regime, RegimeLabel] = {
    Regime.low_vol: RegimeLabel.LOW_VOL,
    Regime.normal: RegimeLabel.NORMAL,
    Regime.elevated: RegimeLabel.ELEVATED,
    Regime.crisis: RegimeLabel.CRISIS,
}


_PORTFOLIO_STATE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "portfolio_state.yaml"
)


# ---------------------------------------------------------------------------
# Drawdown-state and active-risk-parameters helpers (pre-review triage)
# ---------------------------------------------------------------------------


def _zero_drawdown_state() -> DrawdownState:
    """Return a zero-drawdown ``DrawdownState``.

    Used when the singleton row is absent (fresh DB / first run) and as the
    halt-state computation input on bootstrap. The four computed read-time
    fields carry neutral defaults; only ``current_drawdown_pct`` and
    ``intraday_drawdown_pct`` (both zero) and ``cumulative_tier`` (``None``)
    matter for halt detection.
    """
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=0.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


async def _read_drawdown_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> DrawdownState:
    """Read the ``drawdown_state`` singleton row; fall back to zero on absence.

    Phase 1's write path (``process_unprocessed_fills``) seeds the singleton
    row, so a fresh DB legitimately has none before the first invocation
    completes. Treating absence as zero drawdown keeps the orchestrator
    runnable from a clean state without violating the halt-detection
    contract (no drawdown → no halt).
    """
    async with session_factory() as session:
        row = await session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
        if row is None:
            return _zero_drawdown_state()
        return drawdown_state_record_from_row(
            row,
            intraday_drawdown_pct=0.0,
            daily_zone=RiskZone.NORMAL,
            cumulative_zone=RiskZone.NORMAL,
            cumulative_tier=None,
        )


def _build_active_risk_parameters(
    *,
    rule_values: Mapping[str, float],
    regime: Regime,
) -> ActiveRiskParameterSet:
    """Compose an ``ActiveRiskParameterSet`` from a flat rule-values map.

    Pre-review triage simplification: the production pipeline normally
    derives this set through ``compose_phase_1_enforcement`` which in turn
    requires a fully-resolved ``RegimeAdaptationOutput``. Until the
    regime-adaptation orchestrator is threaded through the pipeline
    scheduler (deferred follow-up), we wrap the resolved ``rule_values``
    directly — they already carry the profile * regime * overlay *
    feature-flag fold ``compose_config`` produced, which is what the
    downstream consumers (halt-state computation, repository provider,
    decision pipeline) actually read.

    Each entry's ``rule_label`` / ``unit`` mirror the ``rule_id`` and a
    flat ``"pct"`` unit — the values aren't surfaced anywhere downstream
    in the current pipeline-scheduler call path (the decision pipeline
    only reads ``rule_id`` and ``value`` from the entries).
    """
    entries = tuple(
        ActiveRiskParameterEntry(
            rule_id=rule_id,
            rule_label=rule_id,
            value=value,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=value,
        )
        for rule_id, value in sorted(rule_values.items())
    )
    return ActiveRiskParameterSet(
        regime_label=_REGIME_TO_LABEL[regime],
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def _load_base_profile_rule_values(config_dir: Path) -> Mapping[str, float]:
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
    return profile_config.rule_values


# ---------------------------------------------------------------------------
# Overlay-evaluation helpers
# ---------------------------------------------------------------------------


def _evaluate_pre_event_decision(
    *,
    now: datetime,
    config_dir: Path,
    overlays_map: Mapping[Overlay, PreEventOverlay | StressOverlay],
    scheduler_config: SchedulerConfig,
) -> OverlayActivationDecision:
    """Load the event calendar and run the pre-event activator."""
    pre_event_overlay = overlays_map[Overlay.pre_event]
    if not isinstance(pre_event_overlay, PreEventOverlay):
        msg = f"overlays_map[Overlay.pre_event] is not a PreEventOverlay: {pre_event_overlay!r}"
        raise TypeError(msg)
    event_calendar = load_event_calendar(config_dir / "event_calendar.yaml")
    return evaluate_pre_event_overlay(
        now_utc=now,
        event_calendar=event_calendar,
        scheduler_config=scheduler_config,
        pre_event_overlay=pre_event_overlay,
    )


async def _evaluate_stress_decision(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    overlays_map: Mapping[Overlay, PreEventOverlay | StressOverlay],
) -> OverlayActivationDecision:
    """Fetch the composite alert state via a sync-session bridge and evaluate stress.

    ``fetch_composite_alert_state`` is a synchronous helper that consumes a
    sync ``Session`` (it queries the ``DistillationCompositeState`` table the
    regime-adaptation orchestrator persists). The pipeline scheduler runs on
    async sessions; ``AsyncSession.run_sync`` is SQLAlchemy 2.x's canonical
    bridge that hands the sync helper the underlying ``Session`` connected to
    the same DB.
    """
    stress_overlay = overlays_map[Overlay.stress]
    if not isinstance(stress_overlay, StressOverlay):
        msg = f"overlays_map[Overlay.stress] is not a StressOverlay: {stress_overlay!r}"
        raise TypeError(msg)
    async with session_factory() as session:
        composite_alert_state = await session.run_sync(
            lambda sync_session: fetch_composite_alert_state(sync_session)
        )
    return evaluate_stress_overlay(
        funding_stress_alert_active=composite_alert_state.funding_stress_alert_active,
        market_liquidity_alert_active=composite_alert_state.market_liquidity_alert_active,
        funding_stress_calibration_state=composite_alert_state.funding_stress_calibration_state,
        market_liquidity_calibration_state=composite_alert_state.market_liquidity_calibration_state,
        stress_overlay=stress_overlay,
    )


# ---------------------------------------------------------------------------
# Row-update helpers
# ---------------------------------------------------------------------------


async def _update_row_phase1(
    handle: InvocationHandle,
    *,
    phase1_summary: Phase1Summary,
    staleness_flag: bool,
) -> None:
    """Persist Phase 1 outcomes onto the bound invocation row.

    Writes ``fill_collection_summary_json`` (the serialized
    :class:`Phase1Summary`) and ``staleness_flag`` (the union of every
    sub-fetch's degradation state).
    """
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
# Mode translation (config-layer ``Mode`` → decision-pipeline literal)
# ---------------------------------------------------------------------------


def _mode_to_decision_literal(mode: Mode) -> Literal["normal", "halt"]:
    """Translate the config-layer ``Mode`` to the decision pipeline's literal.

    ``Mode.normal`` → ``"normal"``; ``Mode.halt`` → ``"halt"`` (defensive
    posture). The row's ``active_mode`` column uses a different vocabulary
    (story 03a's ``ActiveMode``); this helper handles only the
    decision-pipeline side. Symmetric with
    :func:`alphamind.scheduler.invocation._mode_to_active_mode_literal`: a
    future ``Mode`` enum expansion raises ``ValueError`` rather than
    silently mis-translating into ``"normal"``.
    """
    if mode is Mode.normal:
        return "normal"
    if mode is Mode.halt:
        return "halt"
    msg = f"unexpected Mode member {mode!r}; orchestrator knows only normal | halt"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Baseline DISTILLATION_CONFIG_CHANGE emission
# ---------------------------------------------------------------------------


async def _emit_baseline_config_change_entry(
    *,
    handle: InvocationHandle,
    config_dir: Path,
    now: datetime,
) -> None:
    """Emit a baseline ``DISTILLATION_CONFIG_CHANGE`` entry per invocation.

    Loads the distillation config from ``<config_dir>/distillation.yaml``
    and calls :func:`emit_distillation_config_change_entry` with
    ``prior=None``. The helper's hash-check de-dup
    (``read_most_recent_config_change_new_hash``) suppresses no-op
    re-emissions on subsequent invocations with byte-identical config; on
    a fresh DB this writes one baseline entry per config-version so the
    verify script's ``check_activity_log`` succeeds even when Phase 1 and
    Phase 2 emit zero entries (clean paper-DB invocation).

    The git SHA is read from the bound invocation row (story 03a stamps
    it on ``open_invocation`` enter). The entry id follows the convention
    used by the Phase 1 / Phase 2 emitters
    (:func:`alphamind.execution.state_persistence.write_paths.phase1._append_activity_log_entry`):
    ``{invocation_id}-{event_type}-{uuid4-hex}``.
    """
    from alphamind.portfolio_state.events.activity_log import EventType
    from alphamind.scripts._common import load_distillation_config

    distillation_config = load_distillation_config(config_dir / "distillation.yaml")
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared before "
            "baseline DISTILLATION_CONFIG_CHANGE emission; "
            "insert_invocation_record should have committed it before Phase 1 opened"
        )
        raise RuntimeError(msg)
    entry_id = (
        f"{handle.invocation_id}-{EventType.DISTILLATION_CONFIG_CHANGE.value}-{uuid.uuid4().hex}"
    )
    await emit_distillation_config_change_entry(
        handle,
        prior=None,
        new=distillation_config,
        timestamp=now,
        git_sha=row.git_sha_at_invocation,
        entry_id=entry_id,
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
    mode_literal: Literal["normal", "halt"],
    halt_state: HaltState | None,
    now: datetime,
    archive_root: Path,
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[str], str],
    assembled_snapshot: AssembledSnapshot,
) -> dict[str, Any]:
    """Assemble the kwargs ``run_decision_pipeline`` requires.

    Pulls from the loaded :class:`PipelineConfig` (resolved feature flags,
    active sectors, agents config + overrides), the pre-built
    :class:`AssembledSnapshot` (the same one the synthesizer reader
    projected from), and the Phase 1 market inputs (so the library
    projector reads consistent prices).
    """
    resolved = pipeline_config.resolved

    feature_flags = FeatureFlagsView(
        options_enabled=resolved.feature_flags.options_enabled,
        short_selling_enabled=resolved.feature_flags.short_selling_enabled,
    )
    library_config = LibraryConfig(
        effective_limits=resolved.rule_values,
        escalation_zones={},  # populated by upstream guardrail composition; minimal default here
        feature_flags=feature_flags,
        active_sectors=tuple(sorted(_active_sectors_from_resolved(resolved))),
        active_regime=resolved.regime_label,
        active_profile=resolved.profile_label,
        conservative_buffer_pct=0.0,
    )
    library_market = phase1_market_inputs

    return {
        "assembled_snapshot": assembled_snapshot,
        "synthesizer_text": analysis_result.synthesizer_result.synthesis_text,
        "retrieval_store": analysis_result.synthesizer_result.retrieval_store,
        "mode": mode_literal,
        "halt_state": halt_state,
        "agents_config": dict(resolved.agents.agents),
        "agent_overrides": dict(resolved.agent_overrides),
        "sector_resolver": sector_resolver,
        "borrow_cost_resolver": None,
        "library_config": library_config,
        "library_market": library_market,
        "profile_feature_flags": feature_flags,
        "state_delivery_config": state_delivery_config,
        "options_enabled": feature_flags.options_enabled,
        "short_selling_enabled": feature_flags.short_selling_enabled,
        "active_sectors": frozenset(library_config.active_sectors),
        "invocation_id": invocation_id,
        "timestamp": now,
        "archive_root": archive_root,
    }


def _active_sectors_from_resolved(resolved: Any) -> set[str]:
    """Read active sectors from the loaded assets config.

    Logs a warning and returns an empty set when the resolved config lacks
    either ``assets`` or ``assets.sectors``. Future schema changes that
    rename or relocate these attributes surface as a visible warning rather
    than silently producing empty data.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; active_sectors empty")
        return set()
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; active_sectors empty")
        return set()
    return set(resolved.assets.sectors.keys())


def _build_sector_resolver(resolved: Any) -> Callable[[str], str]:
    """Build a ticker→sector resolver from the resolved assets config.

    Walks ``resolved.assets.sectors`` (``dict[sector, list[ticker]]``) and
    constructs the inverse map. The returned callable looks up the ticker
    and returns its sector; tickers absent from every sector list resolve
    to ``"UNCLASSIFIED"`` (the same sentinel
    :func:`alphamind.portfolio_state.consumers.synthesizer._project_positions`
    uses). Logs a warning when ``assets.sectors`` is unavailable.
    """
    ticker_to_sector: dict[str, str] = {}
    if not hasattr(resolved, "assets") or not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets.sectors unavailable; sector_resolver returns 'UNCLASSIFIED'")
    else:
        for sector, tickers in resolved.assets.sectors.items():
            for ticker in tickers:
                ticker_to_sector[ticker] = sector

    def _resolver(ticker: str) -> str:
        return ticker_to_sector.get(ticker, "UNCLASSIFIED")

    return _resolver


def _make_repository_providers(
    active_risk_parameters: ActiveRiskParameterSet,
) -> tuple[
    Callable[[], Awaitable[ActiveRiskParameterSet]],
    Callable[[str], Awaitable[ActiveRiskParameterSet]],
]:
    """Build the two closure-providers ``SqlPortfolioStateRepository`` consumes.

    The repository factory's ``active_risk_parameters_provider`` is zero-arg;
    ``prior_active_risk_parameters_provider`` takes the prior invocation's
    resolved-config snapshot path. Both currently yield the same
    ``active_risk_parameters`` value (pre-review triage simplification — the
    prior-snapshot rehydration is deferred to a follow-up story; surfacing
    a stale set keeps the snapshot assembler operational without depending
    on the persisted-snapshot codec landing first).
    """

    async def _active_provider() -> ActiveRiskParameterSet:
        return active_risk_parameters

    async def _prior_provider(_snapshot_path: str) -> ActiveRiskParameterSet:
        return active_risk_parameters

    return _active_provider, _prior_provider


def _price_provider_from_phase1(
    market_inputs: MarketInputs,
) -> StubCurrentPriceProvider:
    """Wrap Phase 1's underlying-price map in the canonical stub price provider.

    Reuses ``StubCurrentPriceProvider`` so the snapshot assembler reads the
    same prices the Reg T wedge consumed during Phase 1 — one consistent
    set per invocation. Future stories swap in a real Alpaca-quotes-backed
    provider once the streaming-quotes path lands.
    """
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


async def run_invocation(  # noqa: PLR0913 — composition surface threads typed inputs
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    now: datetime,
) -> InvocationSummary:
    """Drive one pipeline invocation through the design's three-transaction model.

    Sequences:

    1. Resolve runtime dimensions against a short read-only session.
    2. :func:`insert_invocation_record` inserts + commits the invocation row
       in its own short transaction. The row is durable + visible to
       fresh-session reads from this point forward.
    3. **Phase 1 transaction** — one session wrapping the baseline
       ``DISTILLATION_CONFIG_CHANGE`` emission, ``gather_phase1_inputs``,
       ``process_unprocessed_fills`` (stamps ``phase1_completed_at``), and
       the row's Phase 1 summary writeback. Commits at close.
    4. **Snapshot read between phases** — ``assemble_snapshot`` opens fresh
       sessions via the repository factory and reads the now-committed
       Phase 1 state. Feeds :class:`SnapshotBackedSynthesizerReader`.
    5. **Analysis + decision** (read-only) — ``run_analysis_pipeline`` and
       ``run_decision_pipeline`` run against fresh sessions. The snapshot
       built above flows through both. No transaction commits here.
    6. **Phase 2 transaction** — one session wrapping ``dispatch_phase2``,
       the row's Phase 2 summary writeback, and the
       ``phase2_completed_at`` stamp. Commits at close.
    7. Return :class:`InvocationSummary`.

    Failure semantics (per ``docs/design/mid-pipeline-failure-handling.md``):
      * Phase 1 abort: Phase 1's tx rolls back; the row stays with
        ``phase1_completed_at IS NULL``; the repository's consistency guard
        refuses snapshot reads against this invocation; the next invocation
        retries fills.
      * Between-phase abort (snapshot/analysis/decision): Phase 1 stays
        committed; Phase 2 is skipped; the next scheduled invocation
        regenerates briefs from current state.
      * Phase 2 abort: Phase 2's tx rolls back; ``phase2_completed_at``
        stays NULL; already-committed envelopes from prior loop iterations
        remain durable (per-envelope split lands in Slice 3 / ALP-449).
    """
    start_perf = time.monotonic()
    state_persistence_config = load_state_persistence_config(
        read_yaml_file(config_dir / "main.yaml")
    )
    state_delivery_config = load_state_delivery_config(config_dir / "state_delivery.yaml")
    scheduler_config = SchedulerConfig.model_validate(read_yaml_file(config_dir / "scheduler.yaml"))
    overlays_map = load_overlays(config_dir)

    # Step 1a: read drawdown state + compose a base ActiveRiskParameterSet
    # so we can compute halt_state before resolving runtime dimensions.
    #
    # The orchestrator faces a chicken-and-egg between active_risk_parameters
    # (computed from the resolved fold, which needs runtime) and halt_state
    # (computed from active_risk_parameters, which feeds back into runtime).
    # Pre-review triage simplification: use the base profile's rule_values
    # (regime=normal, no overlays, no fold) for the halt-state computation;
    # the CURRENT invocation's active_risk_parameters — derived from the
    # fully resolved fold below — is what we pass to the repository and to
    # ``run_decision_pipeline``.
    drawdown_state = await _read_drawdown_state(session_factory)
    base_active_risk_parameters = _build_active_risk_parameters(
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
    pre_event_decision = _evaluate_pre_event_decision(
        now=now,
        config_dir=config_dir,
        overlays_map=overlays_map,
        scheduler_config=scheduler_config,
    )
    stress_decision = await _evaluate_stress_decision(
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

    # Step 2: insert the invocation row in its own short transaction. The
    # row is committed before Phase 1 opens, so fresh-session reads (from
    # the SQL repository in particular) can see it across the rest of the
    # invocation.
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

    # Compose the current invocation's active_risk_parameters from the
    # resolved fold (profile * regime * overlays * feature-flags). Threaded
    # into the repository providers (current + prior) so the snapshot
    # assembler reads the same values the decision pipeline consumes.
    active_risk_parameters = _build_active_risk_parameters(
        rule_values=pipeline_config.resolved.rule_values,
        regime=runtime.active_regime,
    )

    # Step 3 — Phase 1 transaction. One session wraps the baseline config
    # change entry, fill integration, the Phase 1 summary writeback, and the
    # ``phase1_completed_at`` stamp (which ``process_unprocessed_fills``
    # emits at write_paths/phase1.py:294). Commits on context exit.
    async with session_factory() as session:
        phase1_handle = InvocationHandle(session=session, invocation_id=invocation_id)
        # Emit a baseline DISTILLATION_CONFIG_CHANGE entry on first invocation
        # per config-version. The helper's hash-check de-dup suppresses
        # no-op re-emissions on subsequent invocations with byte-identical
        # distillation config; on a fresh DB this guarantees at least one
        # ``activity_log`` entry per invocation so the verify script's
        # ``check_activity_log`` succeeds even when Phase 1 / Phase 2 emit
        # zero entries (clean paper-DB invocation with no fills, no commands).
        await _emit_baseline_config_change_entry(
            handle=phase1_handle,
            config_dir=config_dir,
            now=now,
        )
        phase1_inputs = await gather_phase1_inputs(
            handle=phase1_handle,
            venue_config=venue_config,
            execution_mode=execution_mode,
            as_of=now,
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

    # Step 4 — Between-phase snapshot read. Fresh sessions via the
    # repository factory now correctly see the committed Phase 1 state.
    sector_resolver = _build_sector_resolver(pipeline_config.resolved)
    assembled = await _assemble_phase1_snapshot(
        session_factory=session_factory,
        invocation_id=invocation_id,
        state_persistence_config=state_persistence_config,
        active_risk_parameters=active_risk_parameters,
        phase1_market_inputs=phase1_inputs.market_inputs,
        sector_resolver=sector_resolver,
    )
    portfolio_reader = SnapshotBackedSynthesizerReader(
        assembled.snapshot,
        sector_resolver=_adapt_sector_resolver_for_snapshot(sector_resolver),
    )

    # Step 5 — Read-only analysis + decision pipelines. The analysis
    # pipeline consumes a fresh session for its distillation-layer reads;
    # the decision pipeline's repository opens its own sessions via the
    # session_factory.
    async with session_factory() as read_session:
        analysis_handle = InvocationHandle(session=read_session, invocation_id=invocation_id)
        analysis_result = await _run_analysis(
            handle=analysis_handle,
            pipeline_config=pipeline_config,
            config_dir=config_dir,
            archive_root=archive_root,
            now=now,
            portfolio_reader=portfolio_reader,
        )

    mode_literal = _mode_to_decision_literal(runtime.active_mode)
    decision_kwargs = _build_decision_kwargs(
        invocation_id=invocation_id,
        pipeline_config=pipeline_config,
        analysis_result=analysis_result,
        phase1_market_inputs=phase1_inputs.market_inputs,
        mode_literal=mode_literal,
        halt_state=halt_state,
        now=now,
        archive_root=archive_root,
        state_delivery_config=state_delivery_config,
        sector_resolver=sector_resolver,
        assembled_snapshot=assembled,
    )
    decision_result = await run_decision_pipeline(**decision_kwargs)

    # Step 6 — Phase 2. ``dispatch_phase2`` commits each envelope's
    # writes in its own transaction so a mid-batch persistence failure
    # leaves earlier envelopes durable (per ``docs/design/
    # mid-pipeline-failure-handling.md`` "commands submitted before the
    # abort remain committed"). Once dispatch returns, a final short
    # transaction writes the Phase 2 summary onto the invocation row and
    # stamps ``phase2_completed_at``.
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

    duration = time.monotonic() - start_perf
    return InvocationSummary(
        invocation_id=invocation_id,
        trigger_type=trigger_type,
        firing_run_type=firing_run_type,
        phase1_summary=phase1_summary,
        commands_submitted=phase2_summary.commands_submitted,
        commands_rejected=phase2_summary.commands_rejected,
        staleness_flag=phase1_inputs.staleness_flag,
        duration_seconds=duration,
    )


async def _assemble_phase1_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    state_persistence_config: StatePersistenceConfig,
    active_risk_parameters: ActiveRiskParameterSet,
    phase1_market_inputs: MarketInputs,
    sector_resolver: Callable[[str], str],
) -> AssembledSnapshot:
    """Build the post-Phase-1 portfolio snapshot once per invocation.

    Opens fresh sessions through the repository factory; relies on Phase 1
    having already committed so the repository's
    ``phase1_completed_at IS NULL → RepositoryConsistencyError`` guard
    sees a satisfied row. The same ``AssembledSnapshot`` feeds the
    synthesizer reader and (post-Slice 2) the decision pipeline.
    """
    portfolio_state_config = load_portfolio_state_config(_PORTFOLIO_STATE_CONFIG_PATH)
    active_provider, prior_provider = _make_repository_providers(active_risk_parameters)
    repository = build_sql_portfolio_state_repository(
        session_factory=session_factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=active_provider,
        prior_active_risk_parameters_provider=prior_provider,
        config=state_persistence_config,
    )
    price_provider = _price_provider_from_phase1(phase1_market_inputs)
    # ``snapshot_assembled_at`` must be >= ``phase1_committed_at`` per
    # ``PortfolioStateSnapshot``'s ordering validator. Phase 1 stamps the row
    # with wall-clock-at-stamp-time; using a fresh ``datetime.now(UTC)`` here
    # guarantees the snapshot reflects post-Phase-1 reality even when the
    # orchestrator's logical ``now`` predates Phase 1's actual completion
    # (the common case under test fixtures with a frozen ``now``).
    return await assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        sector_resolver=_adapt_sector_resolver_for_snapshot(sector_resolver),
        config=portfolio_state_config,
        now=datetime.now(UTC),
    )


def _adapt_sector_resolver_for_snapshot(
    sector_resolver: Callable[[str], str],
) -> Callable[[Any], str | None]:
    """Adapt a ticker→sector resolver to the position-shaped one the assembler consumes.

    Mirrors ``alphamind.pipeline.decision._adapt_sector_resolver_for_assembler``
    (the decision pipeline's own copy); both will fold into a single shared
    helper once Slice 2 lands the snapshot-passing signature change.
    """
    from alphamind.portfolio_state.consumers.synthesizer import _ticker_from_position

    def _adapter(position: Any) -> str | None:
        ticker = _ticker_from_position(position)
        return sector_resolver(ticker) if ticker else None

    return _adapter


async def _run_analysis(
    *,
    handle: InvocationHandle,
    pipeline_config: PipelineConfig,
    config_dir: Path,
    archive_root: Path,
    now: datetime,
    portfolio_reader: SnapshotBackedSynthesizerReader,
) -> Any:
    """Compose ``run_analysis_pipeline`` inputs from the loaded config + handle.

    Extracted out of :func:`run_invocation` to keep the main orchestrator
    body readable; threads the synthesizer portfolio reader, distillation
    config, agents config + overrides, and the ticker scope through to
    the analysis composition. The reader is built upstream by
    :func:`_build_snapshot_backed_synthesizer_reader` so the synthesizer
    projects the same post-Phase-1 snapshot the decision pipeline will
    consume.
    """
    from alphamind.scripts._common import load_distillation_config

    resolved = pipeline_config.resolved
    ticker_scope = _ticker_scope_from_assets(resolved)
    return await run_analysis_pipeline(
        session=handle.session,  # type: ignore[arg-type]
        invocation_id=handle.invocation_id,
        as_of=now,
        last_invocation_time=now,
        distillation_config=load_distillation_config(config_dir / "distillation.yaml"),
        ticker_scope=ticker_scope,
        universe=frozenset(ticker_scope),
        agents_config={name.value: cfg for name, cfg in resolved.agents.agents.items()},
        sectors_config=_sectors_config_from_assets(resolved),
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
    )


def _ticker_scope_from_assets(resolved: Any) -> tuple[str, ...]:
    """Extract the per-invocation ticker scope from the resolved assets config.

    ``AssetsConfig`` does not expose a top-level ``universe`` field; the
    scope is the alphabetized union of every sector's tickers (mirroring
    :func:`alphamind.scripts._common.load_universe_scope`). Logs a warning
    and returns an empty tuple when ``resolved`` lacks an ``assets`` /
    ``assets.sectors`` attribute path — future schema changes surface as a
    visible warning rather than silent empty data.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; ticker_scope empty")
        return ()
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; ticker_scope empty")
        return ()
    tickers: set[str] = set()
    for sector_tickers in resolved.assets.sectors.values():
        tickers.update(sector_tickers)
    return tuple(sorted(tickers))


def _sectors_config_from_assets(resolved: Any) -> dict[str, list[str]]:
    """Extract the per-sector ticker buckets from the resolved assets config.

    Logs a warning and returns an empty dict when the resolved config
    lacks ``assets`` / ``assets.sectors``.
    """
    if not hasattr(resolved, "assets"):
        log.warning("resolved config has no 'assets' attribute; sectors_config empty")
        return {}
    if not hasattr(resolved.assets, "sectors"):
        log.warning("resolved.assets has no 'sectors' attribute; sectors_config empty")
        return {}
    return {sector: list(tickers) for sector, tickers in resolved.assets.sectors.items()}
