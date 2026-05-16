"""Tests for the freshness contract (story 08)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.freshness import (
    AssembledSnapshot,
    PriceFetchOutcomes,
    SnapshotFreshness,
    compute_snapshot_freshness,
)
from alphamind.portfolio_state.pricing import PriceQuote, PriceSource
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_PHASE1_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_NOW = datetime(2025, 6, 1, 9, 0, 30, tzinfo=UTC)  # 30s after phase1
_ENTRY_AT = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)
_PRICE_AS_OF = datetime(2025, 6, 1, 8, 59, 50, tzinfo=UTC)  # 40s before _NOW

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_config(
    max_phase1_to_snapshot_seconds: float = 300.0,
    max_price_age_seconds: float = 60.0,
) -> PortfolioStateConfig:
    return PortfolioStateConfig(
        pm_decision_log_sliding_window_invocations=5,
        thesis_resolutions_lookback_trading_days=10,
        thesis_quality_aggregates_trailing_windows_days=(5, 20),
        snapshot_freshness_max_phase1_to_snapshot_seconds=max_phase1_to_snapshot_seconds,
        snapshot_freshness_max_price_age_seconds=max_price_age_seconds,
    )


def _make_open_position(position_id: str = "POS-001", ticker: str = "NVDA") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=500.0,
        borrow_rate_pct=None,
        locate_status=None,
        margin_held_usd=None,
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_AT,
        fill_price=price(500.0),
        fill_quantity=100.0,
        slippage=signed_money(0.0),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_ENTRY_AT,
        details=equity,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(52000.0),
        unrealized_pnl_usd=signed_money(2000.0),
        unrealized_pnl_pct=4.0,
        position_weight_pct=100.0,
        position_age_hours=1.5,
        notional_exposure_usd=money(52000.0),
        delta_adjusted_exposure_usd=signed_money(52000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_pending_position(position_id: str = "PEND-001", ticker: str = "AAPL") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=150.0,
        borrow_rate_pct=None,
        locate_status=None,
        margin_held_usd=None,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=equity,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(1600.0),
        unrealized_pnl_usd=signed_money(100.0),
        unrealized_pnl_pct=6.7,
        position_weight_pct=3.0,
        position_age_hours=0.0,
        notional_exposure_usd=money(1600.0),
        delta_adjusted_exposure_usd=signed_money(1600.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=10000.0,
        settled_cash_usd=10000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=10000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=100000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _make_active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )


def _make_portfolio_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=money(0.0),
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=money(0.0),
        daily_total_pnl_usd=money(0.0),
        cumulative_realized_pnl_usd=money(0.0),
        rolling_realized_pnl={
            "1d": money(0.0),
            "3d": money(0.0),
            "5d": money(0.0),
            "20d": money(0.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional_exposure() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(0.0),
        total_short_delta_adjusted_usd=money(0.0),
        net_directional_pct_of_portfolio=0.0,
        gross_pct_of_portfolio=0.0,
    )


def _make_snapshot(
    open_positions: tuple[PositionView, ...] = (),
    pending_positions: tuple[PositionView, ...] = (),
    phase1_committed_at: datetime = _PHASE1_AT,
    snapshot_assembled_at: datetime = _NOW,
) -> PortfolioStateSnapshot:
    from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate

    return PortfolioStateSnapshot(
        invocation_id="inv-test",
        phase1_committed_at=phase1_committed_at,
        snapshot_assembled_at=snapshot_assembled_at,
        pipeline_invocation_started_at=None,
        open_positions=open_positions,
        pending_positions=pending_positions,
        sector_exposure=(),
        directional_exposure=_make_directional_exposure(),
        portfolio_pnl=_make_portfolio_pnl(),
        drawdown=_make_drawdown_state(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=_make_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=ThesisQualityAggregate(
            as_of_timestamp=_NOW,
            resolution_counts_by_window=(),
            duration_stats_by_window=(),
            invalidation_timing_stats_by_window=(),
            signal_hit_rates=(),
            signal_to_thesis_conversions=(),
            conviction_calibration=(),
            conviction_sizing_deviation_by_window=(),
            performance_attribution=(),
            alpha_beta_decomposition_by_window=(),
        ),
        brackets=(),
    )


def _make_fresh_quote(ticker: str, as_of: datetime = _PRICE_AS_OF) -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price_usd=520.0,
        as_of_timestamp=as_of,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )


# ---------------------------------------------------------------------------
# PriceFetchOutcomes tests
# ---------------------------------------------------------------------------


def test_price_fetch_outcomes_happy_path() -> None:
    """Three disjoint sets construct successfully."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"POS-003"}),
        position_ids_unknown_ticker=frozenset({"POS-004"}),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    assert "POS-001" in outcomes.position_ids_priced_fresh
    assert "POS-003" in outcomes.position_ids_priced_stale
    assert "POS-004" in outcomes.position_ids_unknown_ticker


def test_price_fetch_outcomes_all_fresh() -> None:
    """All positions fresh, oldest_price_as_of set."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    assert outcomes.position_ids_priced_stale == frozenset()


def test_price_fetch_outcomes_no_positions() -> None:
    """Zero positions, oldest_price_as_of is None."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    assert outcomes.oldest_price_as_of is None


def test_price_fetch_outcomes_overlapping_fresh_stale_raises() -> None:
    """A position_id in fresh and stale sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset({"POS-001"}),
            position_ids_priced_stale=frozenset({"POS-001"}),
            position_ids_unknown_ticker=frozenset(),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_overlapping_fresh_unknown_raises() -> None:
    """A position_id in fresh and unknown sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset({"POS-001"}),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset({"POS-001"}),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_overlapping_stale_unknown_raises() -> None:
    """A position_id in stale and unknown sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset({"POS-001"}),
            position_ids_unknown_ticker=frozenset({"POS-001"}),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_oldest_price_non_tz_aware_raises() -> None:
    """oldest_price_as_of must be tz-aware UTC."""
    # Strip tzinfo from a known tz-aware datetime to get a naive datetime for testing
    naive_dt = _PHASE1_AT.replace(tzinfo=None)
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset(),
            oldest_price_as_of=naive_dt,
        )


# ---------------------------------------------------------------------------
# SnapshotFreshness tests
# ---------------------------------------------------------------------------


def _make_freshness(
    phase1_committed_at: datetime = _PHASE1_AT,
    snapshot_assembled_at: datetime = _NOW,
    phase1_to_snapshot_seconds: float = 30.0,
    max_phase1_to_snapshot_seconds: float = 300.0,
    phase1_to_snapshot_within_threshold: bool = True,
    total_open_positions: int = 2,
    total_pending_positions: int = 1,
    total_positions: int = 3,
    position_ids_priced_fresh: frozenset[str] = frozenset({"POS-001", "POS-002"}),
    position_ids_priced_stale: frozenset[str] = frozenset({"PEND-001"}),
    position_ids_unknown_ticker: frozenset[str] = frozenset(),
    count_priced_fresh: int = 2,
    count_priced_stale: int = 1,
    count_unknown_ticker: int = 0,
    all_position_prices_fresh: bool = False,
    oldest_price_as_of: datetime | None = _PRICE_AS_OF,
    oldest_price_age_seconds: float | None = 40.0,
    max_price_age_seconds: float = 60.0,
) -> SnapshotFreshness:
    return SnapshotFreshness(
        phase1_committed_at=phase1_committed_at,
        snapshot_assembled_at=snapshot_assembled_at,
        phase1_to_snapshot_seconds=phase1_to_snapshot_seconds,
        max_phase1_to_snapshot_seconds=max_phase1_to_snapshot_seconds,
        phase1_to_snapshot_within_threshold=phase1_to_snapshot_within_threshold,
        total_open_positions=total_open_positions,
        total_pending_positions=total_pending_positions,
        total_positions=total_positions,
        position_ids_priced_fresh=position_ids_priced_fresh,
        position_ids_priced_stale=position_ids_priced_stale,
        position_ids_unknown_ticker=position_ids_unknown_ticker,
        count_priced_fresh=count_priced_fresh,
        count_priced_stale=count_priced_stale,
        count_unknown_ticker=count_unknown_ticker,
        all_position_prices_fresh=all_position_prices_fresh,
        oldest_price_as_of=oldest_price_as_of,
        oldest_price_age_seconds=oldest_price_age_seconds,
        max_price_age_seconds=max_price_age_seconds,
    )


def test_snapshot_freshness_happy_path() -> None:
    """Build SnapshotFreshness with known fixture; verify all fields match."""
    sf = _make_freshness()
    assert sf.phase1_committed_at == _PHASE1_AT
    assert sf.snapshot_assembled_at == _NOW
    assert sf.phase1_to_snapshot_seconds == pytest.approx(30.0)
    assert sf.max_phase1_to_snapshot_seconds == pytest.approx(300.0)
    assert sf.phase1_to_snapshot_within_threshold is True
    assert sf.total_open_positions == 2
    assert sf.total_pending_positions == 1
    assert sf.total_positions == 3
    assert sf.count_priced_fresh == 2
    assert sf.count_priced_stale == 1
    assert sf.count_unknown_ticker == 0
    assert sf.all_position_prices_fresh is False
    assert sf.oldest_price_as_of == _PRICE_AS_OF
    assert sf.oldest_price_age_seconds == pytest.approx(40.0)


def test_snapshot_freshness_count_conservation_violated_raises() -> None:
    """count_priced_fresh + stale + unknown != total_positions raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_positions=3,
            count_priced_fresh=2,
            count_priced_stale=0,
            count_unknown_ticker=0,  # 2 + 0 + 0 = 2 != 3
        )


def test_snapshot_freshness_disjoint_sets_violated_raises() -> None:
    """Position ID in two sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            position_ids_priced_fresh=frozenset({"POS-001", "SHARED"}),
            position_ids_priced_stale=frozenset({"SHARED"}),
            position_ids_unknown_ticker=frozenset(),
            count_priced_fresh=2,
            count_priced_stale=1,
            count_unknown_ticker=0,
            total_positions=3,
        )


def test_snapshot_freshness_negative_phase1_to_snapshot_raises() -> None:
    """phase1_to_snapshot_seconds < 0 raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            phase1_to_snapshot_seconds=-1.0,
        )


def test_snapshot_freshness_oldest_price_after_assembled_raises() -> None:
    """oldest_price_as_of > snapshot_assembled_at raises ValidationError."""
    future = _NOW + timedelta(seconds=10)
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            oldest_price_as_of=future,
            oldest_price_age_seconds=-10.0,  # won't get here — validator fires first
        )


def test_snapshot_freshness_oldest_price_age_none_iff_no_positions() -> None:
    """oldest_price_age_seconds is None iff oldest_price_as_of is None."""
    # Both None — valid (no positions)
    sf_no_pos = _make_freshness(
        total_open_positions=0,
        total_pending_positions=0,
        total_positions=0,
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=0,
        count_priced_stale=0,
        count_unknown_ticker=0,
        all_position_prices_fresh=True,
        oldest_price_as_of=None,
        oldest_price_age_seconds=None,
    )
    assert sf_no_pos.oldest_price_age_seconds is None

    # oldest_price_as_of is None but oldest_price_age_seconds is not None — invalid
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_open_positions=0,
            total_pending_positions=0,
            total_positions=0,
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset(),
            count_priced_fresh=0,
            count_priced_stale=0,
            count_unknown_ticker=0,
            all_position_prices_fresh=True,
            oldest_price_as_of=None,
            oldest_price_age_seconds=5.0,
        )


def test_snapshot_freshness_total_positions_mismatch_raises() -> None:
    """total_positions != total_open + total_pending raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_open_positions=2,
            total_pending_positions=1,
            total_positions=99,  # wrong
        )


# ---------------------------------------------------------------------------
# SnapshotFreshness.is_position_price_stale tests
# ---------------------------------------------------------------------------


def test_is_position_price_stale_fresh_returns_false() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
    )
    assert sf.is_position_price_stale("POS-001") is False
    assert sf.is_position_price_stale("POS-002") is False


def test_is_position_price_stale_stale_returns_true() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
    )
    assert sf.is_position_price_stale("PEND-001") is True


def test_is_position_price_stale_unknown_ticker_returns_true() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset({"PEND-001"}),
        count_priced_stale=0,
        count_unknown_ticker=1,
        all_position_prices_fresh=False,
    )
    assert sf.is_position_price_stale("PEND-001") is True


def test_is_position_price_stale_unknown_position_id_returns_false() -> None:
    """Unknown position ID (not in any set) returns False — no exception."""
    sf = _make_freshness()
    assert sf.is_position_price_stale("POS-DOES-NOT-EXIST") is False


# ---------------------------------------------------------------------------
# SnapshotFreshness.staleness_summary tests
# ---------------------------------------------------------------------------


def test_staleness_summary_all_fresh() -> None:
    sf = _make_freshness(
        total_open_positions=2,
        total_pending_positions=1,
        total_positions=3,
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002", "PEND-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=3,
        count_priced_stale=0,
        count_unknown_ticker=0,
        all_position_prices_fresh=True,
        phase1_to_snapshot_seconds=2.3,
        max_phase1_to_snapshot_seconds=30.0,
        oldest_price_as_of=_PRICE_AS_OF,
        oldest_price_age_seconds=40.0,
    )
    summary = sf.staleness_summary()
    assert summary == "phase1→snapshot 2.3s (within 30.0s); 3/3 positions priced fresh"


def test_staleness_summary_mixed() -> None:
    sf = _make_freshness(
        total_open_positions=2,
        total_pending_positions=1,
        total_positions=3,
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=2,
        count_priced_stale=1,
        count_unknown_ticker=0,
        all_position_prices_fresh=False,
        phase1_to_snapshot_seconds=2.3,
        max_phase1_to_snapshot_seconds=30.0,
        oldest_price_as_of=_PRICE_AS_OF,
        oldest_price_age_seconds=40.0,
    )
    summary = sf.staleness_summary()
    assert "2/3 positions priced fresh" in summary
    assert "1 stale" in summary
    assert "PEND-001" in summary


# ---------------------------------------------------------------------------
# phase1_to_snapshot_within_threshold boundary tests
# ---------------------------------------------------------------------------


def test_phase1_to_snapshot_within_threshold_exactly_at_boundary() -> None:
    """Exactly at max returns True."""
    sf = _make_freshness(
        phase1_to_snapshot_seconds=300.0,
        max_phase1_to_snapshot_seconds=300.0,
        phase1_to_snapshot_within_threshold=True,
    )
    assert sf.phase1_to_snapshot_within_threshold is True


def test_phase1_to_snapshot_within_threshold_1ms_beyond() -> None:
    """1ms beyond max returns False."""
    sf = _make_freshness(
        phase1_to_snapshot_seconds=300.001,
        max_phase1_to_snapshot_seconds=300.0,
        phase1_to_snapshot_within_threshold=False,
    )
    assert sf.phase1_to_snapshot_within_threshold is False


# ---------------------------------------------------------------------------
# compute_snapshot_freshness tests
# ---------------------------------------------------------------------------


def test_compute_snapshot_freshness_happy_path() -> None:
    """2 open + 1 pending positions, all fresh — produces expected SnapshotFreshness."""
    pos1 = _make_open_position("POS-001", "NVDA")
    pos2 = _make_open_position("POS-002", "AAPL")
    pend = _make_pending_position("PEND-001", "MSFT")
    snapshot = _make_snapshot(
        open_positions=(pos1, pos2),
        pending_positions=(pend,),
    )
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002", "PEND-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    config = _make_config(
        max_phase1_to_snapshot_seconds=300.0,
        max_price_age_seconds=60.0,
    )

    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)

    assert freshness.total_open_positions == 2
    assert freshness.total_pending_positions == 1
    assert freshness.total_positions == 3
    assert freshness.count_priced_fresh == 3
    assert freshness.count_priced_stale == 0
    assert freshness.count_unknown_ticker == 0
    assert freshness.all_position_prices_fresh is True
    # phase1 = _PHASE1_AT, assembled = _NOW = phase1 + 30s
    assert freshness.phase1_to_snapshot_seconds == pytest.approx(30.0)
    assert freshness.phase1_to_snapshot_within_threshold is True
    # oldest_price_as_of = _PRICE_AS_OF = _NOW - 40s
    assert freshness.oldest_price_as_of == _PRICE_AS_OF
    assert freshness.oldest_price_age_seconds == pytest.approx(40.0)


def test_compute_snapshot_freshness_extra_position_id_in_outcomes_raises() -> None:
    """fetch_outcomes references a position_id not in snapshot → ValueError."""
    snapshot = _make_snapshot()  # empty
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-DOES-NOT-EXIST"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    with pytest.raises(ValueError, match="position_id not in snapshot"):
        compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())


def test_compute_snapshot_freshness_missing_position_in_outcomes_raises() -> None:
    """Snapshot has a position not classified in fetch_outcomes → ValueError."""
    pos = _make_open_position("POS-001")
    snapshot = _make_snapshot(open_positions=(pos,))
    outcomes = PriceFetchOutcomes(  # POS-001 missing from all sets
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    with pytest.raises(ValueError, match="position_id not classified in fetch_outcomes"):
        compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())


def test_compute_snapshot_freshness_no_positions_oldest_price_age_none() -> None:
    """Zero positions → oldest_price_age_seconds is None."""
    snapshot = _make_snapshot()  # empty
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assert freshness.oldest_price_age_seconds is None
    assert freshness.oldest_price_as_of is None


def test_compute_snapshot_freshness_deterministic() -> None:
    """Identical inputs produce identical results across repeated calls."""
    pos = _make_open_position("POS-001")
    snapshot = _make_snapshot(open_positions=(pos,))
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    config = _make_config()
    result1 = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)
    result2 = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)
    assert result1 == result2


def test_compute_snapshot_freshness_threshold_boundary() -> None:
    """Exactly at threshold: within=True; 1ms beyond: within=False."""
    pos = _make_open_position("POS-001")

    # Exactly at threshold: snapshot_assembled_at = phase1 + 300s
    snapshot_exact = _make_snapshot(
        open_positions=(pos,),
        phase1_committed_at=_PHASE1_AT,
        snapshot_assembled_at=_PHASE1_AT + timedelta(seconds=300),
    )
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PHASE1_AT,
    )
    config = _make_config(max_phase1_to_snapshot_seconds=300.0)
    freshness_exact = compute_snapshot_freshness(
        snapshot_exact, fetch_outcomes=outcomes, config=config
    )
    assert freshness_exact.phase1_to_snapshot_within_threshold is True

    # 1ms beyond
    snapshot_beyond = _make_snapshot(
        open_positions=(pos,),
        phase1_committed_at=_PHASE1_AT,
        snapshot_assembled_at=_PHASE1_AT + timedelta(seconds=300, milliseconds=1),
    )
    freshness_beyond = compute_snapshot_freshness(
        snapshot_beyond, fetch_outcomes=outcomes, config=config
    )
    assert freshness_beyond.phase1_to_snapshot_within_threshold is False


# ---------------------------------------------------------------------------
# AssembledSnapshot tests
# ---------------------------------------------------------------------------


def test_assembled_snapshot_constructs() -> None:
    """AssembledSnapshot bundles snapshot, freshness, and price_map correctly."""
    snapshot = _make_snapshot()
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assembled = AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map={})
    assert assembled.snapshot is snapshot
    assert assembled.freshness is freshness
    assert assembled.price_map == {}


def test_assembled_snapshot_is_frozen() -> None:
    """Mutating snapshot, freshness, or price_map raises an error."""
    snapshot = _make_snapshot()
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assembled = AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map={})
    with pytest.raises(FrozenInstanceError):
        assembled.snapshot = _make_snapshot()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        assembled.freshness = freshness  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        assembled.price_map = {}  # type: ignore[misc]
