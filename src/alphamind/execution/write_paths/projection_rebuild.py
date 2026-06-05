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

Three derivations run in the open Phase-1 write transaction (single writer =
pipeline, ADR-0005):

1. **Order-status projection** — fold every ``TERMINAL_ORDER_STATUS`` event in
   the log onto the ``orders`` cache, setting ``status`` + ``last_update_timestamp``
   (W1c removed the monitor's RMW; nothing else projected the events, so an
   unfilled accepted entry stayed ``PENDING`` forever and the ``entry_no_fill``
   alert never matched). The order row is an optional projection cache — a zero-fill
   terminal event advances its cached status to ``CANCELLED`` / ``EXPIRED``.

2. **Broker-fact-no-Intent classification** — a broker position with no matching
   local Intent overlay (the DVN / manual-trade case) is a **first-class
   projection state**, surfaced in the summary (attach/flag), never a reconcile
   alert. The "Alpaca wins" auto-materialize / alert doctrine is deleted.

3. **Per-thesis PnL ledger derivation** — re-derive each thesis's realized PnL +
   cost basis from its event log (story 03c's :func:`rederive_thesis_pnl_ledger`),
   so the ``thesis_pnl_ledger`` table is populated each run rather than dark.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.execution.broker_adapter.queries import PositionSnapshot, TradeAccountSnapshot
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    alpaca_occ_symbol,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.records_broker_event_log import BrokerEventType
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record as position_row_to_record

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
    rebuild advanced from a TERMINAL_ORDER_STATUS event; ``theses_rederived``
    counts thesis PnL-ledger rows re-derived from the log; ``broker_facts_without_intent``
    carries the DVN/manual-trade positions surfaced as a projection state (never
    alerted).
    """

    order_statuses_projected: int
    theses_rederived: int
    broker_facts_without_intent: tuple[BrokerFactNoIntent, ...]


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
    positions/cash projection. This step adds the three derivations the fill-fold
    does not own: the order-status projection, the broker-fact-no-Intent
    classification, and the per-thesis PnL-ledger derivation. Joins the open
    ``handle.session`` transaction; the surrounding ``InvocationContext`` commits.

    ``alpaca_account`` is accepted for symmetry with the deleted ``reconcile``
    signature (the cash projection rebuilds from the event log + snapshot via the
    fill-fold; no per-delta cash comparison runs here). A snapshot/projection
    mismatch is resolved by the rebuild itself — there is no adjudication.
    """
    del alpaca_account  # No per-delta cash comparison — the rebuild has no adjudication.

    order_statuses_projected = await _project_terminal_order_statuses(handle.session)
    # The per-thesis PnL-ledger re-derivation (critical #1) wiring lands in the
    # next commit in this series.
    theses_rederived = 0
    intent_backed_symbols = await _intent_backed_symbols(handle.session)
    broker_facts = classify_broker_facts_without_intent(
        alpaca_positions=alpaca_positions,
        intent_backed_symbols=intent_backed_symbols,
    )
    for fact in broker_facts:
        log.info(
            "projection rebuild: broker fact with no Intent — %s qty=%s side=%s "
            "(DVN/manual-trade; surfaced as a projection state, not an alert)",
            fact.symbol,
            fact.qty,
            fact.side,
        )

    return ProjectionRebuildSummary(
        order_statuses_projected=order_statuses_projected,
        theses_rederived=theses_rederived,
        broker_facts_without_intent=broker_facts,
    )


async def _project_terminal_order_statuses(session: AsyncSession) -> int:
    """Project every ``TERMINAL_ORDER_STATUS`` event onto its ``orders`` cache row.

    Reads the event payloads, folds them to typed projections (pure), then resolves
    each to a local ``orders`` row — by the broker UUID, falling back to the durable
    pre-commit ``client_order_id`` for an order whose UUID was never backfilled — and
    advances its cached ``status`` + ``last_update_timestamp``. Returns the count of
    rows advanced. A projection whose order row does not resolve (a genuinely
    out-of-band order) is logged and skipped; the order cache is optional, so a
    cache miss is not an error.
    """
    payloads = (
        (
            await session.execute(
                select(BrokerEventLogRow.raw_payload_json).where(
                    BrokerEventLogRow.event_type == BrokerEventType.TERMINAL_ORDER_STATUS.value
                )
            )
        )
        .scalars()
        .all()
    )
    projections = project_terminal_order_statuses(payloads)
    now_iso = datetime.now(UTC).isoformat()
    projected = 0
    for projection in projections:
        row = await _resolve_order_row(session, projection)
        if row is None:
            log.debug(
                "projection rebuild: TERMINAL_ORDER_STATUS for unknown order "
                "(alpaca_order_id=%s client_order_id=%s status=%s) — no local order "
                "cache row; skipping",
                projection.alpaca_order_id,
                projection.client_order_id,
                projection.terminal_status.value,
            )
            continue
        if row.status == projection.terminal_status.value:
            continue
        row.status = projection.terminal_status.value
        row.last_update_timestamp = now_iso
        projected += 1
    return projected


async def _resolve_order_row(
    session: AsyncSession, projection: OrderStatusProjection
) -> OrderRow | None:
    """Resolve the local ``orders`` row a terminal-status projection applies to.

    Resolution mirrors the fill-path's order resolution (ADR-0002): the broker UUID
    first (the captured-at-submission ``alpaca_order_id``), then the durable
    pre-commit ``client_order_id`` for an order whose post-submit UUID backfill was
    lost (``alpaca_order_id`` still NULL, ALP-836/847). Both columns are
    unique-when-present, so ``one_or_none`` surfaces a duplicate as a loud invariant
    breach rather than guessing.
    """
    if projection.alpaca_order_id is not None:
        row = (
            await session.execute(
                select(OrderRow).where(OrderRow.alpaca_order_id == projection.alpaca_order_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    if projection.client_order_id is not None:
        return (
            await session.execute(
                select(OrderRow).where(OrderRow.client_order_id == projection.client_order_id)
            )
        ).scalar_one_or_none()
    return None


async def _intent_backed_symbols(session: AsyncSession) -> frozenset[str]:
    """Broker-snapshot keys (equity ticker / options OCC) for every live local position.

    A broker snapshot position whose ``symbol`` is in this set has an Intent
    overlay; one absent from it is "broker fact, no Intent". Strategy positions
    carry no single broker key (Alpaca reports each leg as its own row), so they
    contribute no key here — their legs surface as broker-fact-no-Intent until a
    leg-level overlay exists, the same scope boundary the deleted reconciler drew.
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
    symbols: set[str] = set()
    for row in rows:
        details = position_row_to_record(row).details
        if isinstance(details, EquityPositionDetails):
            symbols.add(details.ticker)
        elif isinstance(details, OptionsPositionDetails):
            symbols.add(alpaca_occ_symbol(details))
    return frozenset(symbols)


__all__ = [
    "BrokerFactNoIntent",
    "OrderStatusProjection",
    "ProjectionRebuildSummary",
    "classify_broker_facts_without_intent",
    "project_terminal_order_statuses",
    "rebuild_projection",
]
