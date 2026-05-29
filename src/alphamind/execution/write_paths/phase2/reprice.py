"""Non-terminal entry-window reprice writeback (ALP-740).

The continuous monitor's entry-window watcher (ALP-737) cancels a never-filled
``PENDING_ENTRY`` entry once its ``entry_window_deadline`` elapses. ALP-740 adds
the other half: before giving up, *reprice* the resting limit toward the market
(escalate to a marketable limit via :func:`marketable_limit_price`) so an
accepted thesis still gets a fill. The broker round-trip is a cancel-and-replace
(:func:`submit_replace`), which yields a fresh Alpaca order id.

This module owns the OMS-state side of that reprice. In deliberate contrast with
:func:`alphamind.execution.write_paths.phase2.cancel.persist_entry_window_cancel`
— which is *terminal* (CANCELs the entry, DISSOLVEs the bracket, RELEASEs the
full reserved capital, RESOLVEs the thesis ``CANCELLED_NEVER_ENTERED``) — a
reprice is *non-terminal*:

* the bracket stays ``PENDING_ENTRY`` and the thesis is untouched — a fill is
  still being pursued;
* the entry order's ``limit_price`` moves to the new marketable level;
* the new broker id is appended to ``alpaca_order_id_chain`` and
  ``modification_count`` is incremented (Alpaca cancel-and-replace semantics —
  the OMS ``order_id`` is stable, the broker chain extends). ``modification_count``
  doubles as the reprice-loop bound the watcher reads;
* the reserved capital is *adjusted*, not released — by the change in the
  order's notional estimate (``limit_price * remaining_quantity``, the same
  basis the terminal cancel releases on), so the reservation tracks the live
  limit and the eventual cancel's release nets out;
* one ``order_modified`` activity-log entry records the move.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from alphamind._kernel.ids import AlpacaOrderId
from alphamind._kernel.money import Price, money
from alphamind.execution.write_paths.phase2._shared import (
    _emit,
    _emit_capital_reserved,
    _order_notional_usd,
    _release_capital,
    _reserve_capital,
)
from alphamind.portfolio_state.events.activity_log import (
    EventSource,
    EventType,
    OrderModifiedDetail,
)
from alphamind.portfolio_state.records.orders import OrderRecord, PriceParameters
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)


async def persist_entry_window_reprice(
    handle: InvocationHandle,
    *,
    entry_order_id: str,
    new_limit_price: Price,
    new_alpaca_order_id: str,
    reprice_reason: str,
) -> None:
    """Engine-originated reprice of a still-resting entry whose window elapsed.

    Mutates the entry order in place to the new marketable ``limit_price``,
    extends ``alpaca_order_id_chain`` with *new_alpaca_order_id* and increments
    ``modification_count``, adjusts the reserved-capital ledger by the notional
    delta, and emits one ``order_modified`` entry with the reprice provenance in
    ``pm_rationale`` (``EventSource.BRACKET_MANAGER`` — engine-originated, not a
    PM command). The bracket and thesis are left untouched: a reprice keeps the
    bracket ``PENDING_ENTRY`` while a fill is still being pursued.

    Raises ``ValueError`` if the entry order row is missing or carries no
    ``limit_price`` to reprice (a non-limit entry is never a reprice target —
    the watcher routes those to the terminal cancel instead).
    """
    timestamp = datetime.now(UTC)
    target_row = await handle.session.get(OrderRow, entry_order_id)
    if target_row is None:
        msg = f"REPRICE references missing order_id={entry_order_id!r}"
        raise ValueError(msg)
    target = order_row_to_record(target_row)
    old_limit = target.price_parameters.limit_price
    if old_limit is None:
        msg = f"REPRICE target order {entry_order_id!r} has no limit_price to reprice"
        raise ValueError(msg)

    new_alpaca_id = AlpacaOrderId(new_alpaca_order_id)
    # Rebuild the record (re-runs the OrderRecord invariants, incl. the
    # alpaca_order_id == chain[-1] tie) then project to a row so the codec owns
    # the JSON serialisation; copy only the columns the reprice mutates onto the
    # session-attached row (in-place UPDATE, mirroring _writeback_cancel).
    updated = dataclasses.replace(
        target,
        price_parameters=PriceParameters(
            limit_price=new_limit_price,
            stop_trigger_price=target.price_parameters.stop_trigger_price,
        ),
        alpaca_order_id=new_alpaca_id,
        alpaca_order_id_chain=(*target.alpaca_order_id_chain, new_alpaca_id),
        # This is the only writer of an entry order's modification_count in the
        # codebase (PM ADJUSTs touch protective legs, not the entry), so the
        # repricer can read it back as the reprice-loop bound. If a future path
        # ever cancel-and-replaces a PENDING_ENTRY entry, that coupling must be
        # revisited (ALP-740 review).
        modification_count=target.modification_count + 1,
        last_update_timestamp=timestamp,
    )
    projected = order_record_to_row(updated)
    target_row.price_parameters_json = projected.price_parameters_json
    target_row.alpaca_order_id = projected.alpaca_order_id
    target_row.alpaca_order_id_chain_json = projected.alpaca_order_id_chain_json
    target_row.modification_count = projected.modification_count
    target_row.last_update_timestamp = projected.last_update_timestamp

    await _adjust_reservation_for_reprice(
        handle, order=target, old_limit=old_limit, new_limit=new_limit_price, timestamp=timestamp
    )

    _emit(
        handle,
        event_type=EventType.ORDER_MODIFIED,
        order_id=entry_order_id,
        position_id=target.position_id,
        thesis_id=target.originating_thesis_id,
        timestamp=timestamp,
        detail=OrderModifiedDetail(
            field_changed="limit_price",
            old_value=str(old_limit),
            new_value=str(new_limit_price),
            pm_rationale=reprice_reason,
        ),
        source=EventSource.BRACKET_MANAGER,
    )


async def _adjust_reservation_for_reprice(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    old_limit: Price,
    new_limit: Price,
    timestamp: datetime,
) -> None:
    """Adjust reserved capital by the change in the order's notional estimate.

    The notional basis is ``_order_notional_usd`` (``limit_price *
    remaining_quantity``) — the same basis OPEN / ADD reserve on
    (``_order_reserved_notional``) and the terminal CANCEL releases on — so the
    reservation tracks the live limit and the full OPEN → reprice → cancel/fill
    lifecycle conserves: ``reserved_capital_usd`` returns to its pre-reservation
    level and never drifts. (ALP-741 unified OPEN onto this notional basis;
    before that, OPEN reserved ``position_size.dollar_value`` and the basis
    mismatch let a repriced/cancelled entry drive the ledger negative.) A short
    entry repriced down toward the bid frees capital (negative delta → release);
    a long entry repriced up toward the ask reserves more (positive delta →
    reserve). A zero delta (limit unchanged) is a no-op.
    """
    new_notional = _order_notional_usd(price=new_limit, remaining_quantity=order.remaining_quantity)
    old_notional = _order_notional_usd(price=old_limit, remaining_quantity=order.remaining_quantity)
    delta = new_notional - old_notional
    if delta == 0:
        return
    if delta > 0:
        await _reserve_capital(handle, amount_usd=money(delta))
        _emit_capital_reserved(
            handle,
            order_id=order.order_id,
            position_id=order.position_id,
            thesis_id=order.originating_thesis_id,
            amount_usd=money(delta),
            timestamp=timestamp,
        )
    else:
        await _release_capital(
            handle,
            order_id=order.order_id,
            position_id=order.position_id,
            thesis_id=order.originating_thesis_id,
            amount_usd=money(-delta),
            timestamp=timestamp,
        )
