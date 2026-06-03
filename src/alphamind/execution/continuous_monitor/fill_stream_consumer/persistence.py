"""Shared fill-resolution + persist entry point (ALP-763).

The fill-stream consumer (:mod:`task`), the unattributed-fill drain
(:mod:`unattributed_drain`), and the periodic backfill backstop
(:mod:`alphamind.execution.continuous_monitor.activities_backfill.task`) all
need to (a) resolve the local ``orders`` PK a broker fill applies to and (b)
run the resolve → append-OR-quarantine persist for one report. Hosting both
here — rather than in ``task`` — keeps a single source of truth and removes the
module cycle that previously forced ``unattributed_drain`` to lazily import
``task`` and the backfill to reach for ``task``'s private ``_persist_one``.

The persist path NEVER drops a fill: a report whose order row cannot be
resolved is parked on the ``unattributed_fills`` queue (idempotent on
``broker_fill_key``) and alerted once; a later drain integrates it when the
order row materializes (the deferred-Phase-2 race) or it stays queued for
operator review (an out-of-band manual order). A quarantine-write failure is
logged and swallowed so the live consumer degrades rather than crashing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    derive_broker_fill_key,
    fill_report_to_fill_record,
    order_id_for_report,
    terminal_order_status_for,
)
from alphamind.execution.write_paths.fill_persistence import append_fill_record
from alphamind.execution.write_paths.order_status_sync import sync_terminal_order_status
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
    mark_unattributed_fill_alerted,
)
from alphamind.state.records import FillRecord, UnattributedFill
from alphamind.state.tables.orders import OrderRow

log = logging.getLogger(__name__)

# Per ALP-528 (paper-evaluation harness wedge): paper-mode wiring passes a
# callable that enriches each translated FillRecord with a
# ``live_execution_estimate`` before persistence. Live-mode passes ``None`` so
# the hot path is unchanged. Single definition; ``task`` / ``unattributed_drain``
# / the backfill import it from here.
EnrichmentCallable = Callable[[FillRecord], Awaitable[FillRecord]]

# Short in-process re-resolution schedule (seconds) to absorb a sub-second
# order-commit race: a fill-bearing event can land microseconds before the
# deferred Phase-2 ``orders`` writeback commits. Bounded under ~2s. This is the
# LIVE consumer's race absorber only; the backfill passes ``retry_resolve=False``
# (it is itself the slow path and recovers fills that may never resolve).
_RESOLVE_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)


async def persist_fill_report(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
    retry_resolve: bool = True,
) -> None:
    """Translate a single ``FillReport`` and append it in its own transaction.

    Paper-mode wiring (per ALP-528) injects ``enrichment_callable`` so each
    translated :class:`FillRecord` is enriched with a
    ``live_execution_estimate`` before persistence. Live mode passes ``None``;
    the column persists as NULL and the hot path is unchanged.

    ``retry_resolve`` controls the brief in-process re-resolution after an
    initial miss: the live consumer keeps it ``True`` to absorb the
    sub-second order-commit race; the periodic backfill passes ``False`` so it
    quarantines immediately rather than paying the per-fill retry sleep — the
    drain it runs after the sweep handles a fill whose order row exists, and an
    out-of-band fill would never resolve anyway.
    """
    record = fill_report_to_fill_record(report)
    log.debug(
        "fill report received: event_type=%s client_order_id=%s fill_timestamp=%s",
        report.event_type,
        report.client_order_id,
        report.fill_timestamp.isoformat(),
    )
    if record is None:
        await _sync_terminal_status_if_any(report, session_factory=session_factory)
        return
    # Resolve the local ``orders`` PK this fill applies to. The broker's
    # client_order_id does not round-trip the OMS order id for equity entries
    # (it carries the command id) or for native-bracket protective children
    # (Alpaca generates it), so fall back to the captured broker UUID
    # (ALP-746). A fill-bearing event can also arrive *before* the deferred
    # Phase-2 ``orders`` writeback commits — re-resolve a couple of times to
    # absorb that sub-second race (ALP-763), live consumer only.
    async with session_factory() as db:
        oms_order_id = await _resolve_oms_order_id(db, report)
    if oms_order_id is None and retry_resolve:
        oms_order_id = await _retry_resolve_oms_order_id(report, session_factory=session_factory)
    if oms_order_id is None:
        # Never drop: park the raw report on the retry queue and alert. A later
        # drain integrates it once the order materializes (race); a fill that
        # never resolves (out-of-band manual order) stays queued + alerted.
        await _quarantine_unattributed_fill(report, session_factory=session_factory)
        return
    async with session_factory() as db:
        if oms_order_id != record.order_id:
            # Re-derive so ``order_id`` + ``fill_id`` reflect the resolved PK.
            record = fill_report_to_fill_record(report, oms_order_id=oms_order_id)
            if record is None:  # pragma: no cover — gates are identical to the first call
                return
        if enrichment_callable is not None:
            record = await enrichment_callable(record)
        await append_fill_record(db, record)
        await db.commit()


async def _retry_resolve_oms_order_id(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Re-resolve a few times with brief sleeps to absorb a commit race.

    Each attempt re-opens a fresh session so it observes any ``orders`` row the
    deferred Phase-2 writeback committed in the interim. Total wait is bounded
    by :data:`_RESOLVE_RETRY_DELAYS` (well under two seconds). This DOES block
    this consumer's drain loop for that window — concurrent websocket events
    buffer in the subscribe primitive's unbounded queue and are processed once
    the sleep returns. The long race is the reconnect-driven drain's job, not
    this hot path's; the bound keeps the stall sub-2s.
    """
    for delay in _RESOLVE_RETRY_DELAYS:
        await asyncio.sleep(delay)
        async with session_factory() as db:
            oms_order_id = await _resolve_oms_order_id(db, report)
        if oms_order_id is not None:
            return oms_order_id
    return None


async def _quarantine_unattributed_fill(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Park an unresolvable fill on the retry queue and emit a one-time alert.

    Idempotent on ``broker_fill_key`` so a websocket + recovery replay of the
    same fill collapses to one row. The alert is a loud, greppable
    ``log.warning`` — the continuous monitor has no invocation-handle alert
    channel, so collector.log WARNINGs are the operator's alert surface. It
    fires once: the warning + ``mark_unattributed_fill_alerted`` run only when
    ``append_unattributed_fill`` newly inserts the row, so a re-delivered /
    re-parked fill (ON CONFLICT DO NOTHING) does not re-fire the alert.

    A quarantine-write failure must NOT crash the consumer — it would otherwise
    propagate into the reconnect-budget supervisor and burn an attempt / exit
    the task. A transient DB error is logged at ERROR and swallowed; the fill
    is recovered by the next reconnect-driven recovery or the periodic backfill
    sweep (both re-feed it through this path), so degrade-don't-crash holds.
    """
    now = datetime.now(UTC)
    record = UnattributedFill(
        broker_fill_key=derive_broker_fill_key(report),
        alpaca_order_id=report.alpaca_order_id,
        client_order_id=report.client_order_id,
        event_type=report.event_type,
        fill_timestamp=report.fill_timestamp,
        fill_price=report.fill_price or 0.0,
        fill_quantity=report.fill_quantity or 0.0,
        raw_report_json=report.model_dump_json(),
        first_seen_at=now,
        last_retry_at=None,
        retry_count=0,
        alerted=False,
    )
    try:
        async with session_factory() as db:
            inserted = await append_unattributed_fill(db, record)
            if inserted:
                log.warning(
                    "QUARANTINED unattributed fill: broker_fill_key=%s client_order_id=%s "
                    "alpaca_order_id=%s event=%s — no local order row to attribute it to; "
                    "parked for drain (race) or operator review (out-of-band order)",
                    record.broker_fill_key,
                    report.client_order_id,
                    report.alpaca_order_id,
                    report.event_type,
                )
                await mark_unattributed_fill_alerted(db, record.broker_fill_key)
            await db.commit()
    except Exception:
        log.exception(
            "failed to quarantine unattributed fill: broker_fill_key=%s client_order_id=%s "
            "alpaca_order_id=%s event=%s — continuing; recovered by next recovery/backfill sweep",
            record.broker_fill_key,
            report.client_order_id,
            report.alpaca_order_id,
            report.event_type,
        )


async def _sync_terminal_status_if_any(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reflect a broker terminal non-fill event in ``orders.status`` (ALP-739).

    ``canceled`` / ``expired`` events append no fill but must update the local
    order row — otherwise an accepted entry that expires / cancels unfilled
    stays ``PENDING`` and the ``entry_no_fill`` alert never fires. Every other
    non-fill event (``new`` / ``replaced`` / …) carries no terminal
    disposition and no-ops here. Its own short-lived transaction, mirroring
    the per-fill write.

    Scoped to **zero-fill** terminals (``cumulative_filled_quantity == 0``): a
    partially-filled-then-terminal order is left to the fill path + Phase 1,
    which own ``filled_quantity`` and integrate the partials. Stamping a
    terminal status here for a partially-filled order would (a) read
    ``filled_quantity == 0`` until Phase 1 catches up and fire a false
    no-fill alert, and (b) be reverted to ``PARTIALLY_FILLED`` by Phase 1's
    fill integration anyway. The no-fill case is the one the fill path does
    not cover, so it is the only one this sync owns.
    """
    terminal_status = terminal_order_status_for(report)
    if terminal_status is None:
        return
    if report.cumulative_filled_quantity > 0:
        return
    async with session_factory() as db:
        # Resolve by broker UUID when the client_order_id doesn't name a local
        # PK — this is the path an OCO sibling-cancel takes (the broker cancels
        # the unfired protective leg, whose client_order_id Alpaca generated;
        # only the captured leg UUID locates the local row). ALP-746.
        order_id = await _resolve_oms_order_id(db, report)
        if order_id is None:
            log.debug(
                "terminal status for unknown order: client_order_id=%s alpaca_order_id=%s "
                "status=%s — skipping",
                report.client_order_id,
                report.alpaca_order_id,
                terminal_status.value,
            )
            return
        transitioned = await sync_terminal_order_status(
            db,
            order_id=order_id,
            terminal_status=terminal_status,
            observed_at=datetime.now(UTC),
        )
        await db.commit()
    if transitioned:
        log.info(
            "synced terminal order status: order_id=%s status=%s",
            order_id,
            terminal_status.value,
        )


async def _resolve_oms_order_id(db: AsyncSession, report: FillReport) -> str | None:
    """Resolve the local ``orders`` PK a fill / terminal event applies to.

    Three-step resolution:

    1. Treat the report-derived id (``parent_client_order_id or client_order_id``)
       as a candidate PK — the historical / already-aligned path (and the only
       path the test substrate exercises by hand-aligning the two).
    2. (ALP-836) Resolve by the ``client_order_id`` column — the durable
       pre-committed row carries the broker ``client_order_id`` (= command_id for
       an equity entry / close / add) from the instant it is committed, BEFORE its
       real ``alpaca_order_id`` is backfilled. This closes the atomicity-first
       submit→backfill window: a fast fill in that window resolves to the
       pre-committed row instead of stranding (the UUID lookup below would miss it
       because the row still carries the synthetic ``alp-{order_id}`` placeholder).
    3. (ALP-746) Otherwise resolve by the broker UUID of the order that owns the
       local row: for an mleg per-leg child that is the parent's
       ``parent_alpaca_order_id`` (legs do not own ``orders`` rows); for an
       equity entry / close / native-bracket protective child it is the report's
       own ``alpaca_order_id``. The captured-at-submission UUID was written onto
       that row (entry / close / TAKE_PROFIT / PRICE_STOP), so the lookup hits.

    Returns ``None`` when none resolves — the caller declines to attribute
    the event rather than violate the ``fill_records.order_id`` FK.

    ``alpaca_order_id`` is expected unique across ``orders`` rows (captured
    broker UUIDs are distinct per order; synthetic ``alp-{order_id}`` placeholders
    are unique per PK), so the UUID lookup uses ``one_or_none`` — a duplicate
    surfaces loudly as a ``MultipleResultsFound`` invariant breach rather than
    silently attributing the event to an arbitrary row.
    """
    candidate_pk = order_id_for_report(report)
    row = await db.get(OrderRow, candidate_pk)
    if row is not None:
        return row.order_id
    # ALP-836 — resolve the durable pre-committed row by the ``client_order_id``
    # column. ``client_order_id`` is unique-when-present, so ``one_or_none``
    # surfaces a duplicate as a loud invariant breach rather than guessing.
    client_order_id_key = report.parent_client_order_id or report.client_order_id
    stmt = select(OrderRow).where(OrderRow.client_order_id == client_order_id_key)
    row = (await db.execute(stmt)).scalars().one_or_none()
    if row is not None:
        return row.order_id
    uuid_key = report.parent_alpaca_order_id or report.alpaca_order_id
    stmt = select(OrderRow).where(OrderRow.alpaca_order_id == uuid_key)
    row = (await db.execute(stmt)).scalars().one_or_none()
    return row.order_id if row is not None else None


__all__ = [
    "EnrichmentCallable",
    "persist_fill_report",
]
