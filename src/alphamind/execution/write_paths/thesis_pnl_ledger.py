"""Single-writer derivation of the per-thesis PnL ledger (ALP-851 / W2b).

The imperative shell around the pure fold
(:func:`alphamind.execution.position_model.thesis_pnl_derivation.derive_thesis_pnl`).
It is the **sole writer** of ``thesis_pnl_ledger`` (ADR-0005, invariant 3):
read a thesis's ``broker_event_log`` events (fills + activities), fold them into
realized PnL + cost basis, and upsert the ledger row — authored by the pipeline,
never by a broker snapshot.

The write is an idempotent **replace** (not an increment): the ledger is a
*derived view*, so re-deriving from the same event set reproduces the same row.
A projection rebuild or a quantity checkpoint reruns this derivation and the
ledger never drifts — and a checkpoint, which writes positions / quantity, never
calls this writer, so per-thesis PnL cannot be overwritten by a broker snapshot.

Attribution is by ``thesis_id`` alone — the events carry the broker-carried link
(ADR-0002), so there is no join to a lose-able ``orders`` table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.ids import InvocationId, ThesisId
from alphamind.execution.position_model.thesis_pnl_derivation import (
    ThesisPnlDerivation,
    derive_thesis_pnl,
)
from alphamind.state.records_broker_event_log import BrokerEventRecord
from alphamind.state.records_intent import ThesisPnlLedgerRecord
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.broker_event_log_codec import row_to_record as event_row_to_record
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from alphamind.state.tables.thesis_pnl_ledger_codec import record_to_row


async def rederive_thesis_pnl_ledger(
    session: AsyncSession,
    thesis_id: ThesisId,
    invocation_id: InvocationId | None,
) -> ThesisPnlLedgerRecord:
    """Derive *thesis_id*'s realized PnL + cost basis from its event log and upsert it.

    Reads every ``broker_event_log`` row attributed to *thesis_id*, folds them
    via the pure derivation, and writes the ledger row (insert or full replace).
    The caller owns the transaction boundary (mirrors the other write-path
    helpers). Returns the persisted :class:`ThesisPnlLedgerRecord`.
    """
    events = await _events_for_thesis(session, thesis_id)
    derivation = derive_thesis_pnl(thesis_id, events)
    record = _to_ledger_record(thesis_id, derivation, invocation_id)
    await _upsert(session, record)
    return record


async def _events_for_thesis(
    session: AsyncSession, thesis_id: ThesisId
) -> tuple[BrokerEventRecord, ...]:
    """Load every ``broker_event_log`` row attributed to *thesis_id*.

    Keyed on the ``thesis_id`` broker-carried link — no ``orders`` join (ADR-0002,
    invariant 3). Ordering is the derivation's concern; the fold sorts by
    timestamp.
    """
    stmt = select(BrokerEventLogRow).where(BrokerEventLogRow.thesis_id == thesis_id)
    rows = (await session.execute(stmt)).scalars().all()
    return tuple(event_row_to_record(row) for row in rows)


def _to_ledger_record(
    thesis_id: ThesisId,
    derivation: ThesisPnlDerivation,
    invocation_id: InvocationId | None,
) -> ThesisPnlLedgerRecord:
    provenance = json.dumps({"event_keys": list(derivation.provenance_event_keys)}, sort_keys=True)
    return ThesisPnlLedgerRecord(
        thesis_id=thesis_id,
        realized_pnl_usd=derivation.realized_pnl_usd,
        cost_basis_usd=derivation.cost_basis_usd,
        provenance_json=provenance,
        derived_from_invocation_id=invocation_id,
        updated_at=datetime.now(UTC),
    )


async def _upsert(session: AsyncSession, record: ThesisPnlLedgerRecord) -> None:
    """Insert the ledger row, or fully replace an existing one (single writer).

    A full replace — not an increment — keeps the ledger a faithful derived view:
    re-deriving from the same event set yields the same row, so the write is
    idempotent under a projection rebuild. One ``INSERT … ON CONFLICT DO UPDATE``
    on the ``thesis_id`` PK — no read-modify-write branch — matching the peer
    write-path helpers (``broker_event_persistence``, ``fill_persistence``).
    """
    row = record_to_row(record)
    values = {col.name: getattr(row, col.name) for col in ThesisPnlLedgerRow.__table__.columns}
    stmt = sqlite_insert(ThesisPnlLedgerRow).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["thesis_id"],
        set_={k: v for k, v in values.items() if k != "thesis_id"},
    )
    await session.execute(stmt)


__all__ = ["rederive_thesis_pnl_ledger"]
