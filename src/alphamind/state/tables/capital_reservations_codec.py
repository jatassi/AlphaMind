"""Round-trip codec between ``CapitalReservationRecord`` and ``CapitalReservationRow``.

The typed frozen record is authoritative; this module is the only place that
knows the row shape. ``reserved_capital_usd`` is non-negative (``money``).
``released_at`` is null while the reservation is live. Datetimes serialize via
``isoformat``.
"""

from __future__ import annotations

from datetime import datetime

from alphamind._kernel.money import money
from alphamind.state.records_intent import CapitalReservationRecord
from alphamind.state.tables.capital_reservations import CapitalReservationRow


def record_to_row(record: CapitalReservationRecord) -> CapitalReservationRow:
    """Project a ``CapitalReservationRecord`` to its row form."""
    return CapitalReservationRow(
        reservation_id=record.reservation_id,
        thesis_id=record.thesis_id,
        reserved_capital_usd=record.reserved_capital_usd,
        reserved_by_invocation_id=record.reserved_by_invocation_id,
        reserved_at=record.reserved_at.isoformat(),
        released_at=(record.released_at.isoformat() if record.released_at is not None else None),
    )


def row_to_record(row: CapitalReservationRow) -> CapitalReservationRecord:
    """Rehydrate a ``CapitalReservationRow`` back into the typed record."""
    return CapitalReservationRecord(
        reservation_id=row.reservation_id,
        thesis_id=row.thesis_id,
        reserved_capital_usd=money(row.reserved_capital_usd),
        reserved_by_invocation_id=row.reserved_by_invocation_id,
        reserved_at=datetime.fromisoformat(row.reserved_at),
        released_at=(
            datetime.fromisoformat(row.released_at) if row.released_at is not None else None
        ),
    )


__all__ = ["record_to_row", "row_to_record"]
