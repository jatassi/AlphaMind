"""Round-trip codec between ``UnattributedFill`` and ``UnattributedFillRow`` (ALP-763).

The typed Pydantic ``UnattributedFill`` is the authoritative shape; the SQL row
mirrors it column-for-column. ``record_to_row`` projects a record for INSERT;
``row_to_record`` rehydrates a row for the read path. Datetimes serialize via
``isoformat`` (matches the ``fill_records`` codec convention); ``alerted``
serializes to the ``Integer`` 0/1 storage convention.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.state.records import UnattributedFill
from alphamind.state.tables.unattributed_fills import UnattributedFillRow


def record_to_row(record: UnattributedFill) -> UnattributedFillRow:
    """Project an ``UnattributedFill`` to its ``UnattributedFillRow`` form."""
    return UnattributedFillRow(
        broker_fill_key=record.broker_fill_key,
        alpaca_order_id=record.alpaca_order_id,
        client_order_id=record.client_order_id,
        event_type=record.event_type,
        fill_timestamp=record.fill_timestamp.isoformat(),
        fill_price=record.fill_price,
        fill_quantity=record.fill_quantity,
        raw_report_json=record.raw_report_json,
        first_seen_at=record.first_seen_at.isoformat(),
        last_retry_at=(
            record.last_retry_at.isoformat() if record.last_retry_at is not None else None
        ),
        retry_count=record.retry_count,
        alerted=int(record.alerted),
        escalated=int(record.escalated),
    )


def row_to_record(row: UnattributedFillRow) -> UnattributedFill:
    """Rehydrate an ``UnattributedFillRow`` back into the typed ``UnattributedFill``."""
    return UnattributedFill(
        broker_fill_key=row.broker_fill_key,
        alpaca_order_id=row.alpaca_order_id,
        client_order_id=row.client_order_id,
        event_type=row.event_type,
        fill_timestamp=datetime.fromisoformat(row.fill_timestamp),
        fill_price=row.fill_price,
        fill_quantity=row.fill_quantity,
        raw_report_json=row.raw_report_json,
        first_seen_at=datetime.fromisoformat(row.first_seen_at),
        last_retry_at=(
            datetime.fromisoformat(row.last_retry_at) if row.last_retry_at is not None else None
        ),
        retry_count=row.retry_count,
        alerted=bool(row.alerted),
        escalated=bool(row.escalated),
    )


__all__ = ["record_to_row", "row_to_record"]
