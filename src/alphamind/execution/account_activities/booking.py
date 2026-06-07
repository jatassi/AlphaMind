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
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal

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
    LocateStatus,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

logger = logging.getLogger(__name__)

# Reg T initial margin fraction — the same 0.50 basis ``_apply_entry_fill`` stamps
# on a proactive SHORT-equity entry (``write_paths/fill_collection.py``).
_REG_T_INITIAL_MARGIN_FRACTION = 0.50


def _premium_total(details: OptionsPositionDetails) -> Decimal:
    """Total premium paid for the option = contracts x per-contract premium."""
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
        raise TypeError(msg)
    return details


def _closed_option(option: PositionRecord, realized_pnl: Money) -> PositionRecord:
    """Transition the option to CLOSED carrying the realized PnL (no husk)."""
    return dataclasses.replace(
        option,
        status=PositionStatus.CLOSED,
        realized_pnl_to_date_usd=float(realized_pnl),
    )


def book_expiry(option: PositionRecord) -> BookingResult:
    """Book an OTM expiry: realized PnL = ``-premium``; close the option.

    The option expires worthless, so the full premium paid is realized as a
    loss and the position closes with no resulting equity leg. The activity
    itself carries no booking input beyond identifying the option — the loss is
    fully determined by the option's own premium.
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
    borrow_cost_resolver: Callable[[str], float | None],
) -> BookingResult:
    """Book an assignment / exercise via the paired ``OPTRD``.

    The option's extrinsic value is realized as ``-premium`` (the option
    closes); the resulting equity position opens at the strike with cost basis
    ``qty x strike`` from the paired ``OPTRD`` and the option's originating
    thesis link (attribution rides the position->thesis edge, ADR-0002).

    ``borrow_cost_resolver`` (ticker → annualized borrow fee %, ``None`` for an
    uncovered ticker) stamps the four short-only fields when the delivery opens a
    SHORT equity leg — see :func:`_opened_equity_from_trade`. It is consulted
    only for the SHORT case; a LONG delivery never touches it.

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
        borrow_cost_resolver=borrow_cost_resolver,
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
    borrow_cost_resolver: Callable[[str], float | None],
) -> PositionRecord:
    """Build the equity ``PositionRecord`` the paired ``OPTRD`` fully describes.

    Cost basis per share is the strike; share count is the ``OPTRD`` qty;
    direction follows the broker's buy/sell side. The new position inherits the
    option's thesis so per-thesis PnL stays attributable via the
    position→thesis Intent edge — an Intent stub, not a parsed ``client_order_id``.

    A sell-side delivery opens a SHORT equity leg (short-call assignment /
    long-put exercise), which the record requires the four short-only fields for
    (``_check_equity_direction_fields``). They are stamped here mirroring
    ``_apply_entry_fill``: ``borrow_rate_pct`` from the resolver, ``accrued`` 0.0,
    ``locate_status`` LOCATED (the shares are delivered, so the borrow is real),
    and ``margin_held_usd`` the Reg-T initial margin. Unlike ``_apply_entry_fill``
    — which protects a *proactive* SHORT-entry OpenCommand and raises on a missing
    rate — a forced assignment can never be aborted (the shares are already
    delivered), so a resolver miss falls back to a ``0.0`` audit snapshot with a
    warning rather than raising (ALP-862). A LONG delivery keeps all four
    ``None``.
    """
    direction = Direction.LONG if trade.side.lower() == "buy" else Direction.SHORT
    equity_details = EquityPositionDetails(
        ticker=Symbol(trade.equity_symbol),
        share_count=trade.qty,
        average_cost_basis_per_share=float(trade.strike_price),
    )
    if direction is Direction.SHORT:
        equity_details = _stamp_short_fields(equity_details, trade, borrow_cost_resolver)
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


def _stamp_short_fields(
    details: EquityPositionDetails,
    trade: TradeLeg,
    borrow_cost_resolver: Callable[[str], float | None],
) -> EquityPositionDetails:
    """Stamp the four short-only fields on a SHORT equity leg (mirrors ``_apply_entry_fill``).

    ``borrow_rate_pct`` is the resolver's rate for the ticker, falling back to
    ``0.0`` with a warning when the resolver returns ``None`` (an uncovered
    ticker) — a forced assignment is never aborted (ALP-862). The remaining three
    fields are construction-derived: ``accrued_borrow_cost_usd`` starts at 0.0
    (the borrow-accrual monitor increments it), ``locate_status`` is LOCATED (the
    shares were delivered), and ``margin_held_usd`` is the Reg-T initial margin
    ``qty * strike * 0.50``.
    """
    annual_fee_pct = borrow_cost_resolver(trade.equity_symbol)
    if annual_fee_pct is None:
        logger.warning(
            "Assignment booking on %r: borrow_cost_resolver returned None "
            "(no borrow_cost_daily row); stamping borrow_rate_pct=0.0. The "
            "borrow-accrual monitor recomputes accrual against the live broker "
            "rate each tick, so the 0.0 audit snapshot does not corrupt accrual.",
            trade.equity_symbol,
        )
        annual_fee_pct = 0.0
    margin_held_usd = trade.qty * float(trade.strike_price) * _REG_T_INITIAL_MARGIN_FRACTION
    return dataclasses.replace(
        details,
        borrow_rate_pct=annual_fee_pct,
        accrued_borrow_cost_usd=0.0,
        locate_status=LocateStatus.LOCATED,
        margin_held_usd=margin_held_usd,
    )


__all__ = ["book_assignment_or_exercise", "book_expiry"]
