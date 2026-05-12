"""Tests for the in-memory cooldown tracker (story 04b / ALP-439).

The tracker suppresses non-margin emergency emits within the configured window.
Margin-call triggers always pass (broker deadline is non-negotiable per
``docs/design/06-risk-guardrails/breach-behavior.md`` § Emergency invocation
trigger). The tracker is pure: deterministic given ``now`` + ``trigger_type``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)


def _now() -> datetime:
    return datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def test_initial_state_allows_any_trigger() -> None:
    """No prior record → every trigger may fire."""
    tracker = CooldownTracker(cooldown_minutes=30)
    assert tracker.may_fire(now=_now(), trigger_type="regime_jump") is True
    assert tracker.may_fire(now=_now(), trigger_type="multi_rule_breach") is True
    assert tracker.may_fire(now=_now(), trigger_type="drawdown_velocity") is True
    assert tracker.may_fire(now=_now(), trigger_type="margin_call") is True


def test_non_margin_suppressed_within_window() -> None:
    """A non-margin trigger fired one minute ago suppresses the next non-margin."""
    tracker = CooldownTracker(cooldown_minutes=30)
    fire_time = _now()
    tracker.record_fire(now=fire_time, trigger_type="regime_jump")

    one_minute_later = fire_time + timedelta(minutes=1)
    assert tracker.may_fire(now=one_minute_later, trigger_type="regime_jump") is False
    assert tracker.may_fire(now=one_minute_later, trigger_type="multi_rule_breach") is False
    assert tracker.may_fire(now=one_minute_later, trigger_type="drawdown_velocity") is False


def test_margin_call_bypasses_cooldown() -> None:
    """Margin-call passes regardless of prior emits (broker deadline override)."""
    tracker = CooldownTracker(cooldown_minutes=30)
    tracker.record_fire(now=_now(), trigger_type="regime_jump")

    assert tracker.may_fire(now=_now() + timedelta(seconds=1), trigger_type="margin_call") is True


def test_non_margin_allowed_after_window_elapses() -> None:
    """The cooldown lifts after ``cooldown_minutes`` have elapsed."""
    tracker = CooldownTracker(cooldown_minutes=30)
    fire_time = _now()
    tracker.record_fire(now=fire_time, trigger_type="regime_jump")

    after_window = fire_time + timedelta(minutes=30)
    assert tracker.may_fire(now=after_window, trigger_type="regime_jump") is True


def test_record_fire_advances_window() -> None:
    """A second record extends the suppression window from the new fire time."""
    tracker = CooldownTracker(cooldown_minutes=30)
    first = _now()
    tracker.record_fire(now=first, trigger_type="regime_jump")
    # Margin-call records but does not anchor the cooldown for subsequent margin calls.
    second = first + timedelta(minutes=10)
    tracker.record_fire(now=second, trigger_type="regime_jump")

    # 25 minutes after the *second* fire is still inside the 30-min window.
    inside = second + timedelta(minutes=25)
    assert tracker.may_fire(now=inside, trigger_type="multi_rule_breach") is False
    # 30 minutes after the second fire is outside.
    outside = second + timedelta(minutes=30)
    assert tracker.may_fire(now=outside, trigger_type="multi_rule_breach") is True


def test_remaining_seconds_zero_before_first_fire() -> None:
    """Remaining cooldown is 0 when no prior emit exists."""
    tracker = CooldownTracker(cooldown_minutes=30)
    assert tracker.remaining_seconds(now=_now(), trigger_type="regime_jump") == 0


def test_remaining_seconds_after_partial_window() -> None:
    """Remaining cooldown counts down from cooldown_minutes after a fire."""
    tracker = CooldownTracker(cooldown_minutes=30)
    fire_time = _now()
    tracker.record_fire(now=fire_time, trigger_type="regime_jump")

    after_5 = fire_time + timedelta(minutes=5)
    # 30 - 5 = 25 minutes remaining.
    assert tracker.remaining_seconds(now=after_5, trigger_type="regime_jump") == 25 * 60


def test_remaining_seconds_zero_for_margin_call() -> None:
    """Margin-call queries always return 0 (it bypasses cooldown)."""
    tracker = CooldownTracker(cooldown_minutes=30)
    tracker.record_fire(now=_now(), trigger_type="regime_jump")
    assert tracker.remaining_seconds(now=_now(), trigger_type="margin_call") == 0


def test_remaining_seconds_clamped_to_zero_after_window() -> None:
    """After the window elapses, remaining seconds is 0 (not negative)."""
    tracker = CooldownTracker(cooldown_minutes=30)
    fire_time = _now()
    tracker.record_fire(now=fire_time, trigger_type="regime_jump")
    after_window = fire_time + timedelta(minutes=45)
    assert tracker.remaining_seconds(now=after_window, trigger_type="regime_jump") == 0
