"""Round-trip codec between ``ThesisPnlLedgerRecord`` and ``ThesisPnlLedgerRow``.

The typed frozen record is authoritative; this module is the only place that
knows the row shape. ``realized_pnl_usd`` may be negative (a loss), so the read
path rewraps via ``signed_money``; ``cost_basis_usd`` is non-negative
(``money``). Datetimes serialize via ``isoformat``.
"""

from __future__ import annotations

from datetime import datetime

from alphamind._kernel.ids import InvocationId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.state.records_intent import ThesisPnlLedgerRecord
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow


def record_to_row(record: ThesisPnlLedgerRecord) -> ThesisPnlLedgerRow:
    """Project a ``ThesisPnlLedgerRecord`` to its row form."""
    return ThesisPnlLedgerRow(
        thesis_id=record.thesis_id,
        realized_pnl_usd=record.realized_pnl_usd,
        cost_basis_usd=record.cost_basis_usd,
        provenance_json=record.provenance_json,
        derived_from_invocation_id=record.derived_from_invocation_id,
        updated_at=record.updated_at.isoformat(),
    )


def row_to_record(row: ThesisPnlLedgerRow) -> ThesisPnlLedgerRecord:
    """Rehydrate a ``ThesisPnlLedgerRow`` back into the typed record."""
    return ThesisPnlLedgerRecord(
        thesis_id=ThesisId(row.thesis_id),
        realized_pnl_usd=signed_money(row.realized_pnl_usd),
        cost_basis_usd=money(row.cost_basis_usd),
        provenance_json=row.provenance_json,
        derived_from_invocation_id=(
            InvocationId(row.derived_from_invocation_id)
            if row.derived_from_invocation_id is not None
            else None
        ),
        updated_at=datetime.fromisoformat(row.updated_at),
    )


__all__ = ["record_to_row", "row_to_record"]
