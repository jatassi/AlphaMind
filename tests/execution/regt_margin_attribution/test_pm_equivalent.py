"""Tests for the PM-equivalent aggregator (ALP-426, story 04).

``compute_pm_equivalent_margin`` is the thin composer that calls
``compose_class_groups`` + ``stress_class_group`` per group and sums the
results into a portfolio-aggregate PM-equivalent margin.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from alphamind.execution.regt_margin_attribution import (
    IvShockMultipliers,
    RegTMarginAttributionConfig,
    ShockParameters,
    compute_pm_equivalent_margin,
    stress_class_group,
)
from alphamind.execution.regt_margin_attribution.class_groups import ClassGroup
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    MarketInputs,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION = date(2026, 5, 28)
_RISK_FREE_RATE = 0.0425
_SPOT_AAPL = 100.0
_SPOT_NVDA = 500.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    per_symbol_overrides: Mapping[str, float] | None = None,
    unmapped_default: float = 0.20,
    worst_down_multiplier: float = 1.20,
    worst_up_multiplier: float = 0.95,
) -> RegTMarginAttributionConfig:
    return RegTMarginAttributionConfig(
        pm_model_version="test_v1",
        shock_parameters=ShockParameters(
            per_symbol_overrides=dict(per_symbol_overrides or {}),
            high_cap_equity=0.15,
            small_cap_equity=0.20,
            unmapped_default=unmapped_default,
        ),
        iv_shock=IvShockMultipliers(
            worst_down_multiplier=worst_down_multiplier,
            worst_up_multiplier=worst_up_multiplier,
        ),
        risk_free_rate_annual=_RISK_FREE_RATE,
    )


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        fill_price=100.0,
        fill_quantity=10.0,
        slippage=0.0,
        fees=1.0,
    )


def _equity_position(
    *,
    position_id: str,
    ticker: str,
    share_count: float,
    direction: Direction = Direction.LONG,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=ticker,
        share_count=share_count,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=0.05 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=500.0 if is_short else None,
    )
    execution_history = (_fill(),) if status == PositionStatus.OPEN else ()
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=execution_history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _iv_provider(
    *underlyings: str,
) -> FixtureIvProvider:
    surface = {}
    for underlying in underlyings:
        quotes = (
            IvQuote(
                strike=100.0,
                expiration=_EXPIRATION,
                contract_type=ContractType.CALL,
                implied_volatility=0.30,
            ),
            IvQuote(
                strike=100.0,
                expiration=_EXPIRATION,
                contract_type=ContractType.PUT,
                implied_volatility=0.30,
            ),
        )
        surface[underlying] = IvSurfaceEntry(underlying=underlying, quotes=quotes)
    return FixtureIvProvider(surface=surface, realized_vol={})


def _market(
    *,
    underlying_prices: dict[str, float],
    iv_provider: FixtureIvProvider | None = None,
) -> MarketInputs:
    provider = iv_provider if iv_provider is not None else _iv_provider(*underlying_prices)
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=provider,
        as_of=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_positions_returns_zero() -> None:
    """No positions → 0.0."""
    cfg = _config()
    market = _market(underlying_prices={"AAPL": _SPOT_AAPL})

    result = compute_pm_equivalent_margin((), market, cfg)

    assert result == 0.0


def test_single_class_group_equals_stress_class_group_result() -> None:
    """Single equity-only AAPL position → result equals stress_class_group for that group."""
    cfg = _config(per_symbol_overrides={"AAPL": 0.15})
    aapl = _equity_position(position_id="p1", ticker="AAPL", share_count=10.0)
    market = _market(underlying_prices={"AAPL": _SPOT_AAPL})

    result = compute_pm_equivalent_margin((aapl,), market, cfg)

    expected = stress_class_group(
        class_group=ClassGroup(underlying_symbol="AAPL", positions=(aapl,)),
        market_inputs=market,
        config=cfg,
    )
    assert result == pytest.approx(expected)


def test_two_class_groups_sum_correctly() -> None:
    """Long AAPL + long NVDA (two class groups) → result equals sum of both stress results."""
    cfg = _config(per_symbol_overrides={"AAPL": 0.15, "NVDA": 0.20})
    aapl = _equity_position(position_id="p1", ticker="AAPL", share_count=10.0)
    nvda = _equity_position(position_id="p2", ticker="NVDA", share_count=5.0)
    market = _market(underlying_prices={"AAPL": _SPOT_AAPL, "NVDA": _SPOT_NVDA})

    result = compute_pm_equivalent_margin((aapl, nvda), market, cfg)

    aapl_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="AAPL", positions=(aapl,)),
        market_inputs=market,
        config=cfg,
    )
    nvda_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="NVDA", positions=(nvda,)),
        market_inputs=market,
        config=cfg,
    )
    assert result == pytest.approx(aapl_margin + nvda_margin)


def test_hedged_book_no_correlation_offset_applied() -> None:
    """Long AAPL + short NVDA (different underlyings, different class groups).

    v1 has no inter-class correlation netting. The result equals the unhedged
    sum of each class group's stress — no offset applied even though the
    positions might theoretically hedge each other on a correlated move.
    """
    cfg = _config(per_symbol_overrides={"AAPL": 0.15, "NVDA": 0.20})
    long_aapl = _equity_position(
        position_id="p1", ticker="AAPL", share_count=10.0, direction=Direction.LONG
    )
    short_nvda = _equity_position(
        position_id="p2", ticker="NVDA", share_count=5.0, direction=Direction.SHORT
    )
    market = _market(underlying_prices={"AAPL": _SPOT_AAPL, "NVDA": _SPOT_NVDA})

    result = compute_pm_equivalent_margin((long_aapl, short_nvda), market, cfg)

    aapl_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="AAPL", positions=(long_aapl,)),
        market_inputs=market,
        config=cfg,
    )
    nvda_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="NVDA", positions=(short_nvda,)),
        market_inputs=market,
        config=cfg,
    )
    # Straight summation — no inter-class offset in v1.
    assert result == pytest.approx(aapl_margin + nvda_margin)


def test_pending_positions_excluded() -> None:
    """One OPEN NVDA + one PENDING AAPL → result equals only NVDA class group stress."""
    cfg = _config(per_symbol_overrides={"AAPL": 0.15, "NVDA": 0.20})
    open_nvda = _equity_position(
        position_id="p1", ticker="NVDA", share_count=5.0, status=PositionStatus.OPEN
    )
    pending_aapl = _equity_position(
        position_id="p2", ticker="AAPL", share_count=10.0, status=PositionStatus.PENDING
    )
    market = _market(underlying_prices={"AAPL": _SPOT_AAPL, "NVDA": _SPOT_NVDA})

    result = compute_pm_equivalent_margin((open_nvda, pending_aapl), market, cfg)

    nvda_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="NVDA", positions=(open_nvda,)),
        market_inputs=market,
        config=cfg,
    )
    assert result == pytest.approx(nvda_margin)
