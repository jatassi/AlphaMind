"""Append-only ``broker_event_log`` persistence helper (story 02a / ALP-845).

The broker-event log is the gap-free substrate for realized PnL (ADR-0002):
every broker→local event that changes a Broker-Owned Fact lands here as one
immutable row. This helper stages a single INSERT; the caller owns the
transaction boundary, mirroring :mod:`fill_persistence`.

Idempotency is keyed on the ``event_key`` PRIMARY KEY via
``INSERT ... ON CONFLICT DO NOTHING`` — the websocket delivery and a later REST
recovery replay of the *same* event collapse to one row at the storage layer
rather than racing through a separate existence check. The append is
append-only by construction (ADR-0005): no update path is exposed.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.persistence.write_unit import require_immediate_write_unit
from alphamind.state.records_broker_event_log import BrokerEventRecord
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.broker_event_log_codec import record_to_row


async def append_broker_event(session: AsyncSession, record: BrokerEventRecord) -> bool:
    """Persist *record* if its ``event_key`` is new; no-op otherwise.

    The ``ON CONFLICT DO NOTHING`` binds to the ``event_key`` primary key, so a
    re-delivered event (websocket + recovery replay) collapses onto the existing
    row instead of raising a duplicate-key error.

    Returns ``True`` when this call newly inserted the row, ``False`` when the
    ``event_key`` already existed (the conflict path). Callers that book a
    side-effect once per event (e.g. the account-activities handlers booking
    realized PnL) gate on the return so a re-poll does not double-book; the
    fill-path callers ignore it (the event-log row is the only side-effect).

    The session's transaction must have begun ``IMMEDIATE`` (ALP-942 F).
    """
    await require_immediate_write_unit(session, helper="append_broker_event")
    row = record_to_row(record)
    values = {col.name: getattr(row, col.name) for col in BrokerEventLogRow.__table__.columns}
    stmt = sqlite_insert(BrokerEventLogRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["event_key"])
    # DML ``execute`` returns a ``CursorResult`` at runtime (the async facade is
    # typed to the broader ``Result``); ``rowcount`` is 0 when ON CONFLICT DO
    # NOTHING suppressed the insert and 1 on a genuine insert — mirrors
    # ``append_unattributed_fill``'s newness signal.
    result = cast("CursorResult[object]", await session.execute(stmt))
    return result.rowcount > 0


__all__ = ["append_broker_event"]
