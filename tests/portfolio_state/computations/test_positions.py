"""Tests for position-level computation pure functions (story 05a)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind.portfolio_state.computations.positions import (
    MissingLegPriceError,
    compute_delta_adjusted_exposure_usd,
    compute_distance_to_stop_usd,
    compute_distance_to_target_usd,
    compute_market_value_usd,
    compute_notional_exposure_usd,
    compute_position_age_hours,
    compute_position_weight_pct,
    compute_risk_reward_at_current,
    compute_strategy_delta_adjusted_exposure_usd,
    compute_strategy_market_value_usd,
    compute_strategy_notional_exposure_usd,
    compute_unrealized_pnl_pct,
    compute_unrealized_pnl_usd,
)
from alphamind.portfolio_state.pricing import PriceQuote, PriceSource
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
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

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def _price(price_usd: float, ticker: str = "AAPL") -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price_usd=price_usd,
        as_of_timestamp=_NOW,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=_NOW,
        fill_price=100.0,
        fill_quantity=10.0,
        slippage=0.01,
        fees=0.5,
    )


def _equity_position(
    share_count: float = 10.0,
    direction: Direction = Direction.LONG,
    avg_cost: float = 100.0,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    equity_details = EquityPositionDetails(
        ticker="AAPL",
        share_count=share_count,
        average_cost_basis_per_share=avg_cost,
        borrow_rate_pct=0.5 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=500.0 if is_short else None,
    )
    return PositionRecord(
        position_id="pos-001",
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        instrument_type=InstrumentType.EQUITY,
        equity_details=equity_details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0 if direction == Direction.LONG else -1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _greeks(delta: float = 0.5) -> OptionGreeks:
    return OptionGreeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.2)


def _options_details(
    contract_count: float = 2.0,
    contract_multiplier: float = 100.0,
    delta: float = 0.5,
    contract_type: OptionContractType = OptionContractType.CALL,
) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker="AAPL",
        strike_price=150.0,
        expiration_date=date(2025, 3, 21),
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=5.0,
        greeks=_greeks(delta=delta),
    )


def _options_position(
    contract_count: float = 2.0,
    contract_multiplier: float = 100.0,
    delta: float = 0.5,
    contract_type: OptionContractType = OptionContractType.CALL,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    return PositionRecord(
        position_id="pos-002",
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        instrument_type=InstrumentType.OPTIONS,
        options_details=_options_details(
            contract_count=contract_count,
            contract_multiplier=contract_multiplier,
            delta=delta,
            contract_type=contract_type,
        ),
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(
    legs: list[tuple[str, float, float, float]],  # (leg_id, contract_count, multiplier, delta)
    strategy_delta: float = 0.3,
) -> PositionRecord:
    """Build a strategy position from a list of (leg_id, contract_count, multiplier, delta)."""
    strategy_legs = tuple(
        StrategyLeg(
            leg_id=leg_id,
            options=_options_details(
                contract_count=cc,
                contract_multiplier=mult,
                delta=delta,
            ),
        )
        for leg_id, cc, mult, delta in legs
    )
    return PositionRecord(
        position_id="pos-003",
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        instrument_type=InstrumentType.STRATEGY,
        strategy_details=StrategyPositionDetails(
            strategy_type_label="iron_condor",
            legs=strategy_legs,
            net_premium_usd=200.0,
            max_profit_usd=500.0,
            max_loss_usd=-300.0,
            breakeven_levels=(140.0, 160.0),
            strategy_greeks=_greeks(delta=strategy_delta),
        ),
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=500.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _bracket(
    legs: list[tuple[BracketLegType, str, BracketLegEnforcement | None]],
) -> BracketRecord:
    """Build a BracketRecord. Each leg entry: (leg_type, trigger_condition, enforcement).

    enforcement=None → MECHANICAL for TAKE_PROFIT/PRICE_STOP/TIME_EXPIRATION, else ADVISORY.
    """
    bracket_legs = []
    for i, (leg_type, trigger, enforcement) in enumerate(legs):
        if enforcement is None:
            if leg_type in (
                BracketLegType.TAKE_PROFIT,
                BracketLegType.PRICE_STOP,
                BracketLegType.TIME_EXPIRATION,
            ):
                enforcement = BracketLegEnforcement.MECHANICAL
            else:
                enforcement = BracketLegEnforcement.ADVISORY
        bracket_legs.append(
            BracketLeg(
                leg_id=f"leg-{i}",
                leg_type=leg_type,
                order_id=None if leg_type == BracketLegType.EVENT_INVALIDATION else "ord-001",
                trigger_condition=trigger,
                enforcement=enforcement,
                status=BracketLegStatus.ACTIVE,
                pl_based=False,
            )
        )
    return BracketRecord(
        bracket_id="brk-001",
        position_id="pos-001",
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-000",
        protective_legs=tuple(bracket_legs),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


# ---------------------------------------------------------------------------
# Section 1: compute_market_value_usd
# ---------------------------------------------------------------------------


class TestComputeMarketValueUsd:
    def test_equity_long_positive_market_value(self) -> None:
        pos = _equity_position(share_count=10.0, direction=Direction.LONG)
        price = _price(150.0)
        result = compute_market_value_usd(pos, price)
        assert result == pytest.approx(1500.0)

    def test_equity_short_negative_market_value(self) -> None:
        pos = _equity_position(share_count=10.0, direction=Direction.SHORT)
        price = _price(150.0)
        result = compute_market_value_usd(pos, price)
        assert result == pytest.approx(-1500.0)

    def test_options_market_value_uses_option_price(self) -> None:
        pos = _options_position(contract_count=2.0, contract_multiplier=100.0)
        option_price = _price(8.0, ticker="AAPL")
        result = compute_market_value_usd(pos, option_price)
        # 2 contracts * 100 multiplier * $8 = $1600
        assert result == pytest.approx(1600.0)

    def test_strategy_raises_value_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5)])
        price = _price(150.0)
        with pytest.raises(ValueError, match="compute_strategy_market_value_usd"):
            compute_market_value_usd(pos, price)


# ---------------------------------------------------------------------------
# Section 1b: compute_strategy_market_value_usd
# ---------------------------------------------------------------------------


class TestComputeStrategyMarketValueUsd:
    def test_sums_across_legs(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)])
        leg_prices = {
            "leg-A": _price(10.0, ticker="leg-A"),
            "leg-B": _price(5.0, ticker="leg-B"),
        }
        result = compute_strategy_market_value_usd(pos, leg_prices)
        # leg-A: 1 * 100 * 10 = 1000; leg-B: 2 * 50 * 5 = 500; total = 1500
        assert result == pytest.approx(1500.0)

    def test_missing_leg_id_raises_missing_leg_price_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)])
        leg_prices = {"leg-A": _price(10.0, ticker="leg-A")}  # leg-B missing
        with pytest.raises(MissingLegPriceError):
            compute_strategy_market_value_usd(pos, leg_prices)

    def test_missing_leg_price_error_is_key_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5)])
        with pytest.raises(KeyError):
            compute_strategy_market_value_usd(pos, {})


# ---------------------------------------------------------------------------
# Section 2: compute_position_weight_pct
# ---------------------------------------------------------------------------


class TestComputePositionWeightPct:
    def test_basic_weight_calculation(self) -> None:
        result = compute_position_weight_pct(1000.0, 10000.0)
        assert result == pytest.approx(10.0)

    def test_absolute_value_for_short(self) -> None:
        # Short positions have negative market value; weight should be positive
        result = compute_position_weight_pct(-500.0, 10000.0)
        assert result == pytest.approx(5.0)

    def test_zero_portfolio_value_returns_zero(self) -> None:
        result = compute_position_weight_pct(0.0, 0.0)
        assert result == 0.0

    def test_negative_portfolio_value_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_position_weight_pct(100.0, -50.0)


# ---------------------------------------------------------------------------
# Section 3: compute_position_age_hours
# ---------------------------------------------------------------------------


class TestComputePositionAgeHours:
    def test_same_time_returns_zero(self) -> None:
        result = compute_position_age_hours(_NOW, _NOW)
        assert result == 0.0

    def test_two_hours_difference(self) -> None:
        entry = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = compute_position_age_hours(entry, now)
        assert result == pytest.approx(2.0)

    def test_returns_negative_for_future_entry(self) -> None:
        future = datetime(2025, 1, 15, 14, 0, 0, tzinfo=UTC)
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = compute_position_age_hours(future, now)
        assert result < 0.0

    def test_mixed_tz_aware_naive_raises(self) -> None:
        aware = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        naive = aware.replace(tzinfo=None)
        with pytest.raises((TypeError, ValueError)):
            compute_position_age_hours(naive, aware)

    def test_naive_entry_aware_now_raises(self) -> None:
        aware = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        naive = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC).replace(tzinfo=None)
        with pytest.raises((TypeError, ValueError)):
            compute_position_age_hours(naive, aware)


# ---------------------------------------------------------------------------
# Section 4: compute_unrealized_pnl_usd and compute_unrealized_pnl_pct
# ---------------------------------------------------------------------------


class TestComputeUnrealizedPnlUsd:
    def test_long_profit(self) -> None:
        # market_value > cost_basis => positive P/L
        result = compute_unrealized_pnl_usd(
            market_value_usd=1200.0,
            cost_basis_usd=1000.0,
            direction=Direction.LONG,
        )
        assert result == pytest.approx(200.0)

    def test_long_loss(self) -> None:
        result = compute_unrealized_pnl_usd(
            market_value_usd=800.0,
            cost_basis_usd=1000.0,
            direction=Direction.LONG,
        )
        assert result == pytest.approx(-200.0)

    def test_short_profit_when_price_falls(self) -> None:
        # Short: cost_basis was 1000, now market_value (price * qty) is -800
        result = compute_unrealized_pnl_usd(
            market_value_usd=-800.0,
            cost_basis_usd=1000.0,
            direction=Direction.SHORT,
        )
        assert result == pytest.approx(200.0)

    def test_short_loss_when_price_rises(self) -> None:
        # Short: cost_basis was 1000, now market_value is -1200
        result = compute_unrealized_pnl_usd(
            market_value_usd=-1200.0,
            cost_basis_usd=1000.0,
            direction=Direction.SHORT,
        )
        assert result == pytest.approx(-200.0)


class TestComputeUnrealizedPnlPct:
    def test_normal_case(self) -> None:
        result = compute_unrealized_pnl_pct(unrealized_pnl_usd=200.0, cost_basis_usd=1000.0)
        assert result == pytest.approx(20.0)

    def test_zero_cost_basis_returns_zero(self) -> None:
        result = compute_unrealized_pnl_pct(unrealized_pnl_usd=100.0, cost_basis_usd=0.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Section 5: compute_distance_to_target_usd and compute_distance_to_stop_usd
# ---------------------------------------------------------------------------


class TestComputeDistanceToTargetUsd:
    def test_long_numeric_trigger_returns_positive_when_above(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "200.00", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.LONG)
        assert result == pytest.approx(50.0)

    def test_long_numeric_trigger_negative_when_past_target(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "100.00", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.LONG)
        assert result == pytest.approx(-50.0)

    def test_short_numeric_trigger_positive_when_below(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "100.00", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.SHORT)
        assert result == pytest.approx(50.0)

    def test_short_numeric_trigger_negative_when_past_target(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "200.00", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.SHORT)
        assert result == pytest.approx(-50.0)

    def test_non_numeric_trigger_returns_none(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "earnings_announcement", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.LONG)
        assert result is None

    def test_no_take_profit_leg_returns_none(self) -> None:
        bracket = _bracket([(BracketLegType.PRICE_STOP, "100.00", None)])
        result = compute_distance_to_target_usd(150.0, bracket, Direction.LONG)
        assert result is None


class TestComputeDistanceToStopUsd:
    def test_long_numeric_stop_positive_when_above(self) -> None:
        bracket = _bracket(
            [
                (BracketLegType.TAKE_PROFIT, "200.00", None),
                (BracketLegType.PRICE_STOP, "120.00", None),
            ]
        )
        result = compute_distance_to_stop_usd(150.0, bracket, Direction.LONG)
        # LONG: positive when stop is below current price (cushion)
        assert result == pytest.approx(30.0)

    def test_short_numeric_stop_positive_when_above(self) -> None:
        # SHORT: positive when stop is above current price (cushion for short)
        bracket = _bracket(
            [
                (BracketLegType.TAKE_PROFIT, "100.00", None),
                (BracketLegType.PRICE_STOP, "200.00", None),
            ]
        )
        result = compute_distance_to_stop_usd(150.0, bracket, Direction.SHORT)
        assert result == pytest.approx(50.0)

    def test_no_price_stop_leg_returns_none(self) -> None:
        bracket = _bracket([(BracketLegType.TAKE_PROFIT, "200.00", None)])
        result = compute_distance_to_stop_usd(150.0, bracket, Direction.LONG)
        assert result is None

    def test_non_numeric_trigger_returns_none(self) -> None:
        bracket = _bracket(
            [
                (BracketLegType.TAKE_PROFIT, "200.00", None),
                (BracketLegType.PRICE_STOP, "2025-01-15T16:00:00", None),
            ]
        )
        result = compute_distance_to_stop_usd(150.0, bracket, Direction.LONG)
        assert result is None


# ---------------------------------------------------------------------------
# Section 6: compute_risk_reward_at_current
# ---------------------------------------------------------------------------


class TestComputeRiskRewardAtCurrent:
    def test_valid_ratio(self) -> None:
        result = compute_risk_reward_at_current(
            distance_to_target_usd=30.0,
            distance_to_stop_usd=10.0,
        )
        assert result == pytest.approx(3.0)

    def test_target_none_returns_none(self) -> None:
        result = compute_risk_reward_at_current(
            distance_to_target_usd=None,
            distance_to_stop_usd=10.0,
        )
        assert result is None

    def test_stop_none_returns_none(self) -> None:
        result = compute_risk_reward_at_current(
            distance_to_target_usd=30.0,
            distance_to_stop_usd=None,
        )
        assert result is None

    def test_zero_stop_returns_none(self) -> None:
        result = compute_risk_reward_at_current(
            distance_to_target_usd=30.0,
            distance_to_stop_usd=0.0,
        )
        assert result is None

    def test_signed_ratio_when_negative_stop(self) -> None:
        # Negative stop distance means price is past the stop
        result = compute_risk_reward_at_current(
            distance_to_target_usd=30.0,
            distance_to_stop_usd=-10.0,
        )
        # 30 / abs(-10) = 3.0
        assert result == pytest.approx(3.0)

    def test_both_none_returns_none(self) -> None:
        result = compute_risk_reward_at_current(
            distance_to_target_usd=None,
            distance_to_stop_usd=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Section 7: compute_notional_exposure_usd
# ---------------------------------------------------------------------------


class TestComputeNotionalExposureUsd:
    def test_equity_long_is_positive(self) -> None:
        pos = _equity_position(share_count=10.0, direction=Direction.LONG)
        price = _price(150.0)
        result = compute_notional_exposure_usd(pos, price)
        assert result == pytest.approx(1500.0)

    def test_equity_short_is_positive_magnitude(self) -> None:
        # Notional is always non-negative
        pos = _equity_position(share_count=10.0, direction=Direction.SHORT)
        price = _price(150.0)
        result = compute_notional_exposure_usd(pos, price)
        assert result == pytest.approx(1500.0)

    def test_options_uses_underlying_price(self) -> None:
        pos = _options_position(contract_count=2.0, contract_multiplier=100.0)
        underlying_price = _price(200.0, ticker="AAPL")  # underlying price, not option premium
        result = compute_notional_exposure_usd(pos, underlying_price)
        # 2 * 100 * 200 = 40000
        assert result == pytest.approx(40000.0)

    def test_strategy_raises_value_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5)])
        price = _price(150.0)
        with pytest.raises(ValueError):
            compute_notional_exposure_usd(pos, price)


# ---------------------------------------------------------------------------
# Section 7b: compute_strategy_notional_exposure_usd
# ---------------------------------------------------------------------------


class TestComputeStrategyNotionalExposureUsd:
    def test_sums_leg_notionals(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)])
        leg_underlying_prices = {
            "leg-A": _price(200.0, ticker="leg-A"),
            "leg-B": _price(100.0, ticker="leg-B"),
        }
        result = compute_strategy_notional_exposure_usd(pos, leg_underlying_prices)
        # leg-A: 1 * 100 * 200 = 20000; leg-B: 2 * 50 * 100 = 10000; total = 30000
        assert result == pytest.approx(30000.0)

    def test_missing_leg_raises_missing_leg_price_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)])
        with pytest.raises(MissingLegPriceError):
            compute_strategy_notional_exposure_usd(pos, {"leg-A": _price(200.0, ticker="leg-A")})


# ---------------------------------------------------------------------------
# Section 8: compute_delta_adjusted_exposure_usd
# ---------------------------------------------------------------------------


class TestComputeDeltaAdjustedExposureUsd:
    def test_equity_long_positive(self) -> None:
        pos = _equity_position(share_count=10.0, direction=Direction.LONG)
        price = _price(150.0)
        result = compute_delta_adjusted_exposure_usd(pos, price)
        # delta = 1 for long equity
        assert result == pytest.approx(1500.0)

    def test_equity_short_negative(self) -> None:
        pos = _equity_position(share_count=10.0, direction=Direction.SHORT)
        price = _price(150.0)
        result = compute_delta_adjusted_exposure_usd(pos, price)
        # delta = -1 for short equity
        assert result == pytest.approx(-1500.0)

    def test_options_long_call_positive_delta(self) -> None:
        pos = _options_position(contract_count=2.0, contract_multiplier=100.0, delta=0.5)
        underlying_price = _price(200.0)
        result = compute_delta_adjusted_exposure_usd(pos, underlying_price)
        # 2 * 100 * 0.5 * 200 = 20000
        assert result == pytest.approx(20000.0)

    def test_options_long_put_negative_delta(self) -> None:
        # Puts have negative delta even for long positions
        pos = _options_position(
            contract_count=2.0,
            contract_multiplier=100.0,
            delta=-0.4,
            contract_type=OptionContractType.PUT,
        )
        underlying_price = _price(200.0)
        result = compute_delta_adjusted_exposure_usd(pos, underlying_price)
        # 2 * 100 * (-0.4) * 200 = -16000
        assert result == pytest.approx(-16000.0)

    def test_strategy_raises_value_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5)])
        price = _price(150.0)
        with pytest.raises(ValueError):
            compute_delta_adjusted_exposure_usd(pos, price)


# ---------------------------------------------------------------------------
# Section 8b: compute_strategy_delta_adjusted_exposure_usd
# ---------------------------------------------------------------------------


class TestComputeStrategyDeltaAdjustedExposureUsd:
    def test_strategy_delta_times_summed_notional(self) -> None:
        pos = _strategy_position(
            [("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)],
            strategy_delta=0.3,
        )
        leg_underlying_prices = {
            "leg-A": _price(200.0, ticker="leg-A"),
            "leg-B": _price(100.0, ticker="leg-B"),
        }
        result = compute_strategy_delta_adjusted_exposure_usd(pos, leg_underlying_prices)
        # sum notionals: leg-A: 1*100*200=20000, leg-B: 2*50*100=10000 => 30000
        # strategy delta = 0.3
        # result = 0.3 * 30000 = 9000
        assert result == pytest.approx(9000.0)

    def test_missing_leg_raises_missing_leg_price_error(self) -> None:
        pos = _strategy_position([("leg-A", 1.0, 100.0, 0.5), ("leg-B", 2.0, 50.0, -0.3)])
        with pytest.raises(MissingLegPriceError):
            compute_strategy_delta_adjusted_exposure_usd(
                pos, {"leg-A": _price(200.0, ticker="leg-A")}
            )


# ---------------------------------------------------------------------------
# Section 9: MissingLegPriceError is a KeyError subclass
# ---------------------------------------------------------------------------


class TestMissingLegPriceError:
    def test_is_key_error_subclass(self) -> None:
        assert issubclass(MissingLegPriceError, KeyError)

    def test_can_be_raised_and_caught_as_key_error(self) -> None:
        with pytest.raises(KeyError):
            raise MissingLegPriceError("leg-X")


# ---------------------------------------------------------------------------
# Section 10: Purity (identical inputs produce identical outputs)
# ---------------------------------------------------------------------------


class TestPurity:
    @pytest.mark.parametrize("_run", range(3))
    def test_market_value_pure(self, _run: int) -> None:
        pos = _equity_position(share_count=10.0)
        price = _price(150.0)
        assert compute_market_value_usd(pos, price) == pytest.approx(1500.0)

    @pytest.mark.parametrize("_run", range(3))
    def test_position_weight_pct_pure(self, _run: int) -> None:
        assert compute_position_weight_pct(1000.0, 10000.0) == pytest.approx(10.0)

    @pytest.mark.parametrize("_run", range(3))
    def test_position_age_hours_pure(self, _run: int) -> None:
        entry = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert compute_position_age_hours(entry, now) == pytest.approx(2.0)
