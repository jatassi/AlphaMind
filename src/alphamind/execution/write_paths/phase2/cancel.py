"""CANCEL command writeback (capital release, bracket dissolution)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alphamind._kernel.ids import OrderId
from alphamind._kernel.money import Money, money
from alphamind.commands.command_models import CancelCommand
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.phase2._shared import (
    _assert_bracket_readable,
    _cancel_all_bracket_legs,
    _cancel_pending_protective_orders,
    _emit,
    _emit_order_cancelled,
    _release_capital,
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
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.state.tables.theses import ThesisRow


async def persist_entry_window_cancel(
    handle: InvocationHandle,
    *,
    entry_order_id: str,
    cancel_reason: str,
) -> None:
    """Engine-originated cancel of a never-filled entry whose window elapsed (ALP-737).

    The continuous monitor's entry-window watcher calls this *after* the broker
    has accepted the cancel of the resting entry order (the broker-accept is the
    race-safe proof the entry had not filled). It reuses the CANCEL writeback
    state machine (:func:`_writeback_cancel`) so the bracket dissolves, the
    reserved capital is released, and the thesis resolves
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
    # Capital release is only valid for entry-class orders (ENTRY / ADD_ENTRY).
    # Those are the only roles that reserve capital on submission via
    # ``_reserve_capital``; protective legs (TAKE_PROFIT / PRICE_STOP /
    # TIME_STOP) never reserved any. CANCELling a protective leg must NOT
    # release a phantom amount — the ``max(... - amount_usd, 0.0)`` floor in
    # ``_release_capital`` would mask the symptom but leave the ledger off by
    # the protective leg's notional for the remainder of the cell's life.
    if target.role in (OrderRole.ENTRY, OrderRole.ADD_ENTRY):
        # Capital release amount derived from the cancelled order's notional
        # (quantity * limit/stop price for non-market orders, or zero for
        # market orders without price parameters — those have no capital
        # reservation because a market order is filled immediately on
        # submission and the reservation flowed through Phase 1 already).
        release_amount = _order_notional_estimate(target)
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


def _order_notional_estimate(order: OrderRecord) -> Money:
    """Best-effort capital estimate for a cancelled order.

    Uses the order's price parameters (limit price preferred, stop trigger
    fallback) times the remaining quantity. Falls back to ``money("0")`` for
    market orders with no parameters. Returns ``Money`` so callers thread the
    Decimal-backed accumulator through ``_release_capital`` without floats.
    """
    pp = order.price_parameters
    px = pp.limit_price if pp.limit_price is not None else pp.stop_trigger_price
    if px is None:
        return money(0)
    # Quantity may be float in the legacy record types; cast through ``str`` so
    # binary drift never enters the monetary computation.
    return money(Decimal(str(px)) * Decimal(str(order.remaining_quantity)))


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
