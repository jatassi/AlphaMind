"""Tests for detect_conflicts — ALP-316.

Each acceptance criterion maps to at least one test. The mirror-symmetry
property test is the central correctness check.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from alphamind._kernel.ids import (
    OrderId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.decision.analyst.models import (
    EntryOrder,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    Target,
)
from alphamind.decision.proposal_pre_processor import (
    ConflictDetectionResult,
    ConflictType,
    detect_conflicts,
)
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    BracketAdjustNewStopLevel,
    CloseParameters,
    ExposureImpact,
    ModificationParameters,
    PendingOrderAssessment,
    PositionAssessment,
    ReduceParameters,
)
from alphamind.decision.strategist.models import (
    EntryOrder as StratEntryOrder,
)
from alphamind.decision.strategist.models import (
    GuardrailValidationResult as StratGuardrailValidationResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _equity_recommendation(
    rec_id: str,
    underlying: str,
    direction: Literal["long", "short"] = "long",
) -> Recommendation:
    """Build a minimal valid analyst Recommendation for an equity entry."""
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentEquity(
            asset_type="equity", ticker=Symbol(underlying), direction=direction
        ),
        underlying=Symbol(underlying),
        sector="tech",
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=10.0, dollar_value=money(1000.0), pct_of_portfolio=1.0),
        target=Target(
            target_type="absolute_price", price=price(200.0), dollar_pl_target=money(500.0)
        ),
        invalidation_legs=(
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol(underlying),
                    comparator="<=",
                    trigger_price=price(90.0),
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        time_expectation_hours=24.0,
        guardrail_validation_result=GuardrailValidationResult(
            overall="PASS", per_rule=(), checked_at=_NOW
        ),
        thesis_narrative="thesis",
        target_rationale="target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="reason"),),
        position_size_rationale="size",
        counterarguments_acknowledged="ack",
    )


def _option_recommendation(
    rec_id: str,
    underlying: str,
    direction: Literal["long", "short"] = "long",
) -> Recommendation:
    """Build a minimal valid analyst Recommendation for an option entry."""
    return Recommendation(
        recommendation_id=RecommendationId(rec_id),
        instrument=InstrumentOption(
            asset_type="option",
            underlying=Symbol(underlying),
            strike=price(100.0),
            expiration=date(2026, 6, 19),
            contract_type="call",
            direction=direction,
        ),
        underlying=Symbol(underlying),
        sector="tech",
        conviction_level=3,
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=10.0, dollar_value=money(1000.0), pct_of_portfolio=1.0),
        target=Target(
            target_type="absolute_price", price=price(200.0), dollar_pl_target=money(500.0)
        ),
        invalidation_legs=(
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol(underlying),
                    comparator="<=",
                    trigger_price=price(90.0),
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        time_expectation_hours=24.0,
        guardrail_validation_result=GuardrailValidationResult(
            overall="PASS", per_rule=(), checked_at=_NOW
        ),
        thesis_narrative="thesis",
        target_rationale="target",
        invalidation_rationale=(InvalidationRationale(leg_id="INV-1", rationale="reason"),),
        position_size_rationale="size",
        counterarguments_acknowledged="ack",
    )


def _hold_assessment(assessment_id: str, underlying: str) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(assessment_id),
        position_id=PositionId(f"POS-{assessment_id[3:]}"),
        thesis_id=ThesisId(f"THESIS-{assessment_id[3:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="on-track",
        recommended_action="hold",
        status_rationale="thesis intact",
        action_rationale="hold for now",
    )


def _add_assessment(assessment_id: str, underlying: str) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(assessment_id),
        position_id=PositionId(f"POS-{assessment_id[3:]}"),
        thesis_id=ThesisId(f"THESIS-{assessment_id[3:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="on-track",
        recommended_action="add",
        action_parameters=AddParameters(
            action="add",
            additional_quantity=5.0,
            additional_dollar_value=money(500.0),
            entry_order=StratEntryOrder(type="market"),
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=money(0.5), net_directional_impact=money(0.5)
        ),
        guardrail_validation_result=StratGuardrailValidationResult(
            overall="PASS", per_rule=(), checked_at=_NOW
        ),
        add_conviction_justification="conviction increased",
        status_rationale="thesis confirmed",
        action_rationale="add",
    )


def _reduce_assessment(assessment_id: str, underlying: str) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(assessment_id),
        position_id=PositionId(f"POS-{assessment_id[3:]}"),
        thesis_id=ThesisId(f"THESIS-{assessment_id[3:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="partially-realized",
        recommended_action="reduce",
        action_parameters=ReduceParameters(action="reduce", quantity=5.0, order_type="market"),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=signed_money(-0.5),
            net_directional_impact=signed_money(-0.5),
        ),
        status_rationale="thesis half done",
        action_rationale="trim",
        reduce_rationale="locking in half",
    )


def _adjust_bracket_assessment(assessment_id: str, underlying: str) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(assessment_id),
        position_id=PositionId(f"POS-{assessment_id[3:]}"),
        thesis_id=ThesisId(f"THESIS-{assessment_id[3:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="on-track",
        recommended_action="adjust-bracket",
        action_parameters=AdjustBracketParameters(
            action="adjust-bracket",
            new_stop_level=BracketAdjustNewStopLevel(
                trigger_price=price(95.0), order_type="market"
            ),
        ),
        status_rationale="thesis intact",
        action_rationale="trail stop",
        adjustment_rationale="lock more in",
    )


def _close_assessment(assessment_id: str, underlying: str) -> PositionAssessment:
    return PositionAssessment(
        assessment_id=RecommendationId(assessment_id),
        position_id=PositionId(f"POS-{assessment_id[3:]}"),
        thesis_id=ThesisId(f"THESIS-{assessment_id[3:]}"),
        underlying=Symbol(underlying),
        sector="tech",
        thesis_status="invalidated",
        recommended_action="close",
        action_parameters=CloseParameters(
            action="close",
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
        ),
        exposure_impact=ExposureImpact(
            sector_delta_adjusted_change=signed_money(-1.0),
            net_directional_impact=signed_money(-1.0),
        ),
        status_rationale="invalidated",
        action_rationale="closing",
    )


def _entry_pending_order(
    pending_id: str,
    linked_assessment_id: str | None = None,
    recommended_action: Literal["maintain", "modify", "cancel"] = "maintain",
    order_type: Literal["entry_limit", "entry_stop_limit"] = "entry_limit",
) -> PendingOrderAssessment:
    """Build a minimal valid PendingOrderAssessment for an entry order.

    The order's underlying is resolved via ``linked_position_assessment_id``
    pointing at one of the supplied position_assessments.
    """
    modification_parameters = (
        ModificationParameters(new_limit_price=price(99.0))
        if recommended_action == "modify"
        else None
    )
    return PendingOrderAssessment(
        pending_order_assessment_id=RecommendationId(pending_id),
        order_id=OrderId(f"ORD-{pending_id[7:]}"),
        position_id=PositionId(f"POS-{pending_id[7:]}"),
        order_type=order_type,
        order_age_hours=2.0,
        fill_probability_assessment="plausible",
        recommended_action=recommended_action,
        modification_parameters=modification_parameters,
        linked_position_assessment_id=RecommendationId(linked_assessment_id)
        if linked_assessment_id is not None
        else None,
        drift_rationale="drift",
        action_rationale="action",
    )


def _bracket_leg_pending_order(
    pending_id: str, linked_assessment_id: str | None = None
) -> PendingOrderAssessment:
    """Build a bracket-leg pending order, optionally linked to a position assessment."""
    return PendingOrderAssessment(
        pending_order_assessment_id=RecommendationId(pending_id),
        order_id=OrderId(f"ORD-{pending_id[7:]}"),
        position_id=PositionId(f"POS-{pending_id[7:]}"),
        order_type="bracket_price_stop",
        order_age_hours=2.0,
        fill_probability_assessment="unlikely",
        recommended_action="maintain",
        linked_position_assessment_id=RecommendationId(linked_assessment_id)
        if linked_assessment_id is not None
        else None,
        drift_rationale="drift",
        action_rationale="action",
    )


def _no_holds_resolver(_position_id: str) -> Literal["long", "short"]:
    """Default direction resolver that should not be called when no positions are held."""
    raise AssertionError(f"resolver called for {_position_id} unexpectedly")


# ---------------------------------------------------------------------------
# AC: empty inputs produce empty maps
# ---------------------------------------------------------------------------


def test_empty_inputs_produce_empty_maps() -> None:
    result = detect_conflicts(
        recommendations=(),
        position_assessments=(),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    assert isinstance(result, ConflictDetectionResult)
    assert result.analyst_conflicts_by_rec_id == {}
    assert result.strategist_position_conflicts_by_assessment_id == {}
    assert result.strategist_pending_conflicts_by_assessment_id == {}


# ---------------------------------------------------------------------------
# AC: keys exactly cover the input record ids
# ---------------------------------------------------------------------------


def test_keys_cover_inputs_with_no_underlying_overlap() -> None:
    rec = _equity_recommendation("REC-1", "AAPL")
    pos = _hold_assessment("SA-1", "MSFT")
    pending = _entry_pending_order("SA-ORD-1", recommended_action="maintain")
    bracket = _bracket_leg_pending_order("SA-ORD-2")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(pending, bracket),
        held_direction_resolver=lambda _pid: "long",
    )
    assert set(result.analyst_conflicts_by_rec_id.keys()) == {"REC-1"}
    assert set(result.strategist_position_conflicts_by_assessment_id.keys()) == {"SA-1"}
    assert set(result.strategist_pending_conflicts_by_assessment_id.keys()) == {
        "SA-ORD-1",
        "SA-ORD-2",
    }
    assert result.analyst_conflicts_by_rec_id["REC-1"] == ()
    assert result.strategist_position_conflicts_by_assessment_id["SA-1"] == ()
    assert result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"] == ()
    assert result.strategist_pending_conflicts_by_assessment_id["SA-ORD-2"] == ()


# ---------------------------------------------------------------------------
# AC: entry_vs_close — close action on same underlying
# ---------------------------------------------------------------------------


def test_entry_vs_close_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    pos = _close_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].with_assessment_id == "SA-1"
    assert analyst_side[0].with_pending_order_assessment_id is None
    assert analyst_side[0].underlying == "NVDA"
    assert analyst_side[0].conflict_type == ConflictType.entry_vs_close

    strategist_side = result.strategist_position_conflicts_by_assessment_id["SA-1"]
    assert len(strategist_side) == 1
    assert strategist_side[0].with_recommendation_id == "REC-1"
    assert strategist_side[0].underlying == "NVDA"
    assert strategist_side[0].conflict_type == ConflictType.entry_vs_close


# ---------------------------------------------------------------------------
# AC: entry_vs_add — add action on same underlying
# ---------------------------------------------------------------------------


def test_entry_vs_add_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    pos = _add_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    strategist_side = result.strategist_position_conflicts_by_assessment_id["SA-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].with_assessment_id == "SA-1"
    assert analyst_side[0].conflict_type == ConflictType.entry_vs_add
    assert len(strategist_side) == 1
    assert strategist_side[0].with_recommendation_id == "REC-1"
    assert strategist_side[0].conflict_type == ConflictType.entry_vs_add


# ---------------------------------------------------------------------------
# AC: entry_vs_hold — hold action on same underlying, same direction
# ---------------------------------------------------------------------------


def test_entry_vs_hold_same_direction_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA", direction="long")
    pos = _hold_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=lambda _pid: "long",
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    strategist_side = result.strategist_position_conflicts_by_assessment_id["SA-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].conflict_type == ConflictType.entry_vs_hold
    assert len(strategist_side) == 1
    assert strategist_side[0].conflict_type == ConflictType.entry_vs_hold


# ---------------------------------------------------------------------------
# AC: entry_direction_conflict — hold action on same underlying, opposite direction
# ---------------------------------------------------------------------------


def test_entry_direction_conflict_opposite_direction_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA", direction="short")
    pos = _hold_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=lambda _pid: "long",
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    strategist_side = result.strategist_position_conflicts_by_assessment_id["SA-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].conflict_type == ConflictType.entry_direction_conflict
    assert len(strategist_side) == 1
    assert strategist_side[0].conflict_type == ConflictType.entry_direction_conflict


# ---------------------------------------------------------------------------
# AC: reduce action treated as entry_vs_close (partial-close-as-reduction)
# ---------------------------------------------------------------------------


def test_entry_vs_reduce_classified_as_entry_vs_close() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    pos = _reduce_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    strategist_side = result.strategist_position_conflicts_by_assessment_id["SA-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].conflict_type == ConflictType.entry_vs_close
    assert len(strategist_side) == 1
    assert strategist_side[0].conflict_type == ConflictType.entry_vs_close


# ---------------------------------------------------------------------------
# AC: adjust-bracket produces NO conflict on either side
# ---------------------------------------------------------------------------


def test_entry_vs_adjust_bracket_produces_no_conflict() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    pos = _adjust_bracket_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    assert result.analyst_conflicts_by_rec_id["REC-1"] == ()
    assert result.strategist_position_conflicts_by_assessment_id["SA-1"] == ()


# ---------------------------------------------------------------------------
# AC: entry_vs_pending_modify — pending entry order with modify
# ---------------------------------------------------------------------------


def test_entry_vs_pending_modify_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    holding = _hold_assessment("SA-1", "NVDA")
    pending = _entry_pending_order(
        "SA-ORD-1", linked_assessment_id="SA-1", recommended_action="modify"
    )
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(holding,),
        pending_order_assessments=(pending,),
        held_direction_resolver=lambda _pid: "long",
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    pending_side = result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"]
    pending_modify = [
        c
        for c in analyst_side
        if c.with_pending_order_assessment_id == "SA-ORD-1"
        and c.conflict_type == ConflictType.entry_vs_pending_modify
    ]
    assert len(pending_modify) == 1
    assert pending_modify[0].underlying == "NVDA"
    assert len(pending_side) == 1
    assert pending_side[0].with_recommendation_id == "REC-1"
    assert pending_side[0].underlying == "NVDA"
    assert pending_side[0].conflict_type == ConflictType.entry_vs_pending_modify


# ---------------------------------------------------------------------------
# AC: entry_vs_pending_maintain — pending entry order with maintain
# ---------------------------------------------------------------------------


def test_entry_vs_pending_maintain_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    holding = _hold_assessment("SA-1", "NVDA")
    pending = _entry_pending_order(
        "SA-ORD-1", linked_assessment_id="SA-1", recommended_action="maintain"
    )
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(holding,),
        pending_order_assessments=(pending,),
        held_direction_resolver=lambda _pid: "long",
    )
    pending_side = result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"]
    assert any(
        c.conflict_type == ConflictType.entry_vs_pending_maintain
        and c.with_recommendation_id == "REC-1"
        for c in pending_side
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    assert any(
        c.conflict_type == ConflictType.entry_vs_pending_maintain
        and c.with_pending_order_assessment_id == "SA-ORD-1"
        for c in analyst_side
    )


# ---------------------------------------------------------------------------
# AC: entry_vs_pending_cancel — pending entry order with cancel
# ---------------------------------------------------------------------------


def test_entry_vs_pending_cancel_mirror_symmetric() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    holding = _hold_assessment("SA-1", "NVDA")
    pending = _entry_pending_order(
        "SA-ORD-1", linked_assessment_id="SA-1", recommended_action="cancel"
    )
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(holding,),
        pending_order_assessments=(pending,),
        held_direction_resolver=lambda _pid: "long",
    )
    pending_side = result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"]
    assert any(
        c.conflict_type == ConflictType.entry_vs_pending_cancel
        and c.with_recommendation_id == "REC-1"
        for c in pending_side
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    assert any(
        c.conflict_type == ConflictType.entry_vs_pending_cancel
        and c.with_pending_order_assessment_id == "SA-ORD-1"
        for c in analyst_side
    )


# ---------------------------------------------------------------------------
# AC: bracket-leg pending order yields empty conflicts even on same-underlying match
# ---------------------------------------------------------------------------


def test_bracket_leg_pending_order_empty_conflicts_on_match() -> None:
    rec = _equity_recommendation("REC-1", "NVDA")
    parent_holding = _hold_assessment("SA-1", "NVDA")
    bracket = _bracket_leg_pending_order("SA-ORD-1", linked_assessment_id="SA-1")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(parent_holding,),
        pending_order_assessments=(bracket,),
        held_direction_resolver=lambda _pid: "long",
    )
    assert result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"] == ()
    # Analyst still has its hold conflict against SA-1, but no pending entry against SA-ORD-1
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    assert all(c.with_pending_order_assessment_id != "SA-ORD-1" for c in analyst_side)


# ---------------------------------------------------------------------------
# AC: N-by-M product on shared underlying
# ---------------------------------------------------------------------------


def test_multiple_recs_each_get_own_conflict_entries() -> None:
    rec_a = _equity_recommendation("REC-1", "NVDA")
    rec_b = _equity_recommendation("REC-2", "NVDA")
    pos_x = _close_assessment("SA-1", "NVDA")
    pos_y = _add_assessment("SA-2", "NVDA")
    result = detect_conflicts(
        recommendations=(rec_a, rec_b),
        position_assessments=(pos_x, pos_y),
        pending_order_assessments=(),
        held_direction_resolver=_no_holds_resolver,
    )
    # Each rec sees both assessments → 2 entries
    assert len(result.analyst_conflicts_by_rec_id["REC-1"]) == 2
    assert len(result.analyst_conflicts_by_rec_id["REC-2"]) == 2
    # Each assessment sees both recs → 2 entries
    assert len(result.strategist_position_conflicts_by_assessment_id["SA-1"]) == 2
    assert len(result.strategist_position_conflicts_by_assessment_id["SA-2"]) == 2
    # Conflict types per assessment
    sa1_types = {
        c.conflict_type for c in result.strategist_position_conflicts_by_assessment_id["SA-1"]
    }
    sa2_types = {
        c.conflict_type for c in result.strategist_position_conflicts_by_assessment_id["SA-2"]
    }
    assert sa1_types == {ConflictType.entry_vs_close}
    assert sa2_types == {ConflictType.entry_vs_add}


# ---------------------------------------------------------------------------
# AC: mirror-symmetry property test (central correctness check)
# ---------------------------------------------------------------------------


def test_mirror_symmetry_property_walk() -> None:
    """Every analyst-side conflict has a strategist-side counterpart with the
    same underlying and conflict_type, and vice versa, across all conflict
    types simultaneously."""
    rec_long_nvda = _equity_recommendation("REC-1", "NVDA", direction="long")
    rec_short_nvda = _equity_recommendation("REC-2", "NVDA", direction="short")
    rec_aapl = _equity_recommendation("REC-3", "AAPL")
    rec_no_match = _equity_recommendation("REC-4", "TSLA")

    close_nvda = _close_assessment("SA-1", "NVDA")
    hold_nvda = _hold_assessment("SA-2", "NVDA")
    reduce_aapl = _reduce_assessment("SA-3", "AAPL")
    add_aapl = _add_assessment("SA-4", "AAPL")
    bracket_nvda = _adjust_bracket_assessment("SA-5", "NVDA")

    pending_aapl_modify = _entry_pending_order(
        "SA-ORD-1", linked_assessment_id="SA-3", recommended_action="modify"
    )
    pending_nvda_cancel = _entry_pending_order(
        "SA-ORD-2", linked_assessment_id="SA-1", recommended_action="cancel"
    )
    bracket_leg_aapl = _bracket_leg_pending_order("SA-ORD-3", linked_assessment_id="SA-3")

    result = detect_conflicts(
        recommendations=(rec_long_nvda, rec_short_nvda, rec_aapl, rec_no_match),
        position_assessments=(close_nvda, hold_nvda, reduce_aapl, add_aapl, bracket_nvda),
        pending_order_assessments=(
            pending_aapl_modify,
            pending_nvda_cancel,
            bracket_leg_aapl,
        ),
        held_direction_resolver=lambda _pid: "long",
    )

    # Walk analyst → strategist
    for rec_id, analyst_conflicts in result.analyst_conflicts_by_rec_id.items():
        for ac in analyst_conflicts:
            if ac.with_assessment_id is not None:
                mirrors = result.strategist_position_conflicts_by_assessment_id[
                    ac.with_assessment_id
                ]
                matching = [
                    c
                    for c in mirrors
                    if c.with_recommendation_id == rec_id
                    and c.underlying == ac.underlying
                    and c.conflict_type == ac.conflict_type
                ]
                assert len(matching) == 1
            else:
                assert ac.with_pending_order_assessment_id is not None
                mirrors = result.strategist_pending_conflicts_by_assessment_id[
                    ac.with_pending_order_assessment_id
                ]
                matching = [
                    c
                    for c in mirrors
                    if c.with_recommendation_id == rec_id
                    and c.underlying == ac.underlying
                    and c.conflict_type == ac.conflict_type
                ]
                assert len(matching) == 1

    # Walk strategist position → analyst
    for (
        assessment_id,
        position_conflicts,
    ) in result.strategist_position_conflicts_by_assessment_id.items():
        for sc in position_conflicts:
            analyst_mirrors = result.analyst_conflicts_by_rec_id[sc.with_recommendation_id]
            position_matching = [
                c
                for c in analyst_mirrors
                if c.with_assessment_id == assessment_id
                and c.underlying == sc.underlying
                and c.conflict_type == sc.conflict_type
            ]
            assert len(position_matching) == 1

    # Walk strategist pending → analyst
    for (
        pending_id,
        pending_conflicts,
    ) in result.strategist_pending_conflicts_by_assessment_id.items():
        for pc in pending_conflicts:
            analyst_mirrors = result.analyst_conflicts_by_rec_id[pc.with_recommendation_id]
            pending_matching = [
                c
                for c in analyst_mirrors
                if c.with_pending_order_assessment_id == pending_id
                and c.underlying == pc.underlying
                and c.conflict_type == pc.conflict_type
            ]
            assert len(pending_matching) == 1

    # adjust-bracket on NVDA produces no conflicts even though same underlying
    assert result.strategist_position_conflicts_by_assessment_id["SA-5"] == ()
    # Bracket-leg pending order produces no conflicts even though same underlying
    assert result.strategist_pending_conflicts_by_assessment_id["SA-ORD-3"] == ()
    # rec_no_match (TSLA) has no conflicts
    assert result.analyst_conflicts_by_rec_id["REC-4"] == ()


# ---------------------------------------------------------------------------
# Option-instrument direction is honored in hold/direction-conflict matching
# ---------------------------------------------------------------------------


def test_option_recommendation_direction_drives_hold_conflict() -> None:
    rec = _option_recommendation("REC-1", "NVDA", direction="long")
    pos = _hold_assessment("SA-1", "NVDA")
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos,),
        pending_order_assessments=(),
        held_direction_resolver=lambda _pid: "long",
    )
    analyst_side = result.analyst_conflicts_by_rec_id["REC-1"]
    assert len(analyst_side) == 1
    assert analyst_side[0].conflict_type == ConflictType.entry_vs_hold


# ---------------------------------------------------------------------------
# Different underlying produces no conflict (sanity check)
# ---------------------------------------------------------------------------


def test_different_underlying_no_conflict() -> None:
    rec = _equity_recommendation("REC-1", "AAPL")
    pos = _close_assessment("SA-1", "NVDA")
    parent_holding = _hold_assessment("SA-2", "MSFT")
    pending = _entry_pending_order(
        "SA-ORD-1", linked_assessment_id="SA-2", recommended_action="maintain"
    )
    result = detect_conflicts(
        recommendations=(rec,),
        position_assessments=(pos, parent_holding),
        pending_order_assessments=(pending,),
        held_direction_resolver=lambda _pid: "long",
    )
    assert result.analyst_conflicts_by_rec_id["REC-1"] == ()
    assert result.strategist_position_conflicts_by_assessment_id["SA-1"] == ()
    assert result.strategist_position_conflicts_by_assessment_id["SA-2"] == ()
    assert result.strategist_pending_conflicts_by_assessment_id["SA-ORD-1"] == ()
