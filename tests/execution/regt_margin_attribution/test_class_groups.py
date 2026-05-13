"""Tests for class-group composition (ALP-423)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
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
    StrategyLeg,
    StrategyPositionDetails,
)


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        fill_price=100.0,
        fill_quantity=10.0,
        slippage=0.0,
        fees=1.0,
    )


def _greeks() -> OptionGreeks:
    return OptionGreeks(delta=0.5, gamma=0.05, theta=-0.02, vega=0.10)


def _equity_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=0.05 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=500.0 if is_short else None,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _option_position(
    *,
    position_id: str,
    underlying_ticker: str,
    contract_type: OptionContractType = OptionContractType.CALL,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying_ticker),
        strike_price=100.0,
        expiration_date=date(2026, 6, 19),
        contract_type=contract_type,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.5,
        greeks=_greeks(),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(
    *,
    position_id: str,
    underlying_ticker: str,
) -> PositionRecord:
    leg_details = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying_ticker),
        strike_price=100.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.5,
        greeks=_greeks(),
    )
    leg = StrategyLeg(leg_id="leg1", direction=Direction.LONG, options=leg_details)
    details = StrategyPositionDetails(
        strategy_type_label="iron_condor",
        legs=(leg,),
        net_premium_usd=100.0,
        max_profit_usd=200.0,
        max_loss_usd=-300.0,
        breakeven_levels=(98.0, 102.0),
        strategy_greeks=_greeks(),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_empty_positions_returns_empty_class_groups() -> None:
    """compose_class_groups(()) returns ()."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    assert compose_class_groups(()) == ()


def test_single_equity_position_forms_own_class_group() -> None:
    """One long AAPL equity → one ClassGroup with that position."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    aapl = _equity_position(position_id="p1", ticker="AAPL")

    groups = compose_class_groups((aapl,))

    assert len(groups) == 1
    assert groups[0].underlying_symbol == "AAPL"
    assert groups[0].positions == (aapl,)


def test_two_equity_positions_same_underlying_form_one_class_group() -> None:
    """Long AAPL + short AAPL → one ClassGroup with both positions."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    long_aapl = _equity_position(position_id="p1", ticker="AAPL")
    short_aapl = _equity_position(position_id="p2", ticker="AAPL", direction=Direction.SHORT)

    groups = compose_class_groups((long_aapl, short_aapl))

    assert len(groups) == 1
    assert groups[0].underlying_symbol == "AAPL"
    assert set(groups[0].positions) == {long_aapl, short_aapl}


def test_two_equity_positions_different_underlyings_form_two_class_groups() -> None:
    """AAPL + NVDA → two ClassGroups sorted alphabetically."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    nvda = _equity_position(position_id="p1", ticker="NVDA")
    aapl = _equity_position(position_id="p2", ticker="AAPL")

    groups = compose_class_groups((nvda, aapl))

    assert [g.underlying_symbol for g in groups] == ["AAPL", "NVDA"]
    assert groups[0].positions == (aapl,)
    assert groups[1].positions == (nvda,)


def test_equity_plus_option_on_same_underlying_form_one_class_group() -> None:
    """Long AAPL stock + AAPL call → one ClassGroup with both positions."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    stock = _equity_position(position_id="p1", ticker="AAPL")
    call = _option_position(position_id="p2", underlying_ticker="AAPL")

    groups = compose_class_groups((stock, call))

    assert len(groups) == 1
    assert groups[0].underlying_symbol == "AAPL"
    assert set(groups[0].positions) == {stock, call}


def test_strategy_position_groups_with_its_underlying() -> None:
    """NVDA iron condor + long NVDA stock → one ClassGroup containing both."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    stock = _equity_position(position_id="p1", ticker="NVDA")
    strategy = _strategy_position(position_id="p2", underlying_ticker="NVDA")

    groups = compose_class_groups((stock, strategy))

    assert len(groups) == 1
    assert groups[0].underlying_symbol == "NVDA"
    assert set(groups[0].positions) == {stock, strategy}


def test_class_groups_sorted_by_underlying_symbol_ascending() -> None:
    """Input order (NVDA, AAPL, MSFT) → output order (AAPL, MSFT, NVDA)."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    nvda = _equity_position(position_id="p1", ticker="NVDA")
    aapl = _equity_position(position_id="p2", ticker="AAPL")
    msft = _equity_position(position_id="p3", ticker="MSFT")

    groups = compose_class_groups((nvda, aapl, msft))

    assert [g.underlying_symbol for g in groups] == ["AAPL", "MSFT", "NVDA"]


def test_case_inconsistent_underlying_symbols_converge() -> None:
    """Equity with ticker 'aapl' and option with underlying 'AAPL' → one group keyed 'AAPL'."""
    from alphamind.execution.regt_margin_attribution import compose_class_groups

    stock = _equity_position(position_id="p1", ticker="aapl")
    call = _option_position(position_id="p2", underlying_ticker="AAPL")

    groups = compose_class_groups((stock, call))

    assert len(groups) == 1
    assert groups[0].underlying_symbol == "AAPL"
    assert set(groups[0].positions) == {stock, call}


def test_class_group_rejects_empty_positions() -> None:
    """ClassGroup(underlying_symbol='AAPL', positions=()) raises ValueError."""
    from alphamind.execution.regt_margin_attribution import ClassGroup

    with pytest.raises(ValueError, match="positions"):
        ClassGroup(underlying_symbol="AAPL", positions=())


def test_class_group_normalises_underlying_to_uppercase() -> None:
    """ClassGroup(underlying_symbol='aapl', ...).underlying_symbol == 'AAPL'."""
    from alphamind.execution.regt_margin_attribution import ClassGroup

    aapl = _equity_position(position_id="p1", ticker="AAPL")

    group = ClassGroup(underlying_symbol="aapl", positions=(aapl,))

    assert group.underlying_symbol == "AAPL"
