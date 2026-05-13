"""Tests for proposal_pre_processor/observations.py — ALP-315.

Acceptance criteria from ALP-315 story, one test per criterion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind.decision.analyst.models import (
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    Target,
)
from alphamind.decision.proposal_pre_processor.models import (
    BookHealthSummary,
    ConvictionDistribution,
)
from alphamind.decision.proposal_pre_processor.observations import (
    compute_book_health_summary,
    compute_conviction_distribution,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    BracketAdjustNewStopLevel,
    CloseParameters,
    ExposureImpact,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.decision.strategist.models import (
    EntryOrder as StrategistEntryOrder,
)
from alphamind.decision.strategist.models import (
    GuardrailValidationResult as StrategistGuardrailValidationResult,
)
from alphamind.risk_guardrails.guardrail_evaluation import RuleProjection, Status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_guardrail_result(**overrides: Any) -> GuardrailValidationResult:
    defaults: dict[str, Any] = {
        "overall": "PASS",
        "per_rule": (
            RuleProjection(
                rule="per_position_max_size",
                status=Status.PASS,
                current=0.0,
                limit=5.0,
                projected_after=3.0,
                headroom_remaining=2.0,
                unit="% of portfolio",
            ),
        ),
        "delta_adjusted_exposure": 3000.0,
        "greeks": None,
        "cumulative_impact_note": "Proposal #1 of 1.",
        "checked_at": _NOW,
    }
    return GuardrailValidationResult(**(defaults | overrides))


def _make_recommendation(**overrides: Any) -> Recommendation:
    defaults: dict[str, Any] = {
        "recommendation_id": "REC-1",
        "instrument": InstrumentEquity(
            asset_type="equity", ticker=Symbol("NVDA"), direction="long"
        ),
        "underlying": "NVDA",
        "sector": "semis",
        "conviction_level": 4,
        "entry_order": EntryOrder(type="limit", limit_price=842.50),
        "position_size": PositionSize(quantity=4, dollar_value=3370.0, pct_of_portfolio=3.37),
        "target": Target(target_type="absolute_price", price=890.0, dollar_pl_target=190.0),
        "invalidation_legs": (
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=820.0
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        "time_expectation_hours": 18.0,
        "guardrail_validation_result": _make_guardrail_result(),
        "thesis_narrative": "Test thesis.",
        "target_rationale": "Test target rationale.",
        "invalidation_rationale": (
            InvalidationRationale(leg_id="INV-1", rationale="Test inv rationale."),
        ),
        "position_size_rationale": "Test sizing rationale.",
        "counterarguments_acknowledged": "Test counters.",
    }
    return Recommendation(**(defaults | overrides))


def _make_position_assessment(**overrides: Any) -> PositionAssessment:
    """Build a PositionAssessment with safe defaults (recommended_action=hold)."""
    defaults: dict[str, Any] = {
        "assessment_id": "SA-1",
        "position_id": "POS-1",
        "thesis_id": "THESIS-1",
        "underlying": "NVDA",
        "sector": "tech",
        "thesis_status": "on-track",
        "recommended_action": "hold",
        "status_rationale": "thesis intact",
        "action_rationale": "hold for now",
    }
    return PositionAssessment(**(defaults | overrides))


def _make_close_assessment(**overrides: Any) -> PositionAssessment:
    """Build a PositionAssessment with recommended_action=close and required fields."""
    defaults: dict[str, Any] = {
        "assessment_id": "SA-1",
        "position_id": "POS-1",
        "thesis_id": "THESIS-1",
        "underlying": "NVDA",
        "sector": "tech",
        "thesis_status": "on-track",
        "recommended_action": "close",
        "action_parameters": CloseParameters(
            action="close",
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
        ),
        "exposure_impact": ExposureImpact(
            sector_delta_adjusted_change=-3000.0,
            net_directional_impact=-3000.0,
        ),
        "status_rationale": "thesis intact",
        "action_rationale": "closing position",
    }
    return PositionAssessment(**(defaults | overrides))


def _make_reduce_assessment(**overrides: Any) -> PositionAssessment:
    """Build a PositionAssessment with recommended_action=reduce and required fields."""
    defaults: dict[str, Any] = {
        "assessment_id": "SA-1",
        "position_id": "POS-1",
        "thesis_id": "THESIS-1",
        "underlying": "NVDA",
        "sector": "tech",
        "thesis_status": "on-track",
        "recommended_action": "reduce",
        "action_parameters": ReduceParameters(
            action="reduce",
            quantity=2.0,
            order_type="market",
        ),
        "exposure_impact": ExposureImpact(
            sector_delta_adjusted_change=-1500.0,
            net_directional_impact=-1500.0,
        ),
        "reduce_rationale": "trimming exposure",
        "status_rationale": "thesis intact",
        "action_rationale": "reducing position",
    }
    return PositionAssessment(**(defaults | overrides))


# ---------------------------------------------------------------------------
# compute_conviction_distribution tests
# ---------------------------------------------------------------------------


def test_compute_conviction_distribution_empty_sequence() -> None:
    """Empty sequence returns all-zero histogram with total=0."""
    result = compute_conviction_distribution(())
    assert result.total == 0
    assert result.by_level.level_1 == 0
    assert result.by_level.level_2 == 0
    assert result.by_level.level_3 == 0
    assert result.by_level.level_4 == 0
    assert result.by_level.level_5 == 0


def test_compute_conviction_distribution_mixed_levels() -> None:
    """Recommendations with levels 2, 3, 3 → by_level={1:0,2:1,3:2,4:0,5:0}, total=3."""
    recs = (
        _make_recommendation(recommendation_id="REC-1", conviction_level=2),
        _make_recommendation(recommendation_id="REC-2", conviction_level=3),
        _make_recommendation(recommendation_id="REC-3", conviction_level=3),
    )
    result = compute_conviction_distribution(recs)
    assert result.total == 3
    assert result.by_level.level_1 == 0
    assert result.by_level.level_2 == 1
    assert result.by_level.level_3 == 2
    assert result.by_level.level_4 == 0
    assert result.by_level.level_5 == 0


def test_compute_conviction_distribution_all_levels() -> None:
    """One recommendation per level → each slot is 1, total=5."""
    recs = tuple(
        _make_recommendation(recommendation_id=f"REC-{i}", conviction_level=i) for i in range(1, 6)
    )
    result = compute_conviction_distribution(recs)
    assert result.total == 5
    assert result.by_level.level_1 == 1
    assert result.by_level.level_2 == 1
    assert result.by_level.level_3 == 1
    assert result.by_level.level_4 == 1
    assert result.by_level.level_5 == 1


def test_compute_conviction_distribution_rejects_invalid_level() -> None:
    """A recommendation with conviction_level outside 1..5 raises ValueError.

    Pydantic normally prevents this at construction time, but the function
    includes a defensive check.
    """
    # Construct a recommendation with a patched conviction_level via model_construct
    # (bypasses Pydantic validation to simulate invalid data reaching the function).
    bad_rec = Recommendation.model_construct(conviction_level=6)
    with pytest.raises(ValueError, match="conviction_level"):
        compute_conviction_distribution((bad_rec,))


def test_compute_conviction_distribution_round_trip() -> None:
    """ConvictionDistribution survives model_dump(by_alias=True) → model_validate."""
    recs = (
        _make_recommendation(recommendation_id="REC-1", conviction_level=1),
        _make_recommendation(recommendation_id="REC-2", conviction_level=5),
    )
    result = compute_conviction_distribution(recs)
    dumped = result.model_dump(by_alias=True)
    restored = ConvictionDistribution.model_validate(dumped)
    assert restored == result


def test_compute_conviction_distribution_totals_sum_to_n() -> None:
    """For any input of length N, histogram slot sum == N == total."""
    recs = tuple(
        _make_recommendation(recommendation_id=f"REC-{i}", conviction_level=(i % 5) + 1)
        for i in range(7)
    )
    result = compute_conviction_distribution(recs)
    slot_sum = (
        result.by_level.level_1
        + result.by_level.level_2
        + result.by_level.level_3
        + result.by_level.level_4
        + result.by_level.level_5
    )
    assert slot_sum == result.total == len(recs)


def test_compute_conviction_distribution_is_pure() -> None:
    """Same input sequence → same output; inputs are not mutated."""
    recs = (
        _make_recommendation(recommendation_id="REC-1", conviction_level=3),
        _make_recommendation(recommendation_id="REC-2", conviction_level=3),
    )
    result_a = compute_conviction_distribution(recs)
    result_b = compute_conviction_distribution(recs)
    assert result_a == result_b


# ---------------------------------------------------------------------------
# compute_book_health_summary tests
# ---------------------------------------------------------------------------


def test_compute_book_health_summary_empty_sequence() -> None:
    """Empty sequence returns all-zero histogram with remedy_flagged_count=0, total=0."""
    result = compute_book_health_summary(())
    assert result.total == 0
    assert result.remedy_flagged_count == 0
    # by_thesis_status all zero
    bts = result.by_thesis_status
    assert bts.on_track == 0
    assert bts.partially_realized == 0
    assert bts.at_risk == 0
    assert bts.stale == 0
    assert bts.invalidated == 0
    # by_recommended_action all zero
    bra = result.by_recommended_action
    assert bra.hold == 0
    assert bra.reduce == 0
    assert bra.close == 0
    assert bra.adjust_bracket == 0
    assert bra.add == 0


def test_compute_book_health_summary_mixed_assessments() -> None:
    """Three assessments with specific thesis_status + recommended_action → correct counts."""
    assessments = (
        _make_position_assessment(
            assessment_id="SA-1",
            thesis_status="on-track",
            recommended_action="hold",
        ),
        _make_reduce_assessment(
            assessment_id="SA-2",
            thesis_status="on-track",
        ),
        _make_close_assessment(
            assessment_id="SA-3",
            thesis_status="at-risk",
        ),
    )
    result = compute_book_health_summary(assessments)
    assert result.total == 3
    bts = result.by_thesis_status
    assert bts.on_track == 2
    assert bts.partially_realized == 0
    assert bts.at_risk == 1
    assert bts.stale == 0
    assert bts.invalidated == 0
    bra = result.by_recommended_action
    assert bra.hold == 1
    assert bra.reduce == 1
    assert bra.close == 1
    assert bra.adjust_bracket == 0
    assert bra.add == 0


def test_compute_book_health_summary_remedy_flag_none_not_counted() -> None:
    """remedy_flag=None is NOT counted; remedy_flag=non-empty-string IS counted."""
    assessments = (
        _make_position_assessment(assessment_id="SA-1"),  # remedy_flag=None by default
        _make_position_assessment(
            assessment_id="SA-2",
            remedy_flag="adjust stop",
            remedy_rationale="price shifted",
        ),
        _make_position_assessment(
            assessment_id="SA-3",
            remedy_flag="review bracket",
            remedy_rationale="volatility",
        ),
    )
    result = compute_book_health_summary(assessments)
    assert result.remedy_flagged_count == 2


def test_compute_book_health_summary_remedy_flag_empty_string_not_counted() -> None:
    """remedy_flag='' (empty string) does NOT count; only non-empty strings count.

    Note: PositionAssessment validates remedy_flag requires remedy_rationale when
    remedy_flag is not None. An empty string is technically not None, so we must
    also provide remedy_rationale. We use model_construct to bypass Pydantic
    validation since the schema doesn't explicitly prohibit empty strings —
    the acceptance criterion requires the function to handle this gracefully.
    """
    empty_flag_assessment = PositionAssessment.model_construct(
        assessment_id="SA-1",
        position_id="POS-1",
        thesis_id="THESIS-1",
        underlying="NVDA",
        sector="tech",
        thesis_status="on-track",
        recommended_action="hold",
        status_rationale="thesis intact",
        action_rationale="hold for now",
        remedy_flag="",
        remedy_rationale=None,
    )
    populated_flag_assessment = _make_position_assessment(
        assessment_id="SA-2",
        remedy_flag="populated",
        remedy_rationale="reason",
    )
    result = compute_book_health_summary((empty_flag_assessment, populated_flag_assessment))
    assert result.remedy_flagged_count == 1


def test_compute_book_health_summary_all_thesis_statuses() -> None:
    """Every thesis_status value appears exactly once → each slot == 1."""
    statuses = ["on-track", "partially-realized", "at-risk", "stale", "invalidated"]
    assessments = tuple(
        _make_close_assessment(
            assessment_id=f"SA-{i + 1}",
            thesis_status=s,
        )
        for i, s in enumerate(statuses)
    )
    result = compute_book_health_summary(assessments)
    bts = result.by_thesis_status
    assert bts.on_track == 1
    assert bts.partially_realized == 1
    assert bts.at_risk == 1
    assert bts.stale == 1
    assert bts.invalidated == 1
    assert result.total == 5


def test_compute_book_health_summary_all_recommended_actions() -> None:
    """Every recommended_action value appears exactly once → each slot == 1."""
    gvr = StrategistGuardrailValidationResult(
        overall="PASS",
        per_rule=(
            RuleProjection(
                rule="per_position_max_size",
                status=Status.PASS,
                current=0.0,
                limit=5.0,
                projected_after=3.0,
                headroom_remaining=2.0,
                unit="% of portfolio",
            ),
        ),
        checked_at=_NOW,
    )
    exposure = ExposureImpact(sector_delta_adjusted_change=1000.0, net_directional_impact=1000.0)

    assessments = (
        _make_position_assessment(assessment_id="SA-1", recommended_action="hold"),
        _make_reduce_assessment(assessment_id="SA-2"),
        _make_close_assessment(assessment_id="SA-3"),
        _make_position_assessment(
            assessment_id="SA-4",
            recommended_action="adjust-bracket",
            action_parameters=AdjustBracketParameters(
                action="adjust-bracket",
                new_stop_level=BracketAdjustNewStopLevel(
                    trigger_price=800.0,
                    order_type="market",
                ),
            ),
            adjustment_rationale="tightening stop",
        ),
        _make_position_assessment(
            assessment_id="SA-5",
            recommended_action="add",
            action_parameters=AddParameters(
                action="add",
                additional_quantity=2.0,
                additional_dollar_value=500.0,
                entry_order=StrategistEntryOrder(type="market"),
            ),
            exposure_impact=exposure,
            guardrail_validation_result=gvr,
            add_conviction_justification="thesis strengthened",
        ),
    )
    result = compute_book_health_summary(assessments)
    bra = result.by_recommended_action
    assert bra.hold == 1
    assert bra.reduce == 1
    assert bra.close == 1
    assert bra.adjust_bracket == 1
    assert bra.add == 1
    assert result.total == 5


def test_compute_book_health_summary_round_trip() -> None:
    """BookHealthSummary survives model_dump(by_alias=True) → model_validate."""
    assessments = (
        _make_close_assessment(
            assessment_id="SA-1",
            thesis_status="at-risk",
            remedy_flag="check stop",
            remedy_rationale="volatility spike",
        ),
    )
    result = compute_book_health_summary(assessments)
    dumped = result.model_dump(by_alias=True)
    restored = BookHealthSummary.model_validate(dumped)
    assert restored == result


def test_compute_book_health_summary_totals_sum_to_n() -> None:
    """For any input of length N, thesis histogram slot sum == action slot sum == N == total."""
    assessments = (
        _make_position_assessment(assessment_id="SA-1", thesis_status="on-track"),
        _make_close_assessment(assessment_id="SA-2", thesis_status="at-risk"),
        _make_reduce_assessment(assessment_id="SA-3", thesis_status="stale"),
    )
    result = compute_book_health_summary(assessments)
    bts = result.by_thesis_status
    thesis_sum = bts.on_track + bts.partially_realized + bts.at_risk + bts.stale + bts.invalidated
    bra = result.by_recommended_action
    action_sum = bra.hold + bra.reduce + bra.close + bra.adjust_bracket + bra.add
    assert thesis_sum == action_sum == result.total == len(assessments)


def test_compute_book_health_summary_is_pure() -> None:
    """Same input → same output; inputs not mutated."""
    assessments = (
        _make_position_assessment(assessment_id="SA-1"),
        _make_position_assessment(assessment_id="SA-2"),
    )
    result_a = compute_book_health_summary(assessments)
    result_b = compute_book_health_summary(assessments)
    assert result_a == result_b
