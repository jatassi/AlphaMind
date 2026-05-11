"""End-to-end phase orchestrator — ``run_invocation`` (story 03b / ALP-445).

Single async entrypoint the APScheduler driver (story 04a), the emergency
receiver (story 04b), and the CLI ``--once`` path call. Threads Phase 1
fill integration → analysis pipeline → decision pipeline → Phase 2
envelope dispatch through one :class:`InvocationContext` transaction.

Per parent issue ``ALP-431`` § Notes for the orchestrator, any exception
from any phase propagates out; the surrounding context manager rolls back
the open transaction. The long-running caller catches and continues per
parent decision (H).

The function composes existing layer primitives without inventing new
submission paths: ``gather_phase1_inputs`` (this story),
``process_unprocessed_fills`` (story 07 / ALP-365), ``run_analysis_pipeline``
(ALP-276), ``run_decision_pipeline`` (ALP-403), and ``dispatch_phase2``
(this story).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.load import PipelineConfig, load_full_config
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.state_persistence.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
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
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.write_paths.phase1 import (
    Phase1Summary,
    process_unprocessed_fills,
)
from alphamind.pipeline.analysis import run_analysis_pipeline
from alphamind.pipeline.decision import run_decision_pipeline
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.portfolio_state.records.capital import ActiveRiskParameterSet
from alphamind.risk_guardrails.breach_behavior.types import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
)
from alphamind.risk_guardrails.regime_adaptation.types import (
    OverlayActivationDecision,
)
from alphamind.risk_guardrails.state_delivery.config import (
    StateDeliveryConfig,
    load_state_delivery_config,
)
from alphamind.scheduler.invocation import open_invocation
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
# Default-overlay decisions
# ---------------------------------------------------------------------------


def _inactive_overlay_decision(overlay: Overlay) -> OverlayActivationDecision:
    """Build an inactive ``OverlayActivationDecision`` for *overlay*.

    Story 03b's scope does not wire halt-state computation or overlay
    evaluation; both default to inactive. Story 04a / 04b plug the real
    pre-event / stress evaluators into the orchestrator.
    """
    return OverlayActivationDecision(
        overlay=overlay,
        is_active=False,
        rationale=f"{overlay.value} overlay evaluation deferred to a later story",
        pre_event_block_new_positions=False,
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
            "InvocationContext should have inserted it on enter"
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
            "InvocationContext should have inserted it on enter"
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

    ``Mode.halt`` → ``"halt"`` (defensive posture); every other member
    (currently only ``Mode.normal``) routes to ``"normal"``. The row's
    ``active_mode`` column uses a different vocabulary (story 03a's
    ``ActiveMode``); this helper handles only the decision-pipeline side.
    """
    return "halt" if mode is Mode.halt else "normal"


# ---------------------------------------------------------------------------
# Decision-pipeline kwarg builder
# ---------------------------------------------------------------------------


def _build_decision_kwargs(  # noqa: PLR0913 — composition surface threads each layer's inputs through one builder.
    *,
    handle: InvocationHandle,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline_config: PipelineConfig,
    state_persistence_config: StatePersistenceConfig,
    analysis_result: Any,
    phase1_market_inputs: MarketInputs,
    mode_literal: Literal["normal", "halt"],
    halt_state: HaltState | None,
    now: datetime,
    archive_root: Path,
    state_delivery_config: StateDeliveryConfig,
) -> dict[str, Any]:
    """Assemble the ~22 kwargs ``run_decision_pipeline`` requires.

    Pulls from the loaded :class:`PipelineConfig` (resolved feature flags,
    active sectors, agents config + overrides), the open
    :class:`InvocationHandle` (repository), and the Phase 1 market inputs
    (so the library projector reads consistent prices).
    """
    resolved = pipeline_config.resolved

    repository = build_sql_portfolio_state_repository(
        session_factory=session_factory,
        invocation_id=handle.invocation_id,
        active_risk_parameters_provider=_active_risk_parameters_default_provider,
        prior_active_risk_parameters_provider=_prior_active_risk_parameters_default_provider,
        config=state_persistence_config,
    )
    price_provider = _price_provider_from_phase1(phase1_market_inputs)
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
        "repository": repository,
        "price_provider": price_provider,
        "portfolio_state_config": load_portfolio_state_config(
            Path(__file__).resolve().parents[3] / "config" / "portfolio_state.yaml"
        ),
        "synthesizer_text": analysis_result.synthesizer_result.synthesis_text,
        "retrieval_store": analysis_result.synthesizer_result.retrieval_store,
        "mode": mode_literal,
        "halt_state": halt_state,
        "agents_config": dict(resolved.agents.agents),
        "agent_overrides": dict(resolved.agent_overrides),
        "sector_resolver": _default_sector_resolver,
        "borrow_cost_resolver": None,
        "library_config": library_config,
        "library_market": library_market,
        "profile_feature_flags": feature_flags,
        "state_delivery_config": state_delivery_config,
        "options_enabled": feature_flags.options_enabled,
        "short_selling_enabled": feature_flags.short_selling_enabled,
        "active_sectors": frozenset(library_config.active_sectors),
        "invocation_id": handle.invocation_id,
        "timestamp": now,
        "now": now,
        "archive_root": archive_root,
    }


def _active_sectors_from_resolved(resolved: Any) -> set[str]:
    """Read active sectors from the loaded assets config; falls back empty."""
    try:
        return set(resolved.assets.sectors.keys())
    except AttributeError:
        return set()


def _default_sector_resolver(ticker: str) -> str:
    """Conservative default: every unknown ticker resolves to ``"tech"``.

    Story 03b's tests stub ``run_decision_pipeline`` so the resolver is
    never invoked; production threading of a real ticker→sector map is
    out of scope. A future story replaces this with a SectorsConfig-backed
    resolver.
    """
    del ticker
    return "tech"


async def _active_risk_parameters_default_provider() -> ActiveRiskParameterSet:
    """Placeholder for the active-risk-parameters provider.

    Story 03b composes the orchestrator's wiring but does not feed real
    guardrail-enforcement output into the repository — the inner
    ``run_decision_pipeline`` is stubbed in tests, and production wiring
    of the real :func:`compose_phase_1_enforcement` output is deferred to
    a follow-up story. The placeholder raises so a production path that
    accidentally consumes it (rather than stubbing it) surfaces clearly.
    """
    msg = (
        "active_risk_parameters_provider was invoked but story 03b only ships "
        "the orchestrator skeleton; wire compose_phase_1_enforcement output "
        "into this provider in the follow-up story"
    )
    raise NotImplementedError(msg)


async def _prior_active_risk_parameters_default_provider(
    prior_snapshot_path: str,
) -> ActiveRiskParameterSet:
    """Placeholder for the prior-active-risk-parameters provider."""
    del prior_snapshot_path
    msg = (
        "prior_active_risk_parameters_provider was invoked but story 03b only "
        "ships the orchestrator skeleton; wire the prior-snapshot rebuild "
        "into this provider in the follow-up story"
    )
    raise NotImplementedError(msg)


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
    """Drive one pipeline invocation end-to-end through all five phases.

    Sequences:

    1. Resolve runtime dimensions against a short read-only session.
    2. Enter :func:`open_invocation` — opens the per-invocation transaction.
    3. Phase 1 (collect): gather inputs → ``process_unprocessed_fills`` →
       update row → ``stamp_phase_completion(handle, column="phase1_completed_at")``.
    4. Phase 2 (distill + analyze): ``run_analysis_pipeline``.
    5. Phase 3 (decide): ``run_decision_pipeline``.
    6. Phase 4 (execute): ``dispatch_phase2`` → update row →
       ``stamp_phase_completion(handle, column="phase2_completed_at")``.
    7. Exit context manager — commits.
    8. Return :class:`InvocationSummary`.

    Any exception in any step propagates; the surrounding
    ``InvocationContext`` rolls back per
    ``docs/design/mid-pipeline-failure-handling.md``.
    """
    start_perf = time.monotonic()
    state_persistence_config = load_state_persistence_config(
        read_yaml_file(config_dir / "main.yaml")
    )
    state_delivery_config = load_state_delivery_config(config_dir / "state_delivery.yaml")

    # Step 1: resolve runtime dimensions in a short read-only session.
    halt_state: HaltState | None = None  # Story 03b: halt-detection deferred to 04a/b.
    async with session_factory() as short_session:
        runtime = await resolve_runtime_dimensions(
            short_session,
            firing_trigger=firing_run_type,
            halt_state=halt_state,
            pre_event_decision=_inactive_overlay_decision(Overlay.pre_event),
            stress_decision=_inactive_overlay_decision(Overlay.stress),
        )

    # Step 2: open the per-invocation transaction.
    async with open_invocation(
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
    ) as handle:
        # Re-load the pipeline config inside the transaction so the snapshot
        # the orchestrator uses agrees with the one persisted on the row.
        pipeline_config = load_full_config(
            config_dir=config_dir,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id=handle.invocation_id,
            runtime=runtime,
            today=now.astimezone(UTC).date(),
        )

        # Phase 1 — collect.
        phase1_inputs = await gather_phase1_inputs(
            handle=handle,
            venue_config=venue_config,
            execution_mode=execution_mode,
            as_of=now,
        )
        phase1_summary = await process_unprocessed_fills(
            handle,
            phase1_inputs.ca_activities,
            phase1_inputs.alpaca_positions,
            phase1_inputs.alpaca_account,
            market_inputs=phase1_inputs.market_inputs,
            config=state_persistence_config,
        )
        await _update_row_phase1(
            handle,
            phase1_summary=phase1_summary,
            staleness_flag=phase1_inputs.staleness_flag,
        )
        await stamp_phase_completion(handle, column="phase1_completed_at")

        # Phase 2 — distill + analyze.
        analysis_result = await _run_analysis(
            handle=handle,
            pipeline_config=pipeline_config,
            config_dir=config_dir,
            archive_root=archive_root,
            now=now,
        )

        # Phase 3 — decide.
        mode_literal = _mode_to_decision_literal(runtime.active_mode)
        decision_kwargs = _build_decision_kwargs(
            handle=handle,
            session_factory=session_factory,
            pipeline_config=pipeline_config,
            state_persistence_config=state_persistence_config,
            analysis_result=analysis_result,
            phase1_market_inputs=phase1_inputs.market_inputs,
            mode_literal=mode_literal,
            halt_state=halt_state,
            now=now,
            archive_root=archive_root,
            state_delivery_config=state_delivery_config,
        )
        decision_result = await run_decision_pipeline(**decision_kwargs)

        # Phase 4 — execute.
        phase2_summary = await dispatch_phase2(
            handle=handle,
            pm_result=decision_result.pm_result,
            state_persistence_config=state_persistence_config,
        )
        await _update_row_phase2(handle, phase2_summary=phase2_summary)
        await stamp_phase_completion(handle, column="phase2_completed_at")

        invocation_id = handle.invocation_id

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


async def _run_analysis(
    *,
    handle: InvocationHandle,
    pipeline_config: PipelineConfig,
    config_dir: Path,
    archive_root: Path,
    now: datetime,
) -> Any:
    """Compose ``run_analysis_pipeline`` inputs from the loaded config + handle.

    Extracted out of :func:`run_invocation` to keep the main orchestrator
    body readable; threads the synthesizer portfolio reader, distillation
    config, agents config + overrides, and the ticker scope through to
    the analysis composition. The reader / ticker scope are stub-shaped
    while the analysis composition's live-snapshot path remains under
    development; tests stub ``run_analysis_pipeline`` so the values are
    not consulted on the per-test happy path.
    """
    from alphamind.scripts._common import load_distillation_config

    resolved = pipeline_config.resolved
    portfolio_reader = _EmptySynthesizerReader()
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

    Returns an empty tuple when the assets block lacks a universe (e.g.,
    minimal fixtures or schemas that surface the universe under a
    different attribute).
    """
    try:
        return tuple(resolved.assets.universe)
    except AttributeError:
        return ()


def _sectors_config_from_assets(resolved: Any) -> dict[str, list[str]]:
    """Extract the per-sector ticker buckets from the resolved assets config."""
    try:
        return {sector: list(tickers) for sector, tickers in resolved.assets.sectors.items()}
    except AttributeError:
        return {}


class _EmptySynthesizerReader:
    """Empty stand-in implementing :class:`SynthesizerPortfolioStateReader`.

    Story 03b composes the orchestrator's wiring; the live-snapshot reader
    that backs ``get_positions_summary`` / ``get_active_theses_summary`` /
    ``get_exposure_snapshot`` against the open invocation session is
    deferred to a follow-up story. Tests stub ``run_analysis_pipeline`` so
    the reader is never invoked.
    """

    async def get_positions_summary(self) -> tuple[Any, ...]:
        return ()

    async def get_active_theses_summary(self) -> tuple[Any, ...]:
        return ()

    async def get_exposure_snapshot(self) -> Any:
        from alphamind.portfolio_state.consumers.synthesizer import (
            SynthesizerExposureSnapshot,
        )

        return SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        )
