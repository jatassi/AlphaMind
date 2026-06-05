"""Round-trip codec between ``PositionGreeksRecord`` and ``PositionGreeksRow``.

The typed frozen record is authoritative; this module is the only place that
knows the row shape. ``updated_at`` serializes via ``isoformat``.
"""

from __future__ import annotations

from datetime import datetime

from alphamind._kernel.ids import PositionId
from alphamind.state.records_position_greeks import PositionGreeksRecord
from alphamind.state.tables.position_greeks import PositionGreeksRow


def record_to_row(record: PositionGreeksRecord) -> PositionGreeksRow:
    """Project a ``PositionGreeksRecord`` to its row form."""
    return PositionGreeksRow(
        position_id=record.position_id,
        delta=record.delta,
        gamma=record.gamma,
        theta=record.theta,
        vega=record.vega,
        iv=record.iv,
        updated_at=record.updated_at.isoformat(),
    )


def row_to_record(row: PositionGreeksRow) -> PositionGreeksRecord:
    """Rehydrate a ``PositionGreeksRow`` back into the typed record."""
    return PositionGreeksRecord(
        position_id=PositionId(row.position_id),
        delta=row.delta,
        gamma=row.gamma,
        theta=row.theta,
        vega=row.vega,
        iv=row.iv,
        updated_at=datetime.fromisoformat(row.updated_at),
    )


__all__ = ["record_to_row", "row_to_record"]
