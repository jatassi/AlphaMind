"""Tests for paper-evaluation harness spread estimator (ALP-525)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from alphamind._kernel.money import price
from alphamind.execution.paper_evaluation_harness import estimate_spread

# ---------------------------------------------------------------------------
# Cycle 1: tracer bullet — nominal input returns strictly positive Money
# ---------------------------------------------------------------------------


class TestNominalInput:
    def test_returns_positive_money_for_nominal_input(self) -> None:
        """estimate_spread on a normal AAPL-shape input returns positive Money."""
        result = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        assert isinstance(result, Decimal)
        assert result > 0


# ---------------------------------------------------------------------------
# Cycle 2: AAPL-like input falls in a plausible-spread order-of-magnitude band
# ---------------------------------------------------------------------------


class TestAAPLLikeOrderOfMagnitude:
    def test_aapl_shape_spread_is_in_a_few_bps_band(self) -> None:
        """AAPL-like input: spread within [$0.005, $0.50] (few bps to few tens of bps)."""
        result = estimate_spread(
            fill_price=price("200"),
            adv_shares=50_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        assert Decimal("0.005") <= result <= Decimal("0.50")


# ---------------------------------------------------------------------------
# Cycle 3: illiquid input is wider than AAPL-like input
# ---------------------------------------------------------------------------


class TestIlliquidWiderThanLiquid:
    def test_illiquid_wider_than_aapl(self) -> None:
        """Illiquid input ($50, 100k ADV, 30% vol) is wider than AAPL-like spread."""
        aapl = estimate_spread(
            fill_price=price("200"),
            adv_shares=50_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        illiquid = estimate_spread(
            fill_price=price("50"),
            adv_shares=100_000,
            realized_volatility=0.30,
            buffer_pct=10,
        )
        assert illiquid > aapl


# ---------------------------------------------------------------------------
# Cycle 4: monotonicity in ADV (halving ADV widens spread)
# ---------------------------------------------------------------------------


class TestMonotoneDecreasingInADV:
    def test_halving_adv_widens_spread(self) -> None:
        """Spread is monotone-decreasing in ADV: halving widens it."""
        base = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        thinner = estimate_spread(
            fill_price=price("100"),
            adv_shares=5_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        assert thinner > base


# ---------------------------------------------------------------------------
# Cycle 5: monotonicity in vol (doubling vol widens spread until floor saturates)
# ---------------------------------------------------------------------------


class TestMonotoneIncreasingInVolatility:
    def test_doubling_volatility_widens_spread(self) -> None:
        """Spread is monotone-increasing in realized_volatility before floor saturates."""
        base = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        higher_vol = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.40,
            buffer_pct=10,
        )
        assert higher_vol > base


# ---------------------------------------------------------------------------
# Cycle 6: buffer scales linearly (buffer_pct=0 < buffer_pct=10)
# ---------------------------------------------------------------------------


class TestBufferScalesLinearly:
    def test_zero_buffer_strictly_less_than_ten_buffer(self) -> None:
        """buffer_pct=0 produces a spread strictly less than buffer_pct=10."""
        no_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=0,
        )
        with_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        assert no_buffer < with_buffer

    def test_buffer_is_linear_in_one_plus_buffer_over_hundred(self) -> None:
        """Spread scales as (1 + buffer_pct / 100): buffer=10 == 1.10 * buffer=0."""
        no_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=0,
        )
        with_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        # Allow tiny Decimal rounding; assert ratio is exactly 1.10
        assert with_buffer == no_buffer * Decimal("1.10")


# ---------------------------------------------------------------------------
# Cycle 7: adv_shares=0 does not raise; returns at least floor * buffer
# ---------------------------------------------------------------------------


class TestZeroADVDoesNotRaise:
    def test_zero_adv_returns_at_least_floor_times_buffer(self) -> None:
        """adv_shares=0 is clamped (defensive); function returns >= floor * buffer."""
        result = estimate_spread(
            fill_price=price("100"),
            adv_shares=0,
            realized_volatility=0.20,
            buffer_pct=10,
        )
        # Floor is at least 1 cent (penny-tick for actively-traded NMS); with +10% buffer
        # the value is no less than 0.01 * 1.10 = 0.011.
        assert result >= Decimal("0.011")


# ---------------------------------------------------------------------------
# Cycle 8: realized_volatility=0 saturates at floor * buffer
# ---------------------------------------------------------------------------


class TestZeroVolatilitySaturatesAtFloor:
    def test_zero_volatility_returns_floor_times_buffer(self) -> None:
        """realized_volatility=0 -> spread saturates at minimum_spread_floor_dollars * buffer."""
        no_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.0,
            buffer_pct=0,
        )
        with_buffer = estimate_spread(
            fill_price=price("100"),
            adv_shares=10_000_000,
            realized_volatility=0.0,
            buffer_pct=10,
        )
        # Both are at the floor; with buffer scaled by 1.10
        assert no_buffer > 0
        assert with_buffer == no_buffer * Decimal("1.10")


# ---------------------------------------------------------------------------
# Cycle 9: module is pure (no DB / HTTP / MainConfig imports)
# ---------------------------------------------------------------------------


class TestModuleIsPure:
    def test_no_db_http_or_mainconfig_imports(self) -> None:
        """spread.py has no DB, HTTP, or MainConfig imports — verified by grep."""
        from pathlib import Path

        import alphamind.execution.paper_evaluation_harness.spread as spread_module

        source_path = Path(spread_module.__file__)
        source = source_path.read_text()

        forbidden = [
            "import sqlalchemy",
            "from sqlalchemy",
            "import httpx",
            "from httpx",
            "import requests",
            "from requests",
            "MainConfig",
        ]
        for needle in forbidden:
            assert needle not in source, f"spread.py imports forbidden symbol: {needle}"


# ---------------------------------------------------------------------------
# Cycle 10: __init__.py re-exports estimate_spread
# ---------------------------------------------------------------------------


class TestPackageReexports:
    def test_estimate_spread_importable_from_package(self) -> None:
        """estimate_spread is importable from the package top-level."""
        from alphamind.execution.paper_evaluation_harness import estimate_spread

        assert callable(estimate_spread)

    def test_estimate_spread_in_all(self) -> None:
        """estimate_spread is listed in __all__."""
        import alphamind.execution.paper_evaluation_harness as pkg

        assert "estimate_spread" in pkg.__all__


# ---------------------------------------------------------------------------
# Cycle 11: keyword-only arguments
# ---------------------------------------------------------------------------


class TestKeywordOnlyArguments:
    def test_positional_call_raises_type_error(self) -> None:
        """estimate_spread requires keyword arguments per its signature."""
        with pytest.raises(TypeError):
            estimate_spread(price("100"), 10_000_000, 0.20, 10)  # type: ignore[misc]
