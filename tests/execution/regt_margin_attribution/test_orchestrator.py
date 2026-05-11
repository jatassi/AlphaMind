"""Tests for the per-fill attribution orchestrator (ALP-427, story 05).

``compute_attribution`` composes ``compute_regt_margin`` and
``compute_pm_equivalent_margin`` against a pre/post-fill position pair and
packages the eight-field ``RegTMarginAttribution`` record.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from alphamind.execution.regt_margin_attribution import (
    IvShockMultipliers,
    RegTMarginAttributionConfig,
    ShockParameters,
    compute_attribution,
)
from alphamind.execution.state_persistence.write_paths.records import (
    RegTMarginAttribution,
)
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
_SPOT_NVDA = 500.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    pm_model_version: str = "ibkr_mirror_v1_test",
    per_symbol_overrides: Mapping[str, float] | None = None,
) -> RegTMarginAttributionConfig:
    return RegTMarginAttributionConfig(
        pm_model_version=pm_model_version,
        shock_parameters=ShockParameters(
            per_symbol_overrides=dict(per_symbol_overrides or {}),
            high_cap_equity=0.15,
            small_cap_equity=0.20,
            unmapped_default=0.20,
        ),
        iv_shock=IvShockMultipliers(
            worst_down_multiplier=1.20,
            worst_up_multiplier=0.95,
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
    history = (_fill(),) if status == PositionStatus.OPEN else ()
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _iv_provider(*underlyings: str) -> FixtureIvProvider:
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


def test_empty_pre_and_post_positions_produces_zero_record() -> None:
    """Both inputs empty → all dollar fields 0.0; pm_model_version propagated."""
    cfg = _config(pm_model_version="ibkr_mirror_v1_zero")
    market = _market(underlying_prices={})

    result = compute_attribution(
        pre_fill_positions=(),
        post_fill_positions=(),
        market_inputs=market,
        config=cfg,
    )

    assert isinstance(result, RegTMarginAttribution)
    assert result.regt_margin_before == 0.0
    assert result.regt_margin_after == 0.0
    assert result.regt_marginal_consumption == 0.0
    assert result.pm_equivalent_before == 0.0
    assert result.pm_equivalent_after == 0.0
    assert result.pm_marginal_consumption == 0.0
    assert result.regt_excess_over_pm == 0.0
    assert result.pm_model_version == "ibkr_mirror_v1_zero"


def test_no_change_fill_produces_zero_marginal_consumption() -> None:
    """Pre and post position sets identical → all four marginal fields 0.0."""
    cfg = _config()
    pos = _equity_position(position_id="p1", ticker="NVDA", share_count=100.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(pos,),
        post_fill_positions=(pos,),
        market_inputs=market,
        config=cfg,
    )

    assert result.regt_marginal_consumption == 0.0
    assert result.pm_marginal_consumption == 0.0
    assert result.regt_excess_over_pm == 0.0
    # before == after for both metrics (the same positions snapshot twice).
    assert result.regt_margin_before == result.regt_margin_after
    assert result.pm_equivalent_before == result.pm_equivalent_after


def test_buy_long_equity_opens_position_increases_both_requirements() -> None:
    """Pre empty; post: long 100 NVDA at $500 → both margins post > 0; both marginal > 0."""
    cfg = _config(per_symbol_overrides={"NVDA": 0.20})
    post = _equity_position(position_id="p1", ticker="NVDA", share_count=100.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(),
        post_fill_positions=(post,),
        market_inputs=market,
        config=cfg,
    )

    assert result.regt_margin_before == 0.0
    assert result.pm_equivalent_before == 0.0
    assert result.regt_margin_after > 0.0
    assert result.pm_equivalent_after > 0.0
    assert result.regt_marginal_consumption > 0.0
    assert result.pm_marginal_consumption > 0.0


def test_close_long_equity_releases_both_requirements() -> None:
    """Pre: long 100 NVDA; post empty → both marginal consumptions negative.

    ``regt_excess_over_pm`` is the algebraic difference between the two
    marginal deltas.
    """
    cfg = _config(per_symbol_overrides={"NVDA": 0.20})
    pre = _equity_position(position_id="p1", ticker="NVDA", share_count=100.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(pre,),
        post_fill_positions=(),
        market_inputs=market,
        config=cfg,
    )

    assert result.regt_margin_after == 0.0
    assert result.pm_equivalent_after == 0.0
    assert result.regt_marginal_consumption < 0.0
    assert result.pm_marginal_consumption < 0.0
    assert result.regt_excess_over_pm == pytest.approx(
        result.regt_marginal_consumption - result.pm_marginal_consumption
    )


def test_regt_excess_over_pm_equals_regt_minus_pm_marginal() -> None:
    """Algebraic invariant on a non-trivial buy fill (50 → 150 NVDA shares)."""
    cfg = _config(per_symbol_overrides={"NVDA": 0.20})
    pre = _equity_position(position_id="p1", ticker="NVDA", share_count=50.0)
    post = _equity_position(position_id="p1", ticker="NVDA", share_count=150.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(pre,),
        post_fill_positions=(post,),
        market_inputs=market,
        config=cfg,
    )

    # Invariant: excess = regt_marginal - pm_marginal.
    assert result.regt_excess_over_pm == pytest.approx(
        result.regt_marginal_consumption - result.pm_marginal_consumption
    )
    # Marginal consumption = after - before for each metric (sanity).
    assert result.regt_marginal_consumption == pytest.approx(
        result.regt_margin_after - result.regt_margin_before
    )
    assert result.pm_marginal_consumption == pytest.approx(
        result.pm_equivalent_after - result.pm_equivalent_before
    )


def test_pm_model_version_propagates_from_config() -> None:
    """``result.pm_model_version`` matches ``config.pm_model_version`` verbatim."""
    cfg = _config(pm_model_version="ibkr_mirror_v1_2026Q2")
    pre = _equity_position(position_id="p1", ticker="NVDA", share_count=10.0)
    post = _equity_position(position_id="p1", ticker="NVDA", share_count=20.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(pre,),
        post_fill_positions=(post,),
        market_inputs=market,
        config=cfg,
    )

    assert result.pm_model_version == "ibkr_mirror_v1_2026Q2"


def test_record_is_frozen() -> None:
    """Field assignment on the returned record raises (Pydantic frozen contract)."""
    cfg = _config()
    market = _market(underlying_prices={})

    result = compute_attribution(
        pre_fill_positions=(),
        post_fill_positions=(),
        market_inputs=market,
        config=cfg,
    )

    with pytest.raises(ValueError, match="frozen"):
        result.regt_excess_over_pm = 99.0


def test_finite_fields_when_inputs_are_finite() -> None:
    """All seven dollar fields are finite (not nan/inf) when inputs are finite."""
    cfg = _config(per_symbol_overrides={"NVDA": 0.20})
    pre = _equity_position(position_id="p1", ticker="NVDA", share_count=50.0)
    post = _equity_position(position_id="p1", ticker="NVDA", share_count=150.0)
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA})

    result = compute_attribution(
        pre_fill_positions=(pre,),
        post_fill_positions=(post,),
        market_inputs=market,
        config=cfg,
    )

    for field_value in (
        result.regt_margin_before,
        result.regt_margin_after,
        result.regt_marginal_consumption,
        result.pm_equivalent_before,
        result.pm_equivalent_after,
        result.pm_marginal_consumption,
        result.regt_excess_over_pm,
    ):
        assert math.isfinite(field_value)


def test_unknown_underlying_propagates_key_error() -> None:
    """A position symbol missing from ``market_inputs.underlying_prices`` raises KeyError."""
    cfg = _config()
    pre = _equity_position(position_id="p1", ticker="ZZZZ", share_count=10.0)
    market = _market(underlying_prices={})

    with pytest.raises(KeyError):
        compute_attribution(
            pre_fill_positions=(pre,),
            post_fill_positions=(),
            market_inputs=market,
            config=cfg,
        )


def test_batched_fills_pre_state_threading_documented() -> None:
    """Synthetic two-fill batch: at fill 1 pre is empty book; at fill 2 pre is fill 1's post.

    Documents the caller's threading contract — sum of marginal consumptions
    equals the (post-batch minus pre-batch) values for both Reg T and PM
    equivalent.
    """
    cfg = _config(per_symbol_overrides={"NVDA": 0.20, "AAPL": 0.15})
    market = _market(underlying_prices={"NVDA": _SPOT_NVDA, "AAPL": 100.0})

    empty_book: tuple[PositionRecord, ...] = ()
    nvda_only = (_equity_position(position_id="p1", ticker="NVDA", share_count=100.0),)
    nvda_and_aapl = (
        _equity_position(position_id="p1", ticker="NVDA", share_count=100.0),
        _equity_position(position_id="p2", ticker="AAPL", share_count=50.0),
    )

    fill1 = compute_attribution(
        pre_fill_positions=empty_book,
        post_fill_positions=nvda_only,
        market_inputs=market,
        config=cfg,
    )
    fill2 = compute_attribution(
        pre_fill_positions=nvda_only,
        post_fill_positions=nvda_and_aapl,
        market_inputs=market,
        config=cfg,
    )

    # The sum of per-fill marginal consumptions equals the post-batch minus
    # pre-batch values: telescoping through fill1.after == fill2.before.
    assert fill1.regt_marginal_consumption + fill2.regt_marginal_consumption == pytest.approx(
        fill2.regt_margin_after - fill1.regt_margin_before
    )
    assert fill1.pm_marginal_consumption + fill2.pm_marginal_consumption == pytest.approx(
        fill2.pm_equivalent_after - fill1.pm_equivalent_before
    )
    # Telescoping sanity: fill1's post == fill2's pre.
    assert fill1.regt_margin_after == pytest.approx(fill2.regt_margin_before)
    assert fill1.pm_equivalent_after == pytest.approx(fill2.pm_equivalent_before)
