"""Pure-function filtering and projection helpers for activity log entries."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
)

# ---------------------------------------------------------------------------
# Single-dimension filters
# ---------------------------------------------------------------------------


def filter_by_invocation_id(
    entries: tuple[ActivityLogEntry, ...],
    invocation_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose invocation_id exactly matches."""
    return tuple(e for e in entries if e.invocation_id == invocation_id)


def filter_by_event_type(
    entries: tuple[ActivityLogEntry, ...],
    event_type: EventType,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_type exactly matches."""
    return tuple(e for e in entries if e.event_type == event_type)


def filter_by_event_types(
    entries: tuple[ActivityLogEntry, ...],
    event_types: frozenset[EventType],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_type is in event_types (union)."""
    return tuple(e for e in entries if e.event_type in event_types)


def filter_by_event_group(
    entries: tuple[ActivityLogEntry, ...],
    event_group: EventGroup,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_group exactly matches."""
    return tuple(e for e in entries if e.event_group == event_group)


def filter_by_source(
    entries: tuple[ActivityLogEntry, ...],
    source: EventSource,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose source exactly matches."""
    return tuple(e for e in entries if e.source == source)


def filter_by_position_id(
    entries: tuple[ActivityLogEntry, ...],
    position_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose position_id matches; entries with position_id None are excluded."""
    return tuple(e for e in entries if e.position_id == position_id)


def filter_by_order_id(
    entries: tuple[ActivityLogEntry, ...],
    order_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose order_id exactly matches; entries with order_id None are excluded."""
    return tuple(e for e in entries if e.order_id == order_id)


def filter_by_thesis_id(
    entries: tuple[ActivityLogEntry, ...],
    thesis_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose thesis_id exactly matches; entries with thesis_id None are excluded."""
    return tuple(e for e in entries if e.thesis_id == thesis_id)


def filter_by_timestamp_range(
    entries: tuple[ActivityLogEntry, ...],
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries in the half-open interval [start, end).

    Both start and end must be tz-aware UTC or None.  Raises ValueError on
    mixed tz-aware / tz-naive inputs.  A reversed range (start > end) returns
    an empty tuple.
    """
    if (
        start is not None
        and end is not None
        and (start.tzinfo is not None) != (end.tzinfo is not None)
    ):
        msg = "start and end must both be tz-aware or both be tz-naive"
        raise ValueError(msg)

    return tuple(
        e
        for e in entries
        if (start is None or e.timestamp >= start) and (end is None or e.timestamp < end)
    )


# ---------------------------------------------------------------------------
# Category-5 projection helpers
# ---------------------------------------------------------------------------


def intra_invocation_changelog(
    entries: tuple[ActivityLogEntry, ...],
    invocation_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries scoped to a single invocation (category-5a)."""
    return filter_by_invocation_id(entries, invocation_id)


def pm_decision_log(
    entries: tuple[ActivityLogEntry, ...],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries with event_type PM_DECISION (category-5b)."""
    return filter_by_event_type(entries, EventType.PM_DECISION)


def position_modification_trail(
    entries: tuple[ActivityLogEntry, ...],
    position_ids: tuple[str, ...],
) -> dict[str, tuple[ActivityLogEntry, ...]]:
    """Return per-position entry tuples for each requested position_id (category-5c).

    Position IDs with no matching entries are absent from the result dict.
    Empty position_ids returns {}.
    """
    if not position_ids:
        return {}
    result: dict[str, tuple[ActivityLogEntry, ...]] = {}
    for pid in position_ids:
        matching = filter_by_position_id(entries, pid)
        if matching:
            result[pid] = matching
    return result


def recent_pm_decisions_for_position(
    entries: tuple[ActivityLogEntry, ...],
    position_id: str,
    limit: int,
) -> tuple[ActivityLogEntry, ...]:
    """Return the last ``limit`` PM_DECISION entries for a specific position.

    Assumes chronological input order.  Raises ValueError for limit <= 0.
    """
    if limit <= 0:
        msg = "limit must be a positive integer"
        raise ValueError(msg)
    matching = filter_by_position_id(pm_decision_log(entries), position_id)
    return matching[-limit:]


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------


def partition_by_event_group(
    entries: tuple[ActivityLogEntry, ...],
) -> dict[EventGroup, tuple[ActivityLogEntry, ...]]:
    """Group entries by event_group in a single pass.

    Groups with no entries are absent from the result.
    """
    groups: defaultdict[EventGroup, list[ActivityLogEntry]] = defaultdict(list)
    for e in entries:
        groups[e.event_group].append(e)
    return {g: tuple(es) for g, es in groups.items()}


def chronological_sort(
    entries: tuple[ActivityLogEntry, ...],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries sorted by timestamp ascending; ties broken by entry_id ascending."""
    return tuple(sorted(entries, key=lambda e: (e.timestamp, e.entry_id)))
