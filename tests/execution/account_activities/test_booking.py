"""Pure booking-math tests for the option-lifecycle handlers.

The booking core takes the open option ``PositionRecord`` and a typed
``LifecycleEvent`` and returns the realized PnL plus the resulting position
records — without touching the DB or the broker. The handlers (the shell) wrap
this with the load / persist / event-log-append plumbing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from alphamind._kernel.ids import PositionId, Symbol, ThesisId
from alphamind._kernel.money import price, signed_money
from alphamind.execution.account_activities.booking import (
    book_assignment_or_exercise,
    book_expiry,
)
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)


def _open_option(
    *,
    contract_count: float = 5.0,
    premium_paid_per_contract: float = 250.0,
    strike: float = 150.0,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=Symbol("AAPL"),
        strike_price=strike,
        expiration_date=dt.date(2026, 9, 18),
        contract_type=OptionContractType.CALL,
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=OptionGreeks(delta=0.45, gamma=0.02, theta=-0.05, vega=0.10),
    )
    fill = PositionFill(
        fill_timestamp=_TXN - dt.timedelta(days=2),
        fill_price=price(premium_paid_per_contract / 100.0),
        fill_quantity=contract_count,
        slippage=signed_money(0.0),
        fees=signed_money(0.0),
    )
    return PositionRecord(
        position_id=PositionId("pos-opt-1"),
        thesis_id=ThesisId("thesis-1"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_TXN - dt.timedelta(days=2),
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_otm_expiry_books_negative_premium_and_closes_option() -> None:
    """An OTM expiry realizes ``-premium`` and closes the option (no husk)."""
    option = _open_option(contract_count=5.0, premium_paid_per_contract=250.0)
    event = LifecycleEvent(
        activity_id="act-exp-1",
        activity_type=LifecycleActivityType.OPEXP,
        occ_symbol="AAPL250918C00150000",
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )

    result = book_expiry(option, event)

    # premium total = 5 contracts * $250/contract = $1250, lost in full.
    assert result.realized_pnl_usd == signed_money(Decimal("-1250"))
    assert result.closed_option.status is PositionStatus.CLOSED
    assert result.closed_option.realized_pnl_to_date_usd == pytest.approx(-1250.0)
    assert result.opened_equity is None


def _trade_leg(*, symbol: str = "AAPL", qty: float = 500.0, strike: float = 150.0) -> TradeLeg:
    return TradeLeg(
        activity_id="act-trd-1",
        equity_symbol=symbol,
        qty=qty,
        strike_price=price(strike),
        side="buy",
        net_amount=signed_money(-qty * strike),
    )


def test_assignment_opens_equity_at_strike_with_thesis_link() -> None:
    """An assignment opens the equity at the strike, basis = strike, thesis linked."""
    option = _open_option(contract_count=5.0, premium_paid_per_contract=250.0, strike=150.0)
    event = LifecycleEvent(
        activity_id="act-asn-1",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol="AAPL250918C00150000",
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=_trade_leg(symbol="AAPL", qty=500.0, strike=150.0),
    )

    result = book_assignment_or_exercise(option, event, equity_position_id=PositionId("pos-eq-1"))

    assert result.closed_option.status is PositionStatus.CLOSED
    equity = result.opened_equity
    assert equity is not None
    assert equity.status is PositionStatus.OPEN
    assert equity.thesis_id == ThesisId("thesis-1")
    assert equity.parent_position_id == option.position_id
    assert equity.details.ticker == Symbol("AAPL")
    assert equity.details.share_count == 500.0
    # Cost basis is the strike, not the option premium.
    assert equity.details.average_cost_basis_per_share == pytest.approx(150.0)


def test_assignment_without_paired_optrd_surfaces() -> None:
    """A missing paired OPTRD leaves the equity leg underspecified — surface, don't guess."""
    option = _open_option()
    event = LifecycleEvent(
        activity_id="act-asn-2",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol="AAPL250918C00150000",
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )

    with pytest.raises(ValueError, match="no paired OPTRD"):
        book_assignment_or_exercise(option, event, equity_position_id=PositionId("pos-eq-2"))
