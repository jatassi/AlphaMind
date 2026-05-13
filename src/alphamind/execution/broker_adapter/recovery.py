"""Disconnect-recovery primitive (story 03d / ALP-389).

Ships :func:`recover_missed_fills_since` — the GET-based recovery routine the
continuous monitor (ALP-123) invokes after a ``trade_updates`` websocket
disconnect to surface fill events that arrived while the websocket was down.

Calls :meth:`AccountStateQueries.get_orders` with ``status="all"``,
``since=<last_seen_ts>``, ``until=<now>``, and translates each yielded
:class:`OrderSnapshot` into one or more :class:`FillReport` records via
:func:`order_snapshot_to_fill_reports` — using order-state inspection rather
than event-log fetching, since Alpaca's REST surface lacks an event-log
endpoint.

Reports are yielded in fill-timestamp ascending order so the OMS Phase 1
path can integrate them like normal websocket fills; ties broken by Alpaca
order ID for deterministic ordering. The OMS is idempotent on
``client_order_id + event_type`` via the fill_records ledger, so the recovery
routine emits everything in the window without prior-state deduplication.

The reconnect lifecycle (re-establishing the websocket) lives in the
continuous monitor's run-loop (ALP-123); this module ships only the recovery
primitive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, OccSymbol
from alphamind.execution.broker_adapter.fill_stream import (
    FillReport,
    OrderStatus,
    PositionIntentLiteral,
)
from alphamind.execution.broker_adapter.queries import (
    OrderLegSnapshot,
    OrderSnapshot,
)


class _OrdersSource(Protocol):
    """Subset of :class:`AccountStateQueries` the recovery routine consumes.

    Defined as a protocol so the test suite can swap a fake without subclassing
    the real (network-touching) :class:`AccountStateQueries`. Mirrors the
    Protocol pattern used by :func:`subscribe_trade_updates`.
    """

    def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = ...,
        since: datetime | None = ...,
        until: datetime | None = ...,
        symbols: tuple[str, ...] | None = ...,
    ) -> AsyncIterator[OrderSnapshot]: ...


# Alpaca status string -> OMS OrderStatus literal. Statuses not in this map
# (``pending_cancel`` / ``pending_replace`` / ``pending_review`` / ``held`` /
# ``accepted_for_bidding``) are filtered: the design treats them as
# in-flight states the websocket re-subscribe will surface fresh. ``done_for_day``
# is a session-end informational state that maps to the OMS literal.
_STATUS_TO_EVENT: Final[dict[str, OrderStatus]] = {
    "new": "new",
    "accepted": "new",
    "pending_new": "new",
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "replaced": "replaced",
    "stopped": "stopped",
    "done_for_day": "done_for_day",
}

# Statuses whose ``filled_avg_price`` + ``filled_qty`` populate the
# corresponding ``FillReport`` fields. Every other status leaves
# ``fill_price`` / ``fill_quantity`` as ``None`` per the design (the order
# may carry a non-zero ``filled_qty`` from earlier partials, but a recovery
# event for a *new* terminal status carries no incremental fill).
_FILL_BEARING_STATUSES: Final[frozenset[str]] = frozenset({"filled", "partially_filled"})

# OMS PositionIntentLiteral alphabet — used to coerce OrderLegSnapshot's
# untyped ``str`` field at the boundary.
_POSITION_INTENTS: Final[frozenset[str]] = frozenset(
    {"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"}
)


async def recover_missed_fills_since(
    queries: _OrdersSource,
    *,
    since: datetime,
    until: datetime | None = None,
) -> AsyncIterator[FillReport]:
    """Yield ``FillReport`` for orders touched between *since* and *until*.

    Calls :meth:`AccountStateQueries.get_orders` with ``status="all"``,
    paginating through every order that's been touched in the window, and
    translates each :class:`OrderSnapshot` into one or more
    :class:`FillReport` records via :func:`order_snapshot_to_fill_reports`.

    **Buffer-then-sort, not streaming.** This routine intentionally consumes
    the full ``get_orders`` page set into memory before yielding the first
    report so the output can be sorted by terminal-event timestamp. Alpaca's
    ``GET /v2/orders`` cursor is ``submitted_at``-ordered, not
    terminal-event-ordered, so two orders with different submission times
    can have their terminal events in opposite order — surfacing them in
    cursor order would corrupt the OMS's idempotency guarantees on the fill
    sequence. Streaming would also require a per-event ``Heap`` since
    Alpaca's API doesn't expose ``filled_at``-ordered cursors. The routine
    is callable in this buffered shape because recovery windows are designed
    to be brief (the continuous monitor invokes it only after a websocket
    disconnect, lookback typically under an hour); a multi-day backfill is
    out of scope for this primitive.

    Reports are emitted in fill_timestamp ascending order with the parent's
    ``alpaca_order_id`` as the tiebreak (string ordering); mleg children
    stay attached after their parent in original translation order.

    *until* defaults to ``datetime.now(UTC)`` when ``None`` and is also
    forwarded as the upper bound on the get_orders cursor; orders submitted
    after ``until`` are filtered locally as a defensive guard against any
    cursor leakage. *since* is required — calling without it raises
    :class:`TypeError` per Python's keyword-only-required convention.
    """
    effective_until = until if until is not None else datetime.now(UTC)
    groups: list[tuple[FillReport, ...]] = []
    async for snapshot in queries.get_orders(status="all", since=since, until=effective_until):
        if snapshot.submitted_at > effective_until:
            continue
        reports = order_snapshot_to_fill_reports(snapshot)
        if reports:
            groups.append(reports)

    groups.sort(key=_group_sort_key)
    for group in groups:
        for report in group:
            yield report


def _group_sort_key(group: tuple[FillReport, ...]) -> tuple[datetime, str]:
    """Sort by the parent's fill_timestamp; tiebreak on alpaca_order_id."""
    parent = group[0]
    return (parent.fill_timestamp, parent.alpaca_order_id)


def order_snapshot_to_fill_reports(snapshot: OrderSnapshot) -> tuple[FillReport, ...]:
    """Translate one :class:`OrderSnapshot` into zero or more :class:`FillReport`.

    For single-leg / equity orders: returns a single-element tuple. For mleg
    parents with populated ``legs``: returns the parent report followed by
    per-leg child reports, each carrying the parent's ``client_order_id`` and
    ``alpaca_order_id`` for OMS-side correlation.
    """
    parent_event = _STATUS_TO_EVENT.get(snapshot.status)
    if parent_event is None:
        return ()

    # Serialize the snapshot once; every parent + leg report on this
    # snapshot shares the same raw_event_payload reference.
    raw_payload = snapshot.model_dump(mode="json")
    fill_timestamp = _resolve_fill_timestamp(snapshot)
    parent = _build_parent_report(snapshot, parent_event, fill_timestamp, raw_payload)
    if snapshot.order_class != "mleg" or not snapshot.legs:
        return (parent,)

    children = tuple(
        _build_leg_report(leg, parent, fill_timestamp, raw_payload) for leg in snapshot.legs
    )
    return (parent, *children)


def _build_parent_report(
    snapshot: OrderSnapshot,
    event: OrderStatus,
    fill_timestamp: datetime,
    raw_payload: dict[str, Any],
) -> FillReport:
    """Build the single-event / mleg-parent report from *snapshot*."""
    fill_price, fill_quantity = _fill_metrics(snapshot.status, snapshot)
    occ = _occ_symbol_for_parent(snapshot)
    return FillReport(
        client_order_id=ClientOrderId(snapshot.client_order_id),
        alpaca_order_id=AlpacaOrderId(_parent_alpaca_order_id(snapshot, event)),
        parent_client_order_id=None,
        parent_alpaca_order_id=None,
        event_type=event,
        fill_timestamp=fill_timestamp,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        cumulative_filled_quantity=snapshot.filled_qty,
        remaining_quantity=max(snapshot.qty - snapshot.filled_qty, 0.0),
        execution_venue=None,
        occ_symbol=OccSymbol(occ) if occ is not None else None,
        position_intent=None,
        raw_event_payload=raw_payload,
    )


def _build_leg_report(
    leg: OrderLegSnapshot,
    parent: FillReport,
    fill_timestamp: datetime,
    raw_payload: dict[str, Any],
) -> FillReport:
    """Build a per-leg child report for an mleg parent snapshot.

    The leg carries its own ``status`` — Alpaca may report mixed states
    (e.g. 3 of 4 legs filled, 1 still open under thin liquidity), and the
    OMS Phase 1 path needs each leg's individual state. Statuses outside
    the OMS vocabulary fall back to the parent's event for stable correlation.
    """
    leg_event = _STATUS_TO_EVENT.get(leg.status, parent.event_type)
    fill_price, fill_quantity = _fill_metrics(leg.status, leg)
    return FillReport(
        client_order_id=ClientOrderId(leg.order_id),
        alpaca_order_id=AlpacaOrderId(leg.order_id),
        parent_client_order_id=parent.client_order_id,
        parent_alpaca_order_id=parent.alpaca_order_id,
        event_type=leg_event,
        fill_timestamp=fill_timestamp,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        cumulative_filled_quantity=leg.filled_qty,
        remaining_quantity=max(leg.qty - leg.filled_qty, 0.0),
        execution_venue=None,
        occ_symbol=OccSymbol(leg.symbol),
        position_intent=_position_intent_for(leg),
        raw_event_payload=raw_payload,
    )


class _FillBearing(Protocol):
    """Common shape of fill-metric carriers (snapshot or leg)."""

    filled_avg_price: float | None
    filled_qty: float


def _fill_metrics(status: str, source: _FillBearing) -> tuple[float | None, float | None]:
    """Return ``(fill_price, fill_quantity)`` for *source*'s current state.

    Non-fill statuses yield ``(None, None)`` even when *source* carries a
    non-zero ``filled_qty`` from prior partials — the recovery report
    describes the *current* terminal event, not historical fill increments.
    """
    if status not in _FILL_BEARING_STATUSES:
        return None, None
    return source.filled_avg_price, source.filled_qty


def _parent_alpaca_order_id(snapshot: OrderSnapshot, event: OrderStatus) -> str:
    """Return the alpaca_order_id field for a parent FillReport.

    For ``replaced`` events with a populated ``replaced_by`` field, return the
    new replacement order's ID per ``broker-adapter.md`` (the OMS treats the
    replacement as the canonical successor). For every other event, return
    the snapshot's own order_id.
    """
    if event == "replaced" and snapshot.replaced_by:
        return snapshot.replaced_by
    return snapshot.order_id


def _occ_symbol_for_parent(snapshot: OrderSnapshot) -> str | None:
    """Return the ``symbol`` for single-leg options snapshots; ``None`` otherwise.

    Equities carry the underlying ticker we treat as the equity symbol, not
    OCC. Mleg parents have no canonical OCC (the symbol lives on each leg).
    Only single-leg options snapshots surface a symbol that is OCC-shaped.
    """
    if snapshot.asset_class == "us_option" and snapshot.order_class != "mleg":
        return snapshot.symbol
    return None


def _position_intent_for(leg: OrderLegSnapshot) -> PositionIntentLiteral | None:
    """Coerce :class:`OrderLegSnapshot`'s str field to the OMS literal."""
    raw = leg.position_intent
    if raw in _POSITION_INTENTS:
        # Runtime membership check above narrows to the Literal alphabet.
        return cast(PositionIntentLiteral, raw)
    return None


def _resolve_fill_timestamp(snapshot: OrderSnapshot) -> datetime:
    """Resolve the canonical fill timestamp for *snapshot*.

    Uses the most-specific terminal timestamp available — ``filled_at``,
    ``canceled_at``, then ``expired_at`` — and falls back to ``submitted_at``
    when no terminal timestamp exists (in-flight states yield no terminal
    field but still need a stable ordering key).
    """
    for candidate in (snapshot.filled_at, snapshot.canceled_at, snapshot.expired_at):
        if candidate is not None:
            return candidate
    return snapshot.submitted_at
