"""Cash and margin event details — debits, credits, capital reservations, margin calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alphamind._kernel.money import Money, Price
from alphamind.portfolio_state.events.types import (
    CashCreditReason,
    CashDebitReason,
    EventGroup,
    EventType,
)


@dataclass(frozen=True, slots=True)
class CashDebitedDetail:
    """Detail payload for CASH_DEBITED events.

    ``amount_usd`` is the magnitude of the debit (non-negative ``Money``).
    ``new_balance_usd`` is the post-debit balance — may be negative for a
    fully-utilized margin account, so uses signed semantics.
    """

    amount_usd: Money
    reason: CashDebitReason
    new_balance_usd: Money


@dataclass(frozen=True, slots=True)
class CashCreditedDetail:
    """Detail payload for CASH_CREDITED events."""

    amount_usd: Money
    reason: CashCreditReason
    new_balance_usd: Money


@dataclass(frozen=True, slots=True)
class CapitalReservedDetail:
    """Detail payload for CAPITAL_RESERVED events."""

    order_id: str
    amount_usd: Money


@dataclass(frozen=True, slots=True)
class CapitalReleasedDetail:
    """Detail payload for CAPITAL_RELEASED events."""

    order_id: str
    amount_usd: Money


@dataclass(frozen=True, slots=True)
class MarginCallDetail:
    """Detail payload for MARGIN_CALL events."""

    position_id: str
    margin_required_usd: Money
    margin_available_usd: Money
    deficit_usd: Money


@dataclass(frozen=True, slots=True)
class MarginCallResolvedDetail:
    """Detail payload for MARGIN_CALL_RESOLVED events."""

    resolution_method: str


@dataclass(frozen=True, slots=True)
class MarginLiquidationDetail:
    """Detail payload for MARGIN_LIQUIDATION events.

    ``loss_usd`` may be negative (a partially-recovered liquidation) so uses
    signed ``Money`` semantics. ``liquidation_price`` is always positive
    (a ``Price``).
    """

    position_id: str
    liquidation_price: Price
    loss_usd: Money


@dataclass(frozen=True, slots=True)
class BorrowCostAccruedDetail:
    """Detail payload for BORROW_COST_ACCRUED events.

    Emitted once per OPEN SHORT-equity position per daily accrual tick by the
    continuous monitor's borrow-accrual task. ``accrued_amount_usd`` is the
    increment applied this tick; ``cumulative_accrued_usd`` is the running
    total after this tick (mirrors the position's post-tick
    ``accrued_borrow_cost_usd``). ``annual_fee_pct_used`` is the live resolver
    rate the tick consumed (e.g., ``15.0`` for 15%/yr).
    ``notional_usd_used`` is the live notional the accrual was computed
    against (``abs(share_count × close_price)``). ``accrual_date`` is the
    trading day the tick covers.
    """

    accrued_amount_usd: Money
    cumulative_accrued_usd: Money
    annual_fee_pct_used: float
    notional_usd_used: Money
    accrual_date: date


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.CASH_DEBITED, CashDebitedDetail, EventGroup.CASH_AND_MARGIN),
    (EventType.CASH_CREDITED, CashCreditedDetail, EventGroup.CASH_AND_MARGIN),
    (EventType.CAPITAL_RESERVED, CapitalReservedDetail, EventGroup.CASH_AND_MARGIN),
    (EventType.CAPITAL_RELEASED, CapitalReleasedDetail, EventGroup.CASH_AND_MARGIN),
    (EventType.MARGIN_CALL, MarginCallDetail, EventGroup.CASH_AND_MARGIN),
    (
        EventType.MARGIN_CALL_RESOLVED,
        MarginCallResolvedDetail,
        EventGroup.CASH_AND_MARGIN,
    ),
    (
        EventType.MARGIN_LIQUIDATION,
        MarginLiquidationDetail,
        EventGroup.CASH_AND_MARGIN,
    ),
    (
        EventType.BORROW_COST_ACCRUED,
        BorrowCostAccruedDetail,
        EventGroup.CASH_AND_MARGIN,
    ),
]


__all__ = [
    "BorrowCostAccruedDetail",
    "CapitalReleasedDetail",
    "CapitalReservedDetail",
    "CashCreditedDetail",
    "CashDebitedDetail",
    "MarginCallDetail",
    "MarginCallResolvedDetail",
    "MarginLiquidationDetail",
]
