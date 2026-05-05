"""Aggregate observations for the proposal pre-processor — ALP-315 / ALP-317.

Pure functions that compute the conviction distribution, book health summary,
and combined-set guardrail impact from analyst recommendations and strategist
position assessments. The combined-set impact is the only place the
pre-processor invokes the guardrail-evaluation library; the per-rule shape is
the library's canonical output, augmented with signed per-proposal
contributions for FAIL rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from alphamind.decision.analyst.models import Recommendation
from alphamind.decision.proposal_pre_processor.models import (
    BasisSection,
    BookHealthSummary,
    BreachEntry,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ContributorEntry,
    ConvictionDistribution,
    ConvictionHistogram,
    PerRuleEntry,
)
from alphamind.decision.proposal_pre_processor.translator import (
    translate_position_assessment_to_proposed_delta,
    translate_recommendation_to_proposed_delta,
)
from alphamind.decision.strategist.models import PositionAssessment
from alphamind.risk_guardrails.guardrail_evaluation import (
    DeltaAdjustedExposure,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
    ProposedDelta,
    RuleProjection,
    RuleSpec,
    Status,
    build_active_specs,
    evaluate_proposals,
)

__all__ = [
    "LibraryFeatureDisabledError",
    "compute_book_health_summary",
    "compute_combined_set_impact",
    "compute_conviction_distribution",
]


class LibraryFeatureDisabledError(Exception):
    """Raised when the library returns feature-disabled rejections.

    The pre-processor's contract assumes the upstream agent's validation tool
    has already rejected feature-disabled proposals; their presence here is a
    fixture/contract bug worth surfacing rather than papering over.
    """


def compute_conviction_distribution(
    recommendations: Sequence[Recommendation],
) -> ConvictionDistribution:
    """Histogram analyst recommendations by conviction level (1 through 5).

    Watchlist mode: caller passes an empty sequence; result is all zeros with total=0.
    """
    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for rec in recommendations:
        level = rec.conviction_level
        if level not in counts:
            raise ValueError(f"conviction_level {level!r} is outside the valid range 1..5")
        counts[level] += 1

    return ConvictionDistribution(
        by_level=ConvictionHistogram(
            **{
                "1": counts[1],
                "2": counts[2],
                "3": counts[3],
                "4": counts[4],
                "5": counts[5],
            }
        ),
        total=len(recommendations),
    )


def compute_book_health_summary(
    position_assessments: Sequence[PositionAssessment],
) -> BookHealthSummary:
    """Histogram strategist position assessments by thesis_status and recommended_action.

    Empty book: caller passes an empty sequence; result is all zeros,
    remedy_flagged_count=0, total=0.
    """
    thesis_counts: dict[str, int] = {
        "on-track": 0,
        "partially-realized": 0,
        "at-risk": 0,
        "stale": 0,
        "invalidated": 0,
    }
    action_counts: dict[str, int] = {
        "hold": 0,
        "reduce": 0,
        "close": 0,
        "adjust-bracket": 0,
        "add": 0,
    }
    remedy_flagged_count = 0

    for assessment in position_assessments:
        thesis_counts[assessment.thesis_status] += 1
        action_counts[assessment.recommended_action] += 1
        if bool(assessment.remedy_flag):
            remedy_flagged_count += 1

    return BookHealthSummary(
        by_thesis_status=ByThesisStatus(
            **{
                "on-track": thesis_counts["on-track"],
                "partially-realized": thesis_counts["partially-realized"],
                "at-risk": thesis_counts["at-risk"],
                "stale": thesis_counts["stale"],
                "invalidated": thesis_counts["invalidated"],
            }
        ),
        by_recommended_action=ByRecommendedAction(
            **{
                "hold": action_counts["hold"],
                "reduce": action_counts["reduce"],
                "close": action_counts["close"],
                "adjust-bracket": action_counts["adjust-bracket"],
                "add": action_counts["add"],
            }
        ),
        remedy_flagged_count=remedy_flagged_count,
        total=len(position_assessments),
    )


def compute_combined_set_impact(
    *,
    recommendations: Sequence[Recommendation],
    non_hold_position_assessments: Sequence[PositionAssessment],
    snapshot: PortfolioStateSnapshot,
    library_config: LibraryConfig,
    market: MarketInputs,
    snapshot_timestamp: datetime,
    strategist_holds_excluded_count: int,
) -> CombinedSetImpact:
    """Compute §1.A combined_set_impact for the pre-processor bundle.

    Translates each analyst recommendation and each non-hold strategist
    position assessment to a ``ProposedDelta``, runs ``evaluate_proposals``
    once, maps each ``RuleProjection`` to ``PerRuleEntry``, and for each
    FAIL rule attributes signed per-proposal contributions by re-walking
    the matching ``RuleSpec.contribute`` closure.

    The caller is responsible for filtering hold-action assessments before
    calling — ``strategist_holds_excluded_count`` is reported as basis
    metadata only.
    """
    # Analyst recs first, then strategist non-hold actions — preserves caller
    # order so basis IDs and contributor IDs line up with the input sequences.
    proposals = (
        *(
            translate_recommendation_to_proposed_delta(r, snapshot=snapshot)
            for r in recommendations
        ),
        *(
            translate_position_assessment_to_proposed_delta(a, snapshot=snapshot)
            for a in non_hold_position_assessments
        ),
    )
    library_output = evaluate_proposals(
        state=snapshot,
        proposals=proposals,
        config=library_config,
        market=market,
    )
    if library_output.feature_disabled:
        ids = ", ".join(rej.proposal_id for rej in library_output.feature_disabled)
        raise LibraryFeatureDisabledError(
            f"upstream did not filter feature-disabled proposals: {ids}"
        )

    # Every proposal is in delta_adjusted at this point — the feature-disabled
    # check above raised if the gate filtered any out.
    proposals_with_dae = tuple((p, library_output.delta_adjusted[p.id]) for p in proposals)
    spec_lookup = _build_spec_lookup(library_config)

    per_rule_entries = tuple(_to_per_rule_entry(p) for p in library_output.per_rule)
    breach_entries = tuple(
        _build_breach_entry(projection, spec_lookup, proposals_with_dae, snapshot, library_config)
        for projection in library_output.per_rule
        if projection.status is Status.FAIL
    )

    basis = BasisSection(
        analyst_proposal_ids=tuple(r.recommendation_id for r in recommendations),
        strategist_action_ids=tuple(a.assessment_id for a in non_hold_position_assessments),
        strategist_holds_excluded_count=strategist_holds_excluded_count,
        snapshot_timestamp=snapshot_timestamp,
    )
    return CombinedSetImpact(basis=basis, per_rule=per_rule_entries, breaches=breach_entries)


# ---------------------------------------------------------------------------
# Combined-set helpers (private)
# ---------------------------------------------------------------------------


def _to_per_rule_entry(projection: RuleProjection) -> PerRuleEntry:
    """Map a library ``RuleProjection`` to the schema's ``PerRuleEntry``.

    Drops the projection's ``inverse`` flag — the schema absorbs floor-vs-cap
    semantics into the sign of ``headroom_remaining``.
    """
    return PerRuleEntry(
        rule=projection.rule,
        status=projection.status.value,
        current=projection.current,
        limit=projection.limit,
        projected_after=projection.projected_after,
        headroom_remaining=projection.headroom_remaining,
        unit=projection.unit,
    )


def _build_spec_lookup(config: LibraryConfig) -> dict[str, RuleSpec]:
    """Index active rule specs by ``rule_id`` for breach attribution."""
    return {spec.rule_id: spec for spec in build_active_specs(config)}


def _build_breach_entry(
    projection: RuleProjection,
    spec_lookup: dict[str, RuleSpec],
    proposals_with_dae: Sequence[tuple[ProposedDelta, DeltaAdjustedExposure]],
    snapshot: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> BreachEntry:
    """Compute signed contributors for one FAIL projection.

    Looks up the matching ``RuleSpec`` and re-walks ``spec.contribute`` for
    every proposal/dae pair. Zero contributions are dropped. ``overage`` is
    positive for both standard caps and inverse floors.
    """
    spec = spec_lookup[projection.rule]
    contributors = tuple(
        ContributorEntry(proposal_id=proposal.id, contribution=contribution)
        for proposal, dae in proposals_with_dae
        for contribution in (spec.contribute(proposal, dae, snapshot, config),)
        if contribution != 0.0
    )

    overage = (
        projection.limit - projection.projected_after
        if spec.inverse
        else projection.projected_after - projection.limit
    )
    return BreachEntry(
        rule=projection.rule,
        overage=overage,
        unit=projection.unit,
        contributors=contributors,
    )
