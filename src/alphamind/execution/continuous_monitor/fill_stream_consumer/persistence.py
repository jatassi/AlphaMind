"""Shared fill-resolution + persist entry point (ALP-845 / ADR-0002).

The fill-stream consumer (:mod:`task`), the unattributed-fill drain
(:mod:`unattributed_drain`), and the periodic backfill backstop
(:mod:`alphamind.execution.continuous_monitor.activities_backfill.task`) all
run the persist for one report through here, so the self-attribution logic has
a single source of truth.

A fill **self-attributes** by parsing the broker-carried link (story 01b) out
of its ``client_order_id``: the link names the originating thesis (*why*) and
invocation (*when*), and the position is resolved off the thesis→position Intent
edge. The fill then appends to the append-only ``broker_event_log`` (idempotent
on the ``event_key`` PK) carrying the decoded ``thesis_id`` / ``invocation_id``
/ ``position_id`` — **no local ``orders`` row is required for attribution**
(ADR-0002). The order row demotes to an optional projection cache: when one
resolves, the fill is *also* appended to ``fill_records`` so Phase 1 integrates
it; when it does not, the event-log row still captures the fact gap-free.

The strand path is retired: a fill is quarantined to ``unattributed_fills``
(idempotent on ``broker_fill_key``, alerted once) ONLY when it carries no
parseable link — a genuinely out-of-band / manually-placed order. There is no
"AlphaMind-submitted fill we cannot attribute." A quarantine-write failure is
logged and swallowed so the live consumer degrades rather than crashing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    derive_broker_event_key,
    derive_broker_fill_key,
    fill_report_to_fill_record,
    order_id_for_report,
    terminal_order_status_for,
)
from alphamind.execution.oms.command_ids import (
    is_engine_originated,
    is_pm_originated,
    parse_engine_command_id,
    parse_pm_command_id,
)
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.execution.write_paths.fill_persistence import append_fill_record
from alphamind.execution.write_paths.order_status_sync import sync_terminal_order_status
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
    mark_unattributed_fill_alerted,
)
from alphamind.state.records import FillRecord, UnattributedFill
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.theses import ThesisRow

log = logging.getLogger(__name__)

# Per ALP-528 (paper-evaluation harness wedge): paper-mode wiring passes a
# callable that enriches each translated FillRecord with a
# ``live_execution_estimate`` before persistence. Live-mode passes ``None`` so
# the hot path is unchanged. Single definition; ``task`` / ``unattributed_drain``
# / the backfill import it from here.
EnrichmentCallable = Callable[[FillRecord], Awaitable[FillRecord]]


class _BrokerCarriedLink:
    """The decoded broker-carried Intent FK a fill self-attributes through.

    Carries the originating thesis (*why*) and invocation (*when*) parsed out of
    the fill's ``client_order_id`` (story 01b). ``invocation_id`` is the full
    ``inv-``-prefixed form, directly FK-valid against ``invocations``.
    """

    __slots__ = ("invocation_id", "thesis_id")

    def __init__(self, *, thesis_id: ThesisId, invocation_id: InvocationId) -> None:
        self.thesis_id = thesis_id
        self.invocation_id = invocation_id


def _parse_broker_carried_link(client_order_id: str) -> _BrokerCarriedLink | None:
    """Parse the broker-carried link from a fill's ``client_order_id``.

    Tries the PM-originated form (``inv-…~the-…``) then the engine-originated
    form (``MON.…~the-…~inv-…``); both round-trip the thesis + invocation FK
    (ALP-844). Returns ``None`` for any ``client_order_id`` that carries no
    parseable link — a native-bracket protective child (Alpaca-generated id) or
    a genuinely out-of-band / manually-placed order.
    """
    if is_pm_originated(client_order_id):
        pm = parse_pm_command_id(client_order_id)
        return _BrokerCarriedLink(thesis_id=pm.thesis_id, invocation_id=pm.invocation_id)
    if is_engine_originated(client_order_id):
        engine = parse_engine_command_id(client_order_id)
        return _BrokerCarriedLink(thesis_id=engine.thesis_id, invocation_id=engine.invocation_id)
    return None


async def persist_fill_report(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
) -> None:
    """Self-attribute a single ``FillReport`` and persist it in its own transaction.

    A fill-bearing report self-attributes by parsing the broker-carried link
    (story 01b) out of its ``client_order_id``:

    * **Linked** (PM- or engine-originated) → append to ``broker_event_log``
      (idempotent on ``event_key``) carrying the decoded ``thesis_id`` /
      ``invocation_id`` / ``position_id`` — no ``orders`` row required. The order
      row, if it resolves, is an optional projection cache that *also* yields a
      ``fill_records`` row so Phase 1 integrates the fill.
    * **No parseable link** (out-of-band / manually-placed order) → quarantine to
      ``unattributed_fills`` and alert once. No AlphaMind-submitted fill reaches
      this branch — the link always resolves it (ADR-0002).

    Paper-mode wiring (per ALP-528) injects ``enrichment_callable`` so each
    translated :class:`FillRecord` is enriched with a ``live_execution_estimate``
    before the ``fill_records`` append. Live mode passes ``None``; the column
    persists as NULL and the hot path is unchanged.
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

    link = _parse_broker_carried_link(report.client_order_id)
    if link is None:
        # No broker-carried link — a genuinely out-of-band / manually-placed
        # order. Park it on the queue and alert once; an AlphaMind-submitted
        # fill never reaches here (its link always parses).
        await _quarantine_unattributed_fill(report, session_factory=session_factory)
        return

    # Self-attribute via the link: append the broker-fact to the event log with
    # the decoded thesis/invocation, the position resolved off the thesis edge.
    # The ``orders`` row is optional — only an enrichment hop for ``fill_records``.
    async with session_factory() as db:
        position_id = await _resolve_position_id(db, link.thesis_id)
        await append_broker_event(db, _fill_event_record(report, link, position_id))
        oms_order_id = await _resolve_oms_order_id(db, report)
        if oms_order_id is not None:
            if oms_order_id != record.order_id:
                # Re-derive so ``order_id`` + ``fill_id`` reflect the resolved PK.
                record = fill_report_to_fill_record(report, oms_order_id=oms_order_id)
            if record is not None:
                if enrichment_callable is not None:
                    record = await enrichment_callable(record)
                await append_fill_record(db, record)
        await db.commit()


def _fill_event_record(
    report: FillReport,
    link: _BrokerCarriedLink,
    position_id: PositionId | None,
) -> BrokerEventRecord:
    """Project a self-attributed fill into its append-only event-log record."""
    return BrokerEventRecord(
        event_key=derive_broker_event_key(report),
        event_type=BrokerEventType.FILL,
        thesis_id=link.thesis_id,
        invocation_id=link.invocation_id,
        position_id=position_id,
        raw_payload_json=report.model_dump_json(),
        broker_timestamp=report.fill_timestamp,
        captured_at=datetime.now(UTC),
    )


async def _resolve_position_id(db: AsyncSession, thesis_id: ThesisId) -> PositionId | None:
    """Resolve the ``position_id`` a fill attributes to off the thesis edge.

    The broker-carried link names the thesis (*why*); the position is the
    thesis's one-to-one ``positions`` row (ADR-0002 § position→thesis Intent
    edge). Optional enrichment: a fill arriving before the thesis row commits
    (genesis OPEN) leaves ``position_id`` NULL on the event-log row — the
    thesis FK still attributes it, and the position fills in on a later read.
    """
    stmt = select(ThesisRow.position_id).where(ThesisRow.thesis_id == thesis_id)
    position_id = (await db.execute(stmt)).scalars().one_or_none()
    return PositionId(position_id) if position_id is not None else None


async def _quarantine_unattributed_fill(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Park an out-of-band fill on the queue and emit a one-time alert.

    Reached only for a fill that carries no parseable broker-carried link — a
    genuinely out-of-band / manually-placed order (an AlphaMind-submitted fill
    always self-attributes through its link, ADR-0002). Idempotent on
    ``broker_fill_key`` so a websocket + recovery replay of the same fill
    collapses to one row. The alert is a loud, greppable ``log.warning`` — the
    continuous monitor has no invocation-handle alert channel, so collector.log
    WARNINGs are the operator's alert surface. It fires once: the warning +
    ``mark_unattributed_fill_alerted`` run only when ``append_unattributed_fill``
    newly inserts the row, so a re-delivered / re-parked fill (ON CONFLICT DO
    NOTHING) does not re-fire the alert.

    A quarantine-write failure must NOT crash the consumer — it would otherwise
    propagate into the reconnect-budget supervisor and burn an attempt / exit
    the task. A transient DB error is logged at ERROR and swallowed; the fill is
    recovered by the next reconnect-driven recovery or the periodic backfill
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
                    "QUARANTINED out-of-band fill: broker_fill_key=%s client_order_id=%s "
                    "alpaca_order_id=%s event=%s — no broker-carried link to self-attribute; "
                    "parked for operator review (out-of-band / manually-placed order)",
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
