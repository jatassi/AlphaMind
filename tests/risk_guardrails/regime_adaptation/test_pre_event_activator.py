"""Tests for ``pre_event_activator`` — story 06a.

The pure function ``evaluate_pre_event_overlay`` decides whether the pre-event
overlay is active for the current invocation by walking the scheduler's
upcoming firings and counting how many fall before each calendar event.

Activation contract (from
``docs/design/06-risk-guardrails/regime-adaptation.md`` § Scheduled high-impact
events):

- The overlay applies for the ``windows_before_event`` invocations preceding
  the event (default 2).
- The final invocation before the event also blocks new positions.
- The overlay lifts at the first invocation after the event (no special code
  path; falls out of the activation rules).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.config.models.overlays import EventType, Overlay, PreEventOverlay
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.risk_guardrails.regime_adaptation import (
    EventCalendar,
    EventCalendarEntry,
    OverlayActivationDecision,
)
from alphamind.risk_guardrails.regime_adaptation.pre_event_activator import (
    evaluate_pre_event_overlay,
)


def test_top_level_package_reexports_activator_surface() -> None:
    """Story 06a acceptance criterion: ``evaluate_pre_event_overlay`` is
    re-exported from the package ``__init__.py`` so callers can import it via
    the top-level path.
    """
    import alphamind.risk_guardrails.regime_adaptation as regime_adaptation

    assert regime_adaptation.evaluate_pre_event_overlay is evaluate_pre_event_overlay


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _pre_event_overlay(
    windows_before_event: int = 2,
    events: list[str] | None = None,
) -> PreEventOverlay:
    """Build a pre-event overlay matching ``config/overlays/pre-event.yaml``."""
    payload: dict[str, Any] = {
        "activation": {
            "windows_before_event": windows_before_event,
            "events": events
            if events is not None
            else ["fomc", "cpi", "ppi", "pce", "nfp", "earnings"],
        },
        "multipliers": {
            "position_max_size_pct": 0.80,
        },
        "final_invocation_before_event": {
            "block_new_positions": True,
        },
    }
    return PreEventOverlay.model_validate(payload)


def _scheduler_config(
    *,
    timezone: str = "US/Eastern",
    triggers: dict[str, str] | None = None,
) -> SchedulerConfig:
    """Build a scheduler config matching ``config/scheduler.yaml`` by default."""
    payload: dict[str, Any] = {
        "timezone": timezone,
        "max_instances": 1,
        "overlap_dedup_lookback_minutes": 30,
        "triggers": triggers
        if triggers is not None
        else {
            "market_hours_rolling": "30 9,11,13,15 * * mon-fri",
            "off_hours_rolling": "0 0,4,8,20 * * mon-fri",
            "pre_open": "0 9 * * mon-fri",
            "pre_close": "30 15 * * mon-fri",
            "weekend_saturday": "0 10 * * sat",
            "weekend_sunday": "0 18 * * sun",
        },
    }
    return SchedulerConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# Empty calendar
# ---------------------------------------------------------------------------


def test_empty_calendar_returns_inactive() -> None:
    """No upcoming events → no activation, no rationale."""
    calendar = EventCalendar(entries=())
    now_utc = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=_scheduler_config(),
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision == OverlayActivationDecision(
        overlay=Overlay.pre_event,
        is_active=False,
        rationale="",
        pre_event_block_new_positions=False,
    )


# ---------------------------------------------------------------------------
# Final invocation before event (T-1 case)
# ---------------------------------------------------------------------------


def test_final_invocation_before_event_activates_with_block_new() -> None:
    """Zero firings between now and event → final invocation, block new positions.

    Synthetic scheduler with one trigger at 20:00 UTC daily. ``now_utc`` sits
    just before the event; the next firing is well after the event.
    """
    event_timestamp = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=event_timestamp,
                label="FOMC June 2026",
            ),
        )
    )
    now_utc = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={"daily_evening": "0 20 * * *"},
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.overlay == Overlay.pre_event
    assert decision.is_active is True
    assert decision.pre_event_block_new_positions is True
    assert "final invocation before" in decision.rationale
    assert "FOMC June 2026" in decision.rationale


# ---------------------------------------------------------------------------
# T-2 case (one firing before event, window=2)
# ---------------------------------------------------------------------------


def test_t_minus_2_invocation_activates_without_block_new() -> None:
    """One firing between now and event → T-2 of a windows=2 overlay.

    Synthetic schedule with a firing at 17:30 UTC and 20:00 UTC daily. The
    event is at 18:00 UTC. ``now_utc=15:30Z`` yields one firing
    (17:30 UTC) in [15:30, 18:00). With ``windows_before_event=2`` this is
    the T-2 step (active, block_new=False).
    """
    event_timestamp = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=event_timestamp,
                label="FOMC June 2026",
            ),
        )
    )
    now_utc = datetime(2026, 6, 17, 15, 30, tzinfo=UTC)
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={
            "afternoon": "30 17 * * *",
            "evening": "0 20 * * *",
        },
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.overlay == Overlay.pre_event
    assert decision.is_active is True
    assert decision.pre_event_block_new_positions is False
    assert "T-2" in decision.rationale
    assert "FOMC June 2026" in decision.rationale


# ---------------------------------------------------------------------------
# Outside the window (T-3 case with windows=2)
# ---------------------------------------------------------------------------


def test_two_firings_before_event_outside_window_returns_inactive() -> None:
    """Two firings between now and event with windows=2 → outside the window."""
    event_timestamp = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=event_timestamp,
                label="FOMC June 2026",
            ),
        )
    )
    now_utc = datetime(2026, 6, 17, 13, 0, tzinfo=UTC)
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={
            "afternoon_a": "0 15 * * *",  # 15:00 UTC firing
            "afternoon_b": "0 17 * * *",  # 17:00 UTC firing
        },
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is False
    assert decision.pre_event_block_new_positions is False


# ---------------------------------------------------------------------------
# Event well outside window (5 days ahead, production schedule)
# ---------------------------------------------------------------------------


def test_event_well_outside_window_returns_inactive() -> None:
    """A FOMC event 5 days ahead is well past the look-ahead window."""
    now_utc = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)  # Monday noon UTC
    event_timestamp = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)  # 5 days later
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=event_timestamp,
                label="FOMC future",
            ),
        )
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=_scheduler_config(),
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is False
    assert decision.pre_event_block_new_positions is False
    assert decision.rationale == ""


# ---------------------------------------------------------------------------
# Multiple events with one within window
# ---------------------------------------------------------------------------


def test_multiple_events_only_nearer_activates() -> None:
    """Calendar with one within-window event and one far-away event activates
    on the within-window event."""
    now_utc = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    near_event = EventCalendarEntry(
        event_type=EventType.fomc,
        event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
        label="FOMC June 2026",
    )
    far_event = EventCalendarEntry(
        event_type=EventType.cpi,
        event_timestamp_utc=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        label="CPI July 2026",
    )
    calendar = EventCalendar(entries=(near_event, far_event))
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={"daily_evening": "0 20 * * *"},
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is True
    assert decision.pre_event_block_new_positions is True
    assert "FOMC June 2026" in decision.rationale
    assert "CPI July 2026" not in decision.rationale


# ---------------------------------------------------------------------------
# Multiple events all within window — earliest wins
# ---------------------------------------------------------------------------


def test_multiple_events_within_window_earliest_wins() -> None:
    """Two events both within the window — the earlier one drives the rationale."""
    now_utc = datetime(2026, 6, 17, 15, 0, tzinfo=UTC)
    earlier = EventCalendarEntry(
        event_type=EventType.fomc,
        event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
        label="FOMC June",
    )
    later = EventCalendarEntry(
        event_type=EventType.cpi,
        event_timestamp_utc=datetime(2026, 6, 17, 22, 0, tzinfo=UTC),
        label="CPI June",
    )
    # Pass the later event first to confirm sort-by-timestamp behavior.
    calendar = EventCalendar(entries=(later, earlier))
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={"hourly_evening": "0 19 * * *"},  # 19:00 UTC daily
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is True
    # The earlier event is FOMC at 18:00. With now=15:00 and next firing at
    # 19:00 (after the event), zero firings precede the FOMC event → final.
    assert decision.pre_event_block_new_positions is True
    assert "FOMC June" in decision.rationale
    assert "CPI June" not in decision.rationale


# ---------------------------------------------------------------------------
# Event type not in activation.events
# ---------------------------------------------------------------------------


def test_event_type_not_in_activation_events_does_not_activate() -> None:
    """Calendar has an NFP event but the overlay only activates on [fomc, cpi]."""
    now_utc = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.nfp,
                event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
                label="NFP June 2026",
            ),
        )
    )
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={"daily_evening": "0 20 * * *"},
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(events=["fomc", "cpi"]),
    )

    assert decision.is_active is False
    assert decision.pre_event_block_new_positions is False


# ---------------------------------------------------------------------------
# Past and at-now edge cases
# ---------------------------------------------------------------------------


def test_past_event_does_not_activate() -> None:
    """Event 1 day in the past → activator does not look backwards."""
    now_utc = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=datetime(2026, 6, 16, 17, 30, tzinfo=UTC),
                label="FOMC yesterday",
            ),
        )
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=_scheduler_config(),
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is False
    assert decision.pre_event_block_new_positions is False
    assert decision.rationale == ""


def test_function_is_pure_equal_inputs_produce_equal_outputs() -> None:
    """Identical inputs yield identical outputs across repeated calls."""
    now_utc = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
                label="FOMC June 2026",
            ),
        )
    )
    scheduler = _scheduler_config(
        timezone="UTC",
        triggers={"daily_evening": "0 20 * * *"},
    )
    overlay = _pre_event_overlay()

    first = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=overlay,
    )
    second = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=overlay,
    )

    assert first == second


def test_cron_firings_converted_from_scheduler_timezone_to_utc() -> None:
    """Schedule in US/Eastern; verify firing comparisons are UTC-aligned.

    Wednesday 2026-06-17 in EDT (UTC-4). Schedule has a single trigger at
    14:00 ET = 18:00 UTC. The event is 17:30 UTC (i.e., 13:30 ET, before the
    firing). With ``now_utc=12:00`` (08:00 ET on Wednesday), the next firing
    is 18:00 UTC — *after* the event. Zero firings precede the event →
    activates as final invocation.

    A naive interpretation that treats the cron as UTC would put the firing
    at 14:00 UTC (10:00 ET), which would be *before* the event and produce
    is_active=True / block_new_positions=False — a different outcome. This
    test pins the timezone-correct path.
    """
    now_utc = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
    event_timestamp = datetime(2026, 6, 17, 17, 30, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=event_timestamp,
                label="FOMC June 2026",
            ),
        )
    )
    scheduler = _scheduler_config(
        timezone="US/Eastern",
        triggers={"afternoon_et": "0 14 * * *"},  # 14:00 ET → 18:00 UTC in summer
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=scheduler,
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is True
    assert decision.pre_event_block_new_positions is True
    assert "final invocation before" in decision.rationale


def test_event_exactly_at_now_does_not_activate() -> None:
    """Event timestamp equals ``now_utc`` → empty interval, no activation."""
    now_utc = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=now_utc,
                label="FOMC at now",
            ),
        )
    )

    decision = evaluate_pre_event_overlay(
        now_utc=now_utc,
        event_calendar=calendar,
        scheduler_config=_scheduler_config(),
        pre_event_overlay=_pre_event_overlay(),
    )

    assert decision.is_active is False
    assert decision.pre_event_block_new_positions is False
    assert decision.rationale == ""
