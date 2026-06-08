"""Activity-log emission helper + SQL row codec (story 03 / ALP-357).

Two adapters round-trip without loss between the typed
``ActivityLogEntry`` and its ``ActivityLogRow`` storage shape — the read
APIs in ``repository.activity_log_queries`` import the inverse adapter
to rehydrate rows back into typed entries.

``append_activity_log_entry`` joins the ``InvocationContext`` substrate
from story 02b: it adds the row to the open async session; the
surrounding context commits on clean exit and rolls back on exception.

ALP-463: detail payloads switched from Pydantic models to frozen dataclasses;
the codec now uses ``encode_detail`` / ``decode_detail`` from
``portfolio_state.events.codec`` which preserves ``Money`` / ``Price``
precision exactly (Decimal-as-text round-trip mirroring 05b's ``DecimalText``).
"""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    build_activity_log_entry,
    decode_detail,
    encode_detail,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.activity_log import ActivityLogRow


def _entry_at_to_iso(timestamp: datetime) -> str:
    """Render a tz-aware UTC datetime as ISO 8601 with ``Z`` suffix."""
    return timestamp.isoformat().replace("+00:00", "Z")


def _entry_at_from_iso(text: str) -> datetime:
    """Parse the ``Z``-suffixed ISO 8601 form ``_entry_at_to_iso`` produces."""
    return datetime.fromisoformat(text)


def activity_log_entry_to_row(entry: ActivityLogEntry) -> ActivityLogRow:
    """Build an ``ActivityLogRow`` from a typed entry.

    Serializes the per-event-type detail dataclass via ``encode_detail`` so
    ``Money``/``Price`` fields land in storage as Decimal-exact strings.
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
        detail_json=encode_detail(entry.detail),
    )


def activity_log_entry_from_row(row: ActivityLogRow) -> ActivityLogEntry:
    """Rehydrate a row into a typed ``ActivityLogEntry``.

    The detail payload is decoded through the per-event-type detail class
    looked up via ``EVENT_TYPE_TO_DETAIL_CLASS`` — discriminated dispatch on
    ``event_type``.
    """
    event_type = EventType(row.event_type)
    detail_cls = EVENT_TYPE_TO_DETAIL_CLASS[event_type]
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
        detail=decode_detail(row.detail_json, detail_cls),
    )


def append_activity_log_entry(
    handle: InvocationHandle,
    entry: ActivityLogEntry,
) -> None:
    """Persist one activity-log entry inside the open ``InvocationContext`` transaction.

    Validates ``entry.invocation_id`` matches ``handle.invocation_id`` and
    refuses cross-invocation appends. The row is added to the session but
    NOT committed; the surrounding ``InvocationContext`` commits on clean
    exit and rolls back on exception.

    Synchronous because the only DB interaction is ``session.add()`` — the
    SQLAlchemy ``AsyncSession`` exposes ``add`` as a sync method (the
    queue-up happens in-memory; the actual SQL emission is deferred to the
    transaction commit). Keeping the function ``async`` would force every
    caller to ``await`` a never-suspending coroutine.
    """
    if entry.invocation_id != handle.invocation_id:
        msg = (
            f"activity-log entry invocation_id={entry.invocation_id!r} "
            f"does not match the open InvocationContext "
            f"invocation_id={handle.invocation_id!r}"
        )
        raise ValueError(msg)
    handle.session.add(activity_log_entry_to_row(entry))


def emit_activity_log_entry(  # noqa: PLR0913 — imperative-shell wrapper mirrors build_activity_log_entry's ALP-923 signature, minus invocation_id (taken from the handle)
    handle: InvocationHandle,
    *,
    event_type: EventType,
    position_id: str | None,
    order_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource,
    entry_id: str | None = None,
) -> ActivityLogEntry:
    """Build one activity-log entry against the handle and append it.

    The imperative shell over the pure ``build_activity_log_entry``: it binds
    ``invocation_id`` to ``handle.invocation_id``, appends the built entry to the
    open ``InvocationContext`` transaction via ``append_activity_log_entry``, and
    returns the entry so returning callers (operator-console audit) can hand it
    back. ``entry_id`` defaults to the standard token; custom-token callers
    (verb-PK audit rows) override it.
    """
    entry = build_activity_log_entry(
        invocation_id=handle.invocation_id,
        event_type=event_type,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=detail,
        source=source,
        entry_id=entry_id,
    )
    append_activity_log_entry(handle, entry)
    return entry
