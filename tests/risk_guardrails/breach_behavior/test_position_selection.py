"""Tests for the breach_behavior position-selection primitives (story 04d)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    PositionLiquidity,
    PositionRiskReward,
    PositionSelectionAction,
    select_for_drawdown_breach,
    select_for_margin_call,
    select_for_position_max_loss,
    select_for_single_short_max_size_breach,
    select_for_total_short_exposure_breach,
)


def _shipped_config() -> BreachBehaviorConfig:
    """Return a BreachBehaviorConfig with the shipped knob values.

    Used so tests verify behavior under the same constants the production
    YAML carries; if defaults change, these tests must be updated alongside.
    """
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
    )


def _make_long_position(
    *,
    position_id: str,
    ticker: str,
    unrealized_pnl_usd: float,
    position_weight_pct: float = 5.0,
) -> PositionRecord:
    """Construct a synthetic OPEN long equity position for selection tests."""
    fill_timestamp = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=fill_timestamp,
        instrument_type=InstrumentType.EQUITY,
        equity_details=EquityPositionDetails(
            ticker=ticker,
            share_count=10.0,
            average_cost_basis_per_share=100.0,
            borrow_rate_pct=None,
            locate_status=None,
            margin_held_usd=None,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_timestamp,
                fill_price=100.0,
                fill_quantity=10.0,
                slippage=0.01,
                fees=0.5,
            ),
        ),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=unrealized_pnl_usd,
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_short_position(
    *,
    position_id: str,
    ticker: str,
    unrealized_pnl_usd: float = 0.0,
    position_weight_pct: float = 5.0,
) -> PositionRecord:
    """Construct a synthetic OPEN short equity position for selection tests."""
    fill_timestamp = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=fill_timestamp,
        instrument_type=InstrumentType.EQUITY,
        equity_details=EquityPositionDetails(
            ticker=ticker,
            share_count=10.0,
            average_cost_basis_per_share=100.0,
            borrow_rate_pct=0.5,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=200.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_timestamp,
                fill_price=100.0,
                fill_quantity=10.0,
                slippage=0.01,
                fees=0.5,
            ),
        ),
        realized_pnl_to_date_usd=None,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=unrealized_pnl_usd,
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def test_position_liquidity_constructs_with_required_fields() -> None:
    liq = PositionLiquidity(position_id="POS-1", adv_to_position_size_ratio=1.5)
    assert liq.position_id == "POS-1"
    assert liq.adv_to_position_size_ratio == 1.5


def test_position_liquidity_rejects_negative_ratio() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PositionLiquidity(position_id="POS-1", adv_to_position_size_ratio=-0.1)
    assert "adv_to_position_size_ratio" in str(exc_info.value)


def test_position_liquidity_zero_ratio_allowed() -> None:
    """An illiquid position (zero ADV) is permitted; selection still uses ratio as tiebreaker."""
    liq = PositionLiquidity(position_id="POS-1", adv_to_position_size_ratio=0.0)
    assert liq.adv_to_position_size_ratio == 0.0


def test_position_liquidity_is_frozen() -> None:
    liq = PositionLiquidity(position_id="POS-1", adv_to_position_size_ratio=1.0)
    with pytest.raises(ValidationError):
        liq.adv_to_position_size_ratio = 2.0


# ---------------------------------------------------------------------------
# select_for_drawdown_breach
# ---------------------------------------------------------------------------


def test_drawdown_selects_single_losing_position() -> None:
    """One position with a negative P/L is selected; rationale names ticker and loss."""
    losing = _make_long_position(position_id="P-LOSS", ticker="NVDA", unrealized_pnl_usd=-300.0)
    winners = (
        _make_long_position(position_id="P-1", ticker="AAPL", unrealized_pnl_usd=100.0),
        _make_long_position(position_id="P-2", ticker="MSFT", unrealized_pnl_usd=50.0),
    )
    open_positions = (losing, *winners)
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in open_positions
    )

    result = select_for_drawdown_breach(open_positions=open_positions, liquidity=liquidity)

    assert result.position_id == "P-LOSS"
    assert result.action == PositionSelectionAction.FULL_CLOSE
    assert result.target_post_action_size_pct_of_portfolio is None
    assert "NVDA" in result.rationale
    assert "300" in result.rationale


def test_drawdown_largest_loss_wins_among_multiple_losers() -> None:
    """When several positions have negative P/L, the most-negative is selected."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-300.0),
        _make_long_position(position_id="P-C", ticker="CCC", unrealized_pnl_usd=-150.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )

    result = select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)

    assert result.position_id == "P-B"
    assert result.action == PositionSelectionAction.FULL_CLOSE


def test_drawdown_liquidity_tiebreaker_when_losses_tie() -> None:
    """When P/L ties, the more-liquid position (higher ratio) is selected."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-300.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-300.0),
    )
    liquidity = (
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=0.5),
        PositionLiquidity(position_id="P-B", adv_to_position_size_ratio=1.2),
    )

    result = select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)

    assert result.position_id == "P-B"  # more liquid
    assert "tie-broken by liquidity" in result.rationale
    assert "1.20" in result.rationale


def test_drawdown_position_id_tiebreaker_when_loss_and_liquidity_tie() -> None:
    """When P/L and liquidity both tie, lexicographically smallest position_id wins."""
    positions = (
        _make_long_position(position_id="P-Z", ticker="ZZZ", unrealized_pnl_usd=-300.0),
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-300.0),
    )
    liquidity = (
        PositionLiquidity(position_id="P-Z", adv_to_position_size_ratio=1.0),
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=1.0),
    )

    result = select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)

    assert result.position_id == "P-A"


def test_drawdown_no_losing_positions_raises() -> None:
    """A drawdown breach with no losing position is a structural error."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=50.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)
    assert "no losing positions" in str(exc_info.value)


def test_drawdown_empty_open_positions_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        select_for_drawdown_breach(open_positions=(), liquidity=())
    assert "non-empty" in str(exc_info.value)


def test_drawdown_missing_liquidity_entry_raises() -> None:
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-200.0),
    )
    liquidity = (
        # missing P-B
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=1.0),
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)
    assert "P-B" in str(exc_info.value)


# ---------------------------------------------------------------------------
# select_for_position_max_loss
# ---------------------------------------------------------------------------


def test_position_max_loss_selects_breaching_position() -> None:
    """Position-level max loss selector returns the breaching position with FULL_CLOSE."""
    breaching = _make_long_position(position_id="P-LOSS", ticker="NVDA", unrealized_pnl_usd=-500.0)
    others = (_make_long_position(position_id="P-1", ticker="AAPL", unrealized_pnl_usd=10.0),)
    open_positions = (breaching, *others)

    result = select_for_position_max_loss(
        breaching_position_id="P-LOSS",
        open_positions=open_positions,
        loss_pct=-3.5,
        limit_pct=-3.0,
    )

    assert result.position_id == "P-LOSS"
    assert result.action == PositionSelectionAction.FULL_CLOSE
    assert result.target_post_action_size_pct_of_portfolio is None
    assert "NVDA" in result.rationale
    assert "-3.5" in result.rationale
    assert "-3.0" in result.rationale


def test_position_max_loss_missing_breaching_id_raises() -> None:
    open_positions = (
        _make_long_position(position_id="P-1", ticker="AAA", unrealized_pnl_usd=-100.0),
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_position_max_loss(
            breaching_position_id="P-MISSING",
            open_positions=open_positions,
            loss_pct=-3.5,
            limit_pct=-3.0,
        )
    assert "P-MISSING" in str(exc_info.value)


# ---------------------------------------------------------------------------
# select_for_total_short_exposure_breach
# ---------------------------------------------------------------------------


def test_total_short_exposure_happy_path_with_shipped_defaults() -> None:
    """Four shorts at [12, 11, 10, 5] aggregate to 38% > 30 * 1.10 threshold.

    With the shipped 30% total-short limit and 95% trim target, the largest
    short (12%) is selected. Trim target = 30 * 0.95 - (11 + 10 + 5) = 28.5 - 26 = 2.5%.
    """
    shorts = (
        _make_short_position(position_id="S-A", ticker="AAA", position_weight_pct=12.0),
        _make_short_position(position_id="S-B", ticker="BBB", position_weight_pct=11.0),
        _make_short_position(position_id="S-C", ticker="CCC", position_weight_pct=10.0),
        _make_short_position(position_id="S-D", ticker="DDD", position_weight_pct=5.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0) for p in shorts
    )

    result = select_for_total_short_exposure_breach(
        short_positions=shorts,
        liquidity=liquidity,
        total_short_limit_pct_of_portfolio=30.0,
        config=_shipped_config(),
    )

    assert result.position_id == "S-A"
    assert result.action == PositionSelectionAction.PARTIAL_TRIM
    assert result.target_post_action_size_pct_of_portfolio == pytest.approx(2.5)
    assert "AAA" in result.rationale
    assert "2.5" in result.rationale  # trim target
    assert "30" in result.rationale  # cap


def test_total_short_exposure_liquidity_tiebreak_when_sizes_tie() -> None:
    """Two shorts at the same size: more-liquid is selected."""
    shorts = (
        _make_short_position(position_id="S-A", ticker="AAA", position_weight_pct=12.0),
        _make_short_position(position_id="S-B", ticker="BBB", position_weight_pct=12.0),
        _make_short_position(position_id="S-C", ticker="CCC", position_weight_pct=10.0),
    )
    liquidity = (
        PositionLiquidity(position_id="S-A", adv_to_position_size_ratio=0.5),
        PositionLiquidity(position_id="S-B", adv_to_position_size_ratio=1.5),
        PositionLiquidity(position_id="S-C", adv_to_position_size_ratio=1.0),
    )

    result = select_for_total_short_exposure_breach(
        short_positions=shorts,
        liquidity=liquidity,
        total_short_limit_pct_of_portfolio=30.0,
        config=_shipped_config(),
    )

    assert result.position_id == "S-B"  # more liquid


def test_total_short_exposure_non_positive_target_raises() -> None:
    """Other shorts already exceed the trim target; single-position trim cannot cure.

    Aggregate 47%; largest short is 14% (selected by lex). Others sum to 33%, which is
    already > 30 * 0.95 = 28.5% — even a full close of the largest short cannot cure.
    """
    shorts = (
        _make_short_position(position_id="S-A", ticker="AAA", position_weight_pct=14.0),
        _make_short_position(position_id="S-B", ticker="BBB", position_weight_pct=14.0),
        _make_short_position(position_id="S-C", ticker="CCC", position_weight_pct=14.0),
        _make_short_position(position_id="S-D", ticker="DDD", position_weight_pct=5.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0) for p in shorts
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_total_short_exposure_breach(
            short_positions=shorts,
            liquidity=liquidity,
            total_short_limit_pct_of_portfolio=30.0,
            config=_shipped_config(),
        )
    assert "trim target non-positive" in str(exc_info.value)


def test_total_short_exposure_empty_short_positions_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        select_for_total_short_exposure_breach(
            short_positions=(),
            liquidity=(),
            total_short_limit_pct_of_portfolio=30.0,
            config=_shipped_config(),
        )
    assert "non-empty" in str(exc_info.value)


# ---------------------------------------------------------------------------
# select_for_single_short_max_size_breach
# ---------------------------------------------------------------------------


def test_single_short_max_size_happy_path() -> None:
    """Breaching short at 3.5% trims to 2.85% (3.0 * 0.95) under shipped defaults."""
    breaching = _make_short_position(position_id="S-1", ticker="GME", position_weight_pct=3.5)
    open_positions = (breaching,)

    result = select_for_single_short_max_size_breach(
        breaching_position_id="S-1",
        open_positions=open_positions,
        single_short_max_pct_of_portfolio=3.0,
        config=_shipped_config(),
    )

    assert result.position_id == "S-1"
    assert result.action == PositionSelectionAction.PARTIAL_TRIM
    assert result.target_post_action_size_pct_of_portfolio == pytest.approx(2.85)
    assert "GME" in result.rationale
    assert "3.0" in result.rationale  # cap
    assert "2.85" in result.rationale  # target


def test_single_short_max_size_missing_breaching_id_raises() -> None:
    open_positions = (_make_short_position(position_id="S-1", ticker="GME"),)

    with pytest.raises(ValueError) as exc_info:
        select_for_single_short_max_size_breach(
            breaching_position_id="S-MISSING",
            open_positions=open_positions,
            single_short_max_pct_of_portfolio=3.0,
            config=_shipped_config(),
        )
    assert "S-MISSING" in str(exc_info.value)


# ---------------------------------------------------------------------------
# PositionRiskReward
# ---------------------------------------------------------------------------


def test_position_risk_reward_constructs_with_required_fields() -> None:
    rr = PositionRiskReward(position_id="P-1", risk_reward_ratio=1.5)
    assert rr.position_id == "P-1"
    assert rr.risk_reward_ratio == 1.5


def test_position_risk_reward_negative_ratio_allowed() -> None:
    """Negative R/R is pathological-but-possible (price past invalidation); model permits it."""
    rr = PositionRiskReward(position_id="P-1", risk_reward_ratio=-0.2)
    assert rr.risk_reward_ratio == -0.2


def test_position_risk_reward_is_frozen() -> None:
    rr = PositionRiskReward(position_id="P-1", risk_reward_ratio=1.0)
    with pytest.raises(ValidationError):
        rr.risk_reward_ratio = 2.0


# ---------------------------------------------------------------------------
# select_for_margin_call
# ---------------------------------------------------------------------------


def test_margin_call_selects_worst_risk_reward() -> None:
    """Three positions with R/R [1.5, 0.4, 2.0]; worst (0.4) is selected."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-C", ticker="CCC", unrealized_pnl_usd=0.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )
    rr = (
        PositionRiskReward(position_id="P-A", risk_reward_ratio=1.5),
        PositionRiskReward(position_id="P-B", risk_reward_ratio=0.4),
        PositionRiskReward(position_id="P-C", risk_reward_ratio=2.0),
    )

    result = select_for_margin_call(
        open_positions=positions,
        liquidity=liquidity,
        additional_margin_required_usd=15000.0,
        risk_reward_metric=rr,
    )

    assert result.position_id == "P-B"
    assert result.action == PositionSelectionAction.FULL_CLOSE
    assert result.target_post_action_size_pct_of_portfolio is None
    assert "BBB" in result.rationale
    assert "0.4" in result.rationale  # R/R ratio
    assert "15000" in result.rationale or "15,000" in result.rationale  # margin amount


def test_margin_call_liquidity_tiebreak_when_risk_reward_ties() -> None:
    """Two positions tie on R/R; more-liquid is selected."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
    )
    liquidity = (
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=0.5),
        PositionLiquidity(position_id="P-B", adv_to_position_size_ratio=1.5),
    )
    rr = (
        PositionRiskReward(position_id="P-A", risk_reward_ratio=0.5),
        PositionRiskReward(position_id="P-B", risk_reward_ratio=0.5),
    )

    result = select_for_margin_call(
        open_positions=positions,
        liquidity=liquidity,
        additional_margin_required_usd=10000.0,
        risk_reward_metric=rr,
    )

    assert result.position_id == "P-B"  # more liquid


def test_margin_call_negative_risk_reward_is_worst() -> None:
    """Negative R/R means past invalidation; treated as worst-of-worst."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-C", ticker="CCC", unrealized_pnl_usd=0.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )
    rr = (
        PositionRiskReward(position_id="P-A", risk_reward_ratio=0.5),
        PositionRiskReward(position_id="P-B", risk_reward_ratio=-0.2),
        PositionRiskReward(position_id="P-C", risk_reward_ratio=1.0),
    )

    result = select_for_margin_call(
        open_positions=positions,
        liquidity=liquidity,
        additional_margin_required_usd=5000.0,
        risk_reward_metric=rr,
    )
    assert result.position_id == "P-B"


def test_margin_call_missing_risk_reward_entry_raises() -> None:
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )
    rr = (
        # missing P-B
        PositionRiskReward(position_id="P-A", risk_reward_ratio=0.5),
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_margin_call(
            open_positions=positions,
            liquidity=liquidity,
            additional_margin_required_usd=5000.0,
            risk_reward_metric=rr,
        )
    assert "P-B" in str(exc_info.value)


def test_margin_call_empty_open_positions_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        select_for_margin_call(
            open_positions=(),
            liquidity=(),
            additional_margin_required_usd=5000.0,
            risk_reward_metric=(),
        )
    assert "non-empty" in str(exc_info.value)


def test_margin_call_missing_liquidity_entry_raises() -> None:
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
    )
    liquidity = (
        # missing P-B
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=1.0),
    )
    rr = (
        PositionRiskReward(position_id="P-A", risk_reward_ratio=0.5),
        PositionRiskReward(position_id="P-B", risk_reward_ratio=0.4),
    )

    with pytest.raises(ValueError) as exc_info:
        select_for_margin_call(
            open_positions=positions,
            liquidity=liquidity,
            additional_margin_required_usd=5000.0,
            risk_reward_metric=rr,
        )
    assert "P-B" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Cross-cutting: determinism, frozen output, rationale rules
# ---------------------------------------------------------------------------


def test_drawdown_selector_is_deterministic_across_repeated_calls() -> None:
    """100 repeated calls with identical inputs produce identical outputs."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-200.0),
        _make_long_position(position_id="P-C", ticker="CCC", unrealized_pnl_usd=-150.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )

    results = [
        select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)
        for _ in range(100)
    ]

    assert all(r == results[0] for r in results)


def test_margin_call_selector_is_deterministic_across_repeated_calls() -> None:
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=0.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=0.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )
    rr = (
        PositionRiskReward(position_id="P-A", risk_reward_ratio=0.5),
        PositionRiskReward(position_id="P-B", risk_reward_ratio=0.4),
    )

    results = [
        select_for_margin_call(
            open_positions=positions,
            liquidity=liquidity,
            additional_margin_required_usd=10000.0,
            risk_reward_metric=rr,
        )
        for _ in range(100)
    ]
    assert all(r == results[0] for r in results)


def test_returned_position_selection_result_is_frozen() -> None:
    """Output is immutable; assigning to a field raises ValidationError."""
    losing = _make_long_position(position_id="P-LOSS", ticker="X", unrealized_pnl_usd=-100.0)
    liquidity = (PositionLiquidity(position_id="P-LOSS", adv_to_position_size_ratio=1.0),)
    result = select_for_drawdown_breach(open_positions=(losing,), liquidity=liquidity)

    with pytest.raises(ValidationError):
        result.position_id = "OTHER"
    with pytest.raises(ValidationError):
        result.action = PositionSelectionAction.PARTIAL_TRIM


def test_rationale_format_drawdown_uniquely_most_loss() -> None:
    """Drawdown rationale states 'no tiebreaker' when uniquely most-loss."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-300.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0)
        for p in positions
    )
    result = select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)
    assert "uniquely most-loss" in result.rationale
    assert "tie-broken by liquidity" not in result.rationale


def test_drawdown_selector_does_not_mutate_inputs() -> None:
    """Input tuples are unchanged after selection (pure function)."""
    positions = (
        _make_long_position(position_id="P-A", ticker="AAA", unrealized_pnl_usd=-100.0),
        _make_long_position(position_id="P-B", ticker="BBB", unrealized_pnl_usd=-300.0),
    )
    liquidity = (
        PositionLiquidity(position_id="P-A", adv_to_position_size_ratio=1.0),
        PositionLiquidity(position_id="P-B", adv_to_position_size_ratio=1.0),
    )
    positions_before = positions
    liquidity_before = liquidity
    select_for_drawdown_breach(open_positions=positions, liquidity=liquidity)
    assert positions == positions_before
    assert liquidity == liquidity_before


def test_total_short_selector_uses_config_trim_target_not_hardcoded() -> None:
    """Config-driven trim target: changing the config value changes the result."""
    shorts = (
        _make_short_position(position_id="S-A", ticker="AAA", position_weight_pct=12.0),
        _make_short_position(position_id="S-B", ticker="BBB", position_weight_pct=11.0),
        _make_short_position(position_id="S-C", ticker="CCC", position_weight_pct=10.0),
        _make_short_position(position_id="S-D", ticker="DDD", position_weight_pct=5.0),
    )
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=1.0) for p in shorts
    )
    # Operator tunes trim target down to 80% — result should reflect 30 * 0.80 - 26 = -2 — raises
    tuned_config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=80.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
    )
    with pytest.raises(ValueError):
        select_for_total_short_exposure_breach(
            short_positions=shorts,
            liquidity=liquidity,
            total_short_limit_pct_of_portfolio=30.0,
            config=tuned_config,
        )


def test_single_short_selector_uses_config_trim_target_not_hardcoded() -> None:
    """Config-driven trim target: tuning to 90% yields 3.0 * 0.90 = 2.70%."""
    breaching = _make_short_position(position_id="S-1", ticker="GME", position_weight_pct=3.5)
    tuned_config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=90.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
    )

    result = select_for_single_short_max_size_breach(
        breaching_position_id="S-1",
        open_positions=(breaching,),
        single_short_max_pct_of_portfolio=3.0,
        config=tuned_config,
    )
    assert result.target_post_action_size_pct_of_portfolio == pytest.approx(2.70)
