"""Projection rebuild from the broker-event log + the live broker snapshot (ALP-854 / W2a).

The post-fill-integration step that **replaces** ``reconcile()``'s adjudication
path (ADR-0001). Positions / cash are a derived, rebuildable **Projection**, not
a co-equal Mirror patched toward Alpaca on drift. There is no "reconcile," only
"rebuild": the broker-event log is the authority-flow (broker→local), the live
broker snapshot (``get_positions`` / ``get_account``) is the checkpoint that
rebuilds it (decision H — no persisted checkpoint table), and a
snapshot/projection mismatch triggers a rebuild rather than a per-delta
comparison-and-correct. No ``RECONCILIATION_ALERT`` / ``RECONCILIATION_CORRECTION``
row is ever written here.

Four derivations run in the open Phase-1 write transaction (single writer =
pipeline, ADR-0005):

1. **Order-status projection** — fold every ``TERMINAL_ORDER_STATUS`` event in
   the log onto the ``orders`` cache, setting ``status`` + ``last_update_timestamp``
   (W1c removed the monitor's RMW; nothing else projected the events, so an
   unfilled accepted entry stayed ``PENDING`` forever and the ``entry_no_fill``
   alert never matched). The order row is an optional projection cache — a zero-fill
   terminal event advances its cached status to ``CANCELLED`` / ``EXPIRED``.

2. **Entry-window cancel cascade** (ALP-863) — with ``orders.status`` projected,
   every bracket still ``PENDING_ENTRY`` whose ENTRY order just reached a terminal
   status with no recorded fills is dissolved here: the bracket goes DISSOLVED, its
   legs cancel, the reserved capital releases, the thesis resolves
   ``CANCELLED_NEVER_ENTERED``, and the never-filled position goes PENDING→CANCELLED.
   This relocates the monitor's old entry-window cancel writeback (a residual
   cross-process RMW reachable by the ``SQLITE_BUSY_SNAPSHOT`` race) onto the single
   pipeline writer; the monitor keeps only the real-time broker cancel.

3. **Entry-window reprice projection** (ALP-867) — for every still-resting
   ``PENDING_ENTRY`` entry the monitor cancel-and-replaced toward the market, fold
   its ``ENTRY_REPRICED`` events (in ``event_seq`` order) onto the order cache via
   ``persist_entry_window_reprice``: the new ``limit_price`` / ``alpaca_order_id`` /
   ``modification_count`` and the reservation adjustment. This relocates the
   monitor's old reprice writeback (the residual half of the RMW the cancel cascade
   left behind) onto the single pipeline writer. Idempotent: an event whose new
   broker id is already in the order's ``alpaca_order_id_chain`` has been projected,
   so re-projecting reproduces the same row. Sequenced **after** the cancel cascade
   and gated on ``PENDING_ENTRY`` + non-terminal + no-fills so a terminal (cancelled)
   order's dissolve wins over any stale reprice event.

4. **Broker-fact-no-Intent classification** — a broker position with no matching
   local Intent overlay (the DVN / manual-trade case) is a **first-class
   projection state**, surfaced in the summary (attach/flag), never a reconcile
   alert. The "Alpaca wins" auto-materialize / alert doctrine is deleted.

The per-thesis PnL ledger is **not** re-derived here (CR1-cleanup): that is the
sole responsibility of the orchestrator's post-poll
:func:`alphamind.execution.write_paths.phase1.rederive_thesis_ledgers`, which runs
after the account-activities poll so it folds the *complete* log. Re-deriving the
ledgers in the rebuild too — before the poll — was a redundant double-write the
post-poll pass overwrote.

Functional core / imperative shell: the classification and event-fold are pure
functions over plain records; :func:`rebuild_projection` is the thin shell that
reads the log, calls the core, and writes the projection cache.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.money import price
from alphamind.execution.broker_adapter.queries import PositionSnapshot, TradeAccountSnapshot
from alphamind.execution.write_paths.phase2 import (
    persist_entry_window_cancel,
    persist_entry_window_reprice,
)
from alphamind.portfolio_state.records.orders import BracketStatus, OrderStatus
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    alpaca_occ_symbol,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.records_broker_event_log import BrokerEventType
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import row_to_record as order_row_to_record
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record as position_row_to_record
from alphamind.state.tables.projection_rebuild_watermark import (
    PROJECTION_REBUILD_WATERMARK_SINGLETON_ID,
    ProjectionRebuildWatermarkRow,
)

log = logging.getLogger(__name__)

# Alpaca terminal disposition string → local terminal ``OrderStatus`` for the
# order-status projection. Only the zero-fill terminal dispositions reach a
# ``TERMINAL_ORDER_STATUS`` event (the fill path owns filled / partial); an
# unrecognised disposition is left unprojected (the order keeps its cached
# status) rather than guessed.
_TERMINAL_STATUS_BY_NAME: dict[str, OrderStatus] = {
    OrderStatus.CANCELLED.value: OrderStatus.CANCELLED,
    OrderStatus.EXPIRED.value: OrderStatus.EXPIRED,
    OrderStatus.REJECTED.value: OrderStatus.REJECTED,
}

# Position statuses whose local rows carry an Intent overlay (a thesis-linked
# position the pipeline authored). A broker snapshot position matching one of
# these is Intent-backed; one matching none is "broker fact, no Intent".
_LIVE_POSITION_STATUSES = ("OPEN", "PENDING")


@dataclass(frozen=True, slots=True)
class BrokerFactNoIntent:
    """A broker snapshot position with no matching local Intent overlay.

    The DVN / manual-trade case (ADR-0001): a broker-owned position the pipeline
    never authored. A first-class projection state surfaced in the rebuild
    summary — NOT a ``RECONCILIATION_ALERT``. The fields mirror the broker
    snapshot so an operator (or a read-time consumer) sees the broker fact
    verbatim.
    """

    symbol: str
    asset_class: str
    qty: float
    side: str


@dataclass(frozen=True, slots=True)
class PendingWithBrokerHolding:
    """A PENDING-local position whose broker snapshot holds a nonzero quantity.

    A PENDING local position carries quantity 0 by invariant (the entry has not
    been integrated yet); a nonzero broker holding for its symbol means the entry
    fill actually landed at the broker but was dropped / never integrated locally
    — a filled-but-locally-pending divergence. This restores the safety detection
    the deleted reconciler's ``pending_with_broker_holding`` escalation provided
    (RD1): the rebuild only DETECTS it (surfaced here, never mutated), and the
    honest recovery is the fill drain / periodic backfill writing the real fill
    into ``fill_records`` so the next Phase-1 integrates it and flips the position
    PENDING → OPEN with the true basis. ``qty`` is the unsigned magnitude (Alpaca
    reports a signed qty for shorts; ``side`` carries the direction).
    """

    symbol: str
    asset_class: str
    qty: float
    side: str


@dataclass(frozen=True, slots=True)
class OrderStatusProjection:
    """One ``TERMINAL_ORDER_STATUS`` event reduced to its order-cache projection.

    ``alpaca_order_id`` / ``client_order_id`` are the keys that resolve the local
    ``orders`` row (the broker UUID, or the durable pre-commit ``client_order_id``
    when the UUID was never backfilled); ``terminal_status`` is the disposition to
    project. The shell stamps ``last_update_timestamp`` at observation time so the
    ``entry_no_fill`` window sees a late-synced status.
    """

    alpaca_order_id: str | None
    client_order_id: str | None
    terminal_status: OrderStatus


@dataclass(frozen=True, slots=True)
class ProjectionRebuildSummary:
    """Outcome of one :func:`rebuild_projection` call.

    ``order_statuses_projected`` counts ``orders`` rows whose cached status the
    rebuild advanced from a TERMINAL_ORDER_STATUS event; ``entry_window_cancels_cascaded``
    counts the never-filled ``PENDING_ENTRY`` brackets the entry-window cancel cascade
    dissolved this rebuild (ALP-863); ``broker_facts_without_intent`` carries the
    DVN/manual-trade positions surfaced as a projection state (never alerted);
    ``pending_with_broker_holding`` carries PENDING-local positions whose broker holding
    is nonzero (a dropped/un-integrated entry fill — RD1), surfaced as a projection
    signal, never mutated.

    The rebuild does **not** re-derive the per-thesis PnL ledgers (CR1-cleanup):
    that is the sole responsibility of the orchestrator's post-poll
    :func:`alphamind.execution.write_paths.phase1.rederive_thesis_ledgers`, which
    folds the *complete* log (after the account-activities poll). Re-deriving here
    too would be a redundant pre-poll double-write the post-poll pass overwrites.
    """

    order_statuses_projected: int
    broker_facts_without_intent: tuple[BrokerFactNoIntent, ...]
    # Defaulted so the empty-rebuild constructors (recovery / scheduler stubs in
    # phase1.py) that predate RD1 keep their shape; the production rebuild always
    # populates it.
    pending_with_broker_holding: tuple[PendingWithBrokerHolding, ...] = ()
    # Defaulted for the same predate-this-field constructors; the production rebuild
    # always populates it (ALP-863).
    entry_window_cancels_cascaded: int = 0
    # Count of ``orders`` rows whose limit / broker id / modification_count the
    # entry-window reprice projection advanced from ``ENTRY_REPRICED`` events this
    # rebuild (ALP-867); defaulted for the predate-this-field constructors.
    entry_window_reprices_projected: int = 0


# ---------------------------------------------------------------------------
# Functional core — pure derivations over plain records.
# ---------------------------------------------------------------------------


def classify_broker_facts_without_intent(
    *,
    alpaca_positions: Iterable[PositionSnapshot],
    intent_backed_symbols: frozenset[str],
) -> tuple[BrokerFactNoIntent, ...]:
    """Pure: broker snapshot positions with no matching local Intent overlay.

    A broker position whose ``symbol`` is absent from ``intent_backed_symbols``
    (the set of OPEN/PENDING local-position keys — equity ticker or options OCC
    symbol) is the DVN/manual-trade case. ``sorted`` keeps the surfacing order
    deterministic.
    """
    facts = [
        BrokerFactNoIntent(
            symbol=snapshot.symbol,
            asset_class=snapshot.asset_class,
            qty=snapshot.qty,
            side=snapshot.side,
        )
        for snapshot in alpaca_positions
        if snapshot.symbol not in intent_backed_symbols
    ]
    return tuple(sorted(facts, key=lambda fact: fact.symbol))


_QTY_EPSILON = 1e-9


def classify_pending_with_broker_holding(
    *,
    alpaca_positions: Iterable[PositionSnapshot],
    pending_symbols: frozenset[str],
) -> tuple[PendingWithBrokerHolding, ...]:
    """Pure: PENDING-local positions whose broker snapshot quantity is nonzero.

    A broker snapshot position whose ``symbol`` is in ``pending_symbols`` (the set
    of PENDING local-position keys — equity ticker or options OCC symbol) and whose
    ``qty`` magnitude is nonzero is a dropped/un-integrated entry fill (RD1): the
    entry filled at the broker but the local position is still PENDING. Alpaca
    reports a signed ``qty`` for shorts, so the magnitude is compared and surfaced.
    ``sorted`` keeps the surfacing order deterministic.
    """
    holdings = [
        PendingWithBrokerHolding(
            symbol=snapshot.symbol,
            asset_class=snapshot.asset_class,
            qty=abs(snapshot.qty),
            side=snapshot.side,
        )
        for snapshot in alpaca_positions
        if snapshot.symbol in pending_symbols and abs(snapshot.qty) > _QTY_EPSILON
    ]
    return tuple(sorted(holdings, key=lambda holding: holding.symbol))


def project_terminal_order_statuses(
    payloads: Iterable[str],
) -> tuple[OrderStatusProjection, ...]:
    """Pure: fold ``TERMINAL_ORDER_STATUS`` event payloads into order-cache projections.

    Each payload is the canonical-JSON ``raw_payload_json`` a
    ``TERMINAL_ORDER_STATUS`` event carries — the broker ``FillReport`` dump plus
    a ``terminal_status`` key (see ``write_paths/order_status_sync.py``). A payload
    whose ``terminal_status`` is not a recognised zero-fill terminal disposition is
    skipped (the order keeps its cached status). Last-writer-wins per
    ``alpaca_order_id`` is not modelled — a terminal disposition is final, so a
    re-delivered event projects the same status.
    """
    projections: list[OrderStatusProjection] = []
    for payload in payloads:
        decoded = json.loads(payload)
        status_name = decoded.get("terminal_status")
        terminal_status = _TERMINAL_STATUS_BY_NAME.get(str(status_name))
        if terminal_status is None:
            continue
        projections.append(
            OrderStatusProjection(
                alpaca_order_id=decoded.get("alpaca_order_id"),
                client_order_id=decoded.get("client_order_id"),
                terminal_status=terminal_status,
            )
        )
    return tuple(projections)


# ---------------------------------------------------------------------------
# Imperative shell — read the log, call the core, write the projection cache.
# ---------------------------------------------------------------------------


async def rebuild_projection(
    handle: InvocationHandle,
    *,
    alpaca_positions: tuple[PositionSnapshot, ...],
    alpaca_account: TradeAccountSnapshot | None,
) -> ProjectionRebuildSummary:
    """Rebuild the positions/cash Projection from the event log + the live snapshot.

    Runs after Phase-1 fill integration has folded the event log into the
    positions/cash projection. This step adds the two derivations the fill-fold
    does not own: the order-status projection and the broker-fact-no-Intent
    classification. The per-thesis PnL ledgers are re-derived separately by the
    orchestrator's post-poll ``rederive_thesis_ledgers`` (CR1-cleanup), not here.
    Joins the open ``handle.session`` transaction; the surrounding
    ``InvocationContext`` commits.

    ``alpaca_account`` is accepted for symmetry with the deleted ``reconcile``
    signature (the cash projection rebuilds from the event log + snapshot via the
    fill-fold; no per-delta cash comparison runs here). A snapshot/projection
    mismatch is resolved by the rebuild itself — there is no adjudication.
    """
    del alpaca_account  # No per-delta cash comparison — the rebuild has no adjudication.

    order_statuses_projected = await _project_terminal_order_statuses(handle.session)
    # ALP-863 — with ``orders.status`` now projected, run the entry-window cancel
    # cascade (relocated off the always-on monitor) for every never-filled entry
    # whose terminal status just landed: dissolve the bracket, release capital,
    # resolve the thesis, cancel the position. Reuses the Phase-2 CANCEL cascade.
    entry_window_cancels_cascaded = await _dissolve_terminal_pending_entry_brackets(handle)
    # ALP-867 — relocate the monitor's reprice writeback here too: project the
    # ``ENTRY_REPRICED`` events onto every still-resting ``PENDING_ENTRY`` entry.
    # Sequenced AFTER the cancel cascade so a terminal (cancelled) entry is already
    # dissolved and excluded by the reprice gate — the cancel wins over a stale
    # reprice event.
    entry_window_reprices_projected = await _project_entry_window_reprices(handle)
    symbols_by_status = await _live_position_symbols_by_status(handle.session)
    intent_backed_symbols = frozenset().union(*symbols_by_status.values())
    pending_symbols = symbols_by_status.get("PENDING", frozenset())
    broker_facts = classify_broker_facts_without_intent(
        alpaca_positions=alpaca_positions,
        intent_backed_symbols=intent_backed_symbols,
    )
    pending_holdings = classify_pending_with_broker_holding(
        alpaca_positions=alpaca_positions,
        pending_symbols=pending_symbols,
    )
    for fact in broker_facts:
        log.info(
            "projection rebuild: broker fact with no Intent — %s qty=%s side=%s "
            "(DVN/manual-trade; surfaced as a projection state, not an alert)",
            fact.symbol,
            fact.qty,
            fact.side,
        )
    for holding in pending_holdings:
        log.warning(
            "projection rebuild: PENDING-local position with nonzero broker holding "
            "— %s qty=%s side=%s (likely a dropped/un-integrated entry fill; awaiting "
            "fill-drain recovery, no state mutated)",
            holding.symbol,
            holding.qty,
            holding.side,
        )

    return ProjectionRebuildSummary(
        order_statuses_projected=order_statuses_projected,
        broker_facts_without_intent=broker_facts,
        pending_with_broker_holding=pending_holdings,
        entry_window_cancels_cascaded=entry_window_cancels_cascaded,
        entry_window_reprices_projected=entry_window_reprices_projected,
    )


async def _project_terminal_order_statuses(session: AsyncSession) -> int:
    """Project every *new* ``TERMINAL_ORDER_STATUS`` event onto its ``orders`` cache row.

    Reads the event payloads, folds them to typed projections (pure), then
    batch-resolves them to local ``orders`` rows — by the broker UUID, falling back
    to the durable pre-commit ``client_order_id`` for an order whose UUID was never
    backfilled — and advances each row's cached ``status`` + ``last_update_timestamp``.
    Returns the count of rows advanced.

    The **event scan is bounded** (ALP-865): a persisted singleton watermark
    (``projection_rebuild_watermark.last_projected_event_seq``) records the max
    ``event_seq`` (rowid) already projected, so the SELECT reads only
    ``TERMINAL_ORDER_STATUS`` rows with ``event_seq`` greater than it — not the whole
    O(all-history) log every run. After scanning, the watermark advances to the max
    ``event_seq`` read, in **this** Phase-1 write transaction, so the advance commits
    atomically with the projection (a crash rolls both back and the next run
    re-scans). The first run finds no watermark row, treats it as ``0``, and scans
    from the beginning once.

    The resolve is **bounded** too (PR1): the candidate ``orders`` rows are loaded in
    two ``IN``-clause batches restricted to **non-terminal** orders, so an order
    already in a terminal status is never re-resolved or re-stamped (an
    already-projected terminal disposition is final). A projection whose order row
    does not resolve against a non-terminal row is skipped; the order cache is
    optional, so a miss is not an error — and the watermark still advances past it.

    Advancing past an *unresolved* event is safe because a durable ``orders`` row is
    committed before the broker submit that produces the event (ALP-836 atomic order
    persistence), so a terminal event never precedes its order row. An unresolved
    miss is therefore either an order already in a terminal status (final — correct
    to skip) or a genuinely out-of-band order with no local row that will ever exist
    (correct to skip) — never a not-yet-persisted order a later run would have
    resolved. Re-scanning every terminal event each run to "self-heal" such a row
    (the pre-ALP-865 behavior) is exactly the O(all-history) cost this bound removes;
    if a future change reintroduces an event-before-row path, it must re-scan those
    events explicitly rather than rely on the unbounded scan.
    """
    # Read the singleton watermark row once and reuse it for the advance below
    # (avoids a second ``session.get`` of the same row); ``None`` on the first run.
    watermark = await session.get(
        ProjectionRebuildWatermarkRow, PROJECTION_REBUILD_WATERMARK_SINGLETON_ID
    )
    last_projected_seq = watermark.last_projected_event_seq if watermark is not None else 0
    scanned = (
        await session.execute(
            select(BrokerEventLogRow.event_seq, BrokerEventLogRow.raw_payload_json)
            .where(
                BrokerEventLogRow.event_type == BrokerEventType.TERMINAL_ORDER_STATUS.value,
                BrokerEventLogRow.event_seq > last_projected_seq,
            )
            .order_by(BrokerEventLogRow.event_seq)
        )
    ).all()
    if not scanned:
        return 0
    # Rows are ordered by ``event_seq`` ascending, so the last one carries the max.
    max_scanned_seq = scanned[-1].event_seq
    projections = project_terminal_order_statuses(row.raw_payload_json for row in scanned)
    if not projections:
        _advance_projection_watermark(session, watermark, max_scanned_seq)
        return 0
    by_alpaca_id, by_client_id = await _resolve_non_terminal_order_rows(session, projections)
    now_iso = datetime.now(UTC).isoformat()
    projected = 0
    for projection in projections:
        row = None
        if projection.alpaca_order_id is not None:
            row = by_alpaca_id.get(projection.alpaca_order_id)
        if row is None and projection.client_order_id is not None:
            row = by_client_id.get(projection.client_order_id)
        if row is None:
            # Already terminal (filtered out of the batch) or genuinely out-of-band.
            continue
        if row.status == projection.terminal_status.value:
            continue
        row.status = projection.terminal_status.value
        row.last_update_timestamp = now_iso
        projected += 1
    _advance_projection_watermark(session, watermark, max_scanned_seq)
    return projected


def _advance_projection_watermark(
    session: AsyncSession,
    watermark: ProjectionRebuildWatermarkRow | None,
    new_seq: int,
) -> None:
    """Advance the singleton watermark to *new_seq* in the open write transaction.

    *watermark* is the singleton row already loaded at the top of the rebuild
    (``None`` on the first run, before the row exists) — passing it back avoids a
    second ``session.get`` of the same row. Single-writer (Phase-1 pipeline,
    ADR-0005), so the read-then-write on the singleton is race-free; the advance
    joins the open transaction and commits atomically with the projection it bounds.
    """
    if watermark is None:
        session.add(
            ProjectionRebuildWatermarkRow(
                id=PROJECTION_REBUILD_WATERMARK_SINGLETON_ID,
                last_projected_event_seq=new_seq,
            )
        )
    else:
        watermark.last_projected_event_seq = new_seq


# Order statuses already terminal for the order-status projection — a row in one
# of these is final, so the rebuild never re-resolves or re-stamps it (PR1).
_TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    status.value for status in _TERMINAL_STATUS_BY_NAME.values()
)


async def _resolve_non_terminal_order_rows(
    session: AsyncSession, projections: tuple[OrderStatusProjection, ...]
) -> tuple[dict[str, OrderRow], dict[str, OrderRow]]:
    """Batch-resolve candidate ``orders`` rows by alpaca id / client id (PR1).

    Two ``IN``-clause SELECTs — one keyed by ``alpaca_order_id``, one by
    ``client_order_id`` — restricted to **non-terminal** rows, replacing the prior
    two-SELECT-per-event resolve. Resolution priority (broker UUID first, the durable
    pre-commit ``client_order_id`` as the pre-backfill fallback, ADR-0002) is applied
    by the caller against the returned maps. Both columns are unique-when-present, so
    each id maps to at most one row.
    """
    alpaca_ids = {p.alpaca_order_id for p in projections if p.alpaca_order_id is not None}
    client_ids = {p.client_order_id for p in projections if p.client_order_id is not None}
    by_alpaca_id: dict[str, OrderRow] = {}
    by_client_id: dict[str, OrderRow] = {}
    if alpaca_ids:
        rows = (
            (
                await session.execute(
                    select(OrderRow).where(
                        OrderRow.alpaca_order_id.in_(alpaca_ids),
                        OrderRow.status.not_in(_TERMINAL_ORDER_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.alpaca_order_id is not None:
                by_alpaca_id[row.alpaca_order_id] = row
    if client_ids:
        rows = (
            (
                await session.execute(
                    select(OrderRow).where(
                        OrderRow.client_order_id.in_(client_ids),
                        OrderRow.status.not_in(_TERMINAL_ORDER_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.client_order_id is not None:
                by_client_id[row.client_order_id] = row
    return by_alpaca_id, by_client_id


# Provenance the entry-window cancel cascade stamps onto the CANCEL writeback —
# the same reason string the continuous monitor used before the writeback moved
# to the pipeline (ALP-863). Mirrors the ``_ENTRY_NO_FILL_REASONS`` vocabulary the
# operator alert reads off ``orders.status``.
_ENTRY_WINDOW_EXPIRED_REASON = "entry_window_expired"


async def _dissolve_terminal_pending_entry_brackets(handle: InvocationHandle) -> int:
    """Run the entry-window cancel cascade for never-filled terminal entries (ALP-863).

    The continuous monitor's entry-window watcher broker-cancels a patient bracket
    entry whose window elapsed but now writes nothing (single-writer invariant 1,
    ADR-0005). Once ``_project_terminal_order_statuses`` has advanced ``orders.status``
    from the resulting ``TERMINAL_ORDER_STATUS`` event, every bracket still
    ``PENDING_ENTRY`` whose ENTRY order sits in a terminal status with zero recorded
    fills is a never-filled entry that cancelled / expired. The pipeline — the single
    writer — runs the dissolve cascade the monitor used to RMW: reusing
    :func:`persist_entry_window_cancel` unchanged (only its caller moved
    monitor→pipeline), it marks the entry CANCELLED, cancels the protective legs,
    dissolves the bracket, releases the reserved capital, resolves the thesis
    ``CANCELLED_NEVER_ENTERED``, and drives the never-filled position PENDING→CANCELLED.

    The predicate is re-derivable and idempotent: a dissolved bracket is no longer
    ``PENDING_ENTRY`` so it never re-matches, and a terminal entry that has **any**
    recorded fill is excluded by the ``NOT EXISTS (fill_records)`` guard — the same
    ``has_recorded_fills`` gate the monitor's canceller used (it covers both an
    integrated fill and one written by the concurrent fill-stream consumer but not yet
    drained into ``filled_quantity``). With no recorded fill the reused cascade never
    hits its ALP-760 partial-fill early-return, so a never-filled entry always
    dissolves; an entry that filled in the cancel race is integrated by its FILL event
    instead. The disposition is canonicalized to ``CANCELLED`` by the shared CANCEL
    writeback (matching the monitor's prior behavior) whether the projected terminal
    status was CANCELLED / EXPIRED / REJECTED; the no-fill operator alert (ALP-739)
    still fires off the projected ``orders.status``. Returns the count of brackets
    dissolved.
    """
    session = handle.session
    entry_order_ids = (
        (
            await session.execute(
                select(BracketRow.entry_order_id)
                .join(OrderRow, OrderRow.order_id == BracketRow.entry_order_id)
                .where(
                    BracketRow.status == BracketStatus.PENDING_ENTRY.value,
                    OrderRow.status.in_(_TERMINAL_ORDER_STATUSES),
                    # No recorded fill (``has_recorded_fills`` parity): excludes the
                    # cancel-race winner whose FILL integrates the position instead.
                    ~exists().where(FillRecordRow.order_id == OrderRow.order_id),
                )
                .order_by(BracketRow.entry_order_id)
            )
        )
        .scalars()
        .all()
    )
    for entry_order_id in entry_order_ids:
        await persist_entry_window_cancel(
            handle,
            entry_order_id=entry_order_id,
            cancel_reason=_ENTRY_WINDOW_EXPIRED_REASON,
        )
        log.info(
            "projection rebuild: dissolved PENDING_ENTRY bracket for never-filled "
            "terminal entry %s (entry-window cancel cascade relocated off the monitor, "
            "ALP-863)",
            entry_order_id,
        )
    return len(entry_order_ids)


async def _project_entry_window_reprices(handle: InvocationHandle) -> int:
    """Project ``ENTRY_REPRICED`` events onto every still-resting entry (ALP-867).

    The continuous monitor escalates a patient ``PENDING_ENTRY`` entry toward the
    market by cancel-and-replacing it at the broker, but now writes nothing — it
    appends an ``ENTRY_REPRICED`` event instead of RMW'ing the order row (the
    residual half of the single-writer leak the cancel cascade above already
    closed). The pipeline — the single writer — folds those events onto the order
    cache, reusing :func:`persist_entry_window_reprice` unchanged (only its caller
    moved monitor→pipeline): the new ``limit_price`` / ``alpaca_order_id`` /
    ``modification_count`` and the reservation adjustment.

    Events are scanned in ``event_seq`` order (ALP-865's monotonic append cursor)
    and grouped per entry, so each order's reprices fold in append order and the
    last one wins the final limit / broker id. Only genuinely-resting entries are
    projected — ``PENDING_ENTRY`` bracket, non-terminal order, no recorded fills,
    the same liveness the monitor's repricer required. This gate, run **after** the
    cancel cascade, is what makes a terminal (cancelled) entry's dissolve win over a
    stale reprice: a cancelled order is excluded (terminal status / no longer
    ``PENDING_ENTRY``), and a filled entry is integrated by its FILL event rather
    than repriced (which would corrupt the reservation).

    **Idempotent.** A reprice event whose new broker id is already in the order's
    ``alpaca_order_id_chain`` has been projected, so only the un-projected suffix is
    replayed; re-running over the same log reproduces the same row (each
    ``persist_entry_window_reprice`` call extends the chain, bumps
    ``modification_count``, and telescopes the reservation by the limit delta). The
    scan is over all ``ENTRY_REPRICED`` events (not watermark-bounded like the
    terminal-status scan) — reprice events are rare (only patient entries past their
    deadline), so the chain-gated re-fold is cheap. Returns the count of reprices
    projected this rebuild.
    """
    session = handle.session
    # Co-select a real column so the FROM is anchored: ``event_seq`` is a
    # ``literal_column`` over the implicit rowid (ALP-865) and selecting it alone has
    # no FROM clause to resolve against.
    scanned = (
        await session.execute(
            select(BrokerEventLogRow.event_seq, BrokerEventLogRow.raw_payload_json)
            .where(BrokerEventLogRow.event_type == BrokerEventType.ENTRY_REPRICED.value)
            .order_by(BrokerEventLogRow.event_seq)
        )
    ).all()
    if not scanned:
        return 0
    events_by_entry: dict[str, list[dict[str, str]]] = {}
    for row in scanned:
        payload = json.loads(row.raw_payload_json)
        events_by_entry.setdefault(payload["entry_order_id"], []).append(payload)

    live_entry_ids = set(
        (
            await session.execute(
                select(BracketRow.entry_order_id)
                .join(OrderRow, OrderRow.order_id == BracketRow.entry_order_id)
                .where(
                    BracketRow.entry_order_id.in_(list(events_by_entry)),
                    BracketRow.status == BracketStatus.PENDING_ENTRY.value,
                    OrderRow.status.not_in(_TERMINAL_ORDER_STATUSES),
                    # No recorded fill: a filled entry is integrated by its FILL
                    # event, never repriced (which would adjust an already-released
                    # reservation).
                    ~exists().where(FillRecordRow.order_id == OrderRow.order_id),
                )
            )
        )
        .scalars()
        .all()
    )

    projected = 0
    for entry_order_id, events in events_by_entry.items():
        if entry_order_id not in live_entry_ids:
            continue
        order_row = await session.get(OrderRow, entry_order_id)
        if order_row is None:
            continue
        # The broker-id chain is the idempotency ledger: any reprice already folded
        # appended its new id here, so skip those and replay only the un-projected
        # suffix (kept in step with each in-place ``persist_entry_window_reprice``).
        projected_ids: set[str] = {
            str(chain_id) for chain_id in order_row_to_record(order_row).alpaca_order_id_chain
        }
        for payload in events:
            new_alpaca_order_id = payload["new_alpaca_order_id"]
            if new_alpaca_order_id in projected_ids:
                continue
            await persist_entry_window_reprice(
                handle,
                entry_order_id=entry_order_id,
                new_limit_price=price(payload["new_limit"]),
                new_alpaca_order_id=new_alpaca_order_id,
                reprice_reason=payload["reason"],
            )
            projected_ids.add(new_alpaca_order_id)
            projected += 1
            log.info(
                "projection rebuild: projected ENTRY_REPRICED for entry %s → limit %s "
                "broker id %s (reprice writeback relocated off the monitor, ALP-867)",
                entry_order_id,
                payload["new_limit"],
                new_alpaca_order_id,
            )
    return projected


async def _live_position_symbols_by_status(session: AsyncSession) -> dict[str, frozenset[str]]:
    """Broker-snapshot keys (equity ticker / options OCC) per live local-position status.

    One pass over the OPEN/PENDING local positions, grouping each position's broker
    key by its status. The union over the values is the Intent-backed set (a broker
    snapshot position whose ``symbol`` is in it has an Intent overlay; one absent
    from it is "broker fact, no Intent"); the ``"PENDING"`` bucket alone is the
    dropped-fill cross-check (RD1). Strategy positions carry no single broker key
    (Alpaca reports each leg as its own row), so they contribute no key — their legs
    surface as broker-fact-no-Intent until a leg-level overlay exists, the same scope
    boundary the deleted reconciler drew.
    """
    rows = (
        (
            await session.execute(
                select(PositionRow).where(PositionRow.status.in_(_LIVE_POSITION_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    symbols_by_status: dict[str, set[str]] = {status: set() for status in _LIVE_POSITION_STATUSES}
    for row in rows:
        details = position_row_to_record(row).details
        if isinstance(details, EquityPositionDetails):
            symbols_by_status[row.status].add(details.ticker)
        elif isinstance(details, OptionsPositionDetails):
            symbols_by_status[row.status].add(alpaca_occ_symbol(details))
    return {status: frozenset(symbols) for status, symbols in symbols_by_status.items()}


__all__ = [
    "BrokerFactNoIntent",
    "OrderStatusProjection",
    "PendingWithBrokerHolding",
    "ProjectionRebuildSummary",
    "classify_broker_facts_without_intent",
    "classify_pending_with_broker_holding",
    "project_terminal_order_statuses",
    "rebuild_projection",
]
