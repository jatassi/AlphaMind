"""``FillReport`` → ``FillRecord`` translation (story 02c / ALP-435).

The broker adapter's :func:`subscribe_trade_updates` primitive yields
:class:`FillReport` per ``trade_updates`` event. The continuous monitor's
fill-stream consumer translates each report into a persistence-layer
:class:`FillRecord` (the shape :func:`append_fill_record` expects) before
durably appending it to ``fill_records``.

The translator is pure and synchronous: no I/O. Non-fill events
(``new`` / ``canceled`` / ``expired`` / ``replaced`` / ``replace_rejected`` /
``rejected`` / ``done_for_day``) still flow through the activity log via
Phase 1, but they do not append to ``fill_records`` — the translator returns
``None`` for them. Mleg parent events also return ``None`` (the parent
strategy fill is not a per-position fill); per-leg children translate to one
``FillRecord`` each carrying the parent's ``client_order_id``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final

from alphamind._kernel.money import money, price
from alphamind.execution.broker_adapter import FillReport
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
)

# Map FillReport.event_type → portfolio-state OrderStatus enum. Only fill-
# bearing events have entries here; every other event yields ``None`` from the
# translator so it never reaches this table.
_FILL_EVENT_TO_ORDER_STATUS: Final[dict[str, OrderStatus]] = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    # ``stopped`` is documented in broker-adapter.md as a fill variant on
    # venues that surface guaranteed-fill events; OMS treats it as a terminal
    # fill.
    "stopped": OrderStatus.FILLED,
}

# Map FillReport.event_type → terminal non-fill OrderStatus. These events
# append no ``fill_records`` row (``fill_report_to_fill_record`` returns
# ``None``), but the order they reference has reached a terminal state that
# must be captured — a zero-fill terminal event lands as a
# ``TERMINAL_ORDER_STATUS`` row on the append-only ``broker_event_log`` (ALP-849
# / W1c), from which the single (pipeline) writer projects ``orders.status``.
# Otherwise an accepted entry that expires/cancels unfilled stays ``PENDING``
# (the ZS row in ALP-739) and the no-fill alert never fires. ``rejected`` is an
# async post-acceptance broker rejection: the broker accepted the order, then
# rejected it out-of-band, so it too must reach a terminal projection — without
# it the order stays ``PENDING`` forever and its reserved capital never clears
# (CR3). (The synchronous dispatch-time rejection is a separate failure mode the
# abandon path already retires.) ``done_for_day`` is absent: it is non-terminal
# (a GTC order "done for the day" resumes the next session), so mapping it to a
# terminal status would falsely retire a live order.
_TERMINAL_NON_FILL_EVENT_TO_STATUS: Final[dict[str, OrderStatus]] = {
    "canceled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def fill_report_to_fill_record(
    report: FillReport, *, oms_order_id: str | None = None
) -> FillRecord | None:
    """Translate one :class:`FillReport` into a :class:`FillRecord`.

    Returns ``None`` for events that do not produce a persisted fill:

    * Non-fill event types (``new``, ``canceled``, ``expired``, ``replaced``,
      ``replace_rejected``, ``rejected``, ``done_for_day``) — these still
      surface through the activity log at Phase 1 but do not append to
      ``fill_records``.
    * Mleg parent events — the parent strategy fill is not a per-position
      fill; only the per-leg children (``parent_client_order_id`` populated)
      translate to records.
    * Defensive: any fill-bearing event whose ``fill_price`` or
      ``fill_quantity`` is ``None`` (the broker-adapter primitive should
      always populate both for ``filled`` / ``partially_filled`` / ``stopped``;
      a ``None`` here would be a malformed payload and is filtered rather
      than letting Pydantic raise downstream).

    ``oms_order_id`` overrides the report-derived order id with the local
    ``orders`` PK the caller resolved (ALP-746): the broker's ``client_order_id``
    does not round-trip the OMS order id for equity entries (it carries the
    command id) or for native-bracket protective children (Alpaca generates it),
    so the consumer resolves the row by ``alpaca_order_id`` and threads the PK
    here. The ``fill_id`` is derived from the resolved id so the same logical
    fill dedupes identically across the live-stream and recovery paths. ``None``
    preserves the report-derived id (the historical / already-aligned path).
    """
    order_status_after = _FILL_EVENT_TO_ORDER_STATUS.get(report.event_type)
    if order_status_after is None:
        return None

    if _is_mleg_parent(report):
        return None

    if report.fill_price is None or report.fill_quantity is None:
        return None

    # The order id the fill applies to. ``oms_order_id`` (when the consumer
    # resolved the local row by broker UUID) wins; otherwise fall back to the
    # report-derived id. Mleg per-leg children map ``order_id`` to the parent's
    # client_order_id because the OMS persists mleg orders as a single parent
    # :class:`OrderRecord` with ``order_class=MLEG`` and the legs encoded on
    # ``instrument_spec`` — the legs themselves do not own separate rows in
    # the ``orders`` table. Per-leg child fills therefore reference the
    # parent strategy's order id; the alpaca-side leg id is preserved on
    # ``gateway_reference`` for reconciliation. For equity / single-leg
    # options, ``parent_client_order_id`` is ``None`` and the report's own
    # ``client_order_id`` is the OMS order id.
    order_id = oms_order_id if oms_order_id is not None else order_id_for_report(report)
    alpaca_ref = report.alpaca_order_id

    fill_id = _derive_fill_id(
        order_id=order_id,
        alpaca_order_id=alpaca_ref,
        fill_timestamp=report.fill_timestamp,
        fill_price=report.fill_price,
        fill_quantity=report.fill_quantity,
    )

    # ALP-462 — ``report.fill_price`` is still float on FillReport (broker
    # adapter's wire shape, unchanged in this story). Wrap via ``price(...)``
    # at the FillRecord boundary so the durability layer sees Decimal-exact
    # values; ``fees_usd`` defaults to ``money("0")`` for the same reason.
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        fill_timestamp=report.fill_timestamp,
        fill_price=price(str(report.fill_price)),
        fill_quantity=report.fill_quantity,
        remaining_quantity_after=report.remaining_quantity,
        order_status_after=order_status_after,
        slippage_usd=None,
        fees_usd=money("0"),
        execution_venue=report.execution_venue,
        gateway_reference=alpaca_ref,
        persistence_timestamp=datetime.now(UTC),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def terminal_order_status_for(report: FillReport) -> OrderStatus | None:
    """Terminal disposition a zero-fill non-fill event records, else ``None``.

    Returns :attr:`OrderStatus.CANCELLED` for ``canceled`` events,
    :attr:`OrderStatus.EXPIRED` for ``expired`` events, and
    :attr:`OrderStatus.REJECTED` for an async post-acceptance ``rejected`` event
    (CR3); ``None`` for every other event type. The consumer carries this
    disposition onto the ``TERMINAL_ORDER_STATUS`` event-log row (ALP-849 / W1c)
    rather than RMW-ing ``orders.status``. Fill-bearing events are handled by
    :func:`fill_report_to_fill_record`; ``new`` / ``replaced`` /
    ``replace_rejected`` / ``done_for_day`` carry no terminal-unfilled
    disposition this path acts on.
    """
    return _TERMINAL_NON_FILL_EVENT_TO_STATUS.get(report.event_type)


def order_id_for_report(report: FillReport) -> str:
    """The OMS ``orders.order_id`` a report's disposition applies to.

    Mleg per-leg children reference the parent strategy order id (legs do
    not own ``orders`` rows); equity / single-leg events use their own
    ``client_order_id``. Mirrors the order-id resolution in
    :func:`fill_report_to_fill_record`.
    """
    return report.parent_client_order_id or report.client_order_id


def _is_mleg_parent(report: FillReport) -> bool:
    """Whether *report* is an mleg parent strategy event (not a leg child).

    Mleg parent events surface as ``parent_client_order_id is None`` (the
    parent has no parent) plus a populated legs array on the underlying
    raw payload. Equity and single-leg-options events also have
    ``parent_client_order_id is None`` but no legs array, so the legs-array
    presence is the discriminator.

    The raw payload comes from two different upstream paths:

    * Live stream (``fill_stream.translate_trade_update``) dumps the
      alpaca-py ``TradeUpdate`` — legs live at ``payload["order"]["legs"]``.
    * Recovery (``recovery.order_snapshot_to_fill_reports``) dumps the
      adapter's ``OrderSnapshot`` — legs live at ``payload["legs"]``.

    Either path indicates an mleg parent; either path's absence indicates
    a single-event or equity report.
    """
    if report.parent_client_order_id is not None:
        # Per-leg children are never parents.
        return False
    return _raw_payload_has_legs(report.raw_event_payload)


def _raw_payload_has_legs(payload: dict[str, object] | None) -> bool:
    """Inspect either upstream payload shape for a populated legs array."""
    if not payload:
        return False
    # Live-stream shape: legs nested under ``order``.
    order = payload.get("order")
    if isinstance(order, dict):
        nested_legs = order.get("legs")
        if isinstance(nested_legs, list) and len(nested_legs) > 0:
            return True
    # Recovery shape: legs at top level.
    top_legs = payload.get("legs")
    return isinstance(top_legs, list) and len(top_legs) > 0


def derive_broker_event_key(report: FillReport) -> str:
    """Derive the deterministic ``broker_event_log.event_key`` for a fill *report*.

    Identity is the broker-fill tuple ``(alpaca_order_id, fill_timestamp,
    fill_quantity, fill_price)`` — the same broker-authoritative fact whether it
    arrives on the live websocket or a later REST recovery replay, so both
    collapse onto one ``broker_event_log`` row via the ``event_key`` PK
    (idempotency, ADR-0002). The ``fevt-`` prefix distinguishes a fill event key
    from the ``ufill-`` unattributed-queue key and the ``fill-`` ``fill_records``
    id, which share the same underlying tuple. Fill-bearing events always
    populate ``fill_price`` / ``fill_quantity``; the ``or 0.0`` only guards a
    malformed payload the translator filters upstream.
    """
    h = hashlib.sha256(
        f"{report.alpaca_order_id}|{report.fill_timestamp.isoformat()}"
        f"|{report.fill_quantity or 0.0}|{report.fill_price or 0.0}".encode()
    )
    return f"fevt-{h.hexdigest()[:16]}"


def derive_broker_fill_key(report: FillReport) -> str:
    """Derive the deterministic ``unattributed_fills`` primary key for *report*.

    Identity is the broker-fill tuple ``(alpaca_order_id, fill_timestamp,
    fill_quantity, fill_price)`` — the ``fill_records`` dedupe key MINUS
    ``order_id`` (the order id is exactly what is unresolved at quarantine
    time). Mirrors :func:`_derive_fill_id`'s SHA-256 → first-16-hex style so
    the consumer's transient retry and the reconnect-driven drain/backfill all
    converge to a single queue row. Fill-bearing events always populate
    ``fill_price`` / ``fill_quantity``; the ``or 0.0`` only guards a malformed
    payload (which the translator filters before this is reached anyway).
    """
    h = hashlib.sha256(
        f"{report.alpaca_order_id}|{report.fill_timestamp.isoformat()}"
        f"|{report.fill_quantity or 0.0}|{report.fill_price or 0.0}".encode()
    )
    return f"ufill-{h.hexdigest()[:16]}"


def _derive_fill_id(
    *,
    order_id: str,
    alpaca_order_id: str,
    fill_timestamp: datetime,
    fill_price: float,
    fill_quantity: float,
) -> str:
    """Derive a deterministic ``fill_id`` from the event's dedupe key.

    The persistence table dedupes on ``(order_id, fill_timestamp,
    fill_quantity, fill_price)`` — the same logical fill replayed via
    websocket and disconnect-recovery converges to the same fill_id, so the
    ``ON CONFLICT DO NOTHING`` path in :func:`append_fill_record` collapses
    duplicates at the storage layer rather than producing a second row with
    a fresh UUID.

    SHA-256 → first 16 hex chars yields a 64-bit identifier — collision
    probability is vanishing at AlphaMind's throughput (single-digit fills
    per day) and the dedupe constraint backstops any pathological collision
    by rejecting the second row on the natural key.
    """
    h = hashlib.sha256(
        f"{order_id}|{alpaca_order_id}|{fill_timestamp.isoformat()}|{fill_price}|{fill_quantity}".encode()
    )
    return f"fill-{h.hexdigest()[:16]}"
