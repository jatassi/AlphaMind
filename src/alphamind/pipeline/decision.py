"""Decision-layer pipeline composition wiring — story ALP-403.

The single ``async`` entry point a per-invocation pipeline calls to drive
the decision layer end-to-end. Sequences:

1. :func:`apply_agent_overrides` — fold per-trigger overrides onto the four
   decision-layer ``BaseAgentConfig`` slots.
2. :func:`assemble_snapshot` — produce a fresh :class:`PortfolioStateSnapshot`
   from the repository + price provider.
3. :func:`to_library_snapshot` — translate the Pydantic snapshot to the
   math-library dataclass shape consumed by the proposal pre-processor.
4. ``project_*_view`` — produce per-consumer typed views for analyst,
   strategist, and PM.
5. :func:`run_analyst` + :func:`run_strategist` — independent inputs, run
   in parallel under :func:`asyncio.gather` with ``return_exceptions=False``
   per the fail-closed policy in
   ``docs/design/llm-agent-failure-handling.md``.
6. :func:`run_proposal_pre_processor` — derive the typed bundle from both
   agents' outputs.
7. :func:`run_portfolio_manager` — read the bundle and the PM-side
   :class:`CrossConstraintImpact` derived from
   ``aggregate_observations.combined_set_impact``.

Per-trigger budget overrides from ``config/run_types/<trigger>.yaml``
(:attr:`RunTypeConfig.agent_overrides`) reach every decision-layer agent
via :func:`apply_agent_overrides` — the helper folds the override fields
onto each :class:`BaseAgentConfig` and produces the string-keyed mapping
the runners consume.

Per the parent issue's "fail-closed propagation" invariant, any harness
failure in any of the four agents propagates immediately. The runner does
NOT catch and degrade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision.analyst.runner import AnalystResult, run_analyst
from alphamind.decision.portfolio_manager.runner import (
    PMResult,
    run_portfolio_manager,
)
from alphamind.decision.proposal_pre_processor import (
    ProposalPreProcessorBundle,
    run_proposal_pre_processor,
)
from alphamind.decision.proposal_pre_processor.models import CombinedSetImpact
from alphamind.decision.strategist.runner import StrategistResult, run_strategist
from alphamind.pipeline._shared import apply_agent_overrides
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.consumers.analyst import project_analyst_view
from alphamind.portfolio_state.consumers.portfolio_manager import (
    SnapshotBackedThesisComponentReader,
    project_portfolio_manager_view,
)
from alphamind.portfolio_state.consumers.strategist import project_strategist_view
from alphamind.portfolio_state.library_snapshot import (
    LibrarySnapshot,
    to_library_snapshot,
)
from alphamind.portfolio_state.pricing import CurrentPriceProvider
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CorrelationState,
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    DependencyRiskFlag,
    RegimeOverride,
)

# Synthesizer's ``RetrievalStore`` is the harness-side type the analyst,
# strategist, and PM consume.
from alphamind.analysis.synthesizer.retrieval import RetrievalStore  # isort: skip

__all__ = ["DecisionPipelineResult", "run_decision_pipeline"]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionPipelineResult:
    """Bundled return value of :func:`run_decision_pipeline`.

    Carries every typed result the composition produced so callers (the
    upstream pipeline-trigger layer; the verify script) can read them
    directly without re-running any stage.
    """

    pydantic_snapshot: PortfolioStateSnapshot
    library_snapshot: LibrarySnapshot
    analyst_result: AnalystResult
    strategist_result: StrategistResult
    pre_processor_bundle: ProposalPreProcessorBundle
    pm_result: PMResult


# ---------------------------------------------------------------------------
# Mode-dispatch tables (avoid magic strings inline)
# ---------------------------------------------------------------------------

_ANALYST_MODE_FOR_PIPELINE: Mapping[str, Literal["normal", "watchlist"]] = {
    "normal": "normal",
    "halt": "watchlist",
}

_STRATEGIST_MODE_FOR_PIPELINE: Mapping[str, Literal["normal", "defensive_posture"]] = {
    "normal": "normal",
    "halt": "defensive_posture",
}


# ---------------------------------------------------------------------------
# Cross-constraint-impact derivation (parent decision (D))
# ---------------------------------------------------------------------------


def _derive_cross_constraint_impact(
    *,
    combined_set_impact: CombinedSetImpact,
    available_capital_usd: float,
) -> CrossConstraintImpact:
    """Translate the pre-processor's ``combined_set_impact`` into the PM-side
    :class:`CrossConstraintImpact`.

    Parent decision (D) of the work tree fixes the derivation:
    - ``per_rule[*]`` ← ``combined_set_impact.per_rule[*]`` field-for-field
      (``rule`` → ``rule_id``; ``rule`` is also the ``rule_label`` since the
      pre-processor does not carry a separate display label).
    - ``flagged_rule_ids`` ← ``combined_set_impact.breaches[*].rule``.
    - ``available_capital_before_usd`` and ``available_capital_after_usd``
      both source from the snapshot's
      ``cash_ledger.true_deployable_capital_usd`` — no proposal-side capital
      consumption in this iteration.
    """
    per_rule = tuple(
        CrossConstraintImpactPerRule(
            rule_id=entry.rule,
            rule_label=entry.rule,
            current=entry.current,
            projected_after=entry.projected_after,
            limit=entry.limit,
            unit=entry.unit,
        )
        for entry in combined_set_impact.per_rule
    )
    flagged_rule_ids = tuple(b.rule for b in combined_set_impact.breaches)
    return CrossConstraintImpact(
        per_rule=per_rule,
        flagged_rule_ids=flagged_rule_ids,
        available_capital_before_usd=available_capital_usd,
        available_capital_after_usd=available_capital_usd,
    )


# ---------------------------------------------------------------------------
# Composition runner
# ---------------------------------------------------------------------------


async def run_decision_pipeline(  # noqa: PLR0913 — composition surface threads typed inputs through every stage
    *,
    repository: PortfolioStateRepository,
    price_provider: CurrentPriceProvider,
    portfolio_state_config: PortfolioStateConfig,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    mode: Literal["normal", "halt"],
    halt_state: HaltState | None,
    agents_config: Mapping[AgentName, BaseAgentConfig],
    agent_overrides: Mapping[AgentName, Mapping[str, Any]],
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float] | None,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    profile_feature_flags: FeatureFlagsView,
    state_delivery_config: StateDeliveryConfig,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: frozenset[str],
    invocation_id: str,
    timestamp: datetime,
    now: datetime,
    archive_root: Path | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> DecisionPipelineResult:
    """Run the decision-layer composition end-to-end.

    See module docstring for the seven-stage sequence. The runner is pure
    in the sense that it assembles a fresh snapshot, fresh validation-state
    cells, and fresh submit-envelope state on every invocation — no
    module-level state survives between calls.

    Per the fail-closed policy in
    ``docs/design/llm-agent-failure-handling.md``, any failure in any stage
    propagates immediately. ``asyncio.gather(..., return_exceptions=False)``
    on the analyst+strategist branch cancels the in-flight sibling when
    one raises.

    The two clock-shaped kwargs serve different roles: ``now`` flows only
    into :func:`assemble_snapshot` as the assembler-side clock for
    freshness checks against the price provider; ``timestamp`` flows into
    each of the four agent runners as the per-invocation timestamp the
    agents stamp into their structured outputs. Callers typically pass
    the same ``datetime`` for both, but the runner keeps them separate so
    a deterministic-replay harness can pin the snapshot clock to a
    fixture without disturbing the agent-side timestamp.
    """
    # Halt-mode requires a halt_state. Surface the gap with a clear message
    # before the parallel branch starts so callers don't wait on a runner
    # that would itself raise the same.
    if mode == "halt" and halt_state is None:
        raise ValueError(
            "run_decision_pipeline(mode='halt') requires a halt_state; "
            "the analyst and strategist halt-mode renderers cannot be composed without one"
        )

    # 1. Apply per-trigger overrides → string-keyed mapping for runners.
    resolved_agents = apply_agent_overrides(agents_config, agent_overrides)

    # 2. Assemble snapshot — fresh per invocation.
    assembled = await assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        sector_resolver=_adapt_sector_resolver_for_assembler(sector_resolver),
        config=portfolio_state_config,
        now=now,
    )
    pydantic_snapshot = assembled.snapshot

    # 3. Translate to library shape.
    library_snapshot = to_library_snapshot(
        pydantic_snapshot,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    # 4. Project per-consumer views.
    analyst_view = project_analyst_view(
        pydantic_snapshot,
        sector_resolver=_adapt_sector_resolver_for_assembler(sector_resolver),
        per_position_size_rule_id="position_max_size_pct",
        total_portfolio_value_usd=library_snapshot.portfolio_value_usd,
    )
    strategist_view = project_strategist_view(pydantic_snapshot)
    pm_view = project_portfolio_manager_view(pydantic_snapshot)
    thesis_component_reader = SnapshotBackedThesisComponentReader(pydantic_snapshot)

    # 5. Run analyst + strategist in parallel — fail-closed via gather.
    analyst_mode = _ANALYST_MODE_FOR_PIPELINE[mode]
    strategist_mode = _STRATEGIST_MODE_FOR_PIPELINE[mode]
    available_capital_usd = pydantic_snapshot.cash_ledger.true_deployable_capital_usd
    current_price_lookup = await _build_price_lookup(
        snapshot=pydantic_snapshot,
        price_provider=price_provider,
        freshness_threshold_seconds=portfolio_state_config.snapshot_freshness_max_price_age_seconds,
    )
    analyst_result, strategist_result = await asyncio.gather(
        run_analyst(
            mode=analyst_mode,
            synthesizer_text=synthesizer_text,
            retrieval_store=retrieval_store,
            analyst_view=analyst_view,
            risk_budget=pydantic_snapshot.risk_budget,
            active_risk_parameters=pydantic_snapshot.active_risk_parameters,
            profile_feature_flags=profile_feature_flags,
            library_config=library_config,
            library_market=library_market,
            sector_resolver=sector_resolver,
            portfolio_state_snapshot=library_snapshot,
            active_sectors=active_sectors,
            invocation_id=invocation_id,
            timestamp=timestamp,
            state_delivery_config=state_delivery_config,
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
            halt_state=halt_state,
            archive_root=archive_root,
            agent_config=resolved_agents.get(AgentName.analyst.value),
            borrow_cost_resolver=borrow_cost_resolver,
        ),
        run_strategist(
            invocation_id=invocation_id,
            timestamp=timestamp,
            mode=strategist_mode,
            halt_state=halt_state,
            strategist_view=strategist_view,
            synthesizer_brief_text=synthesizer_text,
            retrieval_store=retrieval_store,
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
            active_sectors=tuple(sorted(active_sectors)),
            state_delivery_config=state_delivery_config,
            sector_resolver=sector_resolver,
            total_portfolio_value_usd=library_snapshot.portfolio_value_usd,
            available_for_new_positions_usd=available_capital_usd,
            current_price_lookup=current_price_lookup,
            profile_feature_flags=profile_feature_flags,
            library_config=library_config,
            library_market=library_market,
            starting_snapshot=library_snapshot,
            archive_root=archive_root,
            agent_config=resolved_agents.get(AgentName.strategist.value),
            sector_label_display=sector_label_display,
            regime_transition_breaches=regime_transition_breaches,
            borrow_cost_resolver=borrow_cost_resolver,
            prior_health_snapshots=prior_health_snapshots,
        ),
        return_exceptions=False,
    )

    # 6. Run proposal pre-processor — pure (no I/O, no clock reads).
    pre_processor_bundle = run_proposal_pre_processor(
        analyst_output=analyst_result.output,
        strategist_output=strategist_result.output,
        snapshot=library_snapshot,
        library_config=library_config,
        market=library_market,
        snapshot_timestamp=pydantic_snapshot.snapshot_assembled_at,
        timestamp=timestamp,
    )

    # 7. Build PM cross-constraint impact and run portfolio manager.
    cross_constraint_impact = _derive_cross_constraint_impact(
        combined_set_impact=pre_processor_bundle.aggregate_observations.combined_set_impact,
        available_capital_usd=available_capital_usd,
    )
    pm_result = await run_portfolio_manager(
        mode=mode,
        pre_processor_bundle=pre_processor_bundle,
        synthesizer_text=synthesizer_text,
        retrieval_store=retrieval_store,
        pm_view=pm_view,
        thesis_component_reader=thesis_component_reader,
        risk_budget=pydantic_snapshot.risk_budget,
        active_risk_parameters=pydantic_snapshot.active_risk_parameters,
        profile_feature_flags=profile_feature_flags,
        library_config=library_config,
        library_market=library_market,
        sector_resolver=sector_resolver,
        portfolio_state_snapshot=library_snapshot,
        active_sectors=active_sectors,
        invocation_id=invocation_id,
        timestamp=timestamp,
        state_delivery_config=state_delivery_config,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        total_portfolio_value_usd=library_snapshot.portfolio_value_usd,
        available_for_new_positions_usd=available_capital_usd,
        cross_constraint_impact=cross_constraint_impact,
        halt_state=halt_state,
        pending_orders=pydantic_snapshot.pending_orders,
        current_price_lookup=current_price_lookup,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        active_regime_overrides=active_regime_overrides,
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
        archive_root=archive_root,
        agent_config=resolved_agents.get(AgentName.portfolio_manager.value),
        borrow_cost_resolver=borrow_cost_resolver,
        prior_health_snapshots=prior_health_snapshots,
    )

    return DecisionPipelineResult(
        pydantic_snapshot=pydantic_snapshot,
        library_snapshot=library_snapshot,
        analyst_result=analyst_result,
        strategist_result=strategist_result,
        pre_processor_bundle=pre_processor_bundle,
        pm_result=pm_result,
    )


# ---------------------------------------------------------------------------
# sector_resolver adapter for the assembler + analyst-view projector
# ---------------------------------------------------------------------------


def _adapt_sector_resolver_for_assembler(
    sector_resolver: Callable[[str], str],
) -> Callable[[PositionRecord | PositionView], str | None]:
    """Adapt a ticker→sector resolver to the position-based shape the
    assembler and analyst-view projector consume.

    The runner's public surface accepts ``Callable[[str], str]`` per parent
    decision (H); the assembler and analyst-view projector thread a
    position-based resolver. Reuses
    :func:`alphamind.portfolio_state.consumers.synthesizer._ticker_from_position`
    which already handles the ``PositionRecord | PositionView`` ↦ ticker
    extraction across equity, options, and strategy details.
    """
    from alphamind.portfolio_state.consumers.synthesizer import _ticker_from_position

    def _adapter(position: PositionRecord | PositionView) -> str | None:
        ticker = _ticker_from_position(position)
        return sector_resolver(ticker) if ticker else None

    return _adapter


# ---------------------------------------------------------------------------
# Strategist + PM current_price_lookup builder
# ---------------------------------------------------------------------------


async def _build_price_lookup(
    *,
    snapshot: PortfolioStateSnapshot,
    price_provider: CurrentPriceProvider,
    freshness_threshold_seconds: float,
) -> Callable[[str], float]:
    """Build a synchronous ticker→price lookup for the strategist and PM input
    bundles.

    Pre-fetches every underlying ticker referenced by the snapshot's open
    and pending positions through the provider, then closes over the
    materialized dict. The 0.0 fallback in the closure is unreachable in
    practice — every ticker referenced by the snapshot's open and pending
    positions is enumerated above and its quote is fetched, so the agents
    only ever ask about held positions whose tickers are guaranteed to be
    in the lookup. The strategist's input-bundle renderer raises
    ``ValueError`` on a ``KeyError`` from this callable, so the fallback
    exists only to satisfy the ``Callable[[str], float]`` signature
    without requiring callers to handle ``KeyError``.
    """
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )

    seen: dict[str, None] = {}  # preserves insertion order
    for pos in (*snapshot.open_positions, *snapshot.pending_positions):
        details = pos.record.details
        if isinstance(details, EquityPositionDetails):
            seen[details.ticker] = None
        elif isinstance(details, OptionsPositionDetails):
            seen[details.underlying_ticker] = None
        elif isinstance(details, StrategyPositionDetails):
            for leg in details.legs:
                seen[leg.options.underlying_ticker] = None

    quotes = await price_provider.get_quotes(
        tickers=tuple(seen),
        freshness_threshold_seconds=freshness_threshold_seconds,
    )
    by_ticker = {ticker: quote.price_usd for ticker, quote in quotes.items()}

    def _lookup(ticker: str) -> float:
        return by_ticker.get(ticker, 0.0)

    return _lookup
