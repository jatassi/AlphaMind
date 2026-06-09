"""CLOSE command writeback (close order insertion + activity log)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.commands.command_models import CloseCommand
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.command_execution._shared import (
    _assert_bracket_readable,
    _build_pending_order,
    _cancel_all_bracket_legs,
    _cancel_pending_protective_orders,
    _close_order_direction_for_position,
    _emit_order_cancelled,
    _emit_order_submitted,
    _id_suffix,
    _instrument_spec_for_position,
)
from alphamind.portfolio_state.events.activity_log import EventSource
from alphamind.portfolio_state.records.orders import (
    InstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderRecord,
    OrderRole,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)


async def _writeback_close(
    handle: InvocationHandle,
    *,
    command: CloseCommand,
    result: SubmissionResult,
    extra_metadata: dict[str, Any] | None = None,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """CLOSE: insert close order (status PENDING). Emit order_submitted.

    Reads canonical CloseCommand fields:

    * ``command.quantity`` — numeric (partial close) or ``"all"`` (full close).
      Numeric values become the close order's ``quantity``; ``"all"`` resolves
      to the position's full share count.
    * ``command.order_type`` — ``"market"`` or ``"limit"`` (mapped to
      :class:`OrderType`).
    * ``command.limit_price`` — populated for limit orders.
    * ``command.close_rationale_type`` — feeds the eventual thesis resolution
      category in fill collection; recorded on the order via the originating
      pm_command_id linkage so post-fill processing can re-classify.

    The bracket-leg cancellation and position closure happen on the close
    fill in fill collection (per design doc: state transitions from fills happen in
    fill collection).

    ``extra_metadata`` is folded into the ``order_parameters_json`` payload of
    the emitted ``order_submitted`` activity log entry — the engine-envelope
    submission path (story 04 / ALP-375) uses it to thread
    ``cascade_id`` / ``position_selection_rationale`` / ``rule_breached``
    through to the activity log alongside the existing rationale metadata.

    ``source`` overrides the default ``COMMAND_EXECUTOR`` event source — the
    engine-envelope path tags entries with ``BRACKET_MANAGER`` per
    ``oms-commands.md § Command origins`` and the ``EventSource`` enum docstring.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"CLOSE references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    # Resolve quantity from the union: numeric → that quantity; "all" → the
    # position's full share count.
    if command.quantity == "all":
        if isinstance(position.details, EquityPositionDetails):
            close_qty = position.details.share_count
        else:
            close_qty = getattr(position.details, "contract_count", 0.0)
    else:
        close_qty = float(command.quantity)
    # ``close_qty <= 0`` after the union resolution above can only occur on a
    # CLOSE-all against a PENDING (zero-fill) position — closing-before-fill
    # is a structural contract violation per oms-commands.md § Command origins.
    # Raise rather than fabricate a phantom 1-share order; mirrors the
    # missing-position branch above.
    if close_qty <= 0:
        msg = (
            f"CLOSE references PENDING position_id={command.position_id!r} "
            f"with no fills; cannot resolve close quantity. The PM must wait "
            "for the entry to fill before issuing a CLOSE."
        )
        raise ValueError(msg)

    if command.order_type == "limit":
        order_type = OrderType.LIMIT
        price_parameters = PriceParameters(limit_price=command.limit_price)
    else:
        order_type = OrderType.MARKET
        price_parameters = PriceParameters()

    close_order = _build_close_order(
        order_id=_close_order_id(command.position_id, result.command_id),
        position_id=command.position_id,
        bracket_id=position.bracket_id or "",
        thesis_id=position.thesis_id,
        order_direction=_close_order_direction_for_position(position),
        instrument_spec=_instrument_spec_for_position(position),
        quantity=close_qty,
        order_type=order_type,
        price_parameters=price_parameters,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    handle.session.add(order_record_to_row(close_order))
    # CLOSE-specific rationale metadata: close_rationale_type + invalidation_reason
    # + risk_management_subtype feed the eventual thesis resolution in fill collection.
    # We surface them on the order_submitted detail via the order_parameters_json
    # so the post-fill processor can read them without re-fetching the command.
    rationale_metadata: dict[str, Any] = {
        "close_rationale_type": command.close_rationale_type,
        "invalidation_reason": command.invalidation_reason,
        "risk_management_subtype": command.risk_management_subtype,
        "requested_quantity": ("all" if command.quantity == "all" else float(command.quantity)),
    }
    if extra_metadata:
        rationale_metadata.update(extra_metadata)
    _emit_order_submitted(
        handle,
        order=close_order,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
        extra_parameters=rationale_metadata,
        source=source,
    )

    await _cancel_equity_protective_legs(
        handle, position=position, command=command, timestamp=timestamp
    )


async def _cancel_equity_protective_legs(
    handle: InvocationHandle,
    *,
    position: Any,
    command: CloseCommand,
    timestamp: datetime,
) -> None:
    """Mark an equity bracket's PENDING protective legs CANCELLED on a CLOSE (ALP-937).

    The broker dispatcher cancels the native bracket's broker-enforced legs at the
    broker *before* the SIMPLE close sell (freeing the ``held_for_orders`` shares);
    this writeback reflects that in OMS state so the protective ``orders`` rows do
    not drift ``PENDING`` for orders that no longer rest at the broker. Both
    broker- and monitor-enforced legs (e.g. TIME_STOP) are marked CANCELLED — the
    monitor leg never went to the broker, but the protection it armed is gone too.

    The bracket *status* is deliberately left unchanged: a full close dissolves it
    on the close fill (fill collection, OPEN→CLOSED); a partial reduce leaves it
    ACTIVE with cancelled legs — the remainder is backstopped by the continuous-
    monitor max-loss guardrail until the PM re-evaluates, and re-protected by a
    fresh OCO in a separate follow-up (ALP-938, ALP-937 deliverable D — deferred).
    An ACTIVE bracket whose legs are all CANCELLED is readable (the read invariant
    only forces all-CANCELLED on a DISSOLVED bracket), so ``_assert_bracket_readable``
    is the write-time guard against an unreadable row (ALP-731). No-op for an
    options / strategy CLOSE (the native-bracket share-reservation is equity-only)
    or a position with no bracket.

    **Timing — runs at pre-commit, so the naked-close path stays consistent.** This
    writeback is part of ``precommit_command`` (ALP-836), which commits BEFORE the
    broker dispatch. The dispatch then cancels the same legs at the broker ahead of
    the close sell, so even when that sell is *rejected* (the ALP-937 (F)
    naked-position case), OMS state already reads the legs CANCELLED — matching the
    broker, where they were cancelled before the failed sell. The abandon path tears
    down only the close *order* row; the protective legs correctly stay CANCELLED.
    """
    if not isinstance(position.details, EquityPositionDetails) or not position.bracket_id:
        return
    cancelled_legs = await _cancel_pending_protective_orders(
        handle, bracket_id=position.bracket_id, timestamp=timestamp
    )
    # Transition the parallel ``bracket_legs`` representation too (including any
    # order-less event/advisory leg the order sweep cannot reach), keeping it in
    # lockstep with the cancelled protective orders.
    await _cancel_all_bracket_legs(handle, bracket_id=position.bracket_id)
    for leg in cancelled_legs:
        _emit_order_cancelled(
            handle,
            order=leg,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="protective leg cancelled ahead of equity CLOSE sell (ALP-937)",
            timestamp=timestamp,
        )
    await _assert_bracket_readable(handle, bracket_id=position.bracket_id)


def _close_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-CLOSE-{position_id}-{_id_suffix(command_id)}"


def _build_close_order(  # noqa: PLR0913 — close construction threads ids + sizing + price params.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    order_direction: OrderDirection | None,
    instrument_spec: InstrumentSpec,
    quantity: float,
    order_type: OrderType,
    price_parameters: PriceParameters,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted close order for *position*.

    For a strategy position ``order_direction`` is ``None`` and
    ``instrument_spec`` is the parent :class:`StrategyInstrumentSpec`; the
    record is the MLEG envelope that exits the strategy (ALP-614).
    """
    order_class = OrderClass.MLEG if order_direction is None else OrderClass.SIMPLE
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.CLOSE,
        order_class=order_class,
        direction=order_direction,
        order_type=order_type,
        price_parameters=price_parameters,
        instrument_spec=instrument_spec,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        quantity=quantity,
        alpaca_order_id_override=alpaca_order_id_override,
    )
