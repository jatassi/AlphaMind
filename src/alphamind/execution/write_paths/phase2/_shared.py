"""Shared helpers for the Phase 2 command-execution write path.

Cross-cutting machinery used by two or more of the per-command-kind modules
(:mod:`.open`, :mod:`.close`, :mod:`.adjust`, :mod:`.cancel`, :mod:`.add`):
activity-log emission, cash-ledger primitives, protective-leg cancellation,
bracket modification-history append, generic order construction, position-
derived adapters, and common direction / price-parameter / id helpers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import Money, money, signed_money
from alphamind.commands.command_models import (
    EntryOrder,
    EntryOrderType,
    EquityInstrument,
    NewStopLevel,
    NewTargetLevel,
    OptionInstrument,
    StrategyInstrument,
)
from alphamind.execution.oms.command_ids import synthesize_id_suffix
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketModificationSource,
    CapitalReleasedDetail,
    CapitalReservedDetail,
    EventSource,
    EventType,
    OrderCancelledDetail,
    OrderSubmittedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegModification,
    EquityInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponentType,
)
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)


def _price_to_float(value: Decimal | float | None) -> float | None:
    """Cast Decimal-backed Price/Money to legacy float at the record boundary.

    Retires when ALP-462's downstream record types migrate to Decimal/Money.
    """
    return None if value is None else float(value)


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return the ticker/underlying key for a canonical OMS instrument."""
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


_ENTRY_ORDER_TYPE_TO_PERSISTED: dict[EntryOrderType, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop_limit": OrderType.STOP_LIMIT,
}


_OMS_COMPONENT_TYPE_TO_PERSISTED: dict[str, ThesisComponentType] = {
    "entry_rationale": ThesisComponentType.ENTRY_RATIONALE,
    "target_rationale": ThesisComponentType.TARGET_RATIONALE,
    "invalidation_rationale": ThesisComponentType.INVALIDATION_RATIONALE,
}


def _order_position_direction(position: PositionRecord) -> Direction:
    """Resolve the position-level :class:`Direction` an order builder needs.

    Reads route through :func:`position_direction`: equity / single-leg
    options positions yield their non-None ``LONG`` / ``SHORT`` side. A
    multi-leg strategy has no position-level direction, so the accessor
    returns ``None`` — this helper substitutes an inert ``Direction.LONG``
    placeholder. The resulting ``OrderRecord.direction`` is not a meaningful
    side for a strategy: ALP-588 story 01f made the close path leg-derived,
    and the order-level direction-field reshaping that retires this
    placeholder is the separate ALP-603 follow-on. Mirrors story 02a's
    ``ProposedClose`` treatment in ``breach_behavior/cascade.py``.
    """
    direction = position_direction(position)
    return direction if direction is not None else Direction.LONG


def _order_direction_for_entry(direction: Direction) -> OrderDirection:
    return OrderDirection.BUY if direction == Direction.LONG else OrderDirection.SELL


def _order_direction_for_close(direction: Direction) -> OrderDirection:
    """Direction of the order that closes a position with the given direction."""
    return OrderDirection.SELL if direction == Direction.LONG else OrderDirection.BUY


def _entry_price_parameters(entry_order: EntryOrder) -> PriceParameters:
    """Project an ``EntryOrder`` to the persisted ``PriceParameters`` shape."""
    if entry_order.type == "market":
        return PriceParameters()
    if entry_order.type == "limit":
        return PriceParameters(limit_price=_price_to_float(entry_order.limit_price))
    # stop_limit
    return PriceParameters(
        limit_price=_price_to_float(entry_order.limit_price),
        stop_trigger_price=_price_to_float(entry_order.stop_price),
    )


def _position_quantity(position: PositionRecord) -> float:
    """Best-effort quantity used to size replacement orders.

    OPEN positions return their fill count; PENDING positions (zero fills)
    return ``1.0`` so the OrderRecord quantity invariant holds — Phase 1
    overwrites with the real quantity when the entry fills.
    """
    if isinstance(position.details, EquityPositionDetails):
        qty = position.details.share_count
        return qty if qty > 0 else 1.0
    contracts = getattr(position.details, "contract_count", None)
    if isinstance(contracts, int | float) and contracts > 0:
        return float(contracts)
    return 1.0


def _position_ticker(position: PositionRecord) -> str:
    """Return the ticker / underlying for the typed position-details payload.

    Mirrors :func:`_instrument_ticker_key` for positions. Raises on unsupported
    variants so a new InstrumentType must update this helper.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails):
        # Strategy legs all share the same underlying per typed-record invariant.
        return details.legs[0].options.underlying_ticker
    msg = f"_position_ticker: unsupported variant {type(details).__name__!r}"
    raise ValueError(msg)


def _id_suffix(command_id: str) -> str:
    """Stable 32-hex suffix from *command_id*."""
    return synthesize_id_suffix(command_id)


def _build_pending_order(  # noqa: PLR0913 — captures every NOT-NULL OrderRecord field once.
    *,
    order_id: str,
    position_id: str | None,
    bracket_id: str,
    role: OrderRole,
    order_class: OrderClass,
    direction: OrderDirection,
    order_type: OrderType,
    price_parameters: PriceParameters,
    ticker: str,
    pm_command_id: str,
    thesis_id: str | None,
    timestamp: datetime,
    quantity: float,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build a fresh PENDING :class:`OrderRecord`.

    ``alpaca_order_id_override`` (broker-routing coordinated swap, story 03e /
    ALP-390) wires the broker's real id; otherwise falls back to the synthetic
    ``alp-{order_id}`` placeholder.
    """
    alpaca_id = alpaca_order_id_override or f"alp-{order_id}"
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id) if position_id is not None else None,
        bracket_id=BracketId(bracket_id),
        role=role,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=direction,
        order_type=order_type,
        order_class=order_class,
        price_parameters=price_parameters,
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(alpaca_id),
        alpaca_order_id_chain=(AlpacaOrderId(alpaca_id),),
        submission_timestamp=timestamp,
        last_update_timestamp=timestamp,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        originating_pm_command_id=CommandId(pm_command_id),
        age_hours=0.0,
    )


def _build_entry_order_from_command(  # noqa: PLR0913 — distinct ID, position, bracket, ticker, role threaded through.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    entry_order: EntryOrder,
    quantity: float,
    direction: Direction,
    pm_command_id: str,
    timestamp: datetime,
    role: OrderRole,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted entry / add-entry order from a canonical EntryOrder."""
    persisted_order_type = _ENTRY_ORDER_TYPE_TO_PERSISTED[entry_order.type]
    price_parameters = _entry_price_parameters(entry_order)
    order_class = OrderClass.SIMPLE if role == OrderRole.ADD_ENTRY else OrderClass.BRACKET
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=role,
        order_class=order_class,
        direction=_order_direction_for_entry(direction),
        order_type=persisted_order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


async def _read_cash_row(handle: InvocationHandle) -> CashLedgerRow:
    row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if row is None:
        msg = "cash_ledger singleton missing — Phase 2 cannot reserve capital"
        raise ValueError(msg)
    return row


async def _reserve_capital(handle: InvocationHandle, *, amount_usd: Money) -> None:
    cash_row = await _read_cash_row(handle)
    # Reserve adds non-negative to a non-negative pool — ``money()`` constructor.
    cash_row.reserved_capital_usd = money(cash_row.reserved_capital_usd + amount_usd)
    cash_row.last_updated_at = datetime.now(UTC).isoformat()


def _emit_capital_reserved(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: Money,
    timestamp: datetime,
) -> None:
    _emit(
        handle,
        event_type=EventType.CAPITAL_RESERVED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        # ALP-463 — activity-log detail classes now carry ``Money`` directly;
        # pass through without a float round-trip.
        detail=CapitalReservedDetail(order_id=order_id, amount_usd=amount_usd),
    )


async def _release_capital(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: Money,
    timestamp: datetime,
) -> None:
    cash_row = await _read_cash_row(handle)
    # signed_money admits the negative case so over-release surfaces in the
    # running balance instead of being silently clamped (05b retired the
    # legacy max-zero floor).
    cash_row.reserved_capital_usd = signed_money(cash_row.reserved_capital_usd - amount_usd)
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    _emit(
        handle,
        event_type=EventType.CAPITAL_RELEASED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        # ALP-463 — see ``_emit_capital_reserved`` for the Money-direct migration.
        detail=CapitalReleasedDetail(order_id=order_id, amount_usd=amount_usd),
    )


# Protective-leg roles (entry / add-entry deliberately omitted — the entry-
# CANCEL path cancels the entry separately before any protective sweep).
_ALL_PROTECTIVE_ROLES: frozenset[str] = frozenset(
    {OrderRole.PRICE_STOP.value, OrderRole.TAKE_PROFIT.value, OrderRole.TIME_STOP.value}
)


async def _cancel_pending_protective_orders(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    timestamp: datetime,
    target_roles: frozenset[str] = _ALL_PROTECTIVE_ROLES,
) -> tuple[OrderRecord, ...]:
    """Mark PENDING protective orders matching *target_roles* CANCELLED in place.

    Returns the typed records for activity-log emission. *target_roles*
    defaults to every protective leg role (entry-CANCEL → dissolve-bracket);
    ADJUST / BracketAdjustment narrow via
    :func:`_protective_roles_for_change_fields` so only the targeted leg(s)
    transition to CANCELLED. Entry / add-entry roles are never touched here.
    """
    stmt = (
        select(OrderRow)
        .where(OrderRow.bracket_id == bracket_id)
        .where(OrderRow.status == OrderStatus.PENDING.value)
    )
    rows = list((await handle.session.execute(stmt)).scalars())
    cancelled: list[OrderRecord] = []
    for row in rows:
        if row.order_role not in target_roles:
            continue
        row.status = OrderStatus.CANCELLED.value
        row.last_update_timestamp = timestamp.isoformat()
        cancelled.append(order_row_to_record(row))
    return tuple(cancelled)


def _protective_roles_for_change_fields(
    *,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
) -> frozenset[str]:
    """Return the protective-leg roles the change-fields target.

    NewStopLevel → PRICE_STOP; NewTargetLevel → TAKE_PROFIT;
    new_time_expiration → TIME_STOP. Keeps the OMS-state writeback in lockstep
    with the broker mutation in
    :func:`alphamind.decision.portfolio_manager.submit_envelope._adjust_command_context`,
    so a stop-only ADJUST never also marks the take-profit leg CANCELLED.
    """
    roles: set[str] = set()
    if new_stop_level is not None:
        roles.add(OrderRole.PRICE_STOP.value)
    if new_target_level is not None:
        roles.add(OrderRole.TAKE_PROFIT.value)
    if new_time_expiration_present:
        roles.add(OrderRole.TIME_STOP.value)
    return frozenset(roles)


_MODIFICATION_HISTORY_ADAPTER: TypeAdapter[tuple[BracketLegModification, ...]] = TypeAdapter(
    tuple[BracketLegModification, ...]
)


async def _append_bracket_modification(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    old_order_ids: tuple[str, ...],
    new_order_id: str,
    timestamp: datetime,
    pm_command_id: str,
    rationale: str = "ADJUST command",
) -> None:
    """Append one entry to the bracket's ``modification_history_json``.

    Reads + writes the JSON column directly rather than round-tripping the
    bracket through the codec; the history vocabulary is shared with
    ``brackets_codec``.
    """
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        return
    history = _MODIFICATION_HISTORY_ADAPTER.validate_json(bracket_row.modification_history_json)
    new_history = (
        *history,
        BracketLegModification(
            timestamp=timestamp,
            pm_command_id=CommandId(pm_command_id),
            source=BracketModificationSource.PM.value,
            field_changed="protective_leg_order",
            old_value=",".join(old_order_ids) or "<none>",
            new_value=new_order_id,
            rationale=rationale,
        ),
    )
    bracket_row.modification_history_json = _MODIFICATION_HISTORY_ADAPTER.dump_json(
        new_history
    ).decode()


def _emit_order_submitted(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    pm_command_id: str,
    extra_parameters: dict[str, Any] | None = None,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
) -> None:
    parameters: dict[str, Any] = {
        "order_id": order.order_id,
        "role": order.role.value,
        "direction": order.direction.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "instrument_ticker": getattr(order.instrument_spec, "ticker", ""),
    }
    if order.price_parameters.limit_price is not None:
        parameters["limit_price"] = order.price_parameters.limit_price
    if order.price_parameters.stop_trigger_price is not None:
        parameters["stop_trigger_price"] = order.price_parameters.stop_trigger_price
    if extra_parameters:
        parameters.update(extra_parameters)
    _emit(
        handle,
        event_type=EventType.ORDER_SUBMITTED,
        order_id=order.order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=OrderSubmittedDetail(
            order_parameters_json=parameters,
            pm_command_id=pm_command_id,
        ),
        source=source,
    )


def _emit_order_cancelled(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    position_id: str | None,
    thesis_id: str | None,
    cancel_reason: str,
    timestamp: datetime,
) -> None:
    _emit(
        handle,
        event_type=EventType.ORDER_CANCELLED,
        order_id=order.order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=OrderCancelledDetail(
            cancel_reason=cancel_reason,
            filled_quantity_at_cancellation=int(order.filled_quantity),
        ),
    )


def _emit(
    handle: InvocationHandle,
    *,
    event_type: EventType,
    order_id: str | None,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
) -> None:
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{event_type.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=timestamp,
        event_type=event_type,
        event_group=EVENT_TYPE_TO_GROUP[event_type],
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=detail,
    )
    append_activity_log_entry(handle, entry)
