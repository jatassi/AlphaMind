"""Tests for the event-calendar storage and loader (story 05).

Covers the Pydantic schema, the YAML loader, the half-open window query, and
the staleness reporter declared in
``docs/implementation/06-risk-guardrails/regime-adaptation/05-event-calendar-loader.md``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alphamind.config.models.overlays import EventType
from alphamind.risk_guardrails.regime_adaptation import (
    EventCalendar,
    EventCalendarEntry,
    StaleCalendarReport,
)
from alphamind.risk_guardrails.regime_adaptation.event_calendar import (
    _DEFAULT_STALE_THRESHOLD,
    EventCalendarFile,
    EventCalendarFileEntry,
    EventCalendarParseError,
    load_event_calendar,
    select_events_within_window,
    warn_on_stale_calendar,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SHIPPED_EVENT_CALENDAR_PATH = REPO_ROOT / "config" / "event_calendar.yaml"


# ---------------------------------------------------------------------------
# Loader: shipped file
# ---------------------------------------------------------------------------


def test_shipped_event_calendar_parses_empty() -> None:
    """The shipped ``config/event_calendar.yaml`` parses into an empty calendar."""
    calendar = load_event_calendar(SHIPPED_EVENT_CALENDAR_PATH)
    assert calendar == EventCalendar(entries=())


def test_single_entry_round_trips(tmp_path: Path) -> None:
    """A YAML fixture with one FOMC entry produces the expected typed record."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00Z\n"
        '    label: "FOMC June 2026"\n'
    )
    calendar = load_event_calendar(path)
    assert calendar.entries == (
        EventCalendarEntry(
            event_type=EventType.fomc,
            event_timestamp_utc=datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
            label="FOMC June 2026",
        ),
    )


def test_multiple_entries_sorted_ascending_by_timestamp(tmp_path: Path) -> None:
    """Three entries in scrambled order sort ascending by timestamp."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: cpi\n"
        "    event_timestamp_utc: 2026-05-13T12:30:00Z\n"
        '    label: "CPI April 2026"\n'
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-04-29T18:00:00Z\n"
        '    label: "FOMC April 2026"\n'
        "  - event_type: nfp\n"
        "    event_timestamp_utc: 2026-06-05T12:30:00Z\n"
        '    label: "NFP May 2026"\n'
    )
    calendar = load_event_calendar(path)
    timestamps = [entry.event_timestamp_utc for entry in calendar.entries]
    assert timestamps == sorted(timestamps)
    assert [entry.event_type for entry in calendar.entries] == [
        EventType.fomc,
        EventType.cpi,
        EventType.nfp,
    ]


# ---------------------------------------------------------------------------
# Loader: validation errors
# ---------------------------------------------------------------------------


def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    """``event_timestamp_utc`` without a timezone raises ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00\n"
        '    label: "FOMC June 2026"\n'
    )
    with pytest.raises(EventCalendarParseError, match="event_timestamp_utc"):
        load_event_calendar(path)


def test_non_utc_timestamp_rejected(tmp_path: Path) -> None:
    """``event_timestamp_utc`` with a non-zero offset raises ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00-05:00\n"
        '    label: "FOMC June 2026"\n'
    )
    with pytest.raises(EventCalendarParseError, match="UTC"):
        load_event_calendar(path)


def test_empty_label_rejected(tmp_path: Path) -> None:
    """An entry whose ``label`` is empty raises ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00Z\n"
        '    label: ""\n'
    )
    with pytest.raises(EventCalendarParseError):
        load_event_calendar(path)


def test_unknown_event_type_rejected(tmp_path: Path) -> None:
    """An ``event_type`` outside the ``EventType`` enum raises ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: unknown\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00Z\n"
        '    label: "unknown event"\n'
    )
    with pytest.raises(EventCalendarParseError):
        load_event_calendar(path)


def test_duplicate_event_type_and_timestamp_rejected(tmp_path: Path) -> None:
    """Two entries with the same ``(event_type, timestamp)`` raise ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    path.write_text(
        "entries:\n"
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00Z\n"
        '    label: "FOMC June 2026 (a)"\n'
        "  - event_type: fomc\n"
        "    event_timestamp_utc: 2026-06-17T18:00:00Z\n"
        '    label: "FOMC June 2026 (b)"\n'
    )
    with pytest.raises(EventCalendarParseError, match="duplicate"):
        load_event_calendar(path)


def test_missing_file_raises_with_path(tmp_path: Path) -> None:
    """A non-existent path raises ``EventCalendarParseError`` mentioning the path.

    ``re.escape`` keeps Windows ``C:\\Users\\...``-style paths from being
    interpreted as regex escapes (e.g. ``\\U`` would otherwise raise
    ``re.error: incomplete escape``).
    """
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(EventCalendarParseError, match=re.escape(str(missing))):
        load_event_calendar(missing)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    """A file whose YAML is unparseable raises ``EventCalendarParseError``."""
    path = tmp_path / "event_calendar.yaml"
    # Unclosed flow-style mapping is unambiguously malformed.
    path.write_text("entries: [{event_type: fomc,")
    with pytest.raises(EventCalendarParseError):
        load_event_calendar(path)


# ---------------------------------------------------------------------------
# Lookup API: select_events_within_window
# ---------------------------------------------------------------------------


def _entry(offset_minutes: int, *, event_type: EventType = EventType.fomc) -> EventCalendarEntry:
    """Return an ``EventCalendarEntry`` at ``T0 + offset_minutes``.

    Tests use ``T0`` (2026-04-29T00:00Z) plus minute offsets so timing
    relations are obvious from the call site.
    """
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    return EventCalendarEntry(
        event_type=event_type,
        event_timestamp_utc=base + timedelta(minutes=offset_minutes),
        label=f"event at +{offset_minutes}m",
    )


def test_select_within_window_returns_inner_event_only() -> None:
    """Window ``[T+15, T+25)`` returns only the event at ``T+20``."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            _entry(10, event_type=EventType.fomc),
            _entry(20, event_type=EventType.cpi),
            _entry(30, event_type=EventType.nfp),
        )
    )
    selected = select_events_within_window(
        calendar=calendar,
        now_utc=base + timedelta(minutes=15),
        window_end_utc=base + timedelta(minutes=25),
    )
    assert len(selected) == 1
    assert selected[0].event_type == EventType.cpi


def test_select_within_window_lower_bound_inclusive() -> None:
    """An event at exactly ``now_utc`` is included."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    calendar = EventCalendar(entries=(_entry(20),))
    selected = select_events_within_window(
        calendar=calendar,
        now_utc=base + timedelta(minutes=20),
        window_end_utc=base + timedelta(minutes=30),
    )
    assert len(selected) == 1


def test_select_within_window_upper_bound_exclusive() -> None:
    """An event at exactly ``window_end_utc`` is excluded."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    calendar = EventCalendar(entries=(_entry(30),))
    selected = select_events_within_window(
        calendar=calendar,
        now_utc=base + timedelta(minutes=20),
        window_end_utc=base + timedelta(minutes=30),
    )
    assert selected == ()


def test_select_within_window_returns_sorted_tuple() -> None:
    """Result is sorted ascending by ``event_timestamp_utc``."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    calendar = EventCalendar(
        entries=(
            _entry(40, event_type=EventType.fomc),
            _entry(50, event_type=EventType.cpi),
            _entry(45, event_type=EventType.nfp),
        )
    )
    selected = select_events_within_window(
        calendar=calendar,
        now_utc=base + timedelta(minutes=30),
        window_end_utc=base + timedelta(minutes=60),
    )
    timestamps = [entry.event_timestamp_utc for entry in selected]
    assert timestamps == sorted(timestamps)
    assert len(selected) == 3


def test_select_within_window_rejects_naive_now() -> None:
    """Naive ``now_utc`` raises ``ValueError``."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    naive_now = base.replace(tzinfo=None)
    calendar = EventCalendar(entries=())
    with pytest.raises(ValueError, match="now_utc"):
        select_events_within_window(
            calendar=calendar,
            now_utc=naive_now,
            window_end_utc=base + timedelta(minutes=10),
        )


def test_select_within_window_rejects_naive_window_end() -> None:
    """Naive ``window_end_utc`` raises ``ValueError``."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    naive_end = (base + timedelta(minutes=10)).replace(tzinfo=None)
    calendar = EventCalendar(entries=())
    with pytest.raises(ValueError, match="window_end_utc"):
        select_events_within_window(
            calendar=calendar,
            now_utc=base,
            window_end_utc=naive_end,
        )


def test_select_within_window_rejects_window_end_not_after_now() -> None:
    """``window_end_utc <= now_utc`` raises ``ValueError``."""
    base = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    calendar = EventCalendar(entries=())
    with pytest.raises(ValueError, match="window_end_utc"):
        select_events_within_window(
            calendar=calendar,
            now_utc=base + timedelta(minutes=30),
            window_end_utc=base + timedelta(minutes=30),  # equal to now_utc
        )


# ---------------------------------------------------------------------------
# Staleness reporter
# ---------------------------------------------------------------------------


def test_warn_on_stale_calendar_empty() -> None:
    """An empty calendar reports ``is_stale=False, days_until_latest=None``."""
    now = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    report = warn_on_stale_calendar(calendar=EventCalendar(entries=()), now_utc=now)
    assert report == StaleCalendarReport(
        is_stale=False,
        latest_event_timestamp_utc=None,
        days_until_latest=None,
    )


def test_warn_on_stale_calendar_future_calendar_not_stale() -> None:
    """A calendar with the latest entry 30 days ahead is not stale."""
    now = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    latest = now + timedelta(days=30)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=latest,
                label="future event",
            ),
        )
    )
    report = warn_on_stale_calendar(calendar=calendar, now_utc=now)
    assert report.is_stale is False
    assert report.latest_event_timestamp_utc == latest
    assert report.days_until_latest is not None
    assert report.days_until_latest == pytest.approx(30.0)


def test_warn_on_stale_calendar_near_lapse_is_stale() -> None:
    """A calendar with the latest entry inside the 7-day threshold is stale."""
    now = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    latest = now + timedelta(days=5)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.cpi,
                event_timestamp_utc=latest,
                label="near-future event",
            ),
        )
    )
    report = warn_on_stale_calendar(calendar=calendar, now_utc=now)
    assert report.is_stale is True
    assert report.days_until_latest is not None
    assert report.days_until_latest == pytest.approx(5.0)


def test_warn_on_stale_calendar_past_calendar_is_stale() -> None:
    """A calendar whose latest entry is in the past is stale with negative days."""
    now = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    latest = now - timedelta(days=10)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.nfp,
                event_timestamp_utc=latest,
                label="past event",
            ),
        )
    )
    report = warn_on_stale_calendar(calendar=calendar, now_utc=now)
    assert report.is_stale is True
    assert report.days_until_latest is not None
    assert report.days_until_latest == pytest.approx(-10.0)


def test_warn_on_stale_calendar_uses_latest_entry() -> None:
    """The reporter inspects the maximum timestamp, not the first entry."""
    now = datetime(2026, 4, 29, 0, 0, tzinfo=UTC)
    earliest = now + timedelta(days=2)
    latest = now + timedelta(days=30)
    calendar = EventCalendar(
        entries=(
            EventCalendarEntry(
                event_type=EventType.fomc,
                event_timestamp_utc=earliest,
                label="near event",
            ),
            EventCalendarEntry(
                event_type=EventType.cpi,
                event_timestamp_utc=latest,
                label="far event",
            ),
        )
    )
    report = warn_on_stale_calendar(calendar=calendar, now_utc=now)
    assert report.is_stale is False
    assert report.latest_event_timestamp_utc == latest


def test_default_stale_threshold_is_seven_days() -> None:
    """The module-level default threshold matches the documented 7-day cadence."""
    assert timedelta(days=7) == _DEFAULT_STALE_THRESHOLD


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


def test_loader_symbols_reexported_from_package() -> None:
    """``load_event_calendar``, ``select_events_within_window``,
    ``warn_on_stale_calendar``, and ``EventCalendarParseError`` reach via the
    top-level package surface (story 05 acceptance criterion).
    """
    import alphamind.risk_guardrails.regime_adaptation as package

    assert package.load_event_calendar is load_event_calendar
    assert package.select_events_within_window is select_events_within_window
    assert package.warn_on_stale_calendar is warn_on_stale_calendar
    assert package.EventCalendarParseError is EventCalendarParseError


def test_pydantic_schemas_constructible() -> None:
    """``EventCalendarFile`` and ``EventCalendarFileEntry`` accept their
    documented payloads — exercises the parse-time surface independently of
    the loader.
    """
    parsed = EventCalendarFile.model_validate(
        {
            "entries": [
                {
                    "event_type": "fomc",
                    "event_timestamp_utc": "2026-06-17T18:00:00Z",
                    "label": "FOMC June 2026",
                }
            ]
        }
    )
    assert isinstance(parsed.entries[0], EventCalendarFileEntry)
    assert parsed.entries[0].event_type == EventType.fomc
