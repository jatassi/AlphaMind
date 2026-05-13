"""Tests for ``recompute_greeks`` + strategy aggregation (story 03a).

Sign conventions per ``portfolio_state.records.positions.OptionGreeks``:

* Long call: positive delta, positive gamma, **negative theta**, positive vega.
* Long put: negative delta, positive gamma, negative theta, positive vega.
* The record stores long-equivalent values; the parent position direction
  sign-flips externally when needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind.execution.continuous_monitor.greeks_refresh.recompute import (
    recompute_greeks,
    recompute_strategy_greeks,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    StrategyLeg,
    StrategyPositionDetails,
)


def _options(
    *,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike: float = 200.0,
    expiration: date = date(2026, 6, 19),
) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker=Symbol("AAPL"),
        strike_price=strike,
        expiration_date=expiration,
        contract_type=contract_type,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.5,
        greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
    )


class TestRecomputeGreeksSingleLeg:
    """Per the parent issue ALP-339 sign convention reference."""

    def test_long_call_returns_positive_delta_gamma_vega_negative_theta(self) -> None:
        position = _options(contract_type=OptionContractType.CALL, strike=200.0)
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        greeks = recompute_greeks(
            position=position,
            iv=0.25,
            spot=200.0,
            as_of=as_of,
            risk_free_rate=0.045,
        )
        assert greeks.delta > 0.0
        assert greeks.gamma > 0.0
        assert greeks.theta < 0.0
        assert greeks.vega > 0.0

    def test_long_put_returns_negative_delta(self) -> None:
        position = _options(contract_type=OptionContractType.PUT, strike=200.0)
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        greeks = recompute_greeks(
            position=position,
            iv=0.25,
            spot=200.0,
            as_of=as_of,
            risk_free_rate=0.045,
        )
        assert greeks.delta < 0.0
        assert greeks.gamma > 0.0
        assert greeks.theta < 0.0
        assert greeks.vega > 0.0

    def test_freshness_metadata_stamps(self) -> None:
        position = _options(contract_type=OptionContractType.CALL, strike=200.0)
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        greeks = recompute_greeks(
            position=position,
            iv=0.275,
            spot=200.0,
            as_of=as_of,
            risk_free_rate=0.045,
        )
        assert greeks.iv_used == 0.275
        assert greeks.refresh_failed is False
        assert greeks.as_of_timestamp == as_of

    def test_recompute_is_pure(self) -> None:
        """Same inputs -> same outputs; no clock reads, no I/O."""
        position = _options(contract_type=OptionContractType.CALL, strike=200.0)
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        a = recompute_greeks(
            position=position, iv=0.25, spot=200.0, as_of=as_of, risk_free_rate=0.045
        )
        b = recompute_greeks(
            position=position, iv=0.25, spot=200.0, as_of=as_of, risk_free_rate=0.045
        )
        assert a == b


class TestRecomputeStrategyGreeks:
    """Strategy positions aggregate per-leg greeks; long legs add, short legs subtract."""

    def test_vertical_call_spread_per_leg_then_aggregate(self) -> None:
        long_leg_options = _options(contract_type=OptionContractType.CALL, strike=200.0)
        short_leg_options = _options(contract_type=OptionContractType.CALL, strike=210.0)
        strategy = StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(
                StrategyLeg(leg_id="long-leg", direction=Direction.LONG, options=long_leg_options),
                StrategyLeg(
                    leg_id="short-leg", direction=Direction.SHORT, options=short_leg_options
                ),
            ),
            net_premium_usd=200.0,
            max_profit_usd=800.0,
            max_loss_usd=200.0,
            breakeven_levels=(202.0,),
            strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        per_leg, aggregated = recompute_strategy_greeks(
            strategy=strategy,
            leg_ivs={"long-leg": 0.25, "short-leg": 0.23},
            spot=205.0,
            as_of=as_of,
            risk_free_rate=0.045,
        )
        # Per-leg map is populated for every leg_id.
        assert set(per_leg.keys()) == {"long-leg", "short-leg"}
        long_delta = per_leg["long-leg"].delta
        short_delta = per_leg["short-leg"].delta
        # ITM call (200 strike at 205 spot) has more delta than near-ATM (210).
        assert long_delta > short_delta
        # Aggregated delta = long - short (short leg flips sign).
        assert aggregated.delta == long_delta - short_delta
        # Aggregated vega = long - short (the short leg cancels some vega).
        assert aggregated.vega == per_leg["long-leg"].vega - per_leg["short-leg"].vega
        # Freshness metadata reflects the refresh.
        assert aggregated.as_of_timestamp == as_of
        assert aggregated.refresh_failed is False
        # iv_used carries the mean of leg IVs as a single scalar.
        assert aggregated.iv_used == (0.25 + 0.23) / 2.0

    def test_strategy_leg_quantity_scales_signed_weight(self) -> None:
        """A strategy with leg ``contract_count > 1`` scales the per-leg greeks
        by the absolute contract count; direction handles the sign."""
        long_leg_options = OptionsPositionDetails(
            underlying_ticker=Symbol("SPY"),
            strike_price=500.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=2.0,  # two contracts
            contract_multiplier=100.0,
            premium_paid_per_contract=5.0,
            greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        strategy = StrategyPositionDetails(
            strategy_type_label="long_call_2x",
            legs=(StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=long_leg_options),),
            net_premium_usd=1000.0,
            max_profit_usd=float("inf"),
            max_loss_usd=1000.0,
            breakeven_levels=(510.0,),
            strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        per_leg, aggregated = recompute_strategy_greeks(
            strategy=strategy,
            leg_ivs={"leg-1": 0.25},
            spot=500.0,
            as_of=as_of,
            risk_free_rate=0.045,
        )
        # Aggregated delta = 2 contracts * per-leg delta (long, no sign flip).
        assert aggregated.delta == 2.0 * per_leg["leg-1"].delta
