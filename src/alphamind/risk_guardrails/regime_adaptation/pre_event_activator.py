"""Pre-event overlay activator (story 06a).

Decides whether the ``pre_event`` overlay is active for the current invocation
by walking the scheduler's upcoming firings, counting how many fall before
each calendar event in the look-ahead window, and matching against the
overlay's ``windows_before_event`` knob.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/06a-pre-event-overlay-activator.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from alphamind.config.models.overlays import Overlay, PreEventOverlay
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.risk_guardrails.regime_adaptation.types import (
    EventCalendar,
    EventCalendarEntry,
    OverlayActivationDecision,
)


def evaluate_pre_event_overlay(
    *,
    now_utc: datetime,
    event_calendar: EventCalendar,
    scheduler_config: SchedulerConfig,
    pre_event_overlay: PreEventOverlay,
) -> OverlayActivationDecision:
    """Decide whether the pre-event overlay is active for this invocation.

    Walks the calendar entries whose ``event_type`` is in the overlay's
    ``activation.events`` set. For each, counts the upcoming scheduled firings
    in ``[now_utc, event_timestamp_utc)``:

    - 0 firings → ``is_active=True``, ``pre_event_block_new_positions=True``
      (the current invocation *is* the final firing before the event).
    - ``1..(windows_before_event - 1)`` firings → ``is_active=True``,
      ``pre_event_block_new_positions=False``.
    - ``>= windows_before_event`` firings → outside the window; continue.

    The first activating event wins. No activating event → inactive.
    """
    relevant_event_types = frozenset(pre_event_overlay.activation.events)
    windows_before_event = pre_event_overlay.activation.windows_before_event

    upcoming_events = sorted(
        (
            entry
            for entry in event_calendar.entries
            if entry.event_type in relevant_event_types and entry.event_timestamp_utc > now_utc
        ),
        key=lambda entry: entry.event_timestamp_utc,
    )
    if not upcoming_events:
        return _inactive_decision()

    upcoming_firings = _compute_upcoming_firings(
        now_utc=now_utc,
        scheduler_config=scheduler_config,
        horizon_count=windows_before_event + 1,
    )

    for entry in upcoming_events:
        firings_before = sum(1 for firing in upcoming_firings if firing < entry.event_timestamp_utc)
        if firings_before >= windows_before_event:
            continue
        return _active_decision(
            event=entry,
            firings_before_event=firings_before,
            windows_before_event=windows_before_event,
        )

    return _inactive_decision()


def _compute_upcoming_firings(
    *,
    now_utc: datetime,
    scheduler_config: SchedulerConfig,
    horizon_count: int,
) -> tuple[datetime, ...]:
    """Return the next ``horizon_count`` firings across all triggers, sorted ascending.

    Cron expressions are interpreted in ``scheduler_config.timezone`` and the
    resulting firings are converted to UTC. Duplicates (multiple triggers
    firing at the same UTC instant) collapse to one firing.

    The result is the *unioned* next ``horizon_count`` firings across all
    triggers — not the per-trigger next ``horizon_count``. Trigger A's
    ``horizon_count``-th firing may land later than trigger B's, and we keep
    only the earliest ``horizon_count`` firings overall. Each trigger
    contributes its own next ``horizon_count`` candidates so the merged top-N
    never undercounts when triggers fire densely.
    """
    cron_tz = ZoneInfo(scheduler_config.timezone)
    start_local = now_utc.astimezone(cron_tz)

    candidates: set[datetime] = set()
    for cron_expression in scheduler_config.triggers.values():
        iterator = croniter(cron_expression, start_local)
        for _ in range(horizon_count):
            firing_local = iterator.get_next(datetime)
            candidates.add(firing_local.astimezone(UTC))

    return tuple(sorted(candidates)[:horizon_count])


def _inactive_decision() -> OverlayActivationDecision:
    return OverlayActivationDecision(
        overlay=Overlay.pre_event,
        is_active=False,
        rationale="",
        pre_event_block_new_positions=False,
    )


def _active_decision(
    *,
    event: EventCalendarEntry,
    firings_before_event: int,
    windows_before_event: int,
) -> OverlayActivationDecision:
    if firings_before_event == 0:
        rationale = (
            f"Pre-event window: final invocation before {event.label} "
            f"(event at {event.event_timestamp_utc.isoformat()})"
        )
        return OverlayActivationDecision(
            overlay=Overlay.pre_event,
            is_active=True,
            rationale=rationale,
            pre_event_block_new_positions=True,
        )

    ordinal = firings_before_event + 1
    rationale = (
        f"Pre-event window: invocation T-{ordinal} of {windows_before_event} "
        f"preceding {event.label}"
    )
    return OverlayActivationDecision(
        overlay=Overlay.pre_event,
        is_active=True,
        rationale=rationale,
        pre_event_block_new_positions=False,
    )
