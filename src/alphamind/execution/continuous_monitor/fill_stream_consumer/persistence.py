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
resolves, the fill is *also* appended to ``fill_records`` so fill collection integrates
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

from sqlalchemy import func, select
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
from alphamind.execution.write_paths.order_status_sync import terminal_status_event_record
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
    mark_unattributed_fill_alerted,
)
from alphamind.persistence.write_unit import run_immediate_write_unit
from alphamind.portfolio_state.records.positions import LiveExecutionEstimate
from alphamind.state.records import FillRecord, UnattributedFill
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
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


class _FillAttribution:
    """The thesis/invocation/position a fill attributes to, plus its order PK.

    Two resolution sources (ADR-0002):

    * the **broker-carried link** parsed out of ``client_order_id`` — the primary
      path, carrying both thesis (*why*) and invocation (*when*);
    * the **order-row projection cache** (resolved by broker UUID / pre-committed
      ``client_order_id``, ALP-746) — the path a native-bracket protective child
      takes (Alpaca generates its link-less ``client_order_id``), attributing via
      the order's position→thesis edge with no invocation.

    ``oms_order_id`` is the local ``orders`` PK when one resolves (the optional
    ``fill_records`` enrichment hop), else ``None``.
    """

    __slots__ = ("invocation_id", "oms_order_id", "position_id", "thesis_id")

    def __init__(
        self,
        *,
        thesis_id: ThesisId | None,
        invocation_id: InvocationId | None,
        position_id: PositionId | None,
        oms_order_id: str | None,
    ) -> None:
        self.thesis_id = thesis_id
        self.invocation_id = invocation_id
        self.position_id = position_id
        self.oms_order_id = oms_order_id


class _ProvisionalEstimate:
    """The Scope D enrichment cache: a resolution fingerprint + the estimate.

    Produced by :func:`_provisional_enrichment` on a plain read session BEFORE
    the ``BEGIN IMMEDIATE`` write unit opens, so the (potentially slow)
    enrichment await never holds the cross-process write lock. The fingerprint
    — the resolved ``oms_order_id`` and the (gap-reconciled) ``fill_quantity``
    — lets the write unit attach the cached estimate only when its own
    re-resolution under the write lock landed on the same facts.
    """

    __slots__ = ("estimate", "fill_quantity", "oms_order_id")

    def __init__(
        self,
        *,
        oms_order_id: str,
        fill_quantity: float | None,
        estimate: LiveExecutionEstimate | None,
    ) -> None:
        self.oms_order_id = oms_order_id
        self.fill_quantity = fill_quantity
        self.estimate = estimate


async def _provisional_enrichment(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable,
    recovered: bool,
) -> _ProvisionalEstimate | None:
    """Resolve + reconcile on a plain read session, then enrich (Scope D).

    The read pass mirrors the write unit's resolution (FS1 gap reconcile for a
    recovered report, then attribution) but stages NOTHING — its session is
    closed before ``enrichment_callable`` is awaited, so no transaction is open
    during the enrichment's own DB-backed lookups. Returns ``None`` when no
    order-bearing attribution resolves provisionally (nothing to enrich); the
    write unit then persists a NULL estimate if its own resolution does land an
    order — NULL is the legal live-mode state.
    """
    async with session_factory() as db:
        provisional_report = report
        if recovered:
            reconciled = await _reconcile_recovered_fill_to_gap(db, report)
            if reconciled is None:
                return None
            provisional_report = reconciled
        attribution = await _resolve_attribution(db, provisional_report)
        if attribution is None or attribution.oms_order_id is None:
            return None
        record = fill_report_to_fill_record(
            provisional_report, oms_order_id=attribution.oms_order_id
        )
        assert record is not None
    enriched = await enrichment_callable(record)
    return _ProvisionalEstimate(
        oms_order_id=attribution.oms_order_id,
        fill_quantity=provisional_report.fill_quantity,
        estimate=enriched.live_execution_estimate,
    )


def _attach_provisional_estimate(
    record: FillRecord,
    *,
    final_oms_order_id: str,
    final_fill_quantity: float | None,
    provisional: _ProvisionalEstimate | None,
) -> FillRecord:
    """Attach the cached estimate when the write unit re-resolved the same facts.

    The write unit's re-resolution (under the up-front write lock) is the
    authoritative one; the provisional estimate transfers only when both the
    ``oms_order_id`` and the gap-reconciled quantity match the provisional
    fingerprint. On any other outcome the record persists with
    ``live_execution_estimate = NULL`` (an existing legal state; live mode
    always persists NULL): a genuine fingerprint divergence — the order row or
    the logged-quantity sum moved between the two passes — logs a WARNING,
    while a fill whose order resolved only under the write lock (nothing was
    enriched provisionally, an expected commit-ordering race) logs at DEBUG.
    """
    if provisional is None:
        log.debug(
            "no provisional enrichment for fill_id=%s (the order resolved only "
            "under the write lock); persisting live_execution_estimate=NULL",
            record.fill_id,
        )
        return record
    if (
        provisional.oms_order_id == final_oms_order_id
        and provisional.fill_quantity == final_fill_quantity
    ):
        return record.model_copy(update={"live_execution_estimate": provisional.estimate})
    log.warning(
        "provisional enrichment diverged from the write unit's re-resolution: "
        "provisional=(order_id=%s qty=%s) final=(order_id=%s qty=%s) — "
        "persisting live_execution_estimate=NULL for fill_id=%s",
        provisional.oms_order_id,
        provisional.fill_quantity,
        final_oms_order_id,
        final_fill_quantity,
        record.fill_id,
    )
    return record


async def persist_fill_report(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
    recovered: bool = False,
) -> None:
    """Self-attribute a single ``FillReport`` and persist it in its own transaction.

    A fill-bearing report self-attributes (ADR-0002) and appends to the
    append-only ``broker_event_log`` (idempotent on the ``event_key`` PK)
    carrying the decoded ``thesis_id`` / ``invocation_id`` / ``position_id`` —
    **no local ``orders`` row is required for attribution**:

    * **Linked** (PM- or engine-originated ``client_order_id``) → thesis +
      invocation come straight off the link; the position is the thesis's
      ``positions`` row.
    * **Order-row projection cache** (a link-less native-bracket protective
      child) → thesis + position come off the resolved order's position edge;
      invocation is ``NULL`` (no link carries the *when*).
    * **Neither a link nor a resolvable order row** → a genuinely out-of-band /
      manually-placed order → quarantine to ``unattributed_fills`` and alert once.
      No AlphaMind-submitted fill reaches this branch (ADR-0002).

    When the ``orders`` row resolves it is *also* appended to ``fill_records`` so
    Fill collection integrates the fill. Paper-mode wiring (per ALP-528) injects
    ``enrichment_callable`` so each translated :class:`FillRecord` is enriched
    with a ``live_execution_estimate`` before that append; live mode passes
    ``None`` and the column persists as NULL.

    ``recovered`` distinguishes a LIVE websocket fill (``False``, the default)
    from a REST-recovery snapshot (``True``, the disconnect-recovery / periodic
    backfill sweeps). The live websocket logs PER-EVENT partial increments
    (``update.qty`` / ``update.price``); the REST sweep — sourced from Alpaca's
    ``GET /v2/orders`` — yields ONE report carrying the order's CUMULATIVE
    ``filled_qty`` / ``filled_avg_price``. Appending that cumulative as-is would
    log the same shares twice (the partials AND the cumulative) → the 03c PnL
    fold double-counts (FS1). So a recovered fill is reconciled to the RESIDUAL
    GAP — the cumulative minus the quantity already logged for that
    ``alpaca_order_id`` — and only the gap is appended (gap-free, exactly-once,
    realizing 03b's recovery intent). A non-positive gap appends nothing (the
    log is already complete for that order); idempotency holds because a second
    recovery pass sees gap == 0.

    The write runs as one ``BEGIN IMMEDIATE`` unit via
    :func:`run_immediate_write_unit` (ALP-942): reconcile + resolve + event
    append + fill append in a single atomic transaction (B4) whose write lock
    is held from BEGIN, so a concurrent committer (collector / scheduler)
    serializes behind ``busy_timeout`` instead of invalidating a deferred read
    snapshot into an instant ``SQLITE_BUSY_SNAPSHOT``. The paper-mode
    enrichment await runs BEFORE the unit on a provisional read-only pass
    (Scope D) so the write lock is never held across it.
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

    provisional: _ProvisionalEstimate | None = None
    if enrichment_callable is not None:
        provisional = await _provisional_enrichment(
            report,
            session_factory=session_factory,
            enrichment_callable=enrichment_callable,
            recovered=recovered,
        )

    async def _unit(db: AsyncSession) -> bool:
        # One IMMEDIATE transaction spans reconcile + resolution + the
        # appends, removing the TOCTOU window between resolving attribution
        # and writing the event/fill rows (B4). A retried attempt re-runs the
        # whole unit on a fresh session; the appends are idempotent at the
        # storage layer (ON CONFLICT DO NOTHING).
        unit_report = report
        if recovered:
            # FS1 — a recovery report carries the order's CUMULATIVE fill; reduce
            # it to the residual gap over what is already logged, or skip entirely
            # when the log is already complete for this order. Done in this
            # DB-bearing layer (not the broker adapter, which must not query the
            # local DB) so recovery.py stays a pure broker→FillReport translator.
            reconciled = await _reconcile_recovered_fill_to_gap(db, unit_report)
            if reconciled is None:
                return True
            unit_report = reconciled
        attribution = await _resolve_attribution(db, unit_report)
        if attribution is None:
            # No link AND no resolvable+attributable order-row projection cache —
            # a genuinely out-of-band fill, or a native-bracket child whose
            # position→thesis edge has not committed yet (B1). Quarantine it
            # (outside this unit — the park is its own write transaction); the
            # drain retries an order that later resolves.
            return False
        await append_broker_event(db, _fill_event_record(unit_report, attribution))
        if attribution.oms_order_id is not None:
            # Re-derive so ``order_id`` + ``fill_id`` reflect the resolved PK
            # and the reconciled residual quantity. Only fill-bearing reports
            # reach this block, so the translator returns a record.
            unit_record = fill_report_to_fill_record(
                unit_report, oms_order_id=attribution.oms_order_id
            )
            assert unit_record is not None
            if enrichment_callable is not None:
                unit_record = _attach_provisional_estimate(
                    unit_record,
                    final_oms_order_id=attribution.oms_order_id,
                    final_fill_quantity=unit_report.fill_quantity,
                    provisional=provisional,
                )
            await append_fill_record(db, unit_record)
        return True

    attributed = await run_immediate_write_unit(session_factory, _unit)
    if not attributed:
        await _quarantine_unattributed_fill(report, session_factory=session_factory)


async def _resolve_attribution(db: AsyncSession, report: FillReport) -> _FillAttribution | None:
    """Resolve the thesis/invocation/position a fill attributes to, or ``None``.

    The broker-carried link is the primary source (thesis + invocation); the
    order-row projection cache (ALP-746) is the fallback for a link-less
    native-bracket protective child (thesis + position via the order's position
    edge, no invocation). Returns ``None`` — so the fill is quarantined to
    ``unattributed_fills`` for the drain to retry — in two cases:

    * neither a link nor a resolvable order row — a genuinely out-of-band order;
    * the projection-cache path resolves an order row but BOTH its thesis_id and
      position_id are still None (the order's position→thesis edge has not
      committed yet, ALP-845/B1). A ``broker_event_log`` row is append-only and
      never enriched, so landing one with a NULL link strands the fill forever —
      treat it as not-yet-attributable and let the drain re-resolve it once the
      edge commits.
    """
    link = _parse_broker_carried_link(report.client_order_id)
    if link is not None:
        # FS5 — ``broker_event_log.thesis_id`` is DEFERRABLE INITIALLY DEFERRED, so
        # appending a row that names a not-yet-committed thesis passes the INSERT
        # but violates at ``db.commit()`` and strands the whole report (neither
        # logged nor quarantined). On a genesis OPEN fast-fill the thesis row may
        # not have committed yet; treat it as not-yet-attributable and quarantine
        # (return None) so the drain retries once the thesis lands — exactly the
        # uncommitted-edge quarantine the order-row path already takes below.
        if not await _thesis_row_exists(db, link.thesis_id):
            return None
        # The linked path needs the thesis (from the link) and its position; the
        # order-row resolution hop is only relevant on the projection-cache path.
        position_id = await _resolve_position_id_for_thesis(db, link.thesis_id)
        return _FillAttribution(
            thesis_id=link.thesis_id,
            invocation_id=await _resolve_link_invocation_id(db, link.invocation_id),
            position_id=position_id,
            oms_order_id=await _resolve_oms_order_id(db, report),
        )
    oms_order_id = await _resolve_oms_order_id(db, report)
    if oms_order_id is not None:
        thesis_id, position_id = await _resolve_thesis_for_order(db, oms_order_id)
        if thesis_id is None and position_id is None:
            # Order resolves but its position→thesis edge is uncommitted — not
            # yet attributable. Quarantine (return None); the drain retries.
            return None
        return _FillAttribution(
            thesis_id=thesis_id,
            invocation_id=None,
            position_id=position_id,
            oms_order_id=oms_order_id,
        )
    return None


async def _reconcile_recovered_fill_to_gap(
    db: AsyncSession, report: FillReport
) -> FillReport | None:
    """Reduce a REST-recovery cumulative fill to its residual gap, or ``None`` (FS1).

    A recovery report (Alpaca ``GET /v2/orders``) carries the order's CUMULATIVE
    ``filled_qty`` as its ``fill_quantity`` at the cumulative ``filled_avg_price``,
    whereas the live websocket logs PER-EVENT partial increments. Appending the
    cumulative as-is re-logs shares the partials already captured → the 03c fold
    double-counts. So compute::

        gap = cumulative_filled_quantity - sum(already-logged FILL qty for this order)

    and return a residual :class:`FillReport` of exactly ``gap`` shares at the
    cumulative price, so ``broker_event_log`` reflects the order's true total
    EXACTLY ONCE. ``None`` (append nothing) when:

    * the report carries no cumulative (``None``) — unknown-not-zero, nothing to
      reconcile (mirrors the FS3 guard); the live/fill path owns it; or
    * ``gap <= 0`` — the log is already complete for this order (e.g. a second
      recovery pass after the residual landed sees gap == 0), so the append is a
      no-op and idempotency holds.

    The residual self-attributes through the SAME broker-carried link / order-row
    path as the cumulative report it derived from (only ``fill_quantity`` changes).
    """
    cumulative = report.cumulative_filled_quantity
    if cumulative is None:
        return None
    already_logged = await _logged_fill_quantity_for_order(db, report.alpaca_order_id)
    gap = cumulative - already_logged
    if gap <= 0:
        return None
    return report.model_copy(update={"fill_quantity": gap})


async def _logged_fill_quantity_for_order(db: AsyncSession, alpaca_order_id: str) -> float:
    """Sum the ``fill_quantity`` already logged as FILL rows for *alpaca_order_id*.

    The quantity the 03c fold sees for the order: every ``broker_event_log`` FILL
    row whose payload's ``alpaca_order_id`` matches contributes its per-event
    ``fill_quantity``. ``alpaca_order_id`` / ``fill_quantity`` live in the
    JSON ``raw_payload_json`` (canonical ``serialize_event_payload`` of the
    report), so the sum is pushed into SQL via SQLite-native ``json_extract``
    rather than decoding every row in Python. Returns ``0.0`` when no FILL row
    for the order has been logged yet (a genuinely-dropped fill the recovery
    sweep is the first to see).
    """
    fill_qty = func.json_extract(BrokerEventLogRow.raw_payload_json, "$.fill_quantity")
    stmt = select(func.coalesce(func.sum(fill_qty), 0.0)).where(
        BrokerEventLogRow.event_type == BrokerEventType.FILL.value,
        func.json_extract(BrokerEventLogRow.raw_payload_json, "$.alpaca_order_id")
        == alpaca_order_id,
    )
    return float((await db.execute(stmt)).scalar_one())


def _fill_event_record(
    report: FillReport,
    attribution: _FillAttribution,
) -> BrokerEventRecord:
    """Project a self-attributed fill into its append-only event-log record."""
    return BrokerEventRecord(
        event_key=derive_broker_event_key(report),
        event_type=BrokerEventType.FILL,
        thesis_id=attribution.thesis_id,
        invocation_id=attribution.invocation_id,
        position_id=attribution.position_id,
        # F1 — encode like the other three event producers (account-activities,
        # corporate-actions, terminal order status): the canonical sort_keys +
        # default=str serializer, so FILL rows are byte-consistent with the rest
        # of the log. No correctness change — event_key is tuple-derived.
        raw_payload_json=serialize_event_payload(report.model_dump(mode="json")),
        broker_timestamp=report.fill_timestamp,
        captured_at=datetime.now(UTC),
    )


async def _thesis_row_exists(db: AsyncSession, thesis_id: ThesisId) -> bool:
    """Whether the linked ``theses`` row has committed (FS5).

    The broker-carried link names the thesis a fill attributes to, but
    ``broker_event_log.thesis_id`` is a DEFERRABLE INITIALLY DEFERRED FK — a row
    that names a thesis whose ``theses`` row has not committed passes the INSERT
    and only violates at ``db.commit()``, stranding the whole report. Checking
    existence up front (mirroring :func:`_resolve_position_id_for_thesis`'s cheap
    ``select``) lets the caller quarantine the fill for the drain to retry once
    the thesis lands, instead of crashing the transaction.
    """
    stmt = select(ThesisRow.thesis_id).where(ThesisRow.thesis_id == thesis_id)
    return (await db.execute(stmt)).scalars().one_or_none() is not None


async def _resolve_link_invocation_id(
    db: AsyncSession, invocation_id: InvocationId
) -> InvocationId | None:
    """Return the linked ``invocation_id`` only when its ``invocations`` row exists.

    CL3 — on a cold-start DB the monitor's invocation provider returns the
    ``monitor-bootstrap`` sentinel, which the closer weaves into the engine
    ``client_order_id`` and a returning fill parses back as
    ``InvocationId("inv-monitor-bootstrap")``. ``broker_event_log.invocation_id``
    is a DEFERRABLE INITIALLY DEFERRED FK to ``invocations``, so storing a value
    with no matching row violates at ``db.commit()`` and strands the fill. The
    column is nullable and the thesis link still attributes the fill, so a link
    invocation with no committed ``invocations`` row is stored as ``NULL`` rather
    than as a non-existent FK. This special-cases the bootstrap sentinel without
    hard-coding it: any not-yet-committed invocation nulls the same way.
    """
    stmt = select(InvocationRow.invocation_id).where(InvocationRow.invocation_id == invocation_id)
    exists = (await db.execute(stmt)).scalars().one_or_none() is not None
    return invocation_id if exists else None


async def _resolve_position_id_for_thesis(
    db: AsyncSession, thesis_id: ThesisId
) -> PositionId | None:
    """Resolve the ``position_id`` a thesis owns (the thesis→position edge).

    The broker-carried link names the thesis (*why*); the position is the
    thesis's one-to-one ``positions`` row (ADR-0002 § position→thesis Intent
    edge). A linked fill arriving before the thesis's position row commits
    (genesis OPEN) leaves ``position_id`` NULL on the event-log row — the thesis
    FK still attributes it, and the row STAYS NULL (append-only, never enriched,
    ADR-0005). A read-time JOIN through ``theses → positions`` recovers the
    position from the thesis FK; the column itself is not backfilled.
    """
    stmt = select(ThesisRow.position_id).where(ThesisRow.thesis_id == thesis_id)
    position_id = (await db.execute(stmt)).scalars().one_or_none()
    return PositionId(position_id) if position_id is not None else None


async def _resolve_thesis_for_order(
    db: AsyncSession, oms_order_id: str
) -> tuple[ThesisId | None, PositionId | None]:
    """Resolve ``(thesis_id, position_id)`` off a resolved order's position edge.

    The link-less native-bracket protective child attributes through the
    projection cache: the order row's ``position_id`` names the position, whose
    ``thesis_id`` names the thesis (ADR-0002). Either may be ``NULL`` on the
    event-log row when the order row is not yet position-linked.
    """
    stmt = (
        select(ThesisRow.thesis_id, OrderRow.position_id)
        .select_from(OrderRow)
        .outerjoin(PositionRow, OrderRow.position_id == PositionRow.position_id)
        .outerjoin(ThesisRow, PositionRow.thesis_id == ThesisRow.thesis_id)
        .where(OrderRow.order_id == oms_order_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None, None
    thesis_id, position_id = row
    return (
        ThesisId(thesis_id) if thesis_id is not None else None,
        PositionId(position_id) if position_id is not None else None,
    )


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

    async def _unit(db: AsyncSession) -> bool:
        inserted = await append_unattributed_fill(db, record)
        if inserted:
            await mark_unattributed_fill_alerted(db, record.broker_fill_key)
        return inserted

    try:
        inserted = await run_immediate_write_unit(session_factory, _unit)
    except Exception:
        log.exception(
            "failed to quarantine unattributed fill: broker_fill_key=%s client_order_id=%s "
            "alpaca_order_id=%s event=%s — continuing; recovered by next recovery/backfill sweep",
            record.broker_fill_key,
            report.client_order_id,
            report.alpaca_order_id,
            report.event_type,
        )
        return
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


async def _sync_terminal_status_if_any(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Append a zero-fill terminal order-status event to the log (ALP-739 / W1c).

    ``canceled`` / ``expired`` events append no fill but the order has reached a
    terminal disposition — otherwise an accepted entry that expires / cancels
    unfilled stays ``PENDING`` and the ``entry_no_fill`` alert never fires. Every
    other non-fill event (``new`` / ``replaced`` / …) carries no terminal
    disposition and no-ops here.

    **The monitor no longer RMWs ``orders.status``** (ADR-0005 invariant 1, the
    named second-writer being removed). A terminal event lands as one immutable
    ``TERMINAL_ORDER_STATUS`` row on the append-only ``broker_event_log``
    (idempotent on the ``event_key`` PK), carrying the resolved broker-carried
    link on its INITIAL insert. The ``orders.status`` projection is derived from
    the log by the single (pipeline) writer (04a). Own short-lived transaction,
    mirroring the per-fill write.

    Scoped to **known zero-fill** terminals (``cumulative_filled_quantity == 0``):
    a partially-filled-then-terminal order is left to the fill path + fill collection,
    which own ``filled_quantity`` and integrate the partials. The no-fill case is
    the one the fill path does not cover, so it is the only one this path owns
    (the ALP-739 zero-fill scoping, preserved).

    A ``None`` cumulative is unknown-not-zero (FS3): the broker reported no
    ``filled_qty``, so this path cannot prove the order is no-fill — a
    partially-filled-then-canceled order can arrive with a ``None`` cumulative,
    and projecting a no-fill terminal over it would mis-fire ``entry_no_fill`` /
    retire an order that had partials. So the append is skipped for an unknown
    cumulative too; the fill path owns it.
    """
    terminal_status = terminal_order_status_for(report)
    if terminal_status is None:
        return
    cumulative = report.cumulative_filled_quantity
    if cumulative is None or cumulative > 0:
        return

    async def _unit(db: AsyncSession) -> bool | None:
        # The production ALP-942 failure path: 1-3 attribution SELECTs followed
        # by the event INSERT in one transaction. Under BEGIN IMMEDIATE the
        # reads run behind the up-front write lock, so a concurrent committer
        # (the collector's half-hourly news commit, a mid-pipeline scheduler
        # write) can no longer invalidate this transaction's read snapshot.
        attribution = await _resolve_terminal_attribution(db, report)
        if attribution is None:
            return None
        return await append_broker_event(
            db,
            terminal_status_event_record(
                report,
                terminal_status,
                thesis_id=attribution.thesis_id,
                invocation_id=attribution.invocation_id,
                position_id=attribution.position_id,
            ),
        )

    newly = await run_immediate_write_unit(session_factory, _unit)
    if newly is None:
        # Neither a parseable link nor a resolvable local order row — a
        # genuinely out-of-band / manually-placed order's terminal event.
        # No AlphaMind fact to record; skip (mirrors the fill path's
        # decline-to-attribute, ADR-0002).
        log.debug(
            "terminal status for unknown order: client_order_id=%s alpaca_order_id=%s "
            "status=%s — skipping",
            report.client_order_id,
            report.alpaca_order_id,
            terminal_status.value,
        )
    elif newly:
        log.info(
            "appended terminal order-status event: alpaca_order_id=%s status=%s",
            report.alpaca_order_id,
            terminal_status.value,
        )


async def _resolve_terminal_attribution(
    db: AsyncSession, report: FillReport
) -> _FillAttribution | None:
    """Resolve the link a zero-fill terminal event attributes to, or ``None``.

    Mirrors :func:`_resolve_attribution` but for a non-fill event (there is no
    fill to quarantine, so it never returns the "uncommitted edge" quarantine
    signal — it carries whatever link resolves):

    * **Broker-carried link** (linked entry whose ``client_order_id`` is the PM /
      engine command id) → thesis + invocation off the link; position off the
      thesis's ``positions`` edge.
    * **Order-row projection cache** (an OCO sibling-cancel of a native-bracket
      protective child, whose ``client_order_id`` Alpaca generated, resolved by
      broker UUID, ALP-746) → thesis + position off the order's position edge;
      invocation ``NULL`` (no link carries the *when*).
    * Neither → ``None`` (a genuinely out-of-band order's terminal event).
    """
    link = _parse_broker_carried_link(report.client_order_id)
    if link is not None:
        return _FillAttribution(
            thesis_id=link.thesis_id,
            invocation_id=await _resolve_link_invocation_id(db, link.invocation_id),
            position_id=await _resolve_position_id_for_thesis(db, link.thesis_id),
            oms_order_id=None,
        )
    oms_order_id = await _resolve_oms_order_id(db, report)
    if oms_order_id is None:
        return None
    thesis_id, position_id = await _resolve_thesis_for_order(db, oms_order_id)
    return _FillAttribution(
        thesis_id=thesis_id,
        invocation_id=None,
        position_id=position_id,
        oms_order_id=oms_order_id,
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
       because the row carries NO broker id yet — ``alpaca_order_id`` NULL, ALP-847).
    3. (ALP-746) Otherwise resolve by the broker UUID of the order that owns the
       local row: for an mleg per-leg child that is the parent's
       ``parent_alpaca_order_id`` (legs do not own ``orders`` rows); for an
       equity entry / close / native-bracket protective child it is the report's
       own ``alpaca_order_id``. The captured-at-submission UUID was written onto
       that row (entry / close / TAKE_PROFIT / PRICE_STOP), so the lookup hits.

    Returns ``None`` when none resolves — the caller declines to attribute
    the event rather than violate the ``fill_records.order_id`` FK.

    A non-NULL ``alpaca_order_id`` is expected unique across ``orders`` rows
    (captured broker UUIDs are distinct per order; ALP-847 — an order with no
    broker counterpart carries NULL, which the UUID lookup never matches against a
    real broker fill), so the UUID lookup uses ``one_or_none`` — a duplicate
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
