"""CLOSE command writeback (close order insertion + activity log)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.commands.command_models import CloseCommand
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.phase2._shared import (
    _build_pending_order,
    _close_order_direction_for_position,
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
      category in Phase 1; recorded on the order via the originating
      pm_command_id linkage so post-fill processing can re-classify.

    The bracket-leg cancellation and position closure happen on the close
    fill in Phase 1 (per design doc: state transitions from fills happen in
    Phase 1).

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
    # + risk_management_subtype feed the eventual thesis resolution in Phase 1.
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
