"""Event-calendar storage, loader, lookup, and staleness reporter (story 05).

The operator curates ``config/event_calendar.yaml`` quarterly with the
scheduled high-impact catalysts (FOMC, CPI/PPI/PCE, NFP, major earnings
clusters). The pre-event tightening overlay (story 06a) consumes the typed
``EventCalendar`` produced by ``load_event_calendar`` and queries the upcoming
window with ``select_events_within_window``. The orchestrator (story 09)
surfaces ``warn_on_stale_calendar``'s report through its audit log so the
operator notices when the calendar is about to lapse.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/05-event-calendar-loader.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from alphamind.config.models.overlays import EventType
from alphamind.risk_guardrails.regime_adaptation.types import (
    EventCalendar,
    EventCalendarEntry,
    StaleCalendarReport,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DEFAULT_STALE_THRESHOLD: timedelta = timedelta(days=7)
"""Operator-warning threshold: if the latest calendar entry is closer than
this to ``now_utc``, the calendar is considered stale.

Mirrors the typical operator quarterly-review cadence — one week of buffer to
catch the calendar before it lapses. Per
``feedback_avoid_numeric_anchors.md``, this is named, documented, and
configurable via ``warn_on_stale_calendar``'s ``stale_threshold`` argument."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EventCalendarParseError(ValueError):
    """Raised when the event-calendar YAML is missing, malformed, or invalid.

    Wraps the underlying exception's message and includes the file path so the
    operator can locate the bad file from the audit log.
    """


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class EventCalendarFileEntry(BaseModel):
    """One entry as parsed from ``config/event_calendar.yaml``.

    The runtime contract is ``EventCalendarEntry`` (story 02); this Pydantic
    record exists solely to apply the YAML-validation pattern used throughout
    ``alphamind.config.models``.
    """

    model_config = ConfigDict(frozen=True)

    event_type: EventType
    event_timestamp_utc: datetime
    label: str = Field(min_length=1)

    @field_validator("event_timestamp_utc")
    @classmethod
    def _require_tz_aware_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "event_timestamp_utc")
        return value


class EventCalendarFile(BaseModel):
    """Pydantic model for ``config/event_calendar.yaml``."""

    model_config = ConfigDict(frozen=True)

    entries: list[EventCalendarFileEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_event_calendar(path: Path) -> EventCalendar:
    """Parse the YAML at ``path`` and return the typed ``EventCalendar``.

    Raises ``EventCalendarParseError`` (a ``ValueError`` subclass) if the file
    is missing, malformed, fails Pydantic validation, or contains a duplicate
    ``(event_type, event_timestamp_utc)`` pair. Entries are sorted ascending
    by ``event_timestamp_utc``.
    """
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        msg = f"Event-calendar file not found: {path}"
        raise EventCalendarParseError(msg) from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        msg = f"Event-calendar file malformed: {path}: {exc}"
        raise EventCalendarParseError(msg) from exc

    try:
        parsed = EventCalendarFile.model_validate(raw)
    except ValidationError as exc:
        msg = f"Event-calendar file failed validation: {path}: {exc}"
        raise EventCalendarParseError(msg) from exc

    sorted_entries = sorted(parsed.entries, key=lambda entry: entry.event_timestamp_utc)

    seen: set[tuple[EventType, datetime]] = set()
    adapted: list[EventCalendarEntry] = []
    for entry in sorted_entries:
        key = (entry.event_type, entry.event_timestamp_utc)
        if key in seen:
            msg = (
                f"Event-calendar file contains duplicate "
                f"(event_type={entry.event_type.value}, "
                f"event_timestamp_utc={entry.event_timestamp_utc.isoformat()}) "
                f"entry: {path}"
            )
            raise EventCalendarParseError(msg)
        seen.add(key)
        adapted.append(
            EventCalendarEntry(
                event_type=entry.event_type,
                event_timestamp_utc=entry.event_timestamp_utc,
                label=entry.label,
            )
        )

    return EventCalendar(entries=tuple(adapted))


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


def select_events_within_window(
    *,
    calendar: EventCalendar,
    now_utc: datetime,
    window_end_utc: datetime,
) -> tuple[EventCalendarEntry, ...]:
    """Return calendar entries whose timestamp falls in ``[now_utc, window_end_utc)``.

    The half-open interval mirrors Python's standard convention: the lower
    bound is inclusive, the upper bound is exclusive. Result is sorted
    ascending by ``event_timestamp_utc``.

    Raises ``ValueError`` if either timestamp is naive or non-UTC, or if
    ``window_end_utc <= now_utc``.
    """
    _require_utc(now_utc, "now_utc")
    _require_utc(window_end_utc, "window_end_utc")
    if window_end_utc <= now_utc:
        msg = (
            f"window_end_utc must be strictly greater than now_utc; got "
            f"now_utc={now_utc.isoformat()}, "
            f"window_end_utc={window_end_utc.isoformat()}"
        )
        raise ValueError(msg)

    return tuple(
        sorted(
            (
                entry
                for entry in calendar.entries
                if now_utc <= entry.event_timestamp_utc < window_end_utc
            ),
            key=lambda entry: entry.event_timestamp_utc,
        )
    )


# ---------------------------------------------------------------------------
# Staleness reporter
# ---------------------------------------------------------------------------


def warn_on_stale_calendar(
    *,
    calendar: EventCalendar,
    now_utc: datetime,
    stale_threshold: timedelta = _DEFAULT_STALE_THRESHOLD,
) -> StaleCalendarReport:
    """Inspect ``calendar`` and report whether it is about to lapse.

    The calendar is stale when its latest entry is in the past or fewer than
    ``stale_threshold`` ahead of ``now_utc``. An empty calendar is reported
    as ``is_stale=False, latest_event_timestamp_utc=None,
    days_until_latest=None`` — there is nothing to be stale.

    This function does not raise; the orchestrator (story 09) surfaces the
    report through its audit-log entries and continues running.
    """
    _require_utc(now_utc, "now_utc")
    if not calendar.entries:
        return StaleCalendarReport(
            is_stale=False,
            latest_event_timestamp_utc=None,
            days_until_latest=None,
        )

    latest_timestamp = max(entry.event_timestamp_utc for entry in calendar.entries)
    delta = latest_timestamp - now_utc
    days_until_latest = delta.total_seconds() / 86400.0
    is_stale = delta < stale_threshold
    return StaleCalendarReport(
        is_stale=is_stale,
        latest_event_timestamp_utc=latest_timestamp,
        days_until_latest=days_until_latest,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_utc(value: datetime, field_name: str) -> None:
    """Reject naive or non-UTC timestamps with a ``ValueError``."""
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)
    if offset.total_seconds() != 0:
        msg = f"{field_name} must be UTC (offset 00:00); got {value.isoformat()}"
        raise ValueError(msg)
