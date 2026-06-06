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

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.ids import InvocationId, ThesisId
from alphamind.execution.position_model.thesis_pnl_derivation import (
    ThesisPnlDerivation,
    derive_thesis_pnl,
)
from alphamind.state.records_broker_event_log import BrokerEventRecord, serialize_event_payload
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

    Stamps ``last_derived_event_seq`` to the max ``event_seq`` folded (ALP-865),
    so the dirty-set selector skips this thesis next run unless a newer event
    lands.
    """
    events, max_event_seq = await _events_for_thesis(session, thesis_id)
    derivation = derive_thesis_pnl(thesis_id, events)
    record = _to_ledger_record(thesis_id, derivation, invocation_id, max_event_seq)
    await _upsert(session, record)
    return record


async def rederive_thesis_pnl_ledgers(
    session: AsyncSession,
    thesis_ids: Iterable[ThesisId],
    invocation_id: InvocationId | None,
) -> tuple[ThesisPnlLedgerRecord, ...]:
    """Batched per-thesis rederive — one event fetch for *all* of *thesis_ids*.

    The N+1-free entry point: a single ``SELECT … WHERE thesis_id IN (…)`` loads
    every thesis's events in one round-trip, groups them in-process, then upserts
    each thesis's ledger row through the same single-writer path
    (:func:`_to_ledger_record` / :func:`_upsert`). Per-thesis output is
    byte-identical to the unbatched :func:`rederive_thesis_pnl_ledger`: the events
    are the same set and :func:`derive_thesis_pnl` re-filters to its thesis, so the
    fold sees exactly the rows the single-thesis query would have returned — and
    each row's ``last_derived_event_seq`` is stamped from the same max ``event_seq``
    either path would have computed (ALP-865).

    De-duplicates *thesis_ids* while preserving first-seen order so a repeated id
    is derived (and returned) once. The caller owns the transaction boundary
    (mirrors :func:`rederive_thesis_pnl_ledger`). Returns the persisted records in
    that order.
    """
    unique_ids = tuple(dict.fromkeys(thesis_ids))
    if not unique_ids:
        return ()
    events_by_thesis, max_seq_by_thesis = await _events_by_thesis(session, unique_ids)
    records: list[ThesisPnlLedgerRecord] = []
    for thesis_id in unique_ids:
        events = events_by_thesis.get(thesis_id, ())
        derivation = derive_thesis_pnl(thesis_id, events)
        record = _to_ledger_record(
            thesis_id, derivation, invocation_id, max_seq_by_thesis.get(thesis_id)
        )
        await _upsert(session, record)
        records.append(record)
    return tuple(records)


def _max_event_seq(rows: Sequence[BrokerEventLogRow]) -> int | None:
    """Max ``event_seq`` (rowid) over *rows*, or ``None`` for an empty set.

    The watermark a re-derivation stamps: the latest log position folded into the
    thesis's figures (ALP-865). ``None`` for a thesis with no events leaves the
    watermark unset (an empty ledger is re-derived to the same empty figures).
    """
    seqs = [row.event_seq for row in rows if row.event_seq is not None]
    return max(seqs) if seqs else None


async def _events_for_thesis(
    session: AsyncSession, thesis_id: ThesisId
) -> tuple[tuple[BrokerEventRecord, ...], int | None]:
    """Load every ``broker_event_log`` row attributed to *thesis_id* + its max seq.

    Keyed on the ``thesis_id`` broker-carried link — no ``orders`` join (ADR-0002,
    invariant 3). Ordering is the derivation's concern; the fold sorts by
    timestamp. The second tuple element is the max ``event_seq`` folded (the
    re-derivation watermark), ``None`` when the thesis has no events.
    """
    stmt = select(BrokerEventLogRow).where(BrokerEventLogRow.thesis_id == thesis_id)
    rows = (await session.execute(stmt)).scalars().all()
    return tuple(event_row_to_record(row) for row in rows), _max_event_seq(rows)


async def _events_by_thesis(
    session: AsyncSession, thesis_ids: tuple[ThesisId, ...]
) -> tuple[dict[ThesisId, tuple[BrokerEventRecord, ...]], dict[ThesisId, int]]:
    """Load the events for *all* of *thesis_ids* in one ``IN``-clause SELECT.

    Replaces the per-thesis ``WHERE thesis_id = ?`` fetch (one round-trip per
    thesis, the N+1) with a single ``WHERE thesis_id IN (…)`` read, grouping the
    rows by their broker-carried ``thesis_id`` in-process. A thesis with no events
    is absent from both maps (the caller derives an empty ledger for it). The
    second map is the per-thesis max ``event_seq`` folded — the watermark each
    re-derivation stamps (ALP-865).
    """
    stmt = select(BrokerEventLogRow).where(BrokerEventLogRow.thesis_id.in_(thesis_ids))
    rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[ThesisId, list[BrokerEventRecord]] = {tid: [] for tid in thesis_ids}
    max_seq: dict[ThesisId, int] = {}
    for row in rows:
        record = event_row_to_record(row)
        tid = record.thesis_id
        if tid is not None:
            grouped.setdefault(tid, []).append(record)
            if row.event_seq is not None and row.event_seq > max_seq.get(tid, 0):
                max_seq[tid] = row.event_seq
    return {tid: tuple(records) for tid, records in grouped.items()}, max_seq


def _to_ledger_record(
    thesis_id: ThesisId,
    derivation: ThesisPnlDerivation,
    invocation_id: InvocationId | None,
    last_derived_event_seq: int | None,
) -> ThesisPnlLedgerRecord:
    provenance = serialize_event_payload({"event_keys": list(derivation.provenance_event_keys)})
    return ThesisPnlLedgerRecord(
        thesis_id=thesis_id,
        realized_pnl_usd=derivation.realized_pnl_usd,
        cost_basis_usd=derivation.cost_basis_usd,
        provenance_json=provenance,
        derived_from_invocation_id=invocation_id,
        updated_at=datetime.now(UTC),
        last_derived_event_seq=last_derived_event_seq,
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


__all__ = ["rederive_thesis_pnl_ledger", "rederive_thesis_pnl_ledgers"]
