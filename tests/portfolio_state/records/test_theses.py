"""Tests for thesis records (story 03b)."""
# mypy: disable-error-code="arg-type,call-arg,dict-item,misc,no-untyped-def,no-untyped-call,unused-ignore,no-any-return,var-annotated"

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from alphamind._kernel.ids import (
    PositionId,
    ThesisId,
)
from alphamind.portfolio_state.records.orders import BracketLegType
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    RecentThesisResolution,
    SupportingSignal,
    SupportingSignalStatus,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
    ThesisStatus,
)

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_thesis_status_members() -> None:
    members = {m.value for m in ThesisStatus}
    assert members == {"ON_TRACK", "PARTIALLY_REALIZED", "AT_RISK", "STALE", "INVALIDATED"}


def test_thesis_status_is_str() -> None:
    assert ThesisStatus.ON_TRACK == "ON_TRACK"


def test_thesis_record_status_members() -> None:
    members = {m.value for m in ThesisRecordStatus}
    assert members == {"ACTIVE", "RESOLVED", "CANCELLED"}


def test_thesis_record_status_is_str() -> None:
    assert ThesisRecordStatus.ACTIVE == "ACTIVE"


def test_thesis_component_type_members() -> None:
    members = {m.value for m in ThesisComponentType}
    assert members == {"ENTRY_RATIONALE", "TARGET_RATIONALE", "INVALIDATION_RATIONALE"}


def test_thesis_resolution_category_members() -> None:
    members = {m.value for m in ThesisResolutionCategory}
    assert members == {
        "VALIDATED",
        "PROFITABLE_BUT_WRONG",
        "INVALIDATED_STOPPED_CORRECTLY",
        "INVALIDATED_WRONG_ON_EXIT",
        "CANCELLED_NEVER_ENTERED",
    }


def test_thesis_component_outcome_members() -> None:
    members = {m.value for m in ThesisComponentOutcome}
    assert members == {"VALIDATED", "WRONG", "INCONCLUSIVE"}


def test_supporting_signal_status_members() -> None:
    members = {m.value for m in SupportingSignalStatus}
    assert members == {"PRESENT", "STRENGTHENED", "WEAKENED", "REVERSED"}


def test_bracket_leg_type_not_redeclared_in_theses() -> None:
    """BracketLegType must be the same object as in orders (not a separate declaration)."""
    import alphamind.portfolio_state.records.orders as orders_mod
    import alphamind.portfolio_state.records.theses as theses_mod

    if hasattr(theses_mod, "BracketLegType"):
        assert theses_mod.BracketLegType is orders_mod.BracketLegType, (
            "BracketLegType in theses must be the same object as in orders (not redeclared)"
        )
    assert hasattr(orders_mod, "BracketLegType")


def test_key_assumption_valid_unresolved() -> None:
    ka = KeyAssumption(text="Price holds above 200-day MA", outcome=None)
    assert ka.text == "Price holds above 200-day MA"
    assert ka.outcome is None


def test_key_assumption_valid_resolved() -> None:
    ka = KeyAssumption(text="Earnings beat", outcome=ThesisComponentOutcome.VALIDATED)
    assert ka.outcome == ThesisComponentOutcome.VALIDATED


def test_key_assumption_frozen() -> None:
    ka = KeyAssumption(text="test", outcome=None)
    with pytest.raises(FrozenInstanceError):
        ka.text = "changed"


def test_key_assumption_requires_text() -> None:
    with pytest.raises((ValueError, TypeError)):
        KeyAssumption(outcome=None)


def test_supporting_signal_valid() -> None:
    sig = SupportingSignal(name="unusual options activity", status=SupportingSignalStatus.PRESENT)
    assert sig.name == "unusual options activity"
    assert sig.status == SupportingSignalStatus.PRESENT


def test_supporting_signal_requires_name() -> None:
    with pytest.raises((ValueError, TypeError)):
        SupportingSignal(status="PRESENT")


def test_supporting_signal_requires_status() -> None:
    with pytest.raises((ValueError, TypeError)):
        SupportingSignal(name="earnings revision")


def _make_component(
    component_type: ThesisComponentType = ThesisComponentType.ENTRY_RATIONALE,
    resolution_outcome: ThesisComponentOutcome | None = None,
    resolution_notes: str | None = None,
    linked_bracket_leg_type: BracketLegType | None = None,
) -> ThesisComponent:
    return ThesisComponent(
        component_id="comp-1",
        thesis_id=ThesisId("thesis-1"),
        component_type=component_type,
        linked_bracket_leg_type=linked_bracket_leg_type,
        instrument_reference="AAPL",
        narrative="Price above 200-day MA with increasing volume.",
        key_assumptions=(KeyAssumption(text="momentum holds", outcome=None),),
        generation_timestamp=NOW,
        resolution_outcome=resolution_outcome,
        resolution_notes=resolution_notes,
    )


def _make_full_components(
    resolved: bool = False,
) -> tuple[ThesisComponent, ...]:
    """Return one component of each required type."""
    outcome = ThesisComponentOutcome.VALIDATED if resolved else None
    notes = "resolved" if resolved else None
    return (
        _make_component(ThesisComponentType.ENTRY_RATIONALE, outcome, notes),
        ThesisComponent(
            component_id="comp-2",
            thesis_id=ThesisId("thesis-1"),
            component_type=ThesisComponentType.TARGET_RATIONALE,
            linked_bracket_leg_type=None,
            instrument_reference="AAPL",
            narrative="Target at 200.",
            key_assumptions=(),
            generation_timestamp=NOW,
            resolution_outcome=outcome,
            resolution_notes=notes,
        ),
        ThesisComponent(
            component_id="comp-3",
            thesis_id=ThesisId("thesis-1"),
            component_type=ThesisComponentType.INVALIDATION_RATIONALE,
            linked_bracket_leg_type=BracketLegType.PRICE_STOP,
            instrument_reference="AAPL",
            narrative="Stop at 150.",
            key_assumptions=(),
            generation_timestamp=NOW,
            resolution_outcome=outcome,
            resolution_notes=notes,
        ),
    )


def _make_thesis_record(**overrides: object) -> ThesisRecord:
    """Build a valid ACTIVE ThesisRecord with optional overrides.

    Default time_expectation_hours=24.0 with expected_resolution_at=NOW+24h
    so the cross-field consistency validator passes without needing overrides.
    """
    base: dict[str, object] = {
        "thesis_id": "thesis-1",
        "position_id": "pos-1",
        "summary": "Long AAPL on momentum breakout.",
        "components": _make_full_components(),
        "status": ThesisRecordStatus.ACTIVE,
        "generation_timestamp": NOW,
        "time_expectation_hours": 24.0,
        "age_hours": 2.0,
        "expected_resolution_at": NOW + timedelta(hours=24),
        "resolution_timestamp": None,
        "resolution_category": None,
        "resolution_pnl_usd": None,
        "entry_fill_gap_usd": None,
        "key_catalyst": "earnings beat",
    }
    base.update(overrides)
    return ThesisRecord(**base)


def test_thesis_component_valid() -> None:
    comp = _make_component()
    assert comp.component_type == ThesisComponentType.ENTRY_RATIONALE
    assert comp.linked_bracket_leg_type is None
    assert comp.resolution_outcome is None


def test_thesis_component_with_bracket_leg() -> None:
    comp = _make_component(
        component_type=ThesisComponentType.INVALIDATION_RATIONALE,
        linked_bracket_leg_type=BracketLegType.PRICE_STOP,
    )
    assert comp.linked_bracket_leg_type == BracketLegType.PRICE_STOP


def test_thesis_component_frozen() -> None:
    comp = _make_component()
    with pytest.raises(FrozenInstanceError):
        comp.narrative = "changed"


def test_thesis_component_requires_component_id() -> None:
    with pytest.raises((ValueError, TypeError)):
        ThesisComponent(
            thesis_id=ThesisId("t1"),
            component_type="ENTRY_RATIONALE",
            linked_bracket_leg_type=None,
            instrument_reference="AAPL",
            narrative="x",
            key_assumptions=[],
            generation_timestamp=NOW.isoformat(),
            resolution_outcome=None,
            resolution_notes=None,
        )


def test_thesis_record_valid_active() -> None:
    rec = _make_thesis_record()
    assert rec.thesis_id == "thesis-1"
    assert rec.status == ThesisRecordStatus.ACTIVE
    assert rec.resolution_timestamp is None


def test_thesis_record_frozen() -> None:
    rec = _make_thesis_record()
    with pytest.raises(FrozenInstanceError):
        rec.summary = "changed"


def test_active_thesis_empty_components_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(components=())


def test_active_thesis_non_empty_components_accepted() -> None:
    rec = _make_thesis_record()
    assert len(rec.components) > 0


def test_resolved_thesis_empty_components_rejected() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_thesis_record(
            components=(),
            status=ThesisRecordStatus.RESOLVED,
            resolution_timestamp=NOW,
            resolution_category=ThesisResolutionCategory.VALIDATED,
            resolution_pnl_usd=500.0,
        )


def test_cancelled_thesis_empty_components_accepted() -> None:
    rec = _make_thesis_record(
        components=(),
        status=ThesisRecordStatus.CANCELLED,
        resolution_category=ThesisResolutionCategory.CANCELLED_NEVER_ENTERED,
    )
    assert rec.components == ()


def test_mandatory_coverage_all_three_present_passes() -> None:
    rec = _make_thesis_record()
    types = {c.component_type for c in rec.components}
    assert ThesisComponentType.ENTRY_RATIONALE in types
    assert ThesisComponentType.TARGET_RATIONALE in types
    assert ThesisComponentType.INVALIDATION_RATIONALE in types


def test_mandatory_coverage_missing_entry_rationale_rejected() -> None:
    comps = tuple(
        c
        for c in _make_full_components()
        if c.component_type != ThesisComponentType.ENTRY_RATIONALE
    )
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(components=comps)


def test_mandatory_coverage_missing_target_rationale_rejected() -> None:
    comps = tuple(
        c
        for c in _make_full_components()
        if c.component_type != ThesisComponentType.TARGET_RATIONALE
    )
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(components=comps)


def test_mandatory_coverage_missing_invalidation_rationale_rejected() -> None:
    comps = tuple(
        c
        for c in _make_full_components()
        if c.component_type != ThesisComponentType.INVALIDATION_RATIONALE
    )
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(components=comps)


def test_active_resolution_fields_none_passes() -> None:
    rec = _make_thesis_record()
    assert rec.resolution_timestamp is None
    assert rec.resolution_category is None
    assert rec.resolution_pnl_usd is None


def test_active_with_resolution_timestamp_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(resolution_timestamp=NOW)


def test_active_with_resolution_category_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(resolution_category=ThesisResolutionCategory.VALIDATED)


def test_active_with_resolution_pnl_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(resolution_pnl_usd=100.0)


def _make_resolved_thesis(**overrides: object) -> ThesisRecord:
    base: dict[str, object] = {
        "status": ThesisRecordStatus.RESOLVED,
        "components": _make_full_components(resolved=True),
        "resolution_timestamp": NOW,
        "resolution_category": ThesisResolutionCategory.VALIDATED,
        "resolution_pnl_usd": 500.0,
    }
    base.update(overrides)
    return _make_thesis_record(**base)


def test_resolved_all_fields_populated_passes() -> None:
    rec = _make_resolved_thesis()
    assert rec.resolution_timestamp is not None
    assert rec.resolution_category is not None
    assert rec.resolution_pnl_usd is not None


def test_resolved_missing_timestamp_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_resolved_thesis(resolution_timestamp=None)


def test_resolved_missing_category_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_resolved_thesis(resolution_category=None)


def test_resolved_missing_pnl_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_resolved_thesis(resolution_pnl_usd=None)


def test_resolved_unresolved_component_rejected() -> None:
    """RESOLVED thesis must raise when any component has resolution_outcome=None."""
    comps = _make_full_components(resolved=False)
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_resolved_thesis(components=comps)


def test_age_hours_zero_accepted() -> None:
    rec = _make_thesis_record(age_hours=0.0)
    assert rec.age_hours == 0.0


def test_age_hours_positive_accepted() -> None:
    rec = _make_thesis_record(age_hours=48.0)
    assert rec.age_hours == 48.0


def test_age_hours_negative_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(age_hours=-1.0)


def test_summary_non_empty_passes() -> None:
    rec = _make_thesis_record(summary="Valid summary text.")
    assert rec.summary == "Valid summary text."


def test_summary_empty_rejected() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(summary="")


# ---------------------------------------------------------------------------
# time_expectation_hours field type and constraints (ALP-337)
# ---------------------------------------------------------------------------


def test_time_expectation_field_type() -> None:
    """time_expectation_hours must be typed as float; gt=0 is enforced in __post_init__."""
    import dataclasses as _dc

    fields = {f.name: f for f in _dc.fields(ThesisRecord)}
    assert fields["time_expectation_hours"].type in (float, "float"), (
        f"Expected float annotation, got: {fields['time_expectation_hours'].type}"
    )
    # gt=0 enforcement lives in __post_init__ instead of Pydantic Field metadata.


def test_time_expectation_valid_float_passes() -> None:
    """Valid positive float is accepted."""
    rec = _make_thesis_record(time_expectation_hours=24.0)
    assert rec.time_expectation_hours == 24.0


def test_time_expectation_zero_rejected() -> None:
    """time_expectation_hours=0 must raise ValidationError (gt=0 constraint)."""
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(time_expectation_hours=0, expected_resolution_at=NOW)


def test_time_expectation_negative_rejected() -> None:
    """time_expectation_hours=-5.0 must raise ValidationError."""
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(time_expectation_hours=-5.0, expected_resolution_at=NOW)


def test_time_expectation_fractional_passes() -> None:
    """Fractional float (24.5h) is a valid non-integer value."""
    expected_at = NOW + timedelta(hours=24.5)
    rec = _make_thesis_record(time_expectation_hours=24.5, expected_resolution_at=expected_at)
    assert rec.time_expectation_hours == 24.5


def test_time_expectation_consistency_validator_passes() -> None:
    """Canonical acceptance-criteria case: 48h from 2026-05-01T12:00:00Z → 2026-05-03T12:00:00Z."""
    gen_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    expected_at = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)  # exactly 48h later
    rec = _make_thesis_record(
        generation_timestamp=gen_ts,
        time_expectation_hours=48.0,
        expected_resolution_at=expected_at,
    )
    assert rec.time_expectation_hours == 48.0


def test_time_expectation_consistency_validator_fails_and_names_delta() -> None:
    """Consistency validator raises ValueError naming the actual delta when > 60s."""
    gen_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    # 24h off — expected_resolution_at is 24h earlier than generation + 48h
    bad_resolution = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
    with pytest.raises((ValueError, TypeError)) as exc_info:
        _make_thesis_record(
            generation_timestamp=gen_ts,
            time_expectation_hours=48.0,
            expected_resolution_at=bad_resolution,
        )
    error_str = str(exc_info.value)
    # Error message must name the delta in seconds
    assert "86400" in error_str or "delta" in error_str.lower()


def test_time_expectation_consistency_within_60s_tolerance_passes() -> None:
    """Up to 60 seconds of drift is within the allowed tolerance."""
    gen_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    # 59 seconds short of exact
    expected_at = gen_ts + timedelta(hours=48) - timedelta(seconds=59)
    rec = _make_thesis_record(
        generation_timestamp=gen_ts,
        time_expectation_hours=48.0,
        expected_resolution_at=expected_at,
    )
    assert rec.time_expectation_hours == 48.0


def test_time_expectation_exactly_61s_off_fails() -> None:
    """61 seconds beyond tolerance must be rejected."""
    gen_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    expected_at = gen_ts + timedelta(hours=48) + timedelta(seconds=61)
    with pytest.raises((ValueError, TypeError)):
        _make_thesis_record(
            generation_timestamp=gen_ts,
            time_expectation_hours=48.0,
            expected_resolution_at=expected_at,
        )


def test_recent_thesis_resolution_valid() -> None:
    res = RecentThesisResolution(
        thesis_id=ThesisId("thesis-1"),
        position_id=PositionId("pos-1"),
        resolution_category=ThesisResolutionCategory.VALIDATED,
        component_outcomes=(("comp-1", ThesisComponentOutcome.VALIDATED),),
        resolution_pnl_usd=300.0,
        active_duration_hours=12.0,
        expected_duration_hours=24.0,
        signal_post_mortem=None,
    )
    assert res.resolution_category == ThesisResolutionCategory.VALIDATED
    assert res.signal_post_mortem is None


def test_recent_thesis_resolution_invalidated_with_post_mortem() -> None:
    res = RecentThesisResolution(
        thesis_id=ThesisId("thesis-2"),
        position_id=PositionId("pos-2"),
        resolution_category=ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
        component_outcomes=(("comp-1", ThesisComponentOutcome.WRONG),),
        resolution_pnl_usd=-200.0,
        active_duration_hours=6.0,
        expected_duration_hours=48.0,
        signal_post_mortem="Momentum signal reversed unexpectedly.",
    )
    assert res.signal_post_mortem is not None


def test_recent_thesis_resolution_requires_thesis_id() -> None:
    with pytest.raises((ValueError, TypeError)):
        RecentThesisResolution(
            position_id=PositionId("pos-1"),
            resolution_category="VALIDATED",
            component_outcomes=[],
            resolution_pnl_usd=0.0,
            active_duration_hours=1.0,
            expected_duration_hours=24.0,
            signal_post_mortem=None,
        )


# ---------------------------------------------------------------------------
# position_size_rationale field (ALP-343)
# ---------------------------------------------------------------------------


def test_position_size_rationale_defaults_to_none() -> None:
    """(a) Constructing ThesisRecord without position_size_rationale yields None."""
    rec = _make_thesis_record()
    assert rec.position_size_rationale is None


def test_position_size_rationale_non_empty_string_accepted() -> None:
    """(b) A non-empty rationale string is accepted."""
    rec = _make_thesis_record(
        position_size_rationale="Conviction 4 with tight invalidation supports top-of-band size."
    )
    assert rec.position_size_rationale == (
        "Conviction 4 with tight invalidation supports top-of-band size."
    )


def test_position_size_rationale_empty_string_rejected() -> None:
    """(c) An empty string is rejected by the field validator."""
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(position_size_rationale="")


def test_position_size_rationale_whitespace_only_rejected() -> None:
    """(d) A whitespace-only string is rejected by the field validator."""
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _make_thesis_record(position_size_rationale="   ")


# ---------------------------------------------------------------------------
# ALP-351 — supporting_signals / health_status lifecycle refactor
# ---------------------------------------------------------------------------


def _dataclass_field_names(cls: type) -> set[str]:
    """Return the set of dataclass field names for *cls*."""
    import dataclasses as _dc

    return {f.name for f in _dc.fields(cls)}


def test_supporting_signals_removed_from_thesis_component() -> None:
    """ThesisComponent must no longer carry supporting_signals — moved to ThesisHealthSnapshot."""
    assert "supporting_signals" not in _dataclass_field_names(ThesisComponent)


def test_health_status_removed_from_thesis_record() -> None:
    """ThesisRecord must no longer carry health_status — moved to ThesisHealthSnapshot."""
    assert "health_status" not in _dataclass_field_names(ThesisRecord)


def test_prior_health_status_removed_from_thesis_record() -> None:
    """ThesisRecord must no longer carry prior_health_status — moved to ThesisHealthSnapshot."""
    assert "prior_health_status" not in _dataclass_field_names(ThesisRecord)
