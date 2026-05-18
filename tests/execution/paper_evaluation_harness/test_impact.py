"""Tests for estimate_impact (ALP-526).

Behaviors tested:
1. Basic positive result for standard inputs
2. Doubling fill_shares scales by sqrt(2)
3. Halving adv_shares scales by sqrt(2)
4. Order-type coefficient ordering: limit < market < stop
5. adv_shares=0 clamps to 1.0 (no raise, returns large positive)
6. fill_shares=0 returns money("0")
7. estimated_spread=money("0") returns money("0")
8. coefficient=0 raises ValueError
9. coefficient=-1.0 raises ValueError
10. All returned values are non-negative Money
"""

from decimal import Decimal

import pytest

from alphamind._kernel.money import money
from alphamind.config.models.execution import OrderType
from alphamind.execution.paper_evaluation_harness import estimate_impact

# ---------------------------------------------------------------------------
# Tracer bullet: basic positive result
# ---------------------------------------------------------------------------


def test_basic_positive_result() -> None:
    result = estimate_impact(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    assert isinstance(result, Decimal)
    assert result > Decimal(0)


# ---------------------------------------------------------------------------
# Scaling: doubling fill_shares scales by sqrt(2)
# ---------------------------------------------------------------------------


def test_doubling_fill_shares_scales_by_sqrt2() -> None:
    base = estimate_impact(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    doubled = estimate_impact(
        fill_shares=20_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    ratio = doubled / base
    expected = Decimal(str(2**0.5))
    assert abs(ratio - expected) < Decimal("0.0001")


# ---------------------------------------------------------------------------
# Scaling: halving adv_shares scales by sqrt(2)
# ---------------------------------------------------------------------------


def test_halving_adv_shares_scales_by_sqrt2() -> None:
    base = estimate_impact(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    halved_adv = estimate_impact(
        fill_shares=10_000,
        adv_shares=500_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    ratio = halved_adv / base
    expected = Decimal(str(2**0.5))
    assert abs(ratio - expected) < Decimal("0.0001")


# ---------------------------------------------------------------------------
# Coefficient ordering: limit < market < stop
# ---------------------------------------------------------------------------


def test_order_type_coefficient_ordering() -> None:
    shared = dict(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
    )
    limit_impact = estimate_impact(
        order_type=OrderType.limit,
        coefficient=0.25,
        **shared,  # type: ignore[arg-type]
    )
    market_impact = estimate_impact(
        order_type=OrderType.market,
        coefficient=0.5,
        **shared,  # type: ignore[arg-type]
    )
    stop_impact = estimate_impact(
        order_type=OrderType.stop,
        coefficient=0.75,
        **shared,  # type: ignore[arg-type]
    )
    assert limit_impact < market_impact < stop_impact


# ---------------------------------------------------------------------------
# Edge case: adv_shares=0 clamps to 1.0 (no raise, returns large positive)
# ---------------------------------------------------------------------------


def test_adv_shares_zero_does_not_raise_and_returns_positive() -> None:
    result = estimate_impact(
        fill_shares=10_000,
        adv_shares=0,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    assert result >= Decimal(0)
    # Clamped adv to 1.0 → sqrt(10_000 / 1.0) = 100, much larger than normal
    normal = estimate_impact(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    assert result > normal


# ---------------------------------------------------------------------------
# Edge case: fill_shares=0 returns money("0")
# ---------------------------------------------------------------------------


def test_fill_shares_zero_returns_zero() -> None:
    result = estimate_impact(
        fill_shares=0,
        adv_shares=1_000_000,
        estimated_spread=money("0.05"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    assert result == Decimal(0)


# ---------------------------------------------------------------------------
# Edge case: estimated_spread=money("0") returns money("0")
# ---------------------------------------------------------------------------


def test_zero_spread_returns_zero() -> None:
    result = estimate_impact(
        fill_shares=10_000,
        adv_shares=1_000_000,
        estimated_spread=money("0"),
        order_type=OrderType.market,
        coefficient=0.5,
    )
    assert result == Decimal(0)


# ---------------------------------------------------------------------------
# Edge case: coefficient=0 raises ValueError
# ---------------------------------------------------------------------------


def test_zero_coefficient_raises() -> None:
    with pytest.raises(ValueError, match="coefficient must be strictly positive"):
        estimate_impact(
            fill_shares=10_000,
            adv_shares=1_000_000,
            estimated_spread=money("0.05"),
            order_type=OrderType.market,
            coefficient=0,
        )


# ---------------------------------------------------------------------------
# Edge case: coefficient=-1.0 raises ValueError
# ---------------------------------------------------------------------------


def test_negative_coefficient_raises() -> None:
    with pytest.raises(ValueError, match="coefficient must be strictly positive"):
        estimate_impact(
            fill_shares=10_000,
            adv_shares=1_000_000,
            estimated_spread=money("0.05"),
            order_type=OrderType.market,
            coefficient=-1.0,
        )


# ---------------------------------------------------------------------------
# All returned values are non-negative Money
# ---------------------------------------------------------------------------


def test_all_returns_non_negative() -> None:
    cases = [
        dict(
            fill_shares=10_000,
            adv_shares=1_000_000,
            estimated_spread=money("0.05"),
            order_type=OrderType.market,
            coefficient=0.5,
        ),
        dict(
            fill_shares=1,
            adv_shares=1_000_000,
            estimated_spread=money("0.01"),
            order_type=OrderType.limit,
            coefficient=0.25,
        ),
        dict(
            fill_shares=500,
            adv_shares=1_000,
            estimated_spread=money("0.10"),
            order_type=OrderType.stop,
            coefficient=0.75,
        ),
        dict(
            fill_shares=0,
            adv_shares=1_000_000,
            estimated_spread=money("0.05"),
            order_type=OrderType.market,
            coefficient=0.5,
        ),
        dict(
            fill_shares=10_000,
            adv_shares=0,
            estimated_spread=money("0.05"),
            order_type=OrderType.market,
            coefficient=0.5,
        ),
        dict(
            fill_shares=10_000,
            adv_shares=1_000_000,
            estimated_spread=money("0"),
            order_type=OrderType.market,
            coefficient=0.5,
        ),
    ]
    for kwargs in cases:
        result = estimate_impact(**kwargs)  # type: ignore[arg-type]
        assert result >= Decimal(0), f"Got negative result {result!r} for {kwargs}"
