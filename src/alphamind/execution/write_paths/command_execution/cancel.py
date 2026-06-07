"""CANCEL command writeback (capital release, bracket dissolution)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from alphamind._kernel.ids import OrderId
from alphamind._kernel.money import Money, money
from alphamind.commands.command_models import CancelCommand
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.command_execution._shared import (
    _assert_bracket_readable,
    _cancel_all_bracket_legs,
    _cancel_pending_protective_orders,
    _emit,
    _emit_order_cancelled,
    _order_notional_usd,
    _order_reservation_price,
    _release_capital,
    _unprocessed_filled_quantity,
)
from alphamind.portfolio_state.events.activity_log import (
    BracketDissolvedDetail,
    EventType,
    ThesisResolvedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketStatus,
    OrderRecord,
    OrderRole,
    OrderStatus,
)
from alphamind.portfolio_state.records.positions import PositionStatus
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.theses import ThesisRow


async def persist_entry_window_cancel(
    handle: InvocationHandle,
    *,
    entry_order_id: str,
    cancel_reason: str,
) -> None:
    """Engine-originated cancel of a never-filled entry whose window elapsed (ALP-737).

    The continuous monitor's entry-window watcher calls this once it has
    confirmed (via the broker cancel + a no-recorded-fills check) that the
    resting entry will not fill. It reuses the CANCEL writeback state machine
    (:func:`_writeback_cancel`) so the bracket dissolves, reserved capital is
    released (the order's reserved notional, per ``_order_reserved_notional`` —
    the same basis the PM CANCEL path uses, which for a resting limit entry is
    ``limit_price * remaining_quantity``), and the thesis resolves
    ``CANCELLED_NEVER_ENTERED`` exactly as a PM-originated CANCEL would.

    Distinct from the PM CANCEL path in two ways: there is no PM envelope (so no
    ``command_id`` and no ``pm_decision`` activity-log entry), and provenance
    rides in ``cancel_reason`` (``"entry_window_expired"``) rather than a
    PM-authored string.
    """
    command = CancelCommand(
        command_type="cancel",
        order_id=OrderId(entry_order_id),
        cancel_reason=cancel_reason,
    )
    await _writeback_cancel(handle, command=command)


async def _writeback_cancel(
    handle: InvocationHandle,
    *,
    command: CancelCommand,
    result: SubmissionResult | None = None,
) -> None:
    """CANCEL: mark target order CANCELLED. If the target is an entry leg:
    cancel all bracket legs, resolve thesis CANCELLED, dissolve bracket,
    release reserved capital.

    Reads ``command.order_id`` (target order to cancel) and
    ``command.cancel_reason`` (drives :class:`OrderCancelledDetail`).
    Emit order_cancelled + capital_released + (entry case) thesis_resolved
    + bracket_dissolved.

    **Partial-fill guard (ALP-760).** A PM CANCEL is decided on a snapshot frozen
    before intraday fills, so the targeted entry may have *already (partially)
    filled* by command-execution commit time. Cancelling the unfilled remainder is correct,
    but dissolving the bracket and resolving the thesis ``CANCELLED_NEVER_ENTERED``
    would orphan the filled shares as an unguarded position. When any shares have
    filled — recorded in ``filled_quantity`` (fill-collection-integrated) or as an
    unprocessed ``fill_records`` row the broker reported but fill-collection has not yet
    drained — only the unfilled remainder's reserved capital is released and the
    bracket / thesis are left intact, so fill-collection fill integration drives the
    position PENDING→OPEN with the bracket still protecting the filled portion.
    """
    del result  # Symmetric dispatch signature; CANCEL reads from command + DB.
    timestamp = datetime.now(UTC)
    target_row = await handle.session.get(OrderRow, command.order_id)
    if target_row is None:
        msg = f"CANCEL references missing order_id={command.order_id!r}"
        raise ValueError(msg)
    target = order_row_to_record(target_row)

    target_row.status = OrderStatus.CANCELLED.value
    target_row.last_update_timestamp = timestamp.isoformat()

    _emit_order_cancelled(
        handle,
        order=target,
        position_id=target.position_id,
        thesis_id=target.originating_thesis_id,
        cancel_reason=command.cancel_reason,
        timestamp=timestamp,
    )

    # Recorded fills against this order so far (ALP-760): the order's own
    # ``filled_quantity`` (fills fill-collection already integrated) plus unprocessed
    # ``fill_records`` the broker reported in the stale-snapshot window but
    # fill-collection has not yet drained. ``filled + unprocessed`` is the true filled
    # quantity at commit time regardless of integration ordering.
    unprocessed_filled = await _unprocessed_filled_quantity(handle, order_id=target.order_id)
    total_filled = target.filled_quantity + unprocessed_filled

    # Capital release is only valid for entry-class orders (ENTRY / ADD_ENTRY).
    # Those are the only roles that reserve capital on submission via
    # ``_reserve_capital``; protective legs (TAKE_PROFIT / PRICE_STOP /
    # TIME_STOP) never reserved any, so they are excluded here — the zero-floor
    # in ``_release_capital`` would mask a phantom protective-leg release but
    # leave the ledger off by its notional, so the role guard, not the floor, is
    # what keeps protective cancels honest.
    if target.role in (OrderRole.ENTRY, OrderRole.ADD_ENTRY):
        # Release only the *unfilled remainder*'s reserved notional, on the same
        # ``reservation_price * quantity`` basis OPEN / ADD reserved at
        # submission and the reprice path adjusts (ALP-741). The filled portion's
        # reservation is released by fill-collection when it integrates the fill
        # (``_fill_reservation_release_usd``), so releasing it here too would
        # double-release. With no recorded fill, ``unprocessed_filled`` is 0 and
        # ``order.remaining_quantity`` is the full size, so this is exactly
        # ``_order_reserved_notional`` — the whole reservation (ALP-760).
        # Market entries carry no price → ``money(0)`` (they reserved nothing —
        # skip the no-op release/emit; a market order fills immediately and its
        # consideration flows through fill collection).
        release_amount = _unfilled_remainder_notional(target, unprocessed_filled=unprocessed_filled)
        if release_amount > 0:
            await _release_capital(
                handle,
                order_id=target.order_id,
                position_id=target.position_id,
                thesis_id=target.originating_thesis_id,
                amount_usd=release_amount,
                timestamp=timestamp,
            )

    if target.role != OrderRole.ENTRY:
        return

    # ALP-760: shares have filled — never orphan them. The unfilled remainder was
    # cancelled on the broker (and the order is marked CANCELLED above), but the
    # bracket must keep protecting the filled portion and the thesis must NOT
    # resolve ``CANCELLED_NEVER_ENTERED``. fill-collection fill integration drives the
    # position PENDING→OPEN and activates the (still-intact) bracket.
    if total_filled > 0:
        return

    bracket_row = await handle.session.get(BracketRow, target.bracket_id)
    if bracket_row is None:
        return

    cancelled_legs = await _cancel_pending_protective_orders(
        handle, bracket_id=target.bracket_id, timestamp=timestamp
    )
    leg_order_ids = tuple(o.order_id for o in cancelled_legs)
    # Transition the parallel ``bracket_legs`` representation, not just the
    # protective *orders* — including order-less EVENT/advisory legs the order
    # sweep above can never reach. A DISSOLVED bracket with a non-CANCELLED leg
    # is unreadable on every subsequent state load (ALP-731).
    await _cancel_all_bracket_legs(handle, bracket_id=target.bracket_id)
    bracket_row.status = BracketStatus.DISSOLVED.value
    _emit(
        handle,
        event_type=EventType.BRACKET_DISSOLVED,
        order_id=None,
        position_id=bracket_row.position_id,
        thesis_id=target.originating_thesis_id,
        timestamp=timestamp,
        detail=BracketDissolvedDetail(cancelled_leg_order_ids=leg_order_ids),
    )

    # A never-filled position whose entry just cancelled has no terminal state
    # in the fill path (fill_collection only transitions PENDING→OPEN→CLOSED). Without
    # this it strands in PENDING forever: never priced by the OPEN-only quote
    # stream, perpetually emitting the assembler's stale-sentinel warning
    # (ALP-744). Both the PM CANCEL and the ALP-737 entry-window auto-cancel
    # reach here, so handling it once covers both routes.
    await _cancel_never_filled_position(handle, position_id=bracket_row.position_id)

    if target.originating_thesis_id is not None:
        await _resolve_thesis_cancelled(
            handle,
            thesis_id=target.originating_thesis_id,
            position_id=bracket_row.position_id,
            timestamp=timestamp,
        )

    # Write-time guard: the bracket must round-trip through the read codec the
    # continuous monitor and scheduled invocations use, or one corrupt row
    # becomes a system-wide kill switch (ALP-731).
    await _assert_bracket_readable(handle, bracket_id=target.bracket_id)


def _unfilled_remainder_notional(order: OrderRecord, *, unprocessed_filled: float) -> Money:
    """Reserved notional still attributable to the *unfilled* remainder (ALP-760).

    ``order.remaining_quantity`` already nets out fill-collection-integrated fills;
    subtracting the not-yet-integrated ``unprocessed_filled`` yields the quantity
    the broker still had working and just cancelled. Floors at zero and reuses
    the ``reservation_price * quantity`` basis (``_order_notional_usd``) the whole
    reservation lifecycle shares (ALP-741). A market entry carries no reservation
    price → ``money(0)``.
    """
    px = _order_reservation_price(order)
    if px is None:
        return money(0)
    unfilled_remainder = max(order.remaining_quantity - unprocessed_filled, 0.0)
    return _order_notional_usd(price=px, remaining_quantity=unfilled_remainder)


async def _cancel_never_filled_position(
    handle: InvocationHandle,
    *,
    position_id: str,
) -> None:
    """Drive a never-filled PENDING position to the terminal CANCELLED state.

    No-op unless the position is still PENDING with zero recorded fills:

    * An already-OPEN position (its entry filled; only a leftover entry-order
      reference is being cancelled) must keep ``OPEN``.
    * A partially-filled position (a strategy mid-open, with per-leg fills in
      ``execution_history``) must keep ``PENDING`` — ``CANCELLED`` means *never
      opened*, and the :class:`PositionRecord` invariant forbids any fill on a
      CANCELLED row, so writing it on a filled row would persist a row the read
      codec rejects (the ALP-731 class of unreadable-row corruption).
    """
    position_row = await handle.session.get(PositionRow, position_id)
    if position_row is None:
        return
    if position_row.status != PositionStatus.PENDING.value:
        return
    if json.loads(position_row.execution_history_json):
        return
    position_row.status = PositionStatus.CANCELLED.value


async def _resolve_thesis_cancelled(
    handle: InvocationHandle,
    *,
    thesis_id: str,
    position_id: str,
    timestamp: datetime,
) -> None:
    thesis_row = await handle.session.get(ThesisRow, thesis_id)
    if thesis_row is None:
        return
    thesis_row.status = ThesisRecordStatus.CANCELLED.value
    thesis_row.resolution_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    thesis_row.resolution_category = "CANCELLED_NEVER_ENTERED"
    _emit(
        handle,
        event_type=EventType.THESIS_RESOLVED,
        order_id=None,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=ThesisResolvedDetail(
            resolution_category="CANCELLED_NEVER_ENTERED",
            component_outcomes_json={},
        ),
    )
