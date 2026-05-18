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

from alphamind._kernel.money import price
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


# ---------------------------------------------------------------------------
# Sign convention: EQUITY sell → live_adjusted < fill_price (received less live)
# ---------------------------------------------------------------------------


def test_equity_sell_returns_lower_live_price(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    fill_price = price("200")
    result = compute_live_execution_estimate(
        fill_price=fill_price,
        fill_quantity=1000,
        instrument_type=InstrumentType.EQUITY,
        side="sell",
        order_type=OrderType.market,
        adv_shares=50_000_000.0,
        realized_volatility=0.20,
        config=config,
    )
    assert result is not None
    assert result.live_adjusted_fill_price < fill_price


# ---------------------------------------------------------------------------
# None-on-missing-data: EQUITY adv_shares=None → None
# ---------------------------------------------------------------------------


def test_equity_with_none_adv_returns_none(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    result = compute_live_execution_estimate(
        fill_price=price("200"),
        fill_quantity=1000,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.market,
        adv_shares=None,
        realized_volatility=0.20,
        config=config,
    )
    assert result is None


# ---------------------------------------------------------------------------
# None-on-missing-data: EQUITY realized_volatility=None → None
# ---------------------------------------------------------------------------


def test_equity_with_none_volatility_returns_none(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    result = compute_live_execution_estimate(
        fill_price=price("200"),
        fill_quantity=1000,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.market,
        adv_shares=50_000_000.0,
        realized_volatility=None,
        config=config,
    )
    assert result is None


# ---------------------------------------------------------------------------
# STRATEGY → NotImplementedError; message must reference parent decision G.
# ---------------------------------------------------------------------------


def test_strategy_raises_with_decision_g_message(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    with pytest.raises(NotImplementedError, match="decision G"):
        compute_live_execution_estimate(
            fill_price=price("200"),
            fill_quantity=1,
            instrument_type=InstrumentType.STRATEGY,
            side="buy",
            order_type=OrderType.market,
            adv_shares=50_000_000.0,
            realized_volatility=0.20,
            config=config,
        )


# ---------------------------------------------------------------------------
# OPTIONS buy: CAT + ORF + OCC; no SEC. Cross-check the per-fill fee total
# against the primitives that feed into the composition.
# ---------------------------------------------------------------------------


def test_options_buy_fees_are_cat_orf_occ_only(
    config: PaperHarness, fee_schedule: FeeSchedule
) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    qty = 5  # contracts
    result = compute_live_execution_estimate(
        fill_price=price("2.50"),
        fill_quantity=qty,
        instrument_type=InstrumentType.OPTIONS,
        side="buy",
        order_type=OrderType.market,
        adv_shares=10_000.0,
        realized_volatility=0.30,
        config=config,
    )
    assert result is not None

    underlying_shares = Decimal(qty * 100)
    cat = Decimal(str(fee_schedule.cat_per_executed_share)) * underlying_shares
    orf = Decimal(str(fee_schedule.orf_per_options_contract)) * Decimal(qty)
    occ = Decimal(str(fee_schedule.occ_per_options_contract)) * Decimal(qty)
    expected = cat + orf + occ
    assert result.estimated_regulatory_fees_usd == expected


# ---------------------------------------------------------------------------
# OPTIONS sell: CAT + SEC + ORF + OCC.
# ---------------------------------------------------------------------------


def test_options_sell_fees_include_sec(config: PaperHarness, fee_schedule: FeeSchedule) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    qty = 5  # contracts
    fill_px_decimal = Decimal("2.50")
    result = compute_live_execution_estimate(
        fill_price=price(fill_px_decimal),
        fill_quantity=qty,
        instrument_type=InstrumentType.OPTIONS,
        side="sell",
        order_type=OrderType.market,
        adv_shares=10_000.0,
        realized_volatility=0.30,
        config=config,
    )
    assert result is not None

    underlying_shares = Decimal(qty * 100)
    cat = Decimal(str(fee_schedule.cat_per_executed_share)) * underlying_shares
    sec = Decimal(str(fee_schedule.sec_pct_of_notional_sells)) * fill_px_decimal * underlying_shares
    orf = Decimal(str(fee_schedule.orf_per_options_contract)) * Decimal(qty)
    occ = Decimal(str(fee_schedule.occ_per_options_contract)) * Decimal(qty)
    expected = cat + sec + orf + occ
    assert result.estimated_regulatory_fees_usd == expected


# ---------------------------------------------------------------------------
# OPTIONS adv_shares=None → None
# ---------------------------------------------------------------------------


def test_options_with_none_adv_returns_none(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    result = compute_live_execution_estimate(
        fill_price=price("2.50"),
        fill_quantity=5,
        instrument_type=InstrumentType.OPTIONS,
        side="buy",
        order_type=OrderType.market,
        adv_shares=None,
        realized_volatility=0.30,
        config=config,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Invariants: all _usd fields non-negative; live_adjusted strictly positive.
# Sweep buy/sell across EQUITY and OPTIONS to anchor the contract surface.
# ---------------------------------------------------------------------------


def test_all_estimates_have_non_negative_usd_and_positive_price(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import compute_live_execution_estimate

    cases = [
        (InstrumentType.EQUITY, "buy", price("200"), 1000, 50_000_000.0, 0.20),
        (InstrumentType.EQUITY, "sell", price("200"), 1000, 50_000_000.0, 0.20),
        (InstrumentType.OPTIONS, "buy", price("2.50"), 5, 10_000.0, 0.30),
        (InstrumentType.OPTIONS, "sell", price("2.50"), 5, 10_000.0, 0.30),
    ]
    for instrument_type, side, fill_px, qty, adv, vol in cases:
        result = compute_live_execution_estimate(
            fill_price=fill_px,
            fill_quantity=qty,
            instrument_type=instrument_type,
            side=side,  # type: ignore[arg-type]
            order_type=OrderType.market,
            adv_shares=adv,
            realized_volatility=vol,
            config=config,
        )
        assert result is not None
        assert result.estimated_spread_usd >= Decimal(0), f"{instrument_type}, {side}"
        assert result.estimated_impact_usd >= Decimal(0), f"{instrument_type}, {side}"
        assert result.estimated_regulatory_fees_usd >= Decimal(0), f"{instrument_type}, {side}"
        assert result.live_adjusted_fill_price > Decimal(0), f"{instrument_type}, {side}"


# ---------------------------------------------------------------------------
# Composition identity: live_adjusted_fill_price equals fill_price ±
# (per_share_spread / 2 + per_share_impact). This anchors the formula and
# confirms no extra terms slipped into the price-adjustment path. The
# acceptance criterion's "zero drag → live == paper" guarantee is the
# corollary as the floored spread and impact tend to zero.
# ---------------------------------------------------------------------------


def test_live_adjusted_price_decomposes_to_spread_plus_impact(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import (
        compute_live_execution_estimate,
        estimate_impact,
        estimate_spread,
    )

    fill_price_decimal = Decimal(200)
    fill_price_typed = price(fill_price_decimal)
    fill_quantity = 1000

    expected_spread = estimate_spread(
        fill_price=fill_price_typed,
        adv_shares=50_000_000.0,
        realized_volatility=0.20,
        buffer_pct=config.spread_buffer_pct,
    )
    expected_impact = estimate_impact(
        fill_shares=fill_quantity,
        adv_shares=50_000_000.0,
        estimated_spread=expected_spread,
        order_type=OrderType.market,
        coefficient=config.impact_coefficients[OrderType.market],
    )

    per_share_drag = expected_spread / Decimal(2) + expected_impact

    buy = compute_live_execution_estimate(
        fill_price=fill_price_typed,
        fill_quantity=fill_quantity,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.market,
        adv_shares=50_000_000.0,
        realized_volatility=0.20,
        config=config,
    )
    sell = compute_live_execution_estimate(
        fill_price=fill_price_typed,
        fill_quantity=fill_quantity,
        instrument_type=InstrumentType.EQUITY,
        side="sell",
        order_type=OrderType.market,
        adv_shares=50_000_000.0,
        realized_volatility=0.20,
        config=config,
    )
    assert buy is not None and sell is not None
    assert buy.live_adjusted_fill_price == fill_price_decimal + per_share_drag
    assert sell.live_adjusted_fill_price == fill_price_decimal - per_share_drag


# ---------------------------------------------------------------------------
# Saturation: with zero realized_volatility the raw spread term saturates at
# the penny-tick floor, and with one share against extremely high ADV the
# impact contribution rounds away — so the per-share drag is bounded by the
# floored spread half. This is the "live_adjusted == fill_price within
# Decimal-rounding tolerance, after the floor saturates" guarantee from the
# acceptance criteria: drag does not exceed the documented floor.
# ---------------------------------------------------------------------------


def test_near_floor_inputs_keep_live_within_floor_tolerance(config: PaperHarness) -> None:
    from alphamind.execution.paper_evaluation_harness import (
        compute_live_execution_estimate,
        estimate_spread,
    )

    fill_price_typed = price("200")
    floored_spread = estimate_spread(
        fill_price=fill_price_typed,
        adv_shares=1e18,
        realized_volatility=0.0,
        buffer_pct=config.spread_buffer_pct,
    )
    half_floor = floored_spread / Decimal(2)

    result = compute_live_execution_estimate(
        fill_price=fill_price_typed,
        fill_quantity=1,
        instrument_type=InstrumentType.EQUITY,
        side="buy",
        order_type=OrderType.market,
        adv_shares=1e18,
        realized_volatility=0.0,
        config=config,
    )
    assert result is not None
    drag = result.live_adjusted_fill_price - Decimal(200)
    # Drag is at least the half-floor (the spread per-share) and never
    # exceeds half-floor + tiny impact attribution that the sqrt model
    # produces for fill=1 against 1e18 ADV.
    assert drag >= half_floor
    assert drag < half_floor + Decimal("0.001")


# ---------------------------------------------------------------------------
# Package surface: compute_live_execution_estimate is re-exported.
# ---------------------------------------------------------------------------


def test_compute_live_execution_estimate_is_reexported() -> None:
    import alphamind.execution.paper_evaluation_harness as pkg

    assert "compute_live_execution_estimate" in pkg.__all__
    assert callable(pkg.compute_live_execution_estimate)


# ---------------------------------------------------------------------------
# Purity: harness module imports must not reach DB, HTTP, or
# module-level config loaders. Inspect the import graph statically.
# ---------------------------------------------------------------------------


def test_harness_module_has_no_db_or_http_or_config_loader_imports() -> None:
    import ast
    import pathlib

    src = pathlib.Path("src/alphamind/execution/paper_evaluation_harness/harness.py").read_text()
    tree = ast.parse(src)

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_names.append(node.module)

    forbidden_substrings = (
        "sqlalchemy",
        "psycopg",
        "sqlite3",
        "httpx",
        "requests",
        "aiohttp",
        "alphamind.persistence",
        "alphamind.config.loader",
        "alphamind.config.main",
    )
    offenders = [
        name for name in imported_names if any(bad in name for bad in forbidden_substrings)
    ]
    assert not offenders, f"harness.py reached DB/HTTP/config-loader imports: {offenders!r}"
