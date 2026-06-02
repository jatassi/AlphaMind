"""Tests for the regime-adaptation typed records (story 02).

Story 02 is types-only: every test here verifies a structural property of the
twelve typed records, the module-level constant, the helper, and the
``__post_init__`` invariants documented in the acceptance criteria.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import PositionId
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.config.models.overlays import EventType, Overlay
from alphamind.config.models.regimes import Regime
from alphamind.distillation.calibration import CalibrationState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.regime_adaptation import (
    LOOSENING_INVOCATIONS,
    CompositeAlertState,
    EventCalendar,
    EventCalendarEntry,
    NextTransitionDecision,
    OverlayActivationDecision,
    RegimeAdaptationAuditEntry,
    RegimeAdaptationAuditEventKind,
    RegimeAdaptationOutput,
    RegimeAdaptationState,
    RegimeTransitionBreach,
    RuleMetadata,
    StaleCalendarReport,
    VixBoundaryThresholds,
    overlays_to_strings,
)

# ---------------------------------------------------------------------------
# Shared instance builders — referenced by structural-property tests below
# and by per-record behavior tests further down.
# ---------------------------------------------------------------------------


def _baseline_stable_state() -> RegimeAdaptationState:
    """A canonical STABLE-state ``RegimeAdaptationState`` instance.

    Per-test variants derive from this baseline via ``dataclasses.replace``.
    """
    return RegimeAdaptationState(
        as_of="2026-04-29T12:00:00+00:00",
        invocation_id="INV-001",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="vol_expansion",
        distillation_vix_level=18.5,
        regime_skip_emergency=False,
    )


def _active_risk_parameter_set_stub() -> ActiveRiskParameterSet:
    """A minimal ``ActiveRiskParameterSet`` for ``RegimeAdaptationOutput`` tests."""
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Per-position max size",
                value=5.0,
                unit="% of portfolio",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )


# ---------------------------------------------------------------------------
# Frozen + slots structural checks
# ---------------------------------------------------------------------------


def _build_composite_alert_state() -> CompositeAlertState:
    return CompositeAlertState(
        funding_stress_alert_active=False,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of=None,
        market_liquidity_as_of=None,
    )


def _build_event_calendar_entry() -> EventCalendarEntry:
    return EventCalendarEntry(
        event_type=EventType.fomc,
        event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
        label="FOMC June 2026",
    )


def _build_next_transition_decision() -> NextTransitionDecision:
    return NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def _build_overlay_activation_decision() -> OverlayActivationDecision:
    return OverlayActivationDecision(
        overlay=Overlay.stress,
        is_active=False,
        rationale="",
        pre_event_block_new_positions=False,
    )


def _build_regime_adaptation_output() -> RegimeAdaptationOutput:
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=_active_risk_parameter_set_stub(),
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=_baseline_stable_state(),
        audit_log_entries=(),
    )


def _build_regime_transition_breach() -> RegimeTransitionBreach:
    return RegimeTransitionBreach(
        position_id=None,
        rule_id="net_long_pct",
        rule_label="Net long",
        current_value=85.0,
        new_limit_value=70.0,
        overage=15.0,
        unit="% of portfolio",
    )


def _build_rule_metadata() -> RuleMetadata:
    return RuleMetadata(
        rule_id="position_max_size_pct",
        label="Per-position max size",
        unit="% of portfolio",
    )


def _build_stale_calendar_report() -> StaleCalendarReport:
    return StaleCalendarReport(
        is_stale=False,
        latest_event_timestamp_utc=None,
        days_until_latest=None,
    )


def _build_vix_boundary_thresholds() -> VixBoundaryThresholds:
    return VixBoundaryThresholds(
        low_vol_vix_max=15.0,
        normal_vix_max=20.0,
        elevated_vix_max=35.0,
    )


# Maps every public dataclass to a zero-arg constructor returning a valid
# instance — used by the structural-property tests that probe each record
# in turn.
_RECORD_BUILDERS: tuple[tuple[type, Callable[[], object]], ...] = (
    (CompositeAlertState, _build_composite_alert_state),
    (EventCalendar, lambda: EventCalendar(entries=())),
    (EventCalendarEntry, _build_event_calendar_entry),
    (NextTransitionDecision, _build_next_transition_decision),
    (OverlayActivationDecision, _build_overlay_activation_decision),
    (
        RegimeAdaptationAuditEntry,
        lambda: RegimeAdaptationAuditEntry(
            event_kind=RegimeAdaptationAuditEventKind.regime_transition, payload={}
        ),
    ),
    (RegimeAdaptationOutput, _build_regime_adaptation_output),
    (RegimeAdaptationState, _baseline_stable_state),
    (RegimeTransitionBreach, _build_regime_transition_breach),
    (RuleMetadata, _build_rule_metadata),
    (StaleCalendarReport, _build_stale_calendar_report),
    (VixBoundaryThresholds, _build_vix_boundary_thresholds),
)

_RECORD_TYPES: tuple[type, ...] = tuple(record_type for record_type, _ in _RECORD_BUILDERS)


# ---------------------------------------------------------------------------
# RegimeAdaptationState
# ---------------------------------------------------------------------------


def test_regime_adaptation_state_constructible_with_valid_inputs() -> None:
    state = _baseline_stable_state()
    assert state.active_regime is Regime.normal
    assert state.transition_state is RegimeTransitionState.STABLE
    assert state.transition_invocations_remaining == 0


def test_regime_adaptation_state_rejects_stable_with_remaining_nonzero() -> None:
    with pytest.raises(ValueError) as excinfo:
        dataclasses.replace(
            _baseline_stable_state(),
            transition_invocations_remaining=2,
        )
    msg = str(excinfo.value)
    assert "transition_invocations_remaining" in msg
    assert "STABLE" in msg


def test_regime_adaptation_state_rejects_loosening_without_started_invocation() -> None:
    with pytest.raises(ValueError) as excinfo:
        dataclasses.replace(
            _baseline_stable_state(),
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
            transition_started_invocation_id=None,
            transition_origin_regime=Regime.elevated,
            prior_regime=Regime.elevated,
            active_regime=Regime.normal,
        )
    assert "transition_started_invocation_id" in str(excinfo.value)


def test_regime_adaptation_state_rejects_loosening_without_origin_regime() -> None:
    with pytest.raises(ValueError) as excinfo:
        dataclasses.replace(
            _baseline_stable_state(),
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
            transition_started_invocation_id="INV-LOOSEN-START",
            transition_origin_regime=None,
            prior_regime=Regime.elevated,
            active_regime=Regime.normal,
        )
    assert "transition_origin_regime" in str(excinfo.value)


def test_regime_adaptation_state_accepts_valid_loosening() -> None:
    """A fully-populated LOOSENING state constructs cleanly."""
    state = dataclasses.replace(
        _baseline_stable_state(),
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        transition_started_invocation_id="INV-LOOSEN-START",
        transition_origin_regime=Regime.elevated,
        prior_regime=Regime.elevated,
        active_regime=Regime.normal,
    )
    assert state.transition_state is RegimeTransitionState.LOOSENING
    assert state.transition_started_invocation_id == "INV-LOOSEN-START"
    assert state.transition_origin_regime is Regime.elevated


# ---------------------------------------------------------------------------
# RegimeTransitionBreach
# ---------------------------------------------------------------------------


def test_regime_transition_breach_constructible_per_position() -> None:
    breach = RegimeTransitionBreach(
        position_id=PositionId("POS-AAPL-1"),
        rule_id="position_max_size_pct",
        rule_label="Per-position max size",
        current_value=8.0,
        new_limit_value=5.0,
        overage=3.0,
        unit="% of portfolio",
    )
    assert breach.position_id == "POS-AAPL-1"
    assert breach.overage == 3.0


def test_regime_transition_breach_constructible_aggregate() -> None:
    breach = RegimeTransitionBreach(
        position_id=None,
        rule_id="net_long_pct",
        rule_label="Net long exposure",
        current_value=85.0,
        new_limit_value=70.0,
        overage=15.0,
        unit="% of portfolio",
    )
    assert breach.position_id is None


def test_regime_transition_breach_rejects_zero_overage() -> None:
    with pytest.raises(ValueError) as excinfo:
        RegimeTransitionBreach(
            position_id=None,
            rule_id="net_long_pct",
            rule_label="Net long",
            current_value=70.0,
            new_limit_value=70.0,
            overage=0.0,
            unit="% of portfolio",
        )
    assert "net_long_pct" in str(excinfo.value)


def test_regime_transition_breach_rejects_negative_overage() -> None:
    with pytest.raises(ValueError):
        RegimeTransitionBreach(
            position_id=None,
            rule_id="net_long_pct",
            rule_label="Net long",
            current_value=60.0,
            new_limit_value=70.0,
            overage=-10.0,
            unit="% of portfolio",
        )


def test_regime_transition_breach_rejects_overage_identity_mismatch() -> None:
    with pytest.raises(ValueError):
        RegimeTransitionBreach(
            position_id=None,
            rule_id="net_long_pct",
            rule_label="Net long",
            current_value=85.0,
            new_limit_value=70.0,
            overage=14.0,  # would need to be 15.0
            unit="% of portfolio",
        )


def test_regime_transition_breach_accepts_overage_within_tolerance() -> None:
    """Overage within ``1e-9`` absolute tolerance of ``current - limit`` passes."""
    RegimeTransitionBreach(
        position_id=None,
        rule_id="net_long_pct",
        rule_label="Net long",
        current_value=85.0,
        new_limit_value=70.0,
        overage=15.0 + 1e-10,
        unit="% of portfolio",
    )


# ---------------------------------------------------------------------------
# EventCalendarEntry / EventCalendar
# ---------------------------------------------------------------------------


def test_event_calendar_entry_accepts_tz_aware_datetime() -> None:
    entry = EventCalendarEntry(
        event_type=EventType.fomc,
        event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
        label="FOMC June 2026",
    )
    assert entry.event_type is EventType.fomc


def test_event_calendar_entry_rejects_naive_datetime() -> None:
    # Build a naive datetime by stripping tzinfo from a tz-aware one — calling
    # ``datetime(...)`` without ``tzinfo`` is a lint error that this test
    # intentionally avoids.
    naive = datetime(2026, 6, 17, 18, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError) as excinfo:
        EventCalendarEntry(
            event_type=EventType.fomc,
            event_timestamp_utc=naive,
            label="FOMC June 2026",
        )
    assert "event_timestamp_utc" in str(excinfo.value)


def test_event_calendar_holds_tuple_of_entries() -> None:
    entry = EventCalendarEntry(
        event_type=EventType.cpi,
        event_timestamp_utc=datetime(2026, 5, 14, 12, 30, tzinfo=UTC),
        label="CPI May 2026",
    )
    calendar = EventCalendar(entries=(entry,))
    assert calendar.entries == (entry,)


def test_event_calendar_empty_entries_constructible() -> None:
    EventCalendar(entries=())


# ---------------------------------------------------------------------------
# OverlayActivationDecision
# ---------------------------------------------------------------------------


def test_overlay_activation_decision_pre_event_active_with_block() -> None:
    decision = OverlayActivationDecision(
        overlay=Overlay.pre_event,
        is_active=True,
        rationale="Final invocation before FOMC June 2026",
        pre_event_block_new_positions=True,
    )
    assert decision.pre_event_block_new_positions is True


def test_overlay_activation_decision_stress_active_no_block() -> None:
    decision = OverlayActivationDecision(
        overlay=Overlay.stress,
        is_active=True,
        rationale="Funding stress composite alert active",
        pre_event_block_new_positions=False,
    )
    assert decision.is_active is True


def test_overlay_activation_decision_inactive_with_empty_rationale() -> None:
    decision = OverlayActivationDecision(
        overlay=Overlay.stress,
        is_active=False,
        rationale="",
        pre_event_block_new_positions=False,
    )
    assert decision.is_active is False


def test_overlay_activation_decision_rejects_block_on_stress_overlay() -> None:
    with pytest.raises(ValueError):
        OverlayActivationDecision(
            overlay=Overlay.stress,
            is_active=True,
            rationale="some reason",
            pre_event_block_new_positions=True,
        )


def test_overlay_activation_decision_rejects_block_when_inactive() -> None:
    with pytest.raises(ValueError):
        OverlayActivationDecision(
            overlay=Overlay.pre_event,
            is_active=False,
            rationale="",
            pre_event_block_new_positions=True,
        )


# ---------------------------------------------------------------------------
# VixBoundaryThresholds
# ---------------------------------------------------------------------------


def test_vix_boundary_thresholds_accepts_strictly_increasing() -> None:
    bounds = VixBoundaryThresholds(
        low_vol_vix_max=15.0,
        normal_vix_max=20.0,
        elevated_vix_max=35.0,
    )
    assert bounds.low_vol_vix_max == 15.0


def test_vix_boundary_thresholds_rejects_unordered_bounds() -> None:
    with pytest.raises(ValueError) as excinfo:
        VixBoundaryThresholds(
            low_vol_vix_max=14.0,
            normal_vix_max=10.0,
            elevated_vix_max=35.0,
        )
    msg = str(excinfo.value)
    assert "low_vol_vix_max" in msg
    assert "normal_vix_max" in msg
    assert "elevated_vix_max" in msg


def test_vix_boundary_thresholds_rejects_equal_bounds() -> None:
    with pytest.raises(ValueError):
        VixBoundaryThresholds(
            low_vol_vix_max=15.0,
            normal_vix_max=15.0,
            elevated_vix_max=35.0,
        )


def test_vix_boundary_thresholds_rejects_zero_value() -> None:
    with pytest.raises(ValueError):
        VixBoundaryThresholds(
            low_vol_vix_max=0.0,
            normal_vix_max=20.0,
            elevated_vix_max=35.0,
        )


def test_vix_boundary_thresholds_rejects_negative_value() -> None:
    with pytest.raises(ValueError):
        VixBoundaryThresholds(
            low_vol_vix_max=-1.0,
            normal_vix_max=20.0,
            elevated_vix_max=35.0,
        )


# ---------------------------------------------------------------------------
# NextTransitionDecision
# ---------------------------------------------------------------------------


def test_next_transition_decision_constructible_stable() -> None:
    decision = NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )
    assert decision.transition_state is RegimeTransitionState.STABLE


def test_next_transition_decision_constructible_loosening() -> None:
    decision = NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.elevated,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        transition_started_invocation_id="INV-LOOSEN-START",
        transition_origin_regime=Regime.elevated,
    )
    assert decision.transition_origin_regime is Regime.elevated


def test_next_transition_decision_constructible_tightening() -> None:
    decision = NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )
    assert decision.transition_state is RegimeTransitionState.TIGHTENING


# ---------------------------------------------------------------------------
# CompositeAlertState
# ---------------------------------------------------------------------------


def test_composite_alert_state_constructible_with_populated_rows() -> None:
    state = CompositeAlertState(
        funding_stress_alert_active=True,
        funding_stress_calibration_state=CalibrationState.CALIBRATED,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.CALIBRATED,
        funding_stress_as_of="2026-04-29T12:00:00+00:00",
        market_liquidity_as_of="2026-04-29T12:00:00+00:00",
    )
    assert state.funding_stress_alert_active is True


def test_composite_alert_state_tolerates_none_as_of_fields() -> None:
    state = CompositeAlertState(
        funding_stress_alert_active=False,
        funding_stress_calibration_state=CalibrationState.ACCUMULATING,
        market_liquidity_alert_active=False,
        market_liquidity_calibration_state=CalibrationState.UNAVAILABLE,
        funding_stress_as_of=None,
        market_liquidity_as_of=None,
    )
    assert state.funding_stress_as_of is None
    assert state.market_liquidity_as_of is None


# ---------------------------------------------------------------------------
# RuleMetadata
# ---------------------------------------------------------------------------


def test_rule_metadata_constructible() -> None:
    meta = RuleMetadata(
        rule_id="position_max_size_pct",
        label="Per-position max size",
        unit="% of portfolio",
    )
    assert meta.rule_id == "position_max_size_pct"


# ---------------------------------------------------------------------------
# StaleCalendarReport
# ---------------------------------------------------------------------------


def test_stale_calendar_report_empty_calendar_case() -> None:
    report = StaleCalendarReport(
        is_stale=False,
        latest_event_timestamp_utc=None,
        days_until_latest=None,
    )
    assert report.is_stale is False
    assert report.latest_event_timestamp_utc is None
    assert report.days_until_latest is None


def test_stale_calendar_report_populated_case() -> None:
    report = StaleCalendarReport(
        is_stale=True,
        latest_event_timestamp_utc=datetime(2026, 5, 14, 12, 30, tzinfo=UTC),
        days_until_latest=15.2,
    )
    assert report.is_stale is True


def test_stale_calendar_report_supports_negative_days_until() -> None:
    """Negative ``days_until_latest`` is the "latest event already past" case."""
    report = StaleCalendarReport(
        is_stale=True,
        latest_event_timestamp_utc=datetime(2026, 4, 15, 12, 30, tzinfo=UTC),
        days_until_latest=-14.0,
    )
    assert report.days_until_latest == -14.0


# ---------------------------------------------------------------------------
# RegimeAdaptationAuditEntry
# ---------------------------------------------------------------------------


def test_regime_adaptation_audit_entry_constructible() -> None:
    entry = RegimeAdaptationAuditEntry(
        event_kind=RegimeAdaptationAuditEventKind.regime_transition,
        payload=MappingProxyType({"prior": "normal", "new": "elevated"}),
    )
    assert entry.event_kind == RegimeAdaptationAuditEventKind.regime_transition
    assert entry.payload["new"] == "elevated"


# ---------------------------------------------------------------------------
# RegimeAdaptationOutput
# ---------------------------------------------------------------------------


def test_regime_adaptation_output_constructible() -> None:
    output = RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits=MappingProxyType({"position_max_size_pct": 5.0}),
        active_risk_parameter_set=_active_risk_parameter_set_stub(),
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=_baseline_stable_state(),
        audit_log_entries=(),
    )
    assert output.runtime_dimensions_active_regime is Regime.normal
    assert output.regime_skip_emergency is False


# ---------------------------------------------------------------------------
# overlays_to_strings
# ---------------------------------------------------------------------------


def test_overlays_to_strings_empty_tuple() -> None:
    assert overlays_to_strings(()) == ()


def test_overlays_to_strings_single_overlay() -> None:
    assert overlays_to_strings((Overlay.pre_event,)) == ("pre_event",)


def test_overlays_to_strings_alphabetical_sort() -> None:
    """Result is alphabetically sorted by string value regardless of input order."""
    assert overlays_to_strings((Overlay.stress, Overlay.pre_event)) == (
        "pre_event",
        "stress",
    )


# ---------------------------------------------------------------------------
# LOOSENING_INVOCATIONS constant
# ---------------------------------------------------------------------------


def test_loosening_invocations_is_an_int() -> None:
    assert isinstance(LOOSENING_INVOCATIONS, int)
    # It must be strictly positive (a zero or negative loosening window
    # would degenerate the linear interpolation).
    assert LOOSENING_INVOCATIONS > 0
