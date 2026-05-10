"""Tests for strategy payoff utilities (story 01c)."""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.position_model import (
    compute_strategy_breakeven_levels,
    compute_strategy_max_loss_usd,
    compute_strategy_max_profit_usd,
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
) -> StrategyLeg:
    return StrategyLeg(
        leg_id=leg_id,
        direction=direction,
        options=OptionsPositionDetails(
            underlying_ticker="AAPL",
            strike_price=strike,
            expiration_date=expiration_date,
            contract_type=contract_type,
            contract_count=contract_count,
            contract_multiplier=contract_multiplier,
            premium_paid_per_contract=0.0,
            greeks=_GREEKS,
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
