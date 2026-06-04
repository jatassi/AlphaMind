"""Pure booking math for the option-lifecycle events (ALP-846 / W1b).

The functional core of the handlers. Each function takes the open option
``PositionRecord`` and a typed
:class:`~alphamind.execution.account_activities.records.LifecycleEvent` and
returns a :class:`~alphamind.execution.account_activities.records.BookingResult`
— the realized PnL plus the resulting position records — with no I/O. The shell
(:mod:`alphamind.execution.account_activities.handlers`) persists the result and
appends the event-log rows.

Realized-PnL conventions (ADR-0002, invariant 3):

* **OTM expiry** — the option expires worthless, so the realized PnL is the
  full premium paid, lost: ``-(contract_count * premium_paid_per_contract)``.
* **Assignment / exercise** — the option converts to an equity leg at the
  strike. The realized PnL on the *option* is ``-premium`` (its extrinsic value
  is gone); the strike economics live on the opened equity position's cost
  basis (priced by the paired ``OPTRD``), so per-thesis realized PnL stays
  derivable from the log without double-counting.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import datetime as dt

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import Money, price, signed_money
from alphamind.execution.account_activities.records import (
    BookingResult,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)


def _premium_total(details: OptionsPositionDetails) -> Decimal:
    """Total premium paid for the option = contracts × per-contract premium."""
    return Decimal(str(details.contract_count)) * Decimal(str(details.premium_paid_per_contract))


def _options_details(option: PositionRecord) -> OptionsPositionDetails:
    """Narrow an option position's details, raising if it is not a single-leg option.

    The lifecycle handlers only run for single-leg option positions matched by
    OCC symbol; a strategy / equity payload here is a state inconsistency the
    caller surfaces rather than guesses past.
    """
    details = option.details
    if not isinstance(details, OptionsPositionDetails):
        msg = (
            f"lifecycle booking expects an OptionsPositionDetails position; "
            f"got {type(details).__name__} for {option.position_id!r}"
        )
        raise ValueError(msg)
    return details


def _closed_option(option: PositionRecord, realized_pnl: Money) -> PositionRecord:
    """Transition the option to CLOSED carrying the realized PnL (no husk)."""
    return dataclasses.replace(
        option,
        status=PositionStatus.CLOSED,
        realized_pnl_to_date_usd=float(realized_pnl),
    )


def book_expiry(option: PositionRecord, event: LifecycleEvent) -> BookingResult:
    """Book an OTM expiry: realized PnL = ``-premium``; close the option.

    The option expires worthless, so the full premium paid is realized as a
    loss and the position closes with no resulting equity leg.
    """
    details = _options_details(option)
    realized = signed_money(-_premium_total(details))
    return BookingResult(
        realized_pnl_usd=realized,
        closed_option=_closed_option(option, realized),
        opened_equity=None,
    )


def book_assignment_or_exercise(
    option: PositionRecord,
    event: LifecycleEvent,
    *,
    equity_position_id: PositionId,
) -> BookingResult:
    """Book an assignment / exercise via the paired ``OPTRD``.

    The option's extrinsic value is realized as ``-premium`` (the option
    closes); the resulting equity position opens at the strike with cost basis
    ``qty × strike`` from the paired ``OPTRD`` and the option's originating
    thesis link (attribution rides the position→thesis edge, ADR-0002).

    Raises ``ValueError`` when the event carries no paired ``OPTRD`` — the
    equity leg would be underspecified, so the handler surfaces rather than
    guessing the booking (parent ALP-842 surfacing condition).
    """
    details = _options_details(option)
    trade = event.paired_trade
    if trade is None:
        msg = (
            f"{event.activity_type.value} {event.activity_id!r} on "
            f"{event.occ_symbol!r} has no paired OPTRD activity; the equity leg "
            f"is underspecified — cannot book the assignment/exercise"
        )
        raise ValueError(msg)

    realized = signed_money(-_premium_total(details))
    opened_equity = _opened_equity_from_trade(
        option=option,
        trade=trade,
        equity_position_id=equity_position_id,
        fill_timestamp=event.transaction_time,
    )
    return BookingResult(
        realized_pnl_usd=realized,
        closed_option=_closed_option(option, realized),
        opened_equity=opened_equity,
    )


def _opened_equity_from_trade(
    *,
    option: PositionRecord,
    trade: TradeLeg,
    equity_position_id: PositionId,
    fill_timestamp: dt.datetime,
) -> PositionRecord:
    """Build the equity ``PositionRecord`` the paired ``OPTRD`` fully describes.

    Cost basis per share is the strike; share count is the ``OPTRD`` qty;
    direction follows the broker's buy/sell side. The new position inherits the
    option's thesis so per-thesis PnL stays attributable via the
    position→thesis Intent edge — an Intent stub, not a parsed ``client_order_id``.
    """
    direction = Direction.LONG if trade.side.lower() == "buy" else Direction.SHORT
    equity_details = EquityPositionDetails(
        ticker=Symbol(trade.equity_symbol),
        share_count=trade.qty,
        average_cost_basis_per_share=float(trade.strike_price),
    )
    fill = PositionFill(
        fill_timestamp=fill_timestamp,
        fill_price=price(Decimal(str(trade.strike_price))),
        fill_quantity=trade.qty,
        slippage=signed_money(0.0),
        fees=signed_money(0.0),
    )
    return PositionRecord(
        position_id=equity_position_id,
        thesis_id=option.thesis_id,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=fill_timestamp,
        details=equity_details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=option.position_id,
        origin=None,
    )


__all__ = ["book_assignment_or_exercise", "book_expiry"]
