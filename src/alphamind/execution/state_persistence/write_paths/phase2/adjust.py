"""ADJUST command writeback (bracket adjustment, stop/target modification).

Also owns the shared replacement-order construction (``_new_*_to_order_shape``
helpers + :func:`_build_replacement_order_for_change_fields`) — ADJUST is the
primary user; :mod:`.add` reuses these for its optional ``bracket_adjustment``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.commands.command_models import (
    AdjustCommand,
    NewStopLevel,
    NewTargetLevel,
)
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.write_paths.phase2._shared import (
    _OMS_COMPONENT_TYPE_TO_PERSISTED,
    _append_bracket_modification,
    _build_pending_order,
    _cancel_pending_protective_orders,
    _emit,
    _emit_order_cancelled,
    _emit_order_submitted,
    _id_suffix,
    _order_direction_for_close,
    _position_quantity,
    _position_ticker,
    _price_to_float,
    _protective_roles_for_change_fields,
)
from alphamind.portfolio_state.events.activity_log import (
    BracketModificationSource,
    BracketModifiedDetail,
    EventType,
    ThesisComponentUpdatedDetail,
)
from alphamind.portfolio_state.records.orders import (
    OrderClass,
    OrderRecord,
    OrderRole,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import PositionRecord

_ADJUST_FIELD_LABELS: dict[str, str] = {
    "new_stop_level": "stop_level",
    "new_target_level": "target_level",
    "new_time_expiration": "time_expiration",
    "new_event_invalidation": "event_invalidation",
    "thesis_component_updates": "thesis_components",
}


def _adjust_field_set(command: AdjustCommand) -> str:
    """Return a deterministic label for whichever change-field(s) are set.

    Multi-field adjustments concatenate the labels separated by ``+`` so a
    BracketModifiedDetail.field_changed value still pinpoints what moved.
    """
    set_labels = [
        label for attr, label in _ADJUST_FIELD_LABELS.items() if getattr(command, attr) is not None
    ]
    return "+".join(set_labels) if set_labels else "no_op"


async def _writeback_adjust(
    handle: InvocationHandle,
    *,
    command: AdjustCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """ADJUST: insert new protective leg order(s) and/or thesis component
    updates per the change-field(s) set on the canonical command.

    Dispatches on whichever of ``new_stop_level``, ``new_target_level``,
    ``new_time_expiration``, ``new_event_invalidation``,
    ``thesis_component_updates`` is set. For each price/time/target leg
    change, the existing protective order is CANCELLED and a new PENDING
    replacement is inserted. For thesis_component_updates, no order
    mutations occur — only the activity-log emit captures the change.
    ``BracketModifiedDetail.rationale`` is populated from
    ``command.adjustment_rationale``.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADJUST references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    if position.bracket_id is None:
        msg = f"ADJUST references position with no bracket: {command.position_id!r}"
        raise ValueError(msg)

    cancelled_orders = await _cancel_pending_protective_orders(
        handle,
        bracket_id=position.bracket_id,
        timestamp=timestamp,
        target_roles=_protective_roles_for_change_fields(
            new_stop_level=command.new_stop_level,
            new_target_level=command.new_target_level,
            new_time_expiration_present=command.new_time_expiration is not None,
        ),
    )
    for cancelled in cancelled_orders:
        _emit_order_cancelled(
            handle,
            order=cancelled,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="adjust_command",
            timestamp=timestamp,
        )

    new_protective = _build_replacement_protective_order(
        command=command,
        position=position,
        result=result,
        timestamp=timestamp,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    new_order_id: str
    if new_protective is not None:
        new_order_id = new_protective.order_id
        handle.session.add(order_record_to_row(new_protective))
        _emit_order_submitted(
            handle,
            order=new_protective,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            pm_command_id=result.command_id,
        )
    else:
        # Thesis-only adjustments produce no protective replacement; the
        # bracket modification history still records the adjustment intent.
        new_order_id = "<no_order>"

    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled_orders),
        new_order_id=new_order_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
        rationale=command.adjustment_rationale,
    )
    _emit(
        handle,
        event_type=EventType.BRACKET_MODIFIED,
        order_id=None,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        detail=BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed=_adjust_field_set(command),
            old_value=",".join(o.order_id for o in cancelled_orders) or "<none>",
            new_value=new_order_id,
            rationale=command.adjustment_rationale,
        ),
    )

    if command.thesis_component_updates is not None and position.thesis_id is not None:
        for wc in command.thesis_component_updates:
            component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wc.component_type]
            _emit(
                handle,
                event_type=EventType.THESIS_COMPONENT_UPDATED,
                order_id=None,
                position_id=command.position_id,
                thesis_id=position.thesis_id,
                timestamp=timestamp,
                detail=ThesisComponentUpdatedDetail(
                    component_id=f"{position.thesis_id}-{component_type.value.lower()}",
                    field_changed="narrative",
                    old_value="",
                    new_value=wc.narrative,
                ),
            )


# ---------------------------------------------------------------------------
# Replacement-order construction (shared with .add's bracket_adjustment)
# ---------------------------------------------------------------------------


def _adjust_replacement_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADJUST-{position_id}-{_id_suffix(command_id)}"


def _build_replacement_protective_order(
    *,
    command: AdjustCommand,
    position: PositionRecord,
    result: SubmissionResult,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Build the replacement protective order for an ADJUST.

    Returns ``None`` for thesis-only or event-only adjustments (no broker
    order changes — event invalidation is advisory). Stop / target / time
    changes each map to a single replacement order. When multiple
    change-fields are set on one command, the precedence is
    stop -> target -> time so the resulting order is deterministic.
    """
    new_order_id = _adjust_replacement_order_id(command.position_id, result.command_id)
    return _build_replacement_order_for_change_fields(
        new_order_id=new_order_id,
        new_stop_level=command.new_stop_level,
        new_target_level=command.new_target_level,
        new_time_expiration_present=command.new_time_expiration is not None,
        position=position,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _new_stop_level_to_order_shape(
    stop: NewStopLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if stop.order_type == "limit":
        return (
            OrderRole.PRICE_STOP,
            OrderType.LIMIT,
            PriceParameters(limit_price=_price_to_float(stop.limit_price)),
        )
    if stop.order_type == "stop_limit":
        return (
            OrderRole.PRICE_STOP,
            OrderType.STOP_LIMIT,
            PriceParameters(
                limit_price=_price_to_float(stop.limit_price),
                stop_trigger_price=_price_to_float(stop.trigger_price),
            ),
        )
    return (
        OrderRole.PRICE_STOP,
        OrderType.STOP,
        PriceParameters(stop_trigger_price=_price_to_float(stop.trigger_price)),
    )


def _new_target_level_to_order_shape(
    tgt: NewTargetLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if tgt.order_type == "market":
        return OrderRole.TAKE_PROFIT, OrderType.MARKET, PriceParameters()
    return (
        OrderRole.TAKE_PROFIT,
        OrderType.LIMIT,
        PriceParameters(limit_price=_price_to_float(tgt.price)),
    )


def _build_replacement_order_for_change_fields(
    *,
    new_order_id: str,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
    position: PositionRecord,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Shared replacement-order construction used by ADJUST and ADD's
    optional ``bracket_adjustment``.

    Returns ``None`` when no protective change-field is set (event-only or
    thesis-only paths produce no broker order).
    """
    if new_stop_level is None and new_target_level is None and not new_time_expiration_present:
        return None
    bracket_id = position.bracket_id or ""
    common: dict[str, Any] = {
        "order_id": new_order_id,
        "position_id": position.position_id,
        "bracket_id": bracket_id,
        "order_class": OrderClass.OTO,
        "direction": _order_direction_for_close(position.direction),
        "quantity": _position_quantity(position),
        "ticker": _position_ticker(position),
        "pm_command_id": pm_command_id,
        "thesis_id": position.thesis_id,
        "timestamp": timestamp,
        "alpaca_order_id_override": alpaca_order_id_override,
    }
    if new_stop_level is not None:
        role, order_type, price_parameters = _new_stop_level_to_order_shape(new_stop_level)
    elif new_target_level is not None:
        role, order_type, price_parameters = _new_target_level_to_order_shape(new_target_level)
    else:
        # new_time_expiration set: time-stop becomes a market order at deadline.
        role, order_type, price_parameters = (
            OrderRole.TIME_STOP,
            OrderType.MARKET,
            PriceParameters(),
        )
    return _build_pending_order(
        role=role,
        order_type=order_type,
        price_parameters=price_parameters,
        **common,
    )
