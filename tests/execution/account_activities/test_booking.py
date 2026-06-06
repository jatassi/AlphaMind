"""Pure booking-math tests for the option-lifecycle handlers.

The booking core takes the open option ``PositionRecord`` and a typed
``LifecycleEvent`` and returns the realized PnL plus the resulting position
records — without touching the DB or the broker. The handlers (the shell) wrap
this with the load / persist / event-log-append plumbing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
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
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)


def _const_resolver(rate: float | None) -> Callable[[str], float | None]:
    """A pure ticker→rate resolver returning *rate* for every ticker.

    ``rate=None`` models an uncovered ticker (no ``borrow_cost_daily`` row) so the
    booking exercises its forced-assignment fallback rather than raising.
    """
    return lambda _ticker: rate


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

    result = book_expiry(option)

    # premium total = 5 contracts * $250/contract = $1250, lost in full.
    assert result.realized_pnl_usd == signed_money(Decimal(-1250))
    assert result.closed_option.status is PositionStatus.CLOSED
    assert result.closed_option.realized_pnl_to_date_usd == pytest.approx(-1250.0)
    assert result.opened_equity is None


def _trade_leg(
    *, symbol: str = "AAPL", qty: float = 500.0, strike: float = 150.0, side: str = "buy"
) -> TradeLeg:
    return TradeLeg(
        activity_id="act-trd-1",
        equity_symbol=symbol,
        qty=qty,
        strike_price=price(strike),
        side=side,
        net_amount=signed_money(-qty * strike),
    )


def _assignment_event(*, side: str = "buy", symbol: str = "AAPL") -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-asn-1",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol="AAPL250918C00150000",
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=_trade_leg(symbol=symbol, qty=500.0, strike=150.0, side=side),
    )


def test_assignment_opens_equity_at_strike_with_thesis_link() -> None:
    """A long-side assignment opens the equity at the strike, basis = strike, thesis linked.

    A ``buy`` delivery → LONG equity, so the four short-only fields stay ``None``
    (the record forbids short fields on a LONG position).
    """
    option = _open_option(contract_count=5.0, premium_paid_per_contract=250.0, strike=150.0)
    event = _assignment_event(side="buy")

    result = book_assignment_or_exercise(
        option,
        event,
        equity_position_id=PositionId("pos-eq-1"),
        borrow_cost_resolver=_const_resolver(12.0),
    )

    assert result.closed_option.status is PositionStatus.CLOSED
    equity = result.opened_equity
    assert equity is not None
    assert equity.status is PositionStatus.OPEN
    assert equity.direction is Direction.LONG
    assert equity.thesis_id == ThesisId("thesis-1")
    assert equity.parent_position_id == option.position_id
    details = equity.details
    assert isinstance(details, EquityPositionDetails)
    assert details.ticker == Symbol("AAPL")
    assert details.share_count == 500.0
    # Cost basis is the strike, not the option premium.
    assert details.average_cost_basis_per_share == pytest.approx(150.0)
    # A LONG leg keeps every short-only field None — the resolver is not consulted.
    assert details.borrow_rate_pct is None
    assert details.accrued_borrow_cost_usd is None
    assert details.locate_status is None
    assert details.margin_held_usd is None


def test_short_assignment_stamps_borrow_fields_from_resolver_rate() -> None:
    """A sell-side delivery → SHORT equity stamps the four short-only fields, record accepts it.

    The resolver returns a rate, so ``borrow_rate_pct`` carries it; the other
    three fields mirror ``_apply_entry_fill`` (accrued 0.0, LOCATED, Reg-T margin
    = qty * strike * 0.50). The record's ``_check_equity_direction_fields`` would
    reject a SHORT leg with any of these left ``None``.
    """
    option = _open_option(contract_count=5.0, premium_paid_per_contract=250.0, strike=150.0)
    event = _assignment_event(side="sell")

    result = book_assignment_or_exercise(
        option,
        event,
        equity_position_id=PositionId("pos-eq-1"),
        borrow_cost_resolver=_const_resolver(12.0),
    )

    equity = result.opened_equity
    assert equity is not None
    assert equity.direction is Direction.SHORT
    details = equity.details
    assert isinstance(details, EquityPositionDetails)
    assert details.borrow_rate_pct == pytest.approx(12.0)
    assert details.accrued_borrow_cost_usd == pytest.approx(0.0)
    assert details.locate_status is LocateStatus.LOCATED
    # Reg-T initial margin = qty * strike * 0.50 = 500 * 150 * 0.5.
    assert details.margin_held_usd == pytest.approx(500.0 * 150.0 * 0.50)


def test_short_assignment_falls_back_to_zero_rate_when_resolver_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An uncovered ticker (resolver → None) books with ``borrow_rate_pct == 0.0`` + a warning.

    A forced assignment can never be aborted — the shares are already delivered —
    so a missing ``borrow_cost_daily`` row falls back to a 0.0 audit snapshot
    (the borrow-accrual monitor recomputes against the live rate) rather than
    raising, and logs a warning naming the ticker.
    """
    option = _open_option(contract_count=5.0, premium_paid_per_contract=250.0, strike=150.0)
    event = _assignment_event(side="sell", symbol="GME")

    with caplog.at_level("WARNING"):
        result = book_assignment_or_exercise(
            option,
            event,
            equity_position_id=PositionId("pos-eq-1"),
            borrow_cost_resolver=_const_resolver(None),
        )

    equity = result.opened_equity
    assert equity is not None
    details = equity.details
    assert isinstance(details, EquityPositionDetails)
    assert details.borrow_rate_pct == pytest.approx(0.0)
    # The other three short-only fields are still stamped (record stays valid).
    assert details.accrued_borrow_cost_usd == pytest.approx(0.0)
    assert details.locate_status is LocateStatus.LOCATED
    assert details.margin_held_usd == pytest.approx(500.0 * 150.0 * 0.50)
    # A warning naming the ticker was logged (the booking did not raise).
    assert any("GME" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)


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
        book_assignment_or_exercise(
            option,
            event,
            equity_position_id=PositionId("pos-eq-2"),
            borrow_cost_resolver=_const_resolver(12.0),
        )
