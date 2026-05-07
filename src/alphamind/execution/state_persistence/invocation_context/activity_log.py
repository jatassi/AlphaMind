"""Activity-log emission helper + SQL row codec (story 03 / ALP-357).

Two adapters round-trip without loss between the typed
``ActivityLogEntry`` and its ``ActivityLogRow`` storage shape — the read
APIs in ``repository.activity_log_queries`` import the inverse adapter
to rehydrate rows back into typed entries.

``append_activity_log_entry`` joins the ``InvocationContext`` substrate
from story 02b: it adds the row to the open async session; the
surrounding context commits on clean exit and rolls back on exception.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.tables.activity_log import ActivityLogRow
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
)


def _entry_at_to_iso(timestamp: datetime) -> str:
    """Render a tz-aware UTC datetime as ISO 8601 with ``Z`` suffix."""
    return timestamp.isoformat().replace("+00:00", "Z")


def _entry_at_from_iso(text: str) -> datetime:
    """Parse the ``Z``-suffixed ISO 8601 form ``_entry_at_to_iso`` produces."""
    return datetime.fromisoformat(text)


def activity_log_entry_to_row(entry: ActivityLogEntry) -> ActivityLogRow:
    """Build an ``ActivityLogRow`` from a typed entry.

    Uses the per-event-type detail class' ``model_dump_json()`` so any
    rehydration via the matching detail class is faithful.
    """
    return ActivityLogRow(
        entry_id=entry.entry_id,
        invocation_id=entry.invocation_id,
        entry_at=_entry_at_to_iso(entry.timestamp),
        event_type=entry.event_type.value,
        event_group=entry.event_group.value,
        position_id=entry.position_id,
        order_id=entry.order_id,
        thesis_id=entry.thesis_id,
        source=entry.source.value,
        detail_json=entry.detail.model_dump_json(),
    )


def activity_log_entry_from_row(row: ActivityLogRow) -> ActivityLogEntry:
    """Rehydrate a row into a typed ``ActivityLogEntry``.

    The detail payload is parsed through the per-event-type detail class
    looked up via ``EVENT_TYPE_TO_DETAIL_CLASS`` — discriminated dispatch
    on ``event_type``.
    """
    event_type = EventType(row.event_type)
    # The catalog dict is typed ``dict[EventType, type]`` (i.e. any class)
    # in the source-of-truth module; every value is in fact a Pydantic
    # ``BaseModel`` subclass, so we narrow at the use site rather than
    # editing the source-of-truth annotation.
    detail_cls: type[BaseModel] = EVENT_TYPE_TO_DETAIL_CLASS[event_type]
    return ActivityLogEntry(
        entry_id=row.entry_id,
        invocation_id=row.invocation_id,
        timestamp=_entry_at_from_iso(row.entry_at),
        event_type=event_type,
        event_group=EVENT_TYPE_TO_GROUP[event_type],
        position_id=row.position_id,
        order_id=row.order_id,
        thesis_id=row.thesis_id,
        source=EventSource(row.source),
        detail=detail_cls.model_validate_json(row.detail_json),
    )


async def append_activity_log_entry(
    handle: InvocationHandle,
    entry: ActivityLogEntry,
) -> None:
    """Persist one activity-log entry inside the open ``InvocationContext`` transaction.

    Validates ``entry.invocation_id`` matches ``handle.invocation_id`` and
    refuses cross-invocation appends. The row is added to the session but
    NOT committed; the surrounding ``InvocationContext`` commits on clean
    exit and rolls back on exception.
    """
    if entry.invocation_id != handle.invocation_id:
        msg = (
            f"activity-log entry invocation_id={entry.invocation_id!r} "
            f"does not match the open InvocationContext "
            f"invocation_id={handle.invocation_id!r}"
        )
        raise ValueError(msg)
    handle.session.add(activity_log_entry_to_row(entry))
