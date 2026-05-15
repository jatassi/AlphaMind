"""Order-lifecycle event details — submission through fill / cancel / expiry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alphamind._kernel.money import Money, Price
from alphamind.portfolio_state.events.types import (
    EventGroup,
    EventType,
    OrderRejectionSource,
)


@dataclass(frozen=True, slots=True)
class OrderSubmittedDetail:
    """Detail payload for ORDER_SUBMITTED events."""

    order_parameters_json: dict[str, Any]
    pm_command_id: str


@dataclass(frozen=True, slots=True)
class OrderFilledDetail:
    """Detail payload for ORDER_FILLED events.

    ``slippage`` may be signed (negative if the fill came in better than the
    submitted price); uses signed ``Money`` semantics. ``fees`` is a
    non-negative ``Money``.
    """

    fill_price: Price
    fill_quantity: float
    slippage: Money
    fees: Money


@dataclass(frozen=True, slots=True)
class OrderPartiallyFilledDetail:
    """Detail payload for ORDER_PARTIALLY_FILLED events."""

    fill_price: Price
    fill_quantity: float
    remaining_quantity: float


@dataclass(frozen=True, slots=True)
class OrderCancelledDetail:
    """Detail payload for ORDER_CANCELLED events."""

    cancel_reason: str
    filled_quantity_at_cancellation: int


@dataclass(frozen=True, slots=True)
class OrderExpiredDetail:
    """Detail payload for ORDER_EXPIRED events."""

    filled_quantity_at_expiration: int


@dataclass(frozen=True, slots=True)
class OrderRejectedDetail:
    """Detail payload for ORDER_REJECTED events."""

    rejection_reason: str
    rejection_source: OrderRejectionSource


@dataclass(frozen=True, slots=True)
class OrderModifiedDetail:
    """Detail payload for ORDER_MODIFIED events."""

    field_changed: str
    old_value: str
    new_value: str
    pm_rationale: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.ORDER_SUBMITTED, OrderSubmittedDetail, EventGroup.ORDER_LIFECYCLE),
    (EventType.ORDER_FILLED, OrderFilledDetail, EventGroup.ORDER_LIFECYCLE),
    (
        EventType.ORDER_PARTIALLY_FILLED,
        OrderPartiallyFilledDetail,
        EventGroup.ORDER_LIFECYCLE,
    ),
    (EventType.ORDER_CANCELLED, OrderCancelledDetail, EventGroup.ORDER_LIFECYCLE),
    (EventType.ORDER_EXPIRED, OrderExpiredDetail, EventGroup.ORDER_LIFECYCLE),
    (EventType.ORDER_REJECTED, OrderRejectedDetail, EventGroup.ORDER_LIFECYCLE),
    (EventType.ORDER_MODIFIED, OrderModifiedDetail, EventGroup.ORDER_LIFECYCLE),
]


__all__ = [
    "OrderCancelledDetail",
    "OrderExpiredDetail",
    "OrderFilledDetail",
    "OrderModifiedDetail",
    "OrderPartiallyFilledDetail",
    "OrderRejectedDetail",
    "OrderSubmittedDetail",
]
