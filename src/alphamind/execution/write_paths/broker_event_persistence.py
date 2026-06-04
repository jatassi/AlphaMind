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

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.records_broker_event_log import BrokerEventRecord
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.broker_event_log_codec import record_to_row


async def append_broker_event(session: AsyncSession, record: BrokerEventRecord) -> None:
    """Persist *record* if its ``event_key`` is new; no-op otherwise.

    The ``ON CONFLICT DO NOTHING`` binds to the ``event_key`` primary key, so a
    re-delivered event (websocket + recovery replay) collapses onto the existing
    row instead of raising a duplicate-key error.
    """
    row = record_to_row(record)
    values = {col.name: getattr(row, col.name) for col in BrokerEventLogRow.__table__.columns}
    stmt = sqlite_insert(BrokerEventLogRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["event_key"])
    await session.execute(stmt)


__all__ = ["append_broker_event"]
