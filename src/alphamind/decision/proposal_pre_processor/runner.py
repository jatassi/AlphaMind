"""Public entry point for the proposal pre-processor — ALP-318.

``run_proposal_pre_processor`` composes the prior waves' work — translators,
histogram observations, conflict detection, combined-set impact — into a
single :class:`ProposalPreProcessorBundle`. It is pure: identical inputs
produce identical bundles. No clock reads, no UUID generation, no I/O.

The decision-layer pipeline (ALP-310) and the verify script (story 05)
both call this function.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.decision.analyst.models import AnalystOutput, Recommendation
from alphamind.decision.proposal_pre_processor.assembler import (
    build_analyst_section,
    build_held_direction_resolver,
    build_strategist_section,
    verify_halt_state_consistency,
    verify_invocation_id_consistency,
)
from alphamind.decision.proposal_pre_processor.conflicts import (
    ConflictDetectionResult,
    detect_conflicts,
)
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    ProposalPreProcessorBundle,
)
from alphamind.decision.proposal_pre_processor.observations import (
    compute_book_health_summary,
    compute_combined_set_impact,
    compute_conviction_distribution,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.risk_guardrails.guardrail_evaluation import (
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)

__all__ = ["run_proposal_pre_processor"]


_EMPTY_CONFLICTS = ConflictDetectionResult(
    analyst_conflicts_by_rec_id={},
    strategist_position_conflicts_by_assessment_id={},
    strategist_pending_conflicts_by_assessment_id={},
)


def run_proposal_pre_processor(
    *,
    analyst_output: AnalystOutput,
    strategist_output: StrategistOutput,
    snapshot: PortfolioStateSnapshot,
    library_config: LibraryConfig,
    market: MarketInputs,
    snapshot_timestamp: datetime,
    timestamp: datetime,
) -> ProposalPreProcessorBundle:
    """Compute the proposal pre-processor bundle.

    Inputs are the persisted analyst and strategist outputs (after their own
    Layer-2/3 validation), the Phase-1 portfolio snapshot in library shape,
    the library config + market inputs that the agent-side validation tool
    used, the snapshot's effective timestamp (for §1.A basis), and the
    bundle's finalization timestamp (for the bundle envelope's ``timestamp``
    field).

    Pure: same inputs produce identical bundle output. No clock reads, no
    UUID generation, no I/O.
    """
    verify_halt_state_consistency(analyst_output, strategist_output)
    verify_invocation_id_consistency(analyst_output, strategist_output)

    # Watchlist mode produces no recommendations; the empty tuple flows through
    # so basis IDs and the conviction histogram come out correctly empty.
    # ``or ()`` is a mypy hint — ``mode=normal`` guarantees recommendations is set.
    recommendations: tuple[Recommendation, ...] = (
        (analyst_output.recommendations or ()) if analyst_output.mode == "normal" else ()
    )
    non_hold_position_assessments = tuple(
        a for a in strategist_output.position_assessments if a.recommended_action != "hold"
    )

    aggregate_observations = AggregateObservations(
        combined_set_impact=compute_combined_set_impact(
            recommendations=recommendations,
            non_hold_position_assessments=non_hold_position_assessments,
            snapshot=snapshot,
            library_config=library_config,
            market=market,
            snapshot_timestamp=snapshot_timestamp,
            strategist_holds_excluded_count=(
                len(strategist_output.position_assessments) - len(non_hold_position_assessments)
            ),
        ),
        conviction_distribution=compute_conviction_distribution(recommendations),
        book_health_summary=compute_book_health_summary(strategist_output.position_assessments),
    )

    # Halt mode: per design doc, "conflict detection does not apply" in
    # watchlist mode — wrappers carry empty conflicts uniformly.
    conflicts = (
        _EMPTY_CONFLICTS
        if analyst_output.mode == "watchlist"
        else detect_conflicts(
            recommendations=recommendations,
            position_assessments=strategist_output.position_assessments,
            pending_order_assessments=strategist_output.pending_order_assessments,
            held_direction_resolver=build_held_direction_resolver(snapshot),
        )
    )

    return ProposalPreProcessorBundle(
        invocation_id=analyst_output.invocation_id,
        timestamp=timestamp,
        aggregate_observations=aggregate_observations,
        strategist_section=build_strategist_section(strategist_output, conflicts),
        analyst_section=build_analyst_section(analyst_output, conflicts),
    )
