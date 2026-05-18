"""Tests for compute_live_execution_estimate (ALP-527).

Composes the fee, spread, and impact primitives (stories 01 / 02a / 02b) into a
single dispatch function on InstrumentType.

Acceptance-criteria-anchored behaviors:

1. EQUITY buy with all inputs non-None → LiveExecutionEstimate, live_adjusted > fill_price
2. EQUITY sell with all inputs non-None → live_adjusted < fill_price
3. EQUITY adv_shares=None → None
4. EQUITY realized_volatility=None → None
5. OPTIONS buy → CAT + ORF + OCC fees (no SEC)
6. OPTIONS sell → CAT + SEC + ORF + OCC
7. OPTIONS adv_shares=None → None
8. STRATEGY → NotImplementedError naming decision (G)
9. All _usd fields non-negative; live_adjusted_fill_price strictly positive
10. Zero spread + zero impact → live_adjusted_fill_price == fill_price
11. __init__.py re-exports compute_live_execution_estimate
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from alphamind._kernel.money import Money, Price, price
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.portfolio_state.records.positions import InstrumentType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        cat_per_executed_share=0.000166,
        taf_per_share_sells=0.000145,
        sec_pct_of_notional_sells=0.0000080,
        orf_per_options_contract=0.02188,
        occ_per_options_contract=0.02,
    )


@pytest.fixture()
def config(fee_schedule: FeeSchedule) -> PaperHarness:
    return PaperHarness(
        spread_buffer_pct=10,
        impact_coefficients={
            OrderType.market: 0.5,
            OrderType.limit: 0.25,
            OrderType.stop: 0.75,
        },
        fee_schedule=fee_schedule,
    )


# ---------------------------------------------------------------------------
# Tracer bullet: EQUITY buy returns a LiveExecutionEstimate with all
# expected fields, and live_adjusted_fill_price > fill_price.
# ---------------------------------------------------------------------------


def test_equity_buy_returns_estimate_with_higher_live_price(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    fill_price = price("200")
    result = compute_live_execution_estimate(
        fill_price=fill_price,
        fill_quantity=1000,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.market,
        adv_shares=50_000_000.0,
        realized_volatility=0.20,
        config=config,
    )
    assert result is not None
    assert isinstance(result.estimated_spread_usd, Decimal)
    assert isinstance(result.estimated_impact_usd, Decimal)
    assert isinstance(result.estimated_regulatory_fees_usd, Decimal)
    assert isinstance(result.live_adjusted_fill_price, Decimal)
    assert result.live_adjusted_fill_price > fill_price
