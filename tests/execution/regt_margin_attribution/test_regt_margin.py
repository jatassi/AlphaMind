"""Tests for the per-leg Reg T margin formula (ALP-424 / story 03a)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
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
    StrategyLeg,
    StrategyPositionDetails,
)


def _fill(at_price: float = 100.0, quantity: float = 10.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        fill_price=price(at_price),
        fill_quantity=quantity,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )


def _greeks() -> OptionGreeks:
    return OptionGreeks(delta=0.5, gamma=0.05, theta=-0.02, vega=0.10)


def _equity_position(
    *,
    position_id: str = "eq1",
    ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    share_count: float = 100.0,
    cost_basis: float = 500.0,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=cost_basis,
        borrow_rate_pct=0.05 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=1000.0 if is_short else None,
    )
    # PENDING positions must have empty execution_history (for non-strategy).
    history = () if status == PositionStatus.PENDING else (_fill(),)
    realized = 0.0 if status == PositionStatus.CLOSED else None
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=realized,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _option_position(
    *,
    position_id: str = "opt1",
    underlying_ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike_price: float = 500.0,
    contract_count: float = 1.0,
    premium: float = 10.0,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying_ticker),
        strike_price=strike_price,
        expiration_date=date(2026, 12, 19),
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=premium,
        greeks=_greeks(),
    )
    history = () if status == PositionStatus.PENDING else (_fill(),)
    realized = 0.0 if status == PositionStatus.CLOSED else None
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=realized,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_empty_positions_returns_zero() -> None:
    """compute_regt_margin((), {}) returns 0.0."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    result = compute_regt_margin((), {})

    assert result == 0.0


def test_long_equity_margin_is_50_percent_of_market_value() -> None:
    """Long 100 shares NVDA at $500 → 50% * 100 * 500 = 25_000.0."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _equity_position(ticker=Symbol("NVDA"), share_count=100.0, direction=Direction.LONG)
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(25_000.0)


def test_short_equity_margin_is_150_percent_of_market_value() -> None:
    """Short 100 shares NVDA at $500 → 150% * 100 * 500 = 75_000.0."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _equity_position(ticker=Symbol("NVDA"), share_count=100.0, direction=Direction.SHORT)
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(75_000.0)


def test_long_option_margin_is_100_percent_of_premium_times_multiplier() -> None:
    """Long 5 NVDA calls at $12 premium → 100% * 5 * 12 * 100 = 6_000.0."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.LONG,
        contract_type=OptionContractType.CALL,
        contract_count=5.0,
        premium=12.0,
    )
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(6_000.0)


def test_short_option_call_atm_uses_20_percent_branch() -> None:
    """Short 1 NVDA call, strike=500, underlying=500, premium=10.
    OTM=0; max(500*0.20-0, 500*0.10, 10) * 1 * 100 = max(100,50,10) * 100 = 10_000.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.CALL,
        strike_price=500.0,
        contract_count=1.0,
        premium=10.0,
    )
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(10_000.0)


def test_short_option_call_deep_otm_uses_10_percent_floor() -> None:
    """Short 1 NVDA call, strike=600, underlying=500, premium=1.
    OTM=100; max(500*0.20-100, 500*0.10, 1) * 1 * 100 = max(0,50,1) * 100 = 5_000.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.CALL,
        strike_price=600.0,
        contract_count=1.0,
        premium=1.0,
    )
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(5_000.0)


def test_short_option_call_extreme_otm_10pct_floor_wins() -> None:
    """Short 1 NVDA call, strike=1000, underlying=500, premium=0.50.
    OTM=500; max(500*0.20-500, 500*0.10, 0.5) = max(-400,50,0.5); 10%-floor wins → 5_000.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.CALL,
        strike_price=1000.0,
        contract_count=1.0,
        premium=0.50,
    )
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(5_000.0)


def test_short_option_call_premium_floor_wins() -> None:
    """Contrived: underlying=10, strike=100, premium=5.
    max(10*0.20-90, 10*0.10, 5) = max(-88, 1, 5) = 5; premium wins → 5 * 1 * 100 = 500.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("XYZ"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.CALL,
        strike_price=100.0,
        contract_count=1.0,
        premium=5.0,
    )
    result = compute_regt_margin((pos,), {"XYZ": 10.0})

    assert result == pytest.approx(500.0)


def test_short_option_put_uses_strike_minus_underlying_for_otm() -> None:
    """Short 1 NVDA put, strike=400, underlying=500, premium=1.
    Put OTM = max(underlying - strike, 0) = 100.
    max(500*0.20-100, 500*0.10, 1) * 1 * 100 = max(0,50,1) * 100 = 5_000.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _option_position(
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.PUT,
        strike_price=400.0,
        contract_count=1.0,
        premium=1.0,
    )
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(5_000.0)


def _bull_put_spread_legs() -> tuple[StrategyLeg, StrategyLeg]:
    """5-point-wide NVDA bull put credit spread legs at underlying 500.

    Short put strike=480 (premium 7); long put strike=475 (premium 4).
    """
    short_put = StrategyLeg(
        leg_id="l1",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=480.0,
            expiration_date=date(2026, 12, 19),
            contract_type=OptionContractType.PUT,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=7.0,
            greeks=_greeks(),
        ),
    )
    long_put = StrategyLeg(
        leg_id="l2",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=475.0,
            expiration_date=date(2026, 12, 19),
            contract_type=OptionContractType.PUT,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=4.0,
            greeks=_greeks(),
        ),
    )
    return short_put, long_put


def _strategy_position(
    *,
    legs: tuple[StrategyLeg, ...],
    max_loss_usd: float,
    position_id: str = "strat1",
    strategy_type_label: str = "bull_put_spread",
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    strategy_details = StrategyPositionDetails(
        strategy_type_label=strategy_type_label,
        legs=legs,
        net_premium_usd=300.0,
        max_profit_usd=300.0,
        max_loss_usd=max_loss_usd,
        breakeven_levels=(477.0,),
        strategy_greeks=_greeks(),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=Direction.SHORT,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=strategy_details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_defined_risk_strategy_margin_is_abs_max_loss() -> None:
    """A defined-risk strategy (finite max_loss_usd) margins at abs(max_loss_usd).

    5-wide bull put spread sold for a $3.00/contract credit:
    capped loss = (5 * 100 - 300) = -200; margin = abs(-200) = 200.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _strategy_position(legs=_bull_put_spread_legs(), max_loss_usd=-200.0)
    result = compute_regt_margin((pos,), {"NVDA": 500.0})

    assert result == pytest.approx(200.0)


def test_mixed_portfolio_sums_correctly() -> None:
    """Long AAPL stock + short NVDA call + long QQQ put.

    Sum = individual contributions.
    """
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    # Long AAPL: 200 shares, price=180 → 0.50 * 200 * 180 = 18_000
    aapl = _equity_position(
        position_id=PositionId("eq_aapl"),
        ticker=Symbol("AAPL"),
        direction=Direction.LONG,
        share_count=200.0,
    )

    # Short NVDA call: strike=500, underlying=500, premium=10, 1 contract
    # ATM: max(500*0.20-0, 500*0.10, 10) * 1 * 100 = max(100,50,10) * 100 = 10_000
    nvda_call = _option_position(
        position_id=PositionId("opt_nvda"),
        underlying_ticker=Symbol("NVDA"),
        direction=Direction.SHORT,
        contract_type=OptionContractType.CALL,
        strike_price=500.0,
        contract_count=1.0,
        premium=10.0,
    )

    # Long QQQ put: 3 contracts, premium=5 → 100% * 3 * 5 * 100 = 1_500
    qqq_put = _option_position(
        position_id=PositionId("opt_qqq"),
        underlying_ticker=Symbol("QQQ"),
        direction=Direction.LONG,
        contract_type=OptionContractType.PUT,
        strike_price=400.0,
        contract_count=3.0,
        premium=5.0,
    )

    prices = {"AAPL": 180.0, "NVDA": 500.0, "QQQ": 450.0}
    result = compute_regt_margin((aapl, nvda_call, qqq_put), prices)

    # Long equity 18_000 + short call 10_000 + long put 1_500 = 29_500.
    assert result == pytest.approx(29_500.0)


def test_missing_underlying_price_raises_key_error() -> None:
    """Position with symbol 'ZZZZ' not in underlying_prices raises KeyError."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pos = _equity_position(position_id=PositionId("eq1"), ticker=Symbol("ZZZZ"))
    with pytest.raises(KeyError):
        compute_regt_margin((pos,), {})


def test_pending_position_excluded_from_margin() -> None:
    """A PENDING equity position contributes zero to the margin sum."""
    from alphamind.execution.regt_margin_attribution.regt_margin import compute_regt_margin

    pending = _equity_position(
        position_id=PositionId("eq_pending"),
        ticker=Symbol("NVDA"),
        status=PositionStatus.PENDING,
    )
    result = compute_regt_margin((pending,), {"NVDA": 500.0})

    assert result == 0.0
