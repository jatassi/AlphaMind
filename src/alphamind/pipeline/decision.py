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
from alphamind.portfolio_state.consumers.analyst import project_analyst_view
from alphamind.portfolio_state.consumers.portfolio_manager import (
    SnapshotBackedThesisComponentReader,
    project_portfolio_manager_view,
)
from alphamind.portfolio_state.consumers.strategist import project_strategist_view
from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.library_snapshot import (
    LibrarySnapshot,
    to_library_snapshot,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
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
    assembled_snapshot: AssembledSnapshot,
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
    archive_root: Path | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
) -> DecisionPipelineResult:
    """Run the decision-layer composition end-to-end.

    See module docstring for the six-stage sequence. The runner consumes
    a pre-built :class:`AssembledSnapshot` (assembled by the orchestrator
    between Phase 1 and analysis per the three-transaction model in
    ``docs/design/05-execution-layer/state-persistence.md`` § Snapshot
    isolation) and produces fresh validation-state cells + submit-envelope
    state on every invocation — no module-level state survives between
    calls.

    Per the fail-closed policy in
    ``docs/design/llm-agent-failure-handling.md``, any failure in any stage
    propagates immediately. ``asyncio.gather(..., return_exceptions=False)``
    on the analyst+strategist branch cancels the in-flight sibling when
    one raises.

    ``timestamp`` flows into each of the four agent runners as the
    per-invocation timestamp the agents stamp into their structured
    outputs.
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

    # 2. Pre-built snapshot threaded from the orchestrator (single source of
    # truth — same snapshot the synthesizer reader projected from).
    assembled = assembled_snapshot
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
        sector_resolver=adapt_ticker_sector_resolver(sector_resolver),
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
    current_price_lookup = _price_lookup_from_assembled(assembled)
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
# Strategist + PM current_price_lookup builder
# ---------------------------------------------------------------------------


def _price_lookup_from_assembled(assembled: AssembledSnapshot) -> Callable[[str], float]:
    """Build a synchronous ticker→price lookup over the assembler-materialized
    ``price_map`` (ALP-407).

    Staleness is fixed at the assembler's ``get_quotes`` fetch time (per
    ``config.snapshot_freshness_max_price_age_seconds``); callers cannot
    apply a different freshness threshold here. The pre-PR
    ``_build_price_lookup`` accepted a ``freshness_threshold_seconds``
    parameter that is meaningless now that the assembler is the single
    source of truth on freshness.

    The 0.0 fallback in the closure is unreachable in practice — every
    ticker referenced by the snapshot's open and pending positions was
    enumerated by the assembler and its quote is in ``price_map``, so the
    agents only ever ask about held positions whose tickers are guaranteed
    to be present. The strategist's input-bundle renderer raises
    ``ValueError`` on a ``KeyError`` from this callable, so the fallback
    exists only to satisfy the ``Callable[[str], float]`` signature
    without requiring callers to handle ``KeyError``.
    """
    by_ticker = {ticker: quote.price_usd for ticker, quote in assembled.price_map.items()}

    def _lookup(ticker: str) -> float:
        return by_ticker.get(ticker, 0.0)

    return _lookup
