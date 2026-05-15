"""Append-only fill-record persistence helper (story 05 / ALP-363).

The write is intentionally narrow: one new row, one entity, append-only.
Touches nothing else. Idempotent on the dedupe key
``(order_id, fill_timestamp, fill_quantity, fill_price)`` so transient retry,
``GET /v2/orders`` replay, and reconnect-driven backfill all converge to the
same on-disk shape.

The continuous monitor — owned by the OMS gateway, not the pipeline
invocation — invokes this on every fill event from Alpaca's websocket. The
caller controls the transaction boundary (commit/rollback); this helper only
adds the row.

Idempotency uses ``INSERT ... ON CONFLICT DO NOTHING`` against the
``uq_fill_records_dedupe`` UNIQUE constraint, so concurrent appends with the
same dedupe key collapse to a single row at the storage layer rather than
racing through a separate existence check.
"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.fill_records_codec import (
    record_to_row,
)


async def append_fill_record(session: AsyncSession, fill: FillRecord) -> None:
    """Persist *fill* as ``unprocessed`` if its dedupe key is new; no-op otherwise.

    Forces ``processing_status = unprocessed`` to defend against callers
    accidentally trying to short-circuit Phase 1 by writing a pre-processed
    fill on the monitor path. The transition to ``processed`` belongs to
    Phase 1 and must run inside the integration transaction.
    """
    if fill.processing_status != FillProcessingStatus.UNPROCESSED:
        msg = (
            "append_fill_record only persists unprocessed fills; "
            f"got processing_status={fill.processing_status.value!r}"
        )
        raise ValueError(msg)

    row = record_to_row(fill)
    values = {col.name: getattr(row, col.name) for col in FillRecordRow.__table__.columns}
    stmt = sqlite_insert(FillRecordRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["order_id", "fill_timestamp", "fill_quantity", "fill_price"]
    )
    await session.execute(stmt)


__all__ = ["append_fill_record"]
