"""Tests for PositionView (story 05a — split persistent core from delivery view)."""
# mypy: disable-error-code="arg-type,call-arg,dict-item,misc,no-untyped-def,no-untyped-call,unused-ignore,no-any-return,var-annotated"

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import (
    Symbol,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_FILL = PositionFill(
    fill_timestamp=_NOW,
    fill_price=150.0,
    fill_quantity=100.0,
    slippage=0.01,
    fees=1.0,
)
_LONG_EQUITY = EquityPositionDetails(
    ticker=Symbol("AAPL"),
    share_count=100.0,
    average_cost_basis_per_share=150.0,
)


def _make_record(**overrides: object) -> PositionRecord:
    """Build a valid persistent PositionRecord (post-05a — no computed fields)."""
    kwargs: dict[str, object] = {
        "position_id": "POS-AAPL-001",
        "thesis_id": "THESIS-001",
        "bracket_id": None,
        "status": PositionStatus.OPEN,
        "direction": Direction.LONG,
        "entry_timestamp": _NOW,
        "details": _LONG_EQUITY,
        "execution_history": (_FILL,),
        "realized_pnl_to_date_usd": None,
        "corporate_action_adjustment_needed": False,
        "parent_position_id": None,
        "origin": None,
    }
    kwargs.update(overrides)
    return PositionRecord(**kwargs)


def _make_view(**overrides: object) -> PositionView:
    """Build a valid PositionView wrapping the canonical persistent record."""
    record = _make_record()
    kwargs: dict[str, object] = {
        "record": record,
        "current_market_value_usd": 15_000.0,
        "unrealized_pnl_usd": 500.0,
        "unrealized_pnl_pct": 3.4,
        "position_weight_pct": 10.0,
        "position_age_hours": 24.0,
        "notional_exposure_usd": 15_000.0,
        "delta_adjusted_exposure_usd": 15_000.0,
        "distance_to_target_usd": None,
        "distance_to_stop_usd": None,
        "risk_reward_at_current": None,
    }
    kwargs.update(overrides)
    return PositionView(**kwargs)


# ---------------------------------------------------------------------------
# Happy-path construction
# ---------------------------------------------------------------------------


class TestPositionViewConstruction:
    def test_happy_path(self) -> None:
        view = _make_view()
        assert view.current_market_value_usd == 15_000.0
        assert view.unrealized_pnl_usd == 500.0
        assert view.position_weight_pct == 10.0
        assert view.delta_adjusted_exposure_usd == 15_000.0

    def test_record_field_gives_access_to_persistent_state(self) -> None:
        view = _make_view()
        assert view.record.position_id == "POS-AAPL-001"
        assert view.record.thesis_id == "THESIS-001"
        assert view.record.direction == Direction.LONG
        assert view.record.status == PositionStatus.OPEN
        assert isinstance(view.record.details, EquityPositionDetails)


# ---------------------------------------------------------------------------
# Frozen
# ---------------------------------------------------------------------------


class TestPositionViewFrozen:
    def test_frozen(self) -> None:
        view = _make_view()
        with pytest.raises(FrozenInstanceError):
            view.unrealized_pnl_usd = 9_999.0


# ---------------------------------------------------------------------------
# Field constraints
# ---------------------------------------------------------------------------


class TestPositionViewFieldConstraints:
    """Per 01e — finite-only floats; signed weight allowed; non-negative magnitudes."""

    # finite-only — current_market_value_usd
    def test_current_market_value_usd_inf_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(current_market_value_usd=float("inf"))

    def test_current_market_value_usd_nan_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(current_market_value_usd=float("nan"))

    # finite-only — unrealized_pnl_usd
    def test_unrealized_pnl_usd_inf_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(unrealized_pnl_usd=float("inf"))

    # signed — position_weight_pct can be negative for shorts
    def test_position_weight_pct_negative_passes(self) -> None:
        view = _make_view(position_weight_pct=-3.5)
        assert view.position_weight_pct == -3.5

    def test_position_weight_pct_above_100_passes(self) -> None:
        # leveraged
        view = _make_view(position_weight_pct=145.0)
        assert view.position_weight_pct == 145.0

    def test_position_weight_pct_inf_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(position_weight_pct=float("inf"))

    # non-negative — position_age_hours
    def test_position_age_hours_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(position_age_hours=-1.0)

    def test_position_age_hours_zero_passes(self) -> None:
        view = _make_view(position_age_hours=0.0)
        assert view.position_age_hours == 0.0

    # non-negative — notional_exposure_usd
    def test_notional_exposure_usd_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(notional_exposure_usd=-100.0)

    def test_notional_exposure_usd_zero_passes(self) -> None:
        view = _make_view(notional_exposure_usd=0.0)
        assert view.notional_exposure_usd == 0.0

    # signed — delta_adjusted_exposure_usd
    def test_delta_adjusted_exposure_usd_negative_passes(self) -> None:
        view = _make_view(delta_adjusted_exposure_usd=-50_000.0)
        assert view.delta_adjusted_exposure_usd == -50_000.0

    def test_delta_adjusted_exposure_usd_inf_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_view(delta_adjusted_exposure_usd=float("inf"))

    # nullable — distance_to_target_usd / distance_to_stop_usd / risk_reward_at_current
    def test_distance_and_risk_reward_can_be_none(self) -> None:
        view = _make_view(
            distance_to_target_usd=None,
            distance_to_stop_usd=None,
            risk_reward_at_current=None,
        )
        assert view.distance_to_target_usd is None
        assert view.distance_to_stop_usd is None
        assert view.risk_reward_at_current is None

    def test_distance_can_be_set(self) -> None:
        view = _make_view(
            distance_to_target_usd=5.0,
            distance_to_stop_usd=2.0,
            risk_reward_at_current=2.5,
        )
        assert view.distance_to_target_usd == 5.0
        assert view.distance_to_stop_usd == 2.0
        assert view.risk_reward_at_current == 2.5
