"""Tests for portfolio P/L and drawdown rollup computations (story 05b)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.portfolio_state.computations.pnl import (
    compute_drawdown_by_source_pct,
    compute_portfolio_pnl,
    compute_total_portfolio_value_usd,
)
from alphamind.portfolio_state.records.capital import CashLedger
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
)
from alphamind.portfolio_state.repository import PortfolioPnLInputs
from alphamind.portfolio_state.snapshot import PortfolioPnL

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_FILL = PositionFill(
    fill_timestamp=_NOW,
    fill_price=100.0,
    fill_quantity=10.0,
    slippage=0.0,
    fees=0.0,
)
_LONG_EQUITY = EquityPositionDetails(
    ticker="AAPL",
    share_count=10.0,
    average_cost_basis_per_share=100.0,
)
_SHORT_EQUITY = EquityPositionDetails(
    ticker="TSLA",
    share_count=5.0,
    average_cost_basis_per_share=200.0,
    borrow_rate_pct=0.5,
    locate_status=LocateStatus.LOCATED,
    margin_held_usd=1000.0,
)


def _make_open_position(
    position_id: str,
    unrealized_pnl_usd: float,
    current_market_value_usd: float = 1000.0,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    equity = _LONG_EQUITY if direction == Direction.LONG else _SHORT_EQUITY
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": "OPEN",
            "direction": direction,
            "entry_timestamp": _NOW,
            "details": equity.model_dump(),
            "execution_history": [_FILL.model_dump()],
            "realized_pnl_to_date_usd": None,
            "current_market_value_usd": current_market_value_usd,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "unrealized_pnl_pct": 0.0,
            "position_weight_pct": 10.0,
            "position_age_hours": 1.0,
            "notional_exposure_usd": abs(current_market_value_usd),
            "delta_adjusted_exposure_usd": current_market_value_usd,
            "distance_to_target_usd": None,
            "distance_to_stop_usd": None,
            "risk_reward_at_current": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_pending_position(
    position_id: str,
    current_market_value_usd: float = 500.0,
) -> PositionRecord:
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": "PENDING",
            "direction": "LONG",
            "entry_timestamp": None,
            "details": _LONG_EQUITY.model_dump(),
            "execution_history": [],
            "realized_pnl_to_date_usd": None,
            "current_market_value_usd": current_market_value_usd,
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "position_weight_pct": 5.0,
            "position_age_hours": 0.0,
            "notional_exposure_usd": current_market_value_usd,
            "delta_adjusted_exposure_usd": current_market_value_usd,
            "distance_to_target_usd": None,
            "distance_to_stop_usd": None,
            "risk_reward_at_current": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_cash_ledger(current_cash_usd: float = 5000.0) -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": current_cash_usd,
            "settled_cash_usd": current_cash_usd,
            "reserved_capital_usd": 0.0,
            "available_buying_power_usd": current_cash_usd,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": [],
            "cash_pct_of_portfolio": 50.0,
            "true_deployable_capital_usd": current_cash_usd,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )


def _make_pnl_inputs(**overrides: object) -> PortfolioPnLInputs:
    base: dict[str, object] = {
        "daily_realized_pnl_usd": 200.0,
        "cumulative_realized_pnl_usd": 5000.0,
        "rolling_realized_pnl": {"1d": 200.0, "3d": 600.0, "5d": 900.0, "20d": 3000.0},
        "win_rate_pct": 60.0,
        "average_win_size_usd": 500.0,
        "average_loss_size_usd": 300.0,
        "profit_factor": 1.5,
    }
    base.update(overrides)
    return PortfolioPnLInputs.model_validate(base)


# ---------------------------------------------------------------------------
# compute_portfolio_pnl — happy path
# ---------------------------------------------------------------------------


class TestComputePortfolioPnlHappyPath:
    def test_returns_portfolio_pnl_instance(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        inputs = _make_pnl_inputs()
        result = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        assert isinstance(result, PortfolioPnL)

    def test_total_unrealized_pnl_usd_sums_positions(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        inputs = _make_pnl_inputs()
        result = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        assert result.total_unrealized_pnl_usd == pytest.approx(200.0)

    def test_total_unrealized_pnl_pct_of_portfolio(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        inputs = _make_pnl_inputs()
        # 200 / 10000 * 100 = 2.0%
        result = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        assert result.total_unrealized_pnl_pct_of_portfolio == pytest.approx(2.0)

    def test_passthrough_fields_match_inputs(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        inputs = _make_pnl_inputs()
        result = compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=10000.0)
        assert result.daily_realized_pnl_usd == inputs.daily_realized_pnl_usd
        assert result.cumulative_realized_pnl_usd == inputs.cumulative_realized_pnl_usd
        assert result.win_rate_pct == inputs.win_rate_pct
        assert result.average_win_size_usd == inputs.average_win_size_usd
        assert result.average_loss_size_usd == inputs.average_loss_size_usd
        assert result.profit_factor == inputs.profit_factor

    def test_daily_total_pnl_is_derived(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        inputs = _make_pnl_inputs(daily_realized_pnl_usd=200.0)
        result = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        # 200 (unrealized) + 200 (daily realized) = 400
        assert result.daily_total_pnl_usd == pytest.approx(400.0)

    def test_rolling_realized_pnl_passed_through(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=100.0)
        inputs = _make_pnl_inputs()
        result = compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=10000.0)
        assert result.rolling_realized_pnl["1d"] == pytest.approx(200.0)
        assert result.rolling_realized_pnl["3d"] == pytest.approx(600.0)
        assert result.rolling_realized_pnl["5d"] == pytest.approx(900.0)
        assert result.rolling_realized_pnl["20d"] == pytest.approx(3000.0)

    def test_none_passthrough_fields(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=0.0)
        inputs = _make_pnl_inputs(
            win_rate_pct=None,
            average_win_size_usd=None,
            average_loss_size_usd=None,
            profit_factor=None,
        )
        result = compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=1000.0)
        assert result.win_rate_pct is None
        assert result.average_win_size_usd is None
        assert result.average_loss_size_usd is None
        assert result.profit_factor is None


# ---------------------------------------------------------------------------
# compute_portfolio_pnl — empty portfolio / zero portfolio value
# ---------------------------------------------------------------------------


class TestComputePortfolioPnlEmptyPortfolio:
    def test_empty_positions_zero_total_value(self) -> None:
        inputs = _make_pnl_inputs(
            daily_realized_pnl_usd=0.0,
            cumulative_realized_pnl_usd=0.0,
            rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        )
        result = compute_portfolio_pnl((), inputs, total_portfolio_value_usd=0.0)
        assert result.total_unrealized_pnl_usd == pytest.approx(0.0)
        assert result.total_unrealized_pnl_pct_of_portfolio == pytest.approx(0.0)

    def test_no_zero_division_on_empty_portfolio(self) -> None:
        inputs = _make_pnl_inputs(
            daily_realized_pnl_usd=0.0,
            cumulative_realized_pnl_usd=0.0,
            rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        )
        # Must not raise
        result = compute_portfolio_pnl((), inputs, total_portfolio_value_usd=0.0)
        assert isinstance(result, PortfolioPnL)


# ---------------------------------------------------------------------------
# compute_portfolio_pnl — missing rolling key raises KeyError
# ---------------------------------------------------------------------------


class TestComputePortfolioPnlMissingRollingKey:
    def test_missing_5d_key_raises_key_error(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=100.0)
        inputs = _make_pnl_inputs(rolling_realized_pnl={"1d": 200.0, "3d": 600.0, "20d": 3000.0})
        with pytest.raises(KeyError, match="5d"):
            compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=10000.0)

    def test_missing_1d_key_raises_key_error(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=100.0)
        inputs = _make_pnl_inputs(rolling_realized_pnl={"3d": 600.0, "5d": 900.0, "20d": 3000.0})
        with pytest.raises(KeyError, match="1d"):
            compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=10000.0)


# ---------------------------------------------------------------------------
# compute_portfolio_pnl — negative total_portfolio_value_usd raises ValueError
# ---------------------------------------------------------------------------


class TestComputePortfolioPnlNegativeTotalValue:
    def test_negative_total_portfolio_value_raises(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=100.0)
        inputs = _make_pnl_inputs()
        with pytest.raises(ValueError, match="negative"):
            compute_portfolio_pnl((pos1,), inputs, total_portfolio_value_usd=-1.0)


# ---------------------------------------------------------------------------
# compute_drawdown_by_source_pct — happy path
# ---------------------------------------------------------------------------


class TestComputeDrawdownBySourcePctHappyPath:
    def test_returns_dict_with_negative_pnl_positions_only(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-400.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        pos3 = _make_open_position("P3", unrealized_pnl_usd=200.0)
        result = compute_drawdown_by_source_pct((pos1, pos2, pos3), current_drawdown_pct=4.0)
        assert set(result.keys()) == {"P1", "P2"}
        assert "P3" not in result

    def test_contributions_sum_to_current_drawdown_pct(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-400.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        pos3 = _make_open_position("P3", unrealized_pnl_usd=200.0)
        result = compute_drawdown_by_source_pct((pos1, pos2, pos3), current_drawdown_pct=4.0)
        assert sum(result.values()) == pytest.approx(4.0)

    def test_proportional_contributions(self) -> None:
        # P1 = 400 out of 500 total negative = 80% → 80% of 4.0 = 3.2
        # P2 = 100 out of 500 total negative = 20% → 20% of 4.0 = 0.8
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-400.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        pos3 = _make_open_position("P3", unrealized_pnl_usd=200.0)
        result = compute_drawdown_by_source_pct((pos1, pos2, pos3), current_drawdown_pct=4.0)
        assert result["P1"] == pytest.approx(3.2)
        assert result["P2"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# compute_drawdown_by_source_pct — zero drawdown returns {}
# ---------------------------------------------------------------------------


class TestComputeDrawdownBySourcePctZeroDrawdown:
    def test_zero_drawdown_returns_empty_dict(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-400.0)
        result = compute_drawdown_by_source_pct((pos1,), current_drawdown_pct=0.0)
        assert result == {}


# ---------------------------------------------------------------------------
# compute_drawdown_by_source_pct — no negative P/L positions returns {}
# ---------------------------------------------------------------------------


class TestComputeDrawdownBySourcePctNoNegative:
    def test_all_positive_pnl_returns_empty_dict(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=100.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=200.0)
        result = compute_drawdown_by_source_pct((pos1, pos2), current_drawdown_pct=2.0)
        assert result == {}

    def test_zero_pnl_positions_excluded(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=0.0)
        result = compute_drawdown_by_source_pct((pos1,), current_drawdown_pct=1.0)
        assert result == {}


# ---------------------------------------------------------------------------
# compute_drawdown_by_source_pct — negative current_drawdown_pct raises ValueError
# ---------------------------------------------------------------------------


class TestComputeDrawdownBySourcePctNegativeDrawdown:
    def test_negative_drawdown_raises_value_error(self) -> None:
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-100.0)
        with pytest.raises(ValueError):
            compute_drawdown_by_source_pct((pos1,), current_drawdown_pct=-1.0)


# ---------------------------------------------------------------------------
# compute_total_portfolio_value_usd — happy path
# ---------------------------------------------------------------------------


class TestComputeTotalPortfolioValueUsd:
    def test_cash_plus_open_plus_pending(self) -> None:
        open1 = _make_open_position("O1", unrealized_pnl_usd=0.0, current_market_value_usd=3000.0)
        open2 = _make_open_position("O2", unrealized_pnl_usd=0.0, current_market_value_usd=2000.0)
        pending1 = _make_pending_position("PEN1", current_market_value_usd=1000.0)
        cash = _make_cash_ledger(current_cash_usd=5000.0)
        result = compute_total_portfolio_value_usd((open1, open2), (pending1,), cash)
        # 5000 + 3000 + 2000 + 1000 = 11000
        assert result == pytest.approx(11000.0)

    def test_short_positions_use_abs(self) -> None:
        # Short position has negative market value; abs should flip it positive
        short_pos = _make_open_position(
            "SHORT1",
            unrealized_pnl_usd=0.0,
            current_market_value_usd=-2000.0,
            direction=Direction.SHORT,
        )
        cash = _make_cash_ledger(current_cash_usd=5000.0)
        result = compute_total_portfolio_value_usd((short_pos,), (), cash)
        # 5000 + abs(-2000) = 7000
        assert result == pytest.approx(7000.0)

    def test_empty_positions(self) -> None:
        cash = _make_cash_ledger(current_cash_usd=10000.0)
        result = compute_total_portfolio_value_usd((), (), cash)
        assert result == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# Determinism: identical inputs → identical outputs across repeated calls
# ---------------------------------------------------------------------------


class TestDeterminism:
    @pytest.mark.parametrize("run", range(3))
    def test_compute_portfolio_pnl_deterministic(self, run: int) -> None:
        del run
        pos1 = _make_open_position("P1", unrealized_pnl_usd=300.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        inputs = _make_pnl_inputs()
        r1 = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        r2 = compute_portfolio_pnl((pos1, pos2), inputs, total_portfolio_value_usd=10000.0)
        assert r1 == r2

    @pytest.mark.parametrize("run", range(3))
    def test_compute_drawdown_by_source_pct_deterministic(self, run: int) -> None:
        del run
        pos1 = _make_open_position("P1", unrealized_pnl_usd=-400.0)
        pos2 = _make_open_position("P2", unrealized_pnl_usd=-100.0)
        r1 = compute_drawdown_by_source_pct((pos1, pos2), current_drawdown_pct=4.0)
        r2 = compute_drawdown_by_source_pct((pos1, pos2), current_drawdown_pct=4.0)
        assert r1 == r2

    @pytest.mark.parametrize("run", range(3))
    def test_compute_total_portfolio_value_usd_deterministic(self, run: int) -> None:
        del run
        pos1 = _make_open_position("P1", unrealized_pnl_usd=0.0, current_market_value_usd=3000.0)
        pending1 = _make_pending_position("PEN1", current_market_value_usd=1000.0)
        cash = _make_cash_ledger(current_cash_usd=5000.0)
        r1 = compute_total_portfolio_value_usd((pos1,), (pending1,), cash)
        r2 = compute_total_portfolio_value_usd((pos1,), (pending1,), cash)
        assert r1 == r2
