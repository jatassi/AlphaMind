"""Tests for bracket-thesis coverage cross-validator (ALP-333)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    SupportingSignal,
    SupportingSignalStatus,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_leg(
    leg_id: str,
    leg_type: BracketLegType,
    enforcement: BracketLegEnforcement = BracketLegEnforcement.MECHANICAL,
    status: BracketLegStatus = BracketLegStatus.ACTIVE,
) -> BracketLeg:
    order_id = None if leg_type == BracketLegType.EVENT_INVALIDATION else "ord-1"
    return BracketLeg(
        leg_id=leg_id,
        leg_type=leg_type,
        order_id=order_id,
        trigger_condition="trigger",
        enforcement=enforcement,
        status=status,
        pl_based=False,
    )


def _make_bracket(legs: tuple[BracketLeg, ...]) -> BracketRecord:
    """Build a minimal valid BracketRecord with given legs."""
    return BracketRecord(
        bracket_id="brkt-1",
        position_id="pos-1",
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-entry",
        protective_legs=legs,
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_component(
    component_id: str,
    component_type: ThesisComponentType,
    linked_bracket_leg_id: str | None = None,
    linked_bracket_leg_type: BracketLegType | None = None,
) -> ThesisComponent:
    return ThesisComponent(
        component_id=component_id,
        thesis_id="thesis-1",
        component_type=component_type,
        linked_bracket_leg_type=linked_bracket_leg_type,
        linked_bracket_leg_id=linked_bracket_leg_id,
        instrument_reference="AAPL",
        narrative="narrative",
        key_assumptions=(KeyAssumption(text="holds", outcome=None),),
        supporting_signals=(SupportingSignal(name="vol", status=SupportingSignalStatus.PRESENT),),
        generation_timestamp=NOW,
        resolution_outcome=None,
        resolution_notes=None,
    )


def _make_thesis(
    components: tuple[ThesisComponent, ...],
    *,
    status: ThesisRecordStatus = ThesisRecordStatus.ACTIVE,
) -> ThesisRecord:
    """Build a ThesisRecord with given components.

    ThesisRecord._check_mandatory_coverage requires all 3 component types for
    ACTIVE/RESOLVED status. If the supplied components don't satisfy this, caller
    must pass status=ThesisRecordStatus.CANCELLED to bypass the within-record rule.
    """
    return ThesisRecord(
        thesis_id="thesis-1",
        position_id="pos-1",
        summary="Test thesis",
        components=components,
        status=status,
        health_status=None,
        prior_health_status=None,
        generation_timestamp=NOW,
        time_expectation_hours="24-48h",
        age_hours=1.0,
        expected_resolution_at=NOW,
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="catalyst",
    )


def _full_covered_thesis(
    tp_leg_id: str = "leg-tp",
    ps_leg_id: str = "leg-ps",
) -> ThesisRecord:
    """Thesis with ENTRY + TARGET (covering TAKE_PROFIT) + INVALIDATION (covering PRICE_STOP)."""
    return _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-tp",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id=tp_leg_id,
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
            _make_component(
                "c-ps",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id=ps_leg_id,
                linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            ),
        )
    )


# ---------------------------------------------------------------------------
# 1. Tracer bullet: field exists and is importable
# ---------------------------------------------------------------------------


def test_linked_bracket_leg_id_field_exists() -> None:
    """ThesisComponent must have linked_bracket_leg_id field defaulting to None."""
    assert "linked_bracket_leg_id" in ThesisComponent.model_fields
    comp = _make_component("c1", ThesisComponentType.ENTRY_RATIONALE)
    assert comp.linked_bracket_leg_id is None


# ---------------------------------------------------------------------------
# 2. Backward-compat: existing callers omitting the field still work
# ---------------------------------------------------------------------------


def test_thesis_component_without_linked_bracket_leg_id_is_valid() -> None:
    """Constructing ThesisComponent without linked_bracket_leg_id must not raise."""
    comp = ThesisComponent(
        component_id="c1",
        thesis_id="t1",
        component_type=ThesisComponentType.ENTRY_RATIONALE,
        linked_bracket_leg_type=None,
        instrument_reference="SPY",
        narrative="entry rationale",
        key_assumptions=(),
        supporting_signals=(),
        generation_timestamp=NOW,
        resolution_outcome=None,
        resolution_notes=None,
    )
    assert comp.linked_bracket_leg_id is None


# ---------------------------------------------------------------------------
# 3. Import path
# ---------------------------------------------------------------------------


def test_validate_bracket_thesis_coverage_importable() -> None:
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    assert callable(validate_bracket_thesis_coverage)


# ---------------------------------------------------------------------------
# 4. (a) Coverage pass: all legs have compatible covering components
# ---------------------------------------------------------------------------


def test_coverage_pass_take_profit_and_price_stop() -> None:
    """Bracket with TAKE_PROFIT + PRICE_STOP legs each covered by compatible components."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    ps_leg = _make_leg("leg-ps", BracketLegType.PRICE_STOP)
    bracket = _make_bracket((tp_leg, ps_leg))
    thesis = _full_covered_thesis("leg-tp", "leg-ps")

    validate_bracket_thesis_coverage(bracket, thesis)


# ---------------------------------------------------------------------------
# 5. (b) Coverage fail: one uncovered leg
# ---------------------------------------------------------------------------


def test_coverage_fail_one_uncovered_leg() -> None:
    """One PRICE_STOP leg with no coverage component raises ValueError naming it."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    ps_leg = _make_leg("leg-ps-uncovered", BracketLegType.PRICE_STOP)
    bracket = _make_bracket((tp_leg, ps_leg))

    # Only covers the TAKE_PROFIT leg, not PRICE_STOP.
    # Use CANCELLED status to bypass ThesisRecord._check_mandatory_coverage
    # (which requires all 3 component types for ACTIVE/RESOLVED).
    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-tp",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id="leg-tp",
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
        ),
        status=ThesisRecordStatus.CANCELLED,
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert "leg-ps-uncovered" in msg
    assert "PRICE_STOP" in msg


# ---------------------------------------------------------------------------
# 6. (c) Coverage fail: multiple uncovered legs — all appear in error, stable order
# ---------------------------------------------------------------------------


def test_coverage_fail_multiple_uncovered_legs_all_in_error() -> None:
    """Three uncovered legs → all three leg_ids appear in the ValueError, insertion order."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    ps_leg = _make_leg("leg-ps", BracketLegType.PRICE_STOP)
    te_leg = _make_leg(
        "leg-te",
        BracketLegType.TIME_EXPIRATION,
        enforcement=BracketLegEnforcement.ADVISORY,
    )
    bracket = _make_bracket((tp_leg, ps_leg, te_leg))

    # Only ENTRY_RATIONALE — no leg-linking components at all.
    # Use CANCELLED to bypass ThesisRecord._check_mandatory_coverage.
    thesis = _make_thesis(
        (_make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),),
        status=ThesisRecordStatus.CANCELLED,
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert "leg-tp" in msg
    assert "leg-ps" in msg
    assert "leg-te" in msg

    # Stable insertion order: leg-tp appears before leg-ps, leg-ps before leg-te
    assert msg.index("leg-tp") < msg.index("leg-ps") < msg.index("leg-te")


# ---------------------------------------------------------------------------
# 7. (d) Component-type mismatch: TAKE_PROFIT leg covered only by INVALIDATION_RATIONALE → fail
# ---------------------------------------------------------------------------


def test_coverage_fail_type_mismatch_take_profit_by_invalidation() -> None:
    """TAKE_PROFIT leg covered only by INVALIDATION_RATIONALE component must fail."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    bracket = _make_bracket((tp_leg,))

    # INVALIDATION_RATIONALE is not compatible with TAKE_PROFIT.
    # Use CANCELLED to bypass ThesisRecord._check_mandatory_coverage
    # (which would also reject a thesis missing TARGET_RATIONALE in ACTIVE status).
    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-wrong-type",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id="leg-tp",
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
        ),
        status=ThesisRecordStatus.CANCELLED,
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert "leg-tp" in msg
    assert "TAKE_PROFIT" in msg


# ---------------------------------------------------------------------------
# 8. (e) Missing ENTRY_RATIONALE → fail
# ---------------------------------------------------------------------------


def test_coverage_fail_missing_entry_rationale() -> None:
    """Thesis with no ENTRY_RATIONALE component raises ValueError."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    # BracketRecord requires a MECHANICAL backstop leg, use TAKE_PROFIT
    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    bracket = _make_bracket((tp_leg,))

    # CANCELLED bypasses ThesisRecord._check_mandatory_coverage so we can omit ENTRY_RATIONALE.
    thesis = ThesisRecord(
        thesis_id="thesis-1",
        position_id="pos-1",
        summary="cancelled",
        components=(
            _make_component(
                "c-tp",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id="leg-tp",
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
        ),
        status=ThesisRecordStatus.CANCELLED,
        health_status=None,
        prior_health_status=None,
        generation_timestamp=NOW,
        time_expectation_hours="24h",
        age_hours=0.0,
        expected_resolution_at=NOW,
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="catalyst",
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert "ENTRY_RATIONALE" in msg


# ---------------------------------------------------------------------------
# 9. (f) Components with linked_bracket_leg_id=None are silently ignored
# ---------------------------------------------------------------------------


def test_components_with_no_link_ignored_for_leg_coverage() -> None:
    """Components with linked_bracket_leg_id=None do NOT satisfy leg coverage."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    bracket = _make_bracket((tp_leg,))

    # TARGET_RATIONALE component but NOT linked to the leg.
    # Use CANCELLED to bypass ThesisRecord._check_mandatory_coverage
    # (which would reject a thesis missing INVALIDATION_RATIONALE in ACTIVE status).
    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-tp-unlinked",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id=None,
            ),
        ),
        status=ThesisRecordStatus.CANCELLED,
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert "leg-tp" in msg  # still uncovered


# ---------------------------------------------------------------------------
# 10. (g) TIME_EXPIRATION leg covered by INVALIDATION_RATIONALE → passes
# ---------------------------------------------------------------------------


def test_coverage_pass_time_expiration_with_invalidation_rationale() -> None:
    """TIME_EXPIRATION leg covered by INVALIDATION_RATIONALE component passes."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    # Need a MECHANICAL backstop; TIME_EXPIRATION qualifies
    te_leg = _make_leg("leg-te", BracketLegType.TIME_EXPIRATION)
    bracket = _make_bracket((te_leg,))

    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-te",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id="leg-te",
                linked_bracket_leg_type=BracketLegType.TIME_EXPIRATION,
            ),
            _make_component("c-tgt", ThesisComponentType.TARGET_RATIONALE),
        )
    )

    validate_bracket_thesis_coverage(bracket, thesis)


# ---------------------------------------------------------------------------
# 11. (g) EVENT_INVALIDATION leg covered by INVALIDATION_RATIONALE → passes
# ---------------------------------------------------------------------------


def test_coverage_pass_event_invalidation_with_invalidation_rationale() -> None:
    """EVENT_INVALIDATION leg covered by INVALIDATION_RATIONALE component passes."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    ei_leg = _make_leg(
        "leg-ei",
        BracketLegType.EVENT_INVALIDATION,
        enforcement=BracketLegEnforcement.ADVISORY,
    )
    # Need a MECHANICAL backstop alongside the EVENT_INVALIDATION advisory leg
    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    bracket = _make_bracket((tp_leg, ei_leg))

    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-tp",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id="leg-tp",
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
            _make_component(
                "c-ei",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id="leg-ei",
                linked_bracket_leg_type=BracketLegType.EVENT_INVALIDATION,
            ),
        )
    )

    validate_bracket_thesis_coverage(bracket, thesis)


# ---------------------------------------------------------------------------
# 12. Single uncovered leg: leg_id and leg_type appear exactly once in message
# ---------------------------------------------------------------------------


def test_error_message_contains_leg_id_and_type_exactly_once() -> None:
    """One uncovered leg of 4: error message contains its leg_id and type once."""
    from alphamind.execution.thesis_model import validate_bracket_thesis_coverage

    tp_leg = _make_leg("leg-tp", BracketLegType.TAKE_PROFIT)
    ps_leg = _make_leg("leg-ps", BracketLegType.PRICE_STOP)
    te_leg = _make_leg("leg-te", BracketLegType.TIME_EXPIRATION)
    ei_leg = _make_leg(
        "leg-ei",
        BracketLegType.EVENT_INVALIDATION,
        enforcement=BracketLegEnforcement.ADVISORY,
    )
    bracket = _make_bracket((tp_leg, ps_leg, te_leg, ei_leg))

    # Cover tp, ps, te — leave ei uncovered
    thesis = _make_thesis(
        (
            _make_component("c-entry", ThesisComponentType.ENTRY_RATIONALE),
            _make_component(
                "c-tp",
                ThesisComponentType.TARGET_RATIONALE,
                linked_bracket_leg_id="leg-tp",
                linked_bracket_leg_type=BracketLegType.TAKE_PROFIT,
            ),
            _make_component(
                "c-ps",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id="leg-ps",
                linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            ),
            _make_component(
                "c-te",
                ThesisComponentType.INVALIDATION_RATIONALE,
                linked_bracket_leg_id="leg-te",
                linked_bracket_leg_type=BracketLegType.TIME_EXPIRATION,
            ),
        )
    )

    with pytest.raises(ValueError) as exc_info:
        validate_bracket_thesis_coverage(bracket, thesis)

    msg = str(exc_info.value)
    assert msg.count("leg-ei") == 1
    assert msg.count("EVENT_INVALIDATION") == 1
    # Covered legs must not appear in the error
    assert "leg-tp" not in msg
    assert "leg-ps" not in msg
    assert "leg-te" not in msg
