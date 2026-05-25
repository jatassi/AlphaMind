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
   in parallel under :class:`asyncio.TaskGroup` per the fail-closed policy
   in ``docs/design/llm-agent-failure-handling.md``.
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
import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind._kernel.mode import PipelineMode
from alphamind._kernel.money import Money, Price, money, price, signed_money
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.config.models.guardrails import ProgressiveTier
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
from alphamind.execution.guardrail_enforcement import (
    compose_phase_1_enforcement,
    make_active_risk_parameters_provider,
)
from alphamind.pipeline._shared import apply_agent_overrides, build_phase1_enforcement_inputs
from alphamind.portfolio_state.consumers.analyst import project_analyst_view
from alphamind.portfolio_state.consumers.portfolio_manager import (
    SnapshotBackedThesisComponentReader,
    project_portfolio_manager_view,
)
from alphamind.portfolio_state.consumers.strategist import project_strategist_view
from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
    build_risk_budget_consumption,
)
from alphamind.risk_guardrails.library_snapshot import (
    LibrarySnapshot,
    to_library_snapshot,
)
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    RegimeTransitionBreach,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CorrelationState,
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    DependencyRiskFlag,
    RegimeOverride,
)
from alphamind.state.config import StatePersistenceConfig

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

    ``drawdown_tier`` (story ALP-433) is the classified cumulative-drawdown
    progressive-response tier from the pipeline's Phase 1 composition
    step. ``None`` when ``drawdown_state.current_drawdown_pct`` is below
    every configured tier trigger; otherwise the deepest active tier the
    classifier resolved. Surfaced here so downstream halt-mode and
    emergency-trigger callers do not redo the classification.
    """

    pydantic_snapshot: PortfolioStateSnapshot
    library_snapshot: LibrarySnapshot
    analyst_result: AnalystResult
    strategist_result: StrategistResult
    pre_processor_bundle: ProposalPreProcessorBundle
    pm_result: PMResult
    drawdown_tier: DrawdownTier | None


# ---------------------------------------------------------------------------
# Cross-constraint-impact derivation (parent decision (D))
# ---------------------------------------------------------------------------


def _derive_cross_constraint_impact(
    *,
    combined_set_impact: CombinedSetImpact,
    available_capital_usd: Money,
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
            status=entry.status,
            headroom_remaining=entry.headroom_remaining,
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


async def run_decision_pipeline(  # noqa: PLR0913 — composition surface threads typed inputs through every stage plus ALP-497 progress
    *,
    assembled_snapshot: AssembledSnapshot,
    repository: PortfolioStateRepository,
    regime_output: RegimeAdaptationOutput,
    progressive_tiers: tuple[ProgressiveTier, ...],
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    mode: Literal["normal", "halt"],
    halt_state: HaltState | None,
    agents_config: Mapping[AgentName, BaseAgentConfig],
    agent_overrides: Mapping[AgentName, Mapping[str, Any]],
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float | None] | None,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    profile_feature_flags: FeatureFlagsView,
    state_delivery_config: StateDeliveryConfig,
    state_persistence_config: StatePersistenceConfig,
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
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
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
    propagates immediately. The analyst+strategist branch runs under
    :class:`asyncio.TaskGroup`, which cancels the in-flight sibling when
    one of the parallel branches raises and re-raises the failures inside
    a ``BaseExceptionGroup``; we unwrap the first non-``CancelledError``
    child so callers see the same exception type they did under
    ``asyncio.gather``.

    ``timestamp`` flows into each of the four agent runners as the
    per-invocation timestamp the agents stamp into their structured
    outputs.

    Phase 1 enforcement composition (story ALP-433) runs once per call:
    the runner reads :class:`DrawdownState` via
    :func:`build_phase1_enforcement_inputs`, calls
    :func:`compose_phase_1_enforcement` against
    *regime_output* + *progressive_tiers*, and uses the resulting
    :class:`Phase1EnforcementResult` to (a) override the snapshot's
    ``active_risk_parameters`` with the composed parameter set (so every
    downstream agent reads the same canonical view) and (b) surface
    ``drawdown_tier`` on the bundled result. The same
    :func:`make_active_risk_parameters_provider` adapter is also built so
    SQL-repo callers do not duplicate provider construction.
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

    # 2. Compose Phase 1 enforcement once per invocation — read drawdown
    # from the repository, apply progressive-tier overrides on top of the
    # regime-resolved parameter set, build the provider adapter so the
    # snapshot assembler / SQL repo factory have one canonical entry point.
    # The provider is awaited inline below so the same closure shape that
    # ``SqlPortfolioStateRepository`` consumes also feeds the snapshot
    # override — one canonical construction path.
    phase1_regime, phase1_drawdown, phase1_tiers = build_phase1_enforcement_inputs(
        repository=repository,
        regime_output=regime_output,
        progressive_tiers=progressive_tiers,
    )
    phase1_result = compose_phase_1_enforcement(
        regime_output=phase1_regime,
        drawdown_state=phase1_drawdown,
        progressive_tiers=phase1_tiers,
    )
    active_risk_parameters_provider = make_active_risk_parameters_provider(phase1_result)
    composed_active_risk_parameters = active_risk_parameters_provider()

    # 3. Pre-built snapshot threaded from the orchestrator. Re-write the
    # snapshot's ``active_risk_parameters`` with the composed Phase 1
    # output so every downstream agent and state-delivery renderer reads
    # the canonical post-override view.
    assembled = assembled_snapshot
    pydantic_snapshot = dataclasses.replace(
        assembled.snapshot, active_risk_parameters=composed_active_risk_parameters
    )

    # 3. Translate to library shape.
    library_snapshot = to_library_snapshot(
        pydantic_snapshot,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    # 3b. Project the in-scope risk-budget consumption against the library
    # snapshot and overlay it on ``pydantic_snapshot``. The SQL repository's
    # ``get_risk_budget_consumption`` returns an empty passthrough (ALP-503);
    # the projection is the single source of truth for the budget the
    # analyst / strategist / PM input bundles consume.
    risk_budget = build_risk_budget_consumption(library_snapshot, library_config)
    pydantic_snapshot = dataclasses.replace(pydantic_snapshot, risk_budget=risk_budget)

    # 4. ALP-657 — wrap production-aggregation floats into ``Money`` once at the
    # pipeline boundary so every downstream consumer (analyst projection,
    # strategist runner, PM runner, cross-constraint impact derivation) reads
    # the same Decimal-typed value. Closes the state-delivery rendering gap
    # ALP-462 deferred. ``true_deployable_capital_usd`` is documented to
    # legitimately go negative during settlement-cycle compression
    # (see ``compute_true_deployable_capital_usd`` + test_deployable_capital_negative_allowed),
    # so it must wrap via ``signed_money`` — ``money`` would raise on negatives.
    total_portfolio_value_usd = money(str(library_snapshot.portfolio_value_usd))
    available_capital_usd = signed_money(
        str(pydantic_snapshot.cash_ledger.true_deployable_capital_usd)
    )

    # 5. Project per-consumer views.
    analyst_view = project_analyst_view(
        pydantic_snapshot,
        sector_resolver=adapt_ticker_sector_resolver(sector_resolver),
        per_position_size_rule_id="position_max_size_pct",
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_capital_usd=available_capital_usd,
    )
    strategist_view = project_strategist_view(pydantic_snapshot)
    pm_view = project_portfolio_manager_view(pydantic_snapshot)
    thesis_component_reader = SnapshotBackedThesisComponentReader(pydantic_snapshot)

    # 6. Run analyst + strategist in parallel — fail-closed via TaskGroup.
    pipeline_mode = PipelineMode(mode)
    analyst_mode = pipeline_mode.to_analyst_pipeline_mode()
    strategist_mode = pipeline_mode.to_strategist_pipeline_mode()
    current_price_lookup = _price_lookup_from_assembled(assembled)

    def _strategist_price_lookup(ticker: str) -> float:
        return float(current_price_lookup(ticker))

    progress.phase_start("analyst")
    progress.phase_start("strategist")
    try:
        async with asyncio.TaskGroup() as tg:
            analyst_task = tg.create_task(
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
                    progress=progress,
                    phase="analyst",
                )
            )
            strategist_task = tg.create_task(
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
                    total_portfolio_value_usd=total_portfolio_value_usd,
                    available_for_new_positions_usd=available_capital_usd,
                    current_price_lookup=_strategist_price_lookup,
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
                    progress=progress,
                    phase="strategist",
                )
            )
    except BaseExceptionGroup as eg:
        # Preserve the prior ``asyncio.gather`` API: callers see the first
        # non-``CancelledError`` failure unchanged. The group is attached as
        # ``__cause__`` via ``raise ... from eg`` so diagnostics still
        # surface every concurrent failure. ``first_non_cancelled`` returns
        # ``None`` only when every child is ``CancelledError`` (external
        # cancellation of the parent task) — re-raise the group in that case.
        first = first_non_cancelled(eg)
        if first is not None:
            raise first from eg
        raise

    analyst_result = analyst_task.result()
    strategist_result = strategist_task.result()
    progress.phase_done("analyst")
    progress.phase_done("strategist")

    # 7. Run proposal pre-processor — pure (no I/O, no clock reads).
    progress.phase_start("pre_processor")
    pre_processor_bundle = run_proposal_pre_processor(
        analyst_output=analyst_result.output,
        strategist_output=strategist_result.output,
        snapshot=library_snapshot,
        library_config=library_config,
        market=library_market,
        snapshot_timestamp=pydantic_snapshot.snapshot_assembled_at,
        timestamp=timestamp,
    )
    progress.phase_done("pre_processor")

    # 8. Build PM cross-constraint impact and run portfolio manager.
    cross_constraint_impact = _derive_cross_constraint_impact(
        combined_set_impact=pre_processor_bundle.aggregate_observations.combined_set_impact,
        available_capital_usd=available_capital_usd,
    )
    progress.phase_start("pm")
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
        state_persistence_config=state_persistence_config,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        total_portfolio_value_usd=total_portfolio_value_usd,
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
        progress=progress,
        phase="pm",
    )
    progress.phase_done("pm")

    return DecisionPipelineResult(
        pydantic_snapshot=pydantic_snapshot,
        library_snapshot=library_snapshot,
        analyst_result=analyst_result,
        strategist_result=strategist_result,
        pre_processor_bundle=pre_processor_bundle,
        pm_result=pm_result,
        drawdown_tier=phase1_result.drawdown_tier,
    )


# ---------------------------------------------------------------------------
# Strategist + PM current_price_lookup builder
# ---------------------------------------------------------------------------


def _price_lookup_from_assembled(assembled: AssembledSnapshot) -> Callable[[str], Price]:
    """Build a synchronous ticker→price lookup over the assembler-materialized
    ``price_map`` (ALP-407).

    Staleness is fixed at the assembler's ``get_quotes`` fetch time (per
    ``config.snapshot_freshness_max_price_age_seconds``); callers cannot
    apply a different freshness threshold here. The pre-PR
    ``_build_price_lookup`` accepted a ``freshness_threshold_seconds``
    parameter that is meaningless now that the assembler is the single
    source of truth on freshness.

    Raises ``KeyError`` for a ticker absent from ``price_map``. Every ticker
    referenced by the snapshot's open and pending positions is enumerated by
    the assembler, so callers only ever ask about held positions whose
    quotes are guaranteed present — a miss indicates a bug upstream. Both
    consumers (halt_mode and strategist input bundles) wrap the call in a
    ``try/except KeyError`` that re-raises as ``ValueError`` with the
    ticker; emitting ``KeyError`` here preserves that loud-failure contract.
    """
    by_ticker: dict[str, Price] = {
        ticker: price(quote.price_usd) for ticker, quote in assembled.price_map.items()
    }

    def _lookup(ticker: str) -> Price:
        return by_ticker[ticker]

    return _lookup
