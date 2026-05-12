"""Tests for ``HaltTransitionTracker`` (story 03b / ALP-437).

The tracker emits zero or more ``ActivityLogEntry`` objects per observation —
``HALT_ACTIVATED`` on inactive→active transitions, ``HALT_LIFTED`` on
active→inactive, no entry while the halt persists in either state.
``observe(halt_state, ...)`` is the single public entry point.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.execution.continuous_monitor.breach_loop import HaltTransitionTracker
from alphamind.portfolio_state.events.activity_log import (
    EventGroup,
    EventSource,
    EventType,
    HaltActivatedDetail,
    HaltLiftedDetail,
)
from alphamind.risk_guardrails.breach_behavior import HaltState

_AT = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _entry_id(prefix: str, ts: datetime, counter: int) -> str:
    return f"{prefix}-{ts.isoformat()}-{counter:04d}"


def test_initial_inactive_observation_emits_no_entries() -> None:
    tracker = HaltTransitionTracker()
    entries = tuple(
        tracker.observe(
            halt_state=None,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=lambda counter: _entry_id("e", _AT, counter),
        )
    )
    assert entries == ()


def test_inactive_to_active_emits_halt_activated() -> None:
    tracker = HaltTransitionTracker()
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )
    entries = tuple(
        tracker.observe(
            halt_state=halt,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=lambda counter: _entry_id("e", _AT, counter),
        )
    )
    assert len(entries) == 1
    e = entries[0]
    assert e.event_type is EventType.HALT_ACTIVATED
    assert e.event_group is EventGroup.RISK_AND_GUARDRAIL
    assert isinstance(e.detail, HaltActivatedDetail)
    assert e.detail.halt_type == "daily_drawdown"
    assert e.detail.current_drawdown_pct == 5.0
    assert e.detail.limit_pct == 5.0
    assert e.detail.detected_at == _AT


def test_persistence_in_active_state_emits_no_event() -> None:
    tracker = HaltTransitionTracker()
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=5.5,
        daily_drawdown_limit_pct=5.0,
    )
    counter = 0

    def factory(c: int) -> str:
        return _entry_id("e", _AT, c)

    first = tuple(
        tracker.observe(
            halt_state=halt,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
        )
    )
    assert len(first) == 1
    del counter

    persistent = tuple(
        tracker.observe(
            halt_state=halt,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
        )
    )
    assert persistent == ()


def test_active_to_inactive_emits_halt_lifted() -> None:
    tracker = HaltTransitionTracker()
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )

    def factory(c: int) -> str:
        return _entry_id("e", _AT, c)

    tuple(
        tracker.observe(
            halt_state=halt,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
        )
    )
    lift_entries = tuple(
        tracker.observe(
            halt_state=None,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
            current_daily_drawdown_pct=2.0,
            current_cumulative_drawdown_pct=0.0,
        )
    )
    assert len(lift_entries) == 1
    e = lift_entries[0]
    assert e.event_type is EventType.HALT_LIFTED
    assert isinstance(e.detail, HaltLiftedDetail)
    assert e.detail.halt_type == "daily_drawdown"
    assert e.detail.current_drawdown_pct == 2.0
    assert e.detail.lifted_at == _AT


def test_simultaneous_daily_and_cumulative_emits_two_entries() -> None:
    tracker = HaltTransitionTracker()
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )
    entries = tuple(
        tracker.observe(
            halt_state=halt,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=lambda c: _entry_id("e", _AT, c),
            current_cumulative_drawdown_pct=12.5,
            cumulative_full_halt_limit_pct=12.0,
        )
    )
    assert len(entries) == 2
    halt_types = sorted(e.detail.halt_type for e in entries)
    assert halt_types == ["cumulative_drawdown_tier3", "daily_drawdown"]
    cumulative = next(e for e in entries if e.detail.halt_type == "cumulative_drawdown_tier3")
    assert isinstance(cumulative.detail, HaltActivatedDetail)
    assert cumulative.detail.current_drawdown_pct == 12.5
    assert cumulative.detail.limit_pct == 12.0


def test_independent_daily_lift_emits_lifted_while_cumulative_persists() -> None:
    """If daily lifts but cumulative full-halt persists, only daily's lift event fires."""
    tracker = HaltTransitionTracker()
    both = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )
    factory = lambda c: _entry_id("e", _AT, c)  # noqa: E731

    tuple(
        tracker.observe(
            halt_state=both,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
            current_cumulative_drawdown_pct=12.5,
        )
    )

    cum_only = HaltState(
        daily_halt_active=False,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=2.0,
        daily_drawdown_limit_pct=5.0,
    )
    entries = tuple(
        tracker.observe(
            halt_state=cum_only,
            invocation_id="mon-1",
            at=_AT,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=factory,
            current_cumulative_drawdown_pct=12.5,
        )
    )
    assert len(entries) == 1
    assert entries[0].event_type is EventType.HALT_LIFTED
    assert entries[0].detail.halt_type == "daily_drawdown"
