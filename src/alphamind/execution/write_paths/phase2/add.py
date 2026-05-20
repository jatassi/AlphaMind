"""ADD command writeback (position add, thesis-component append, capital reserve).

Reuses :mod:`.adjust`'s replacement-order construction for the optional
``bracket_adjustment`` field on the ADD command.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.commands.command_models import (
    AddCommand,
    BracketAdjustment,
)
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.write_paths.phase2._shared import (
    _OMS_COMPONENT_TYPE_TO_PERSISTED,
    _append_bracket_modification,
    _build_entry_order_from_command,
    _cancel_pending_protective_orders,
    _emit,
    _emit_capital_reserved,
    _emit_order_cancelled,
    _emit_order_submitted,
    _id_suffix,
    _order_position_direction,
    _protective_roles_for_change_fields,
    _reserve_capital,
)
from alphamind.execution.write_paths.phase2.adjust import (
    _apply_protective_leg_modification,
    _build_replacement_order_for_change_fields,
)
from alphamind.portfolio_state.events.activity_log import (
    BracketModificationSource,
    BracketModifiedDetail,
    EventType,
    ThesisComponentAddedDetail,
)
from alphamind.portfolio_state.records.orders import OrderRole
from alphamind.portfolio_state.records.positions import PositionRecord
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


async def _writeback_add(
    handle: InvocationHandle,
    *,
    command: AddCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """ADD: insert add-entry order (PENDING). Append a new thesis component.
    Reserve capital. Optionally cancel + resubmit modified bracket legs.
    Emit order_submitted + thesis_component_added + capital_reserved
    (+ bracket_modified entries if a bracket_adjustment was supplied).

    Reads canonical :class:`AddCommand` fields:

    * ``additional_quantity`` — entry order quantity
    * ``additional_dollar_value`` — capital reservation amount
    * ``entry_order`` — order type / price parameters
    * ``thesis_addition_component`` — appended to the existing thesis
    * ``bracket_adjustment`` (optional) — cancel + resubmit modified protective legs

    Canonical :class:`AddCommand` (story 01a) carries no embedded instrument
    — it references an existing position by id. The ticker is derived from
    the position's details payload.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADD references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    bracket_id = position.bracket_id or ""
    ticker: str = (
        getattr(position.details, "ticker", None)
        or getattr(position.details, "underlying_ticker", None)
        or ""
    )

    add_order_id = _add_order_id(command.position_id, result.command_id)
    add_order = _build_entry_order_from_command(
        order_id=add_order_id,
        position_id=command.position_id,
        bracket_id=bracket_id,
        thesis_id=position.thesis_id,
        ticker=ticker,
        entry_order=command.entry_order,
        quantity=command.additional_quantity,
        direction=_order_position_direction(position),
        pm_command_id=result.command_id,
        timestamp=timestamp,
        role=OrderRole.ADD_ENTRY,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    handle.session.add(order_record_to_row(add_order))
    _emit_order_submitted(
        handle,
        order=add_order,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )

    if position.thesis_id is not None:
        component_id = _new_component_id(position.thesis_id, result.command_id)
        wire_component_type = command.thesis_addition_component.component_type
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wire_component_type]
        _emit(
            handle,
            event_type=EventType.THESIS_COMPONENT_ADDED,
            order_id=None,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            detail=ThesisComponentAddedDetail(
                component_id=component_id,
                component_type=component_type.value,
            ),
        )

    await _reserve_capital(handle, amount_usd=command.additional_dollar_value)
    _emit_capital_reserved(
        handle,
        order_id=add_order_id,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        amount_usd=command.additional_dollar_value,
        timestamp=timestamp,
    )

    if command.bracket_adjustment is not None:
        await _apply_bracket_adjustment(
            handle,
            adjustment=command.bracket_adjustment,
            position=position,
            pm_command_id=result.command_id,
            timestamp=timestamp,
        )


async def _apply_bracket_adjustment(
    handle: InvocationHandle,
    *,
    adjustment: BracketAdjustment,
    position: PositionRecord,
    pm_command_id: str,
    timestamp: datetime,
) -> None:
    """Apply an ADD-time :class:`BracketAdjustment` — cancel old protective
    legs, insert the replacement, append bracket modification history.
    """
    if position.bracket_id is None:
        return
    cancelled = await _cancel_pending_protective_orders(
        handle,
        bracket_id=position.bracket_id,
        timestamp=timestamp,
        target_roles=_protective_roles_for_change_fields(
            new_stop_level=adjustment.new_stop_level,
            new_target_level=adjustment.new_target_level,
            new_time_expiration_present=adjustment.new_time_expiration is not None,
        ),
    )
    for cancelled_order in cancelled:
        _emit_order_cancelled(
            handle,
            order=cancelled_order,
            position_id=position.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="add_command_bracket_adjustment",
            timestamp=timestamp,
        )

    new_order_id = f"ORD-ADD-ADJ-{position.position_id}-{_id_suffix(pm_command_id)}"
    new_order = _build_replacement_order_for_change_fields(
        new_order_id=new_order_id,
        new_stop_level=adjustment.new_stop_level,
        new_target_level=adjustment.new_target_level,
        new_time_expiration_present=adjustment.new_time_expiration is not None,
        position=position,
        pm_command_id=pm_command_id,
        timestamp=timestamp,
    )
    new_order_label = new_order.order_id if new_order is not None else "<no_order>"
    if new_order is not None:
        handle.session.add(order_record_to_row(new_order))
        _emit_order_submitted(
            handle,
            order=new_order,
            position_id=position.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            pm_command_id=pm_command_id,
        )
        await _apply_protective_leg_modification(
            handle,
            bracket_id=position.bracket_id,
            new_stop_level=adjustment.new_stop_level,
            new_target_level=adjustment.new_target_level,
            new_time_expiration=adjustment.new_time_expiration,
            position=position,
            replacement_order_id=new_order.order_id,
        )
    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled),
        new_order_id=new_order_label,
        timestamp=timestamp,
        pm_command_id=pm_command_id,
        rationale="ADD bracket_adjustment",
    )
    _emit(
        handle,
        event_type=EventType.BRACKET_MODIFIED,
        order_id=None,
        position_id=position.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        detail=BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="bracket_adjustment",
            old_value=",".join(o.order_id for o in cancelled) or "<none>",
            new_value=new_order_label,
            rationale="ADD bracket_adjustment",
        ),
    )


def _add_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADD-{position_id}-{_id_suffix(command_id)}"


def _new_component_id(thesis_id: str, command_id: str) -> str:
    return f"{thesis_id}-add-{_id_suffix(command_id)}"
