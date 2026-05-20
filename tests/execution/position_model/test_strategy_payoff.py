"""Tests for strategy payoff utilities (story 01c)."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.position_model import (
    compute_strategy_breakeven_levels,
    compute_strategy_greeks,
    compute_strategy_max_loss_usd,
    compute_strategy_max_profit_usd,
    compute_strategy_net_premium_usd,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    StrategyLeg,
)

# Type alias for any of the three payoff functions — used to parametrize the
# shared validation tests over all three.
PayoffFn = Callable[[tuple[StrategyLeg, ...], float], Any]

# A neutral set of greeks; the payoff computation does not consume them but the
# typed record requires the field.
_GREEKS = OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)
_EXP = date(2026, 6, 19)


def _leg(
    leg_id: str,
    direction: Direction | None,
    contract_type: OptionContractType,
    strike: float,
    contract_count: float = 1.0,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
    expiration_date: date = _EXP,
    premium_paid_per_contract: float = 0.0,
    greeks: OptionGreeks = _GREEKS,
) -> StrategyLeg:
    return StrategyLeg(
        leg_id=leg_id,
        direction=direction,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=strike,
            expiration_date=expiration_date,
            contract_type=contract_type,
            contract_count=contract_count,
            contract_multiplier=contract_multiplier,
            premium_paid_per_contract=premium_paid_per_contract,
            greeks=greeks,
        ),
    )


# ---------------------------------------------------------------------------
# Named strategy structures (acceptance criteria 5-9)
# ---------------------------------------------------------------------------


class TestBullCallVerticalSpread:
    """Long 1 call @ 100, short 1 call @ 110, mult=100, net debit $300."""

    def _legs(self) -> tuple[StrategyLeg, ...]:
        return (
            _leg("L1", Direction.LONG, OptionContractType.CALL, 100.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )

    def test_max_profit(self) -> None:
        assert compute_strategy_max_profit_usd(self._legs(), 300.0) == 700.0

    def test_max_loss(self) -> None:
        assert compute_strategy_max_loss_usd(self._legs(), 300.0) == -300.0

    def test_breakevens(self) -> None:
        assert compute_strategy_breakeven_levels(self._legs(), 300.0) == (103.0,)


class TestBearPutVerticalSpread:
    """Long 1 put @ 100, short 1 put @ 90, mult=100, net debit $300."""

    def _legs(self) -> tuple[StrategyLeg, ...]:
        return (
            _leg("L1", Direction.LONG, OptionContractType.PUT, 100.0),
            _leg("L2", Direction.SHORT, OptionContractType.PUT, 90.0),
        )

    def test_max_profit(self) -> None:
        assert compute_strategy_max_profit_usd(self._legs(), 300.0) == 700.0

    def test_max_loss(self) -> None:
        assert compute_strategy_max_loss_usd(self._legs(), 300.0) == -300.0

    def test_breakevens(self) -> None:
        assert compute_strategy_breakeven_levels(self._legs(), 300.0) == (97.0,)


class TestIronCondor:
    """Long put 90 / short put 95 / short call 105 / long call 110, credit $200."""

    def _legs(self) -> tuple[StrategyLeg, ...]:
        return (
            _leg("L1", Direction.LONG, OptionContractType.PUT, 90.0),
            _leg("L2", Direction.SHORT, OptionContractType.PUT, 95.0),
            _leg("L3", Direction.SHORT, OptionContractType.CALL, 105.0),
            _leg("L4", Direction.LONG, OptionContractType.CALL, 110.0),
        )

    def test_max_profit(self) -> None:
        assert compute_strategy_max_profit_usd(self._legs(), -200.0) == 200.0

    def test_max_loss(self) -> None:
        assert compute_strategy_max_loss_usd(self._legs(), -200.0) == -300.0

    def test_breakevens(self) -> None:
        assert compute_strategy_breakeven_levels(self._legs(), -200.0) == (93.0, 107.0)


class TestLongStraddle:
    """Long 1 call @ 100, long 1 put @ 100, mult=100, net debit $500."""

    def _legs(self) -> tuple[StrategyLeg, ...]:
        return (
            _leg("L1", Direction.LONG, OptionContractType.CALL, 100.0),
            _leg("L2", Direction.LONG, OptionContractType.PUT, 100.0),
        )

    def test_max_profit_unbounded(self) -> None:
        assert compute_strategy_max_profit_usd(self._legs(), 500.0) == float("inf")

    def test_max_loss(self) -> None:
        assert compute_strategy_max_loss_usd(self._legs(), 500.0) == -500.0

    def test_breakevens(self) -> None:
        assert compute_strategy_breakeven_levels(self._legs(), 500.0) == (95.0, 105.0)


class TestLongStrangle:
    """Long 1 call @ 105, long 1 put @ 95, mult=100, net debit $300."""

    def _legs(self) -> tuple[StrategyLeg, ...]:
        return (
            _leg("L1", Direction.LONG, OptionContractType.CALL, 105.0),
            _leg("L2", Direction.LONG, OptionContractType.PUT, 95.0),
        )

    def test_max_profit_unbounded(self) -> None:
        assert compute_strategy_max_profit_usd(self._legs(), 300.0) == float("inf")

    def test_max_loss(self) -> None:
        assert compute_strategy_max_loss_usd(self._legs(), 300.0) == -300.0

    def test_breakevens(self) -> None:
        assert compute_strategy_breakeven_levels(self._legs(), 300.0) == (92.0, 108.0)


# ---------------------------------------------------------------------------
# Net premium aggregation (story 01b)
# ---------------------------------------------------------------------------


class TestComputeStrategyNetPremium:
    def test_debit_vertical_spread_returns_positive(self) -> None:
        # Bull call spread: buy 1 call @ 100 for $5/contract, sell 1 call @ 110
        # for $2/contract. mult=100. Net = +5*100 - 2*100 = +300 (net debit).
        legs = (
            _leg(
                "L1", Direction.LONG, OptionContractType.CALL, 100.0, premium_paid_per_contract=5.0
            ),
            _leg(
                "L2", Direction.SHORT, OptionContractType.CALL, 110.0, premium_paid_per_contract=2.0
            ),
        )
        assert compute_strategy_net_premium_usd(legs) == 300.0

    def test_credit_vertical_spread_returns_negative(self) -> None:
        # Bear call spread: sell the lower strike, buy the higher.
        # Sell 1 call @ 100 for $5/contract, buy 1 call @ 110 for $2/contract.
        # mult=100. Net = -5*100 + 2*100 = -300 (net credit — SHORT subtracts).
        legs = (
            _leg(
                "L1", Direction.SHORT, OptionContractType.CALL, 100.0, premium_paid_per_contract=5.0
            ),
            _leg(
                "L2", Direction.LONG, OptionContractType.CALL, 110.0, premium_paid_per_contract=2.0
            ),
        )
        assert compute_strategy_net_premium_usd(legs) == -300.0

    def test_short_strangle_returns_negative(self) -> None:
        # Short strangle: sell 1 call @ 110 for $3, sell 1 put @ 90 for $4.
        # mult=100. Net = -3*100 - 4*100 = -700 (net credit).
        legs = (
            _leg(
                "L1", Direction.SHORT, OptionContractType.CALL, 110.0, premium_paid_per_contract=3.0
            ),
            _leg(
                "L2", Direction.SHORT, OptionContractType.PUT, 90.0, premium_paid_per_contract=4.0
            ),
        )
        assert compute_strategy_net_premium_usd(legs) == -700.0

    def test_contract_count_scales_premium(self) -> None:
        # 3 long calls @ 100 for $5/contract. Net = +3 * 100 * 5 = +1500.
        legs = (
            _leg(
                "L1",
                Direction.LONG,
                OptionContractType.CALL,
                100.0,
                contract_count=3.0,
                premium_paid_per_contract=5.0,
            ),
        )
        assert compute_strategy_net_premium_usd(legs) == 1500.0


# ---------------------------------------------------------------------------
# Validation paths (acceptance criteria 10-13)
# ---------------------------------------------------------------------------


_FUNCTIONS: tuple[PayoffFn, ...] = (
    compute_strategy_max_profit_usd,
    compute_strategy_max_loss_usd,
    compute_strategy_breakeven_levels,
)


@pytest.mark.parametrize("func", _FUNCTIONS)
class TestValidationErrors:
    def test_empty_legs_raises(self, func: PayoffFn) -> None:
        with pytest.raises(ValueError, match="legs"):
            func((), 0.0)

    def test_missing_direction_raises_with_leg_id(self, func: PayoffFn) -> None:
        legs = (
            _leg("BAD-LEG", None, OptionContractType.CALL, 100.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )
        with pytest.raises(ValueError, match="BAD-LEG"):
            func(legs, 0.0)

    def test_zero_contract_count_raises_with_leg_id(self, func: PayoffFn) -> None:
        legs = (
            _leg("ZERO-LEG", Direction.LONG, OptionContractType.CALL, 100.0, contract_count=0.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )
        with pytest.raises(ValueError, match="ZERO-LEG"):
            func(legs, 0.0)

    def test_negative_contract_count_raises_with_leg_id(self, func: PayoffFn) -> None:
        legs = (
            _leg("NEG-LEG", Direction.LONG, OptionContractType.CALL, 100.0, contract_count=-1.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )
        with pytest.raises(ValueError, match="NEG-LEG"):
            func(legs, 0.0)

    def test_mixed_expiration_dates_raises(self, func: PayoffFn) -> None:
        legs = (
            _leg("L1", Direction.LONG, OptionContractType.CALL, 100.0, expiration_date=_EXP),
            _leg(
                "L2",
                Direction.SHORT,
                OptionContractType.CALL,
                110.0,
                expiration_date=date(2026, 7, 17),
            ),
        )
        with pytest.raises(ValueError, match="expiration"):
            func(legs, 0.0)


# ---------------------------------------------------------------------------
# Net greeks aggregation (story 01b)
# ---------------------------------------------------------------------------


class TestComputeStrategyGreeks:
    def test_net_long_delta_structure_returns_positive_delta(self) -> None:
        # Bull call spread: long call delta +0.60, short call delta +0.30.
        # Both legs are 1 contract, mult 100. The SHORT leg flips its +0.30 to
        # -0.30, so net = (0.60 - 0.30) / 2 = +0.15.
        long_leg = _leg(
            "L1",
            Direction.LONG,
            OptionContractType.CALL,
            100.0,
            greeks=OptionGreeks(delta=0.60, gamma=0.04, theta=-0.05, vega=0.10),
        )
        short_leg = _leg(
            "L2",
            Direction.SHORT,
            OptionContractType.CALL,
            110.0,
            greeks=OptionGreeks(delta=0.30, gamma=0.02, theta=-0.03, vega=0.06),
        )
        result = compute_strategy_greeks((long_leg, short_leg))
        assert result.delta > 0.0
        assert result.delta == pytest.approx(0.15)

    def test_net_short_delta_structure_returns_negative_delta(self) -> None:
        # Short strangle: short call (long-equivalent delta +0.30) and short put
        # (long-equivalent delta -0.30). Both SHORT, both 1 contract.
        # leg_sign flips: -(+0.30) and -(-0.30) → -0.30 and +0.30 → net 0?
        # Make it net-short: short call delta +0.45, short put delta -0.20.
        # SHORT flips: -0.45 and +0.20 → sum -0.25 / 2 = -0.125.
        short_call = _leg(
            "L1",
            Direction.SHORT,
            OptionContractType.CALL,
            110.0,
            greeks=OptionGreeks(delta=0.45, gamma=0.03, theta=-0.04, vega=0.08),
        )
        short_put = _leg(
            "L2",
            Direction.SHORT,
            OptionContractType.PUT,
            90.0,
            greeks=OptionGreeks(delta=-0.20, gamma=0.02, theta=-0.03, vega=0.05),
        )
        result = compute_strategy_greeks((short_call, short_put))
        assert result.delta < 0.0
        assert result.delta == pytest.approx(-0.125)

    def test_leg_sign_flips_every_greek_for_short_leg(self) -> None:
        # A single SHORT leg: every greek should be the negative of the leg's
        # long-equivalent greek (contract-weighted average over one leg is the
        # signed greek itself).
        short_leg = _leg(
            "L1",
            Direction.SHORT,
            OptionContractType.CALL,
            100.0,
            greeks=OptionGreeks(delta=0.50, gamma=0.04, theta=-0.06, vega=0.12),
        )
        result = compute_strategy_greeks((short_leg,))
        assert result.delta == pytest.approx(-0.50)
        assert result.gamma == pytest.approx(-0.04)
        assert result.theta == pytest.approx(0.06)
        assert result.vega == pytest.approx(-0.12)

    def test_long_leg_preserves_greek_signs(self) -> None:
        # A single LONG leg: the contract-weighted average is the leg's greeks
        # unchanged (leg_sign = +1).
        long_leg = _leg(
            "L1",
            Direction.LONG,
            OptionContractType.CALL,
            100.0,
            greeks=OptionGreeks(delta=0.50, gamma=0.04, theta=-0.06, vega=0.12),
        )
        result = compute_strategy_greeks((long_leg,))
        assert result.delta == pytest.approx(0.50)
        assert result.gamma == pytest.approx(0.04)
        assert result.theta == pytest.approx(-0.06)
        assert result.vega == pytest.approx(0.12)

    def test_short_heavy_structure_yields_negative_net_gamma_and_vega(self) -> None:
        # Short strangle: two SHORT legs both with positive long-equivalent
        # gamma/vega. leg_sign flips both → net gamma and vega are negative.
        short_call = _leg(
            "L1",
            Direction.SHORT,
            OptionContractType.CALL,
            110.0,
            greeks=OptionGreeks(delta=0.30, gamma=0.03, theta=-0.04, vega=0.08),
        )
        short_put = _leg(
            "L2",
            Direction.SHORT,
            OptionContractType.PUT,
            90.0,
            greeks=OptionGreeks(delta=-0.25, gamma=0.02, theta=-0.03, vega=0.06),
        )
        result = compute_strategy_greeks((short_call, short_put))
        assert result.gamma < 0.0
        assert result.vega < 0.0

    def test_contract_weighting_uses_contract_count_and_multiplier(self) -> None:
        # Leg A: 3 long calls, delta +0.60. Leg B: 1 long call, delta +0.20.
        # Weighted average = (3*100*0.60 + 1*100*0.20) / (3*100 + 1*100)
        #                  = (180 + 20) / 400 = 0.50.
        leg_a = _leg(
            "L1",
            Direction.LONG,
            OptionContractType.CALL,
            100.0,
            contract_count=3.0,
            greeks=OptionGreeks(delta=0.60, gamma=0.0, theta=0.0, vega=0.0),
        )
        leg_b = _leg(
            "L2",
            Direction.LONG,
            OptionContractType.CALL,
            110.0,
            contract_count=1.0,
            greeks=OptionGreeks(delta=0.20, gamma=0.0, theta=0.0, vega=0.0),
        )
        result = compute_strategy_greeks((leg_a, leg_b))
        assert result.delta == pytest.approx(0.50)

    def test_result_clears_iv_and_timestamp(self) -> None:
        long_leg = _leg(
            "L1",
            Direction.LONG,
            OptionContractType.CALL,
            100.0,
            greeks=OptionGreeks(
                delta=0.50,
                gamma=0.04,
                theta=-0.06,
                vega=0.12,
                as_of_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
                iv_used=0.25,
            ),
        )
        result = compute_strategy_greeks((long_leg,))
        assert result.iv_used is None
        assert result.as_of_timestamp is None

    def test_refresh_failed_is_ored_across_legs(self) -> None:
        ok_leg = _leg(
            "L1",
            Direction.LONG,
            OptionContractType.CALL,
            100.0,
            greeks=OptionGreeks(delta=0.5, gamma=0.0, theta=0.0, vega=0.0, refresh_failed=False),
        )
        stale_leg = _leg(
            "L2",
            Direction.SHORT,
            OptionContractType.CALL,
            110.0,
            greeks=OptionGreeks(delta=0.3, gamma=0.0, theta=0.0, vega=0.0, refresh_failed=True),
        )
        # One leg stale → strategy refresh_failed is True.
        assert compute_strategy_greeks((ok_leg, stale_leg)).refresh_failed is True
        # All legs fresh → strategy refresh_failed is False.
        ok_leg_2 = _leg(
            "L3",
            Direction.SHORT,
            OptionContractType.CALL,
            110.0,
            greeks=OptionGreeks(delta=0.3, gamma=0.0, theta=0.0, vega=0.0, refresh_failed=False),
        )
        assert compute_strategy_greeks((ok_leg, ok_leg_2)).refresh_failed is False


class TestGreeksValidationErrors:
    """`compute_strategy_greeks` delegates to `_validate_legs`."""

    def test_empty_legs_raises(self) -> None:
        with pytest.raises(ValueError, match="legs"):
            compute_strategy_greeks(())

    def test_missing_direction_raises_with_leg_id(self) -> None:
        legs = (
            _leg("BAD-LEG", None, OptionContractType.CALL, 100.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )
        with pytest.raises(ValueError, match="BAD-LEG"):
            compute_strategy_greeks(legs)

    def test_zero_contract_count_raises_with_leg_id(self) -> None:
        legs = (
            _leg("ZERO-LEG", Direction.LONG, OptionContractType.CALL, 100.0, contract_count=0.0),
        )
        with pytest.raises(ValueError, match="ZERO-LEG"):
            compute_strategy_greeks(legs)


class TestNetPremiumValidationErrors:
    """`compute_strategy_net_premium_usd` delegates to `_validate_legs`."""

    def test_empty_legs_raises(self) -> None:
        with pytest.raises(ValueError, match="legs"):
            compute_strategy_net_premium_usd(())

    def test_missing_direction_raises_with_leg_id(self) -> None:
        legs = (
            _leg("BAD-LEG", None, OptionContractType.CALL, 100.0),
            _leg("L2", Direction.SHORT, OptionContractType.CALL, 110.0),
        )
        with pytest.raises(ValueError, match="BAD-LEG"):
            compute_strategy_net_premium_usd(legs)

    def test_zero_contract_count_raises_with_leg_id(self) -> None:
        legs = (
            _leg("ZERO-LEG", Direction.LONG, OptionContractType.CALL, 100.0, contract_count=0.0),
        )
        with pytest.raises(ValueError, match="ZERO-LEG"):
            compute_strategy_net_premium_usd(legs)

    def test_negative_contract_count_raises_with_leg_id(self) -> None:
        legs = (
            _leg("NEG-LEG", Direction.LONG, OptionContractType.CALL, 100.0, contract_count=-1.0),
        )
        with pytest.raises(ValueError, match="NEG-LEG"):
            compute_strategy_net_premium_usd(legs)


# ---------------------------------------------------------------------------
# Breakeven properties (acceptance criteria 14-15)
# ---------------------------------------------------------------------------


class TestBreakevenProperties:
    def test_breakevens_sorted_ascending(self) -> None:
        # Iron condor delivers two breakevens; assert sorted.
        legs = (
            _leg("L1", Direction.LONG, OptionContractType.PUT, 90.0),
            _leg("L2", Direction.SHORT, OptionContractType.PUT, 95.0),
            _leg("L3", Direction.SHORT, OptionContractType.CALL, 105.0),
            _leg("L4", Direction.LONG, OptionContractType.CALL, 110.0),
        )
        result = compute_strategy_breakeven_levels(legs, -200.0)
        assert list(result) == sorted(result)

    def test_no_zero_crossing_returns_empty(self) -> None:
        # Iron condor with $1000 credit — payoff is positive across the entire
        # underlying-price range (max_loss > 0 in the worst region).
        # Long put 90 / short put 95 / short call 105 / long call 110 has
        # worst-region payoff = -500 + net_premium. With net_premium = -1000 (credit),
        # worst case = -500 - (-1000) = +500. Best case = 0 - (-1000) = +1000.
        # Always positive → no zero crossing.
        legs = (
            _leg("L1", Direction.LONG, OptionContractType.PUT, 90.0),
            _leg("L2", Direction.SHORT, OptionContractType.PUT, 95.0),
            _leg("L3", Direction.SHORT, OptionContractType.CALL, 105.0),
            _leg("L4", Direction.LONG, OptionContractType.CALL, 110.0),
        )
        result = compute_strategy_breakeven_levels(legs, -1000.0)
        assert result == ()
