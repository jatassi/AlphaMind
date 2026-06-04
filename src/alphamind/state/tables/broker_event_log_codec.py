"""Round-trip codec between ``BrokerEventRecord`` and ``BrokerEventLogRow``.

The typed frozen ``BrokerEventRecord`` is the authoritative shape; this module
is the only place that knows the row shape. ``record_to_row`` projects a record
for INSERT (the sole write path — the log is append-only, ADR-0005);
``row_to_record`` rehydrates a row on the read path. Datetimes serialize via
``isoformat`` matching the convention the other state codecs use.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow


def record_to_row(record: BrokerEventRecord) -> BrokerEventLogRow:
    """Project a ``BrokerEventRecord`` to its append-only row form."""
    return BrokerEventLogRow(
        event_key=record.event_key,
        event_type=record.event_type.value,
        thesis_id=record.thesis_id,
        invocation_id=record.invocation_id,
        position_id=record.position_id,
        raw_payload_json=record.raw_payload_json,
        broker_timestamp=(
            record.broker_timestamp.isoformat()
            if record.broker_timestamp is not None
            else None
        ),
        captured_at=record.captured_at.isoformat(),
    )


def row_to_record(row: BrokerEventLogRow) -> BrokerEventRecord:
    """Rehydrate a ``BrokerEventLogRow`` back into the typed record."""
    return BrokerEventRecord(
        event_key=row.event_key,
        event_type=BrokerEventType(row.event_type),
        thesis_id=row.thesis_id,
        invocation_id=row.invocation_id,
        position_id=row.position_id,
        raw_payload_json=row.raw_payload_json,
        broker_timestamp=(
            datetime.fromisoformat(row.broker_timestamp)
            if row.broker_timestamp is not None
            else None
        ),
        captured_at=datetime.fromisoformat(row.captured_at),
    )


__all__ = ["record_to_row", "row_to_record"]
