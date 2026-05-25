"""Tests for the debug-e2e synthetic portfolio (story 02b / ALP-498).

Covers the dataclass invariants and the module-level ``SYNTHETIC_PORTFOLIO``
constant — shape, frozen/slots flags, position/thesis counts, and the
fixture content from design § 4.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
)
from alphamind.scheduler.debug_e2e.portfolio import (
    FRESH_START_PORTFOLIO,
    SYNTHETIC_PORTFOLIO,
    SyntheticEquity,
    SyntheticOption,
    SyntheticPortfolio,
    SyntheticPosition,
    SyntheticStrategy,
    SyntheticStrategyLeg,
    SyntheticThesis,
)

# ---------------------------------------------------------------------------
# (a) Dataclass invariants — every Synthetic* shape is frozen + slots
# ---------------------------------------------------------------------------


class TestDataclassInvariants:
    @pytest.mark.parametrize(
        "cls",
        [
            SyntheticEquity,
            SyntheticOption,
            SyntheticStrategyLeg,
            SyntheticStrategy,
            SyntheticThesis,
            SyntheticPortfolio,
        ],
    )
    def test_dataclass_is_frozen_and_slots(self, cls: type) -> None:
        assert dataclasses.is_dataclass(cls)
        params = cls.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen, f"{cls.__name__} must be frozen"
        # slots dataclasses do not carry a __dict__ on instances.
        assert "__slots__" in cls.__dict__, f"{cls.__name__} must declare slots"


# ---------------------------------------------------------------------------
# (b) Type-alias surface — SyntheticPosition is the documented union
# ---------------------------------------------------------------------------


class TestPositionAlias:
    def test_synthetic_position_is_union_of_three_shapes(self) -> None:
        """``SyntheticPosition`` aliases ``Equity | Option | Strategy``."""
        # ``typing.get_args`` on a ``X | Y | Z`` union returns the members.
        from typing import get_args

        members = set(get_args(SyntheticPosition))
        assert members == {SyntheticEquity, SyntheticOption, SyntheticStrategy}


# ---------------------------------------------------------------------------
# (c) SYNTHETIC_PORTFOLIO constant — 8 positions / 8 theses / cash
# ---------------------------------------------------------------------------


class TestSyntheticPortfolioConstant:
    def test_has_eight_positions(self) -> None:
        assert len(SYNTHETIC_PORTFOLIO.positions) == 8

    def test_has_eight_theses_one_per_position(self) -> None:
        assert len(SYNTHETIC_PORTFOLIO.theses) == 8

    def test_starting_cash_usd_matches_design(self) -> None:
        assert SYNTHETIC_PORTFOLIO.starting_cash_usd == Decimal(24440)

    def test_starting_cash_usd_is_decimal(self) -> None:
        """Money discipline: ``starting_cash_usd`` is a ``Decimal``, not a float.

        Keeps the seed path (which constructs ``Decimal(str(...))``) on
        the same exact representation as the portfolio fixture, avoiding
        any float→Decimal round-trip surprises.
        """
        assert isinstance(SYNTHETIC_PORTFOLIO.starting_cash_usd, Decimal)


# ---------------------------------------------------------------------------
# (d) Fixture contents — every position from design § 4 is present
# ---------------------------------------------------------------------------


class TestEquityFixtures:
    def test_nvda_long_30_at_620(self) -> None:
        nvda = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticEquity) and p.symbol == Symbol("NVDA")
        )
        assert nvda.direction is Direction.LONG
        assert nvda.qty == 30
        assert nvda.avg_cost == 620.0
        assert nvda.borrow_rate_pct is None

    def test_jpm_long_100_at_190(self) -> None:
        jpm = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticEquity) and p.symbol == Symbol("JPM")
        )
        assert jpm.direction is Direction.LONG
        assert jpm.qty == 100
        assert jpm.avg_cost == 190.0

    def test_googl_long_70_at_200(self) -> None:
        googl = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticEquity) and p.symbol == Symbol("GOOGL")
        )
        assert googl.direction is Direction.LONG
        assert googl.qty == 70
        assert googl.avg_cost == 200.0

    def test_cop_long_130_at_115(self) -> None:
        cop = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticEquity) and p.symbol == Symbol("COP")
        )
        assert cop.direction is Direction.LONG
        assert cop.qty == 130
        assert cop.avg_cost == 115.0

    def test_tsla_short_30_at_250_with_borrow_rate(self) -> None:
        tsla = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticEquity) and p.symbol == Symbol("TSLA")
        )
        assert tsla.direction is Direction.SHORT
        assert tsla.qty == 30
        assert tsla.avg_cost == 250.0
        assert tsla.borrow_rate_pct == 1.5


class TestOptionFixtures:
    def test_aapl_call_230_270d_three_contracts(self) -> None:
        aapl = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticOption) and p.underlying == Symbol("AAPL")
        )
        assert aapl.contract_type is OptionContractType.CALL
        assert aapl.strike == 230.0
        assert aapl.expiration_offset_days == 270
        assert aapl.contracts == 3
        assert aapl.premium_per_contract == 14.50

    def test_meta_put_480_90d_one_contract(self) -> None:
        meta = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticOption) and p.underlying == Symbol("META")
        )
        assert meta.contract_type is OptionContractType.PUT
        assert meta.strike == 480.0
        assert meta.expiration_offset_days == 90
        assert meta.contracts == 1
        assert meta.premium_per_contract == 22.00


class TestStrategyFixture:
    def test_msft_bull_call_spread(self) -> None:
        msft = next(
            p
            for p in SYNTHETIC_PORTFOLIO.positions
            if isinstance(p, SyntheticStrategy) and p.underlying == Symbol("MSFT")
        )
        assert msft.strategy_label == "bull_call_spread"
        assert msft.expiration_offset_days == 180
        # Two legs: long 2x 440 + short 2x 480
        assert len(msft.legs) == 2
        long_leg = next(leg for leg in msft.legs if leg.direction is Direction.LONG)
        short_leg = next(leg for leg in msft.legs if leg.direction is Direction.SHORT)
        assert long_leg.contract_type is OptionContractType.CALL
        assert long_leg.strike == 440.0
        assert long_leg.contracts == 2
        assert short_leg.contract_type is OptionContractType.CALL
        assert short_leg.strike == 480.0
        assert short_leg.contracts == 2
        # Net debit 12.30 * 2 = 24.60
        assert msft.net_premium == pytest.approx(24.60)


class TestTheses:
    def test_each_thesis_indexes_a_distinct_position(self) -> None:
        indices = [t.position_index for t in SYNTHETIC_PORTFOLIO.theses]
        assert sorted(indices) == list(range(8))

    def test_each_thesis_carries_nonempty_text(self) -> None:
        for thesis in SYNTHETIC_PORTFOLIO.theses:
            assert thesis.headline.strip()
            assert thesis.rationale.strip()


# ---------------------------------------------------------------------------
# (e) Immutability — frozen prevents field mutation
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_synthetic_portfolio_constant_is_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            SYNTHETIC_PORTFOLIO.starting_cash_usd = Decimal(0)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (f) FRESH_START_PORTFOLIO — clean-slate fixture (ALP-618)
# ---------------------------------------------------------------------------


class TestFreshStartPortfolio:
    def test_has_zero_positions(self) -> None:
        assert FRESH_START_PORTFOLIO.positions == ()

    def test_has_zero_theses(self) -> None:
        assert FRESH_START_PORTFOLIO.theses == ()

    def test_starting_cash_usd_is_one_hundred_thousand(self) -> None:
        assert FRESH_START_PORTFOLIO.starting_cash_usd == Decimal(100_000)

    def test_starting_cash_usd_is_decimal(self) -> None:
        """Money discipline — ``Decimal`` not ``float`` (cf. SYNTHETIC_PORTFOLIO)."""
        assert isinstance(FRESH_START_PORTFOLIO.starting_cash_usd, Decimal)

    def test_is_a_synthetic_portfolio_instance(self) -> None:
        assert isinstance(FRESH_START_PORTFOLIO, SyntheticPortfolio)

    def test_constant_is_immutable(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            FRESH_START_PORTFOLIO.starting_cash_usd = Decimal(0)  # type: ignore[misc]
