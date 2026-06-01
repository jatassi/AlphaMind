"""Persistence helpers for the ``unattributed_fills`` retry queue (ALP-763).

A raw broker fill event arrives before its local ``orders`` row is committed
(deferred Phase-2 writeback). The continuous monitor parks the event here and a
later drain replays it once the order materializes. These helpers stage rows /
run queries only; the caller owns the transaction boundary (commit/rollback),
mirroring ``fill_persistence``.

``append_unattributed_fill`` is idempotent on the ``broker_fill_key`` primary
key via ``INSERT ... ON CONFLICT DO NOTHING``, so transient retry and
reconnect-driven backfill converge to a single row at the storage layer rather
than racing through a separate existence check.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.records import UnattributedFill
from alphamind.state.tables.unattributed_fills import UnattributedFillRow
from alphamind.state.tables.unattributed_fills_codec import record_to_row, row_to_record


async def append_unattributed_fill(session: AsyncSession, record: UnattributedFill) -> None:
    """Persist *record* if its ``broker_fill_key`` is new; no-op otherwise."""
    row = record_to_row(record)
    values = {col.name: getattr(row, col.name) for col in UnattributedFillRow.__table__.columns}
    stmt = sqlite_insert(UnattributedFillRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["broker_fill_key"])
    await session.execute(stmt)


async def list_unattributed_fills(session: AsyncSession) -> list[UnattributedFill]:
    """Return every queued fill, oldest first (by ``first_seen_at``)."""
    result = await session.execute(
        select(UnattributedFillRow).order_by(UnattributedFillRow.first_seen_at)
    )
    return [row_to_record(row) for row in result.scalars().all()]


async def delete_unattributed_fill(session: AsyncSession, broker_fill_key: str) -> None:
    """Remove the queued fill identified by *broker_fill_key* (drain success)."""
    await session.execute(
        delete(UnattributedFillRow).where(UnattributedFillRow.broker_fill_key == broker_fill_key)
    )


async def mark_unattributed_fill_alerted(session: AsyncSession, broker_fill_key: str) -> None:
    """Flag the queued fill as alerted so the drain does not re-alert."""
    await session.execute(
        update(UnattributedFillRow)
        .where(UnattributedFillRow.broker_fill_key == broker_fill_key)
        .values(alerted=1)
    )


async def touch_unattributed_fill_retry(
    session: AsyncSession, broker_fill_key: str, *, observed_at: datetime
) -> None:
    """Record a drain attempt: bump ``retry_count`` and set ``last_retry_at``."""
    await session.execute(
        update(UnattributedFillRow)
        .where(UnattributedFillRow.broker_fill_key == broker_fill_key)
        .values(
            retry_count=UnattributedFillRow.retry_count + 1,
            last_retry_at=observed_at.isoformat(),
        )
    )


__all__ = [
    "append_unattributed_fill",
    "delete_unattributed_fill",
    "list_unattributed_fills",
    "mark_unattributed_fill_alerted",
    "touch_unattributed_fill_retry",
]
