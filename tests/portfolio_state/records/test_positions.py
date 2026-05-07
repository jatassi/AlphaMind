"""Tests for position records (story 03a)."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)


class TestDirectionEnum:
    def test_members(self) -> None:
        assert set(Direction) == {Direction.LONG, Direction.SHORT}

    def test_string_values(self) -> None:
        assert Direction.LONG == "LONG"
        assert Direction.SHORT == "SHORT"


class TestInstrumentTypeEnum:
    def test_members(self) -> None:
        assert set(InstrumentType) == {
            InstrumentType.EQUITY,
            InstrumentType.OPTIONS,
            InstrumentType.STRATEGY,
        }

    def test_string_values(self) -> None:
        assert InstrumentType.EQUITY == "EQUITY"
        assert InstrumentType.OPTIONS == "OPTIONS"
        assert InstrumentType.STRATEGY == "STRATEGY"


class TestOptionContractTypeEnum:
    def test_members(self) -> None:
        assert set(OptionContractType) == {OptionContractType.CALL, OptionContractType.PUT}

    def test_string_values(self) -> None:
        assert OptionContractType.CALL == "CALL"
        assert OptionContractType.PUT == "PUT"


class TestPositionStatusEnum:
    def test_members(self) -> None:
        assert set(PositionStatus) == {
            PositionStatus.PENDING,
            PositionStatus.OPEN,
            PositionStatus.CLOSED,
        }

    def test_string_values(self) -> None:
        assert PositionStatus.PENDING == "PENDING"
        assert PositionStatus.OPEN == "OPEN"
        assert PositionStatus.CLOSED == "CLOSED"


class TestLocateStatusEnum:
    def test_members(self) -> None:
        assert set(LocateStatus) == {LocateStatus.LOCATED, LocateStatus.AT_RISK_OF_RECALL}

    def test_string_values(self) -> None:
        assert LocateStatus.LOCATED == "LOCATED"
        assert LocateStatus.AT_RISK_OF_RECALL == "AT_RISK_OF_RECALL"


class TestOptionGreeks:
    def test_valid_construction(self) -> None:
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3)
        assert g.delta == 0.5
        assert g.gamma == 0.1
        assert g.theta == -0.02
        assert g.vega == 0.3

    def test_frozen(self) -> None:
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3)
        with pytest.raises(ValidationError):
            g.delta = 0.9

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(ValidationError):
            OptionGreeks.model_validate({"delta": 0.5})

    def test_freshness_defaults_all_none_or_false(self) -> None:
        """(a) All freshness fields default to None/False; legacy construction succeeds."""
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3)
        assert g.as_of_timestamp is None
        assert g.iv_used is None
        assert g.refresh_failed is False

    def test_populated_freshness_fields_succeed(self) -> None:
        """(b) Populated freshness fields with valid values succeed."""
        ts = datetime.now(tz=UTC)
        g = OptionGreeks(
            delta=0.5,
            gamma=0.1,
            theta=-0.02,
            vega=0.3,
            as_of_timestamp=ts,
            iv_used=0.25,
            refresh_failed=True,
        )
        assert g.as_of_timestamp == ts
        assert g.iv_used == 0.25
        assert g.refresh_failed is True

    def test_naive_datetime_rejected(self) -> None:
        """(c) Naive datetime (no tzinfo) raises ValidationError."""
        with pytest.raises(ValidationError):
            OptionGreeks(
                delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, as_of_timestamp=datetime.now()
            )  # noqa: DTZ005

    def test_tz_aware_datetime_accepted(self) -> None:
        """(c) tz-aware datetime is accepted."""
        ts = datetime.now(tz=UTC)
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, as_of_timestamp=ts)
        assert g.as_of_timestamp == ts

    def test_zero_iv_used_rejected(self) -> None:
        """(d) iv_used=0.0 raises ValidationError."""
        with pytest.raises(ValidationError):
            OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, iv_used=0.0)

    def test_negative_iv_used_rejected(self) -> None:
        """(d) iv_used=-0.1 raises ValidationError."""
        with pytest.raises(ValidationError):
            OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, iv_used=-0.1)

    def test_positive_iv_used_accepted(self) -> None:
        """(d) iv_used=0.45 succeeds."""
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, iv_used=0.45)
        assert g.iv_used == 0.45

    def test_refresh_failed_true_with_freshness_populated(self) -> None:
        """(e) refresh_failed=True when freshness fields are populated."""
        ts = datetime.now(tz=UTC)
        g = OptionGreeks(
            delta=0.5,
            gamma=0.1,
            theta=-0.02,
            vega=0.3,
            as_of_timestamp=ts,
            iv_used=0.30,
            refresh_failed=True,
        )
        assert g.refresh_failed is True

    def test_frozen_after_freshness_field_set(self) -> None:
        """(f) Frozen model — can't mutate as_of_timestamp after construction."""
        ts = datetime.now(tz=UTC)
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, as_of_timestamp=ts)
        with pytest.raises(ValidationError):
            g.as_of_timestamp = datetime.now(tz=UTC)

    def test_docstring_sign_convention_paragraphs(self) -> None:
        """Class docstring includes the four sign-convention paragraphs."""
        doc = OptionGreeks.__doc__ or ""
        assert "delta" in doc
        assert "gamma" in doc
        assert "theta" in doc
        assert "vega" in doc


class TestPositionFill:
    def _now_utc(self) -> datetime:
        return datetime.now(tz=UTC)

    def test_valid_construction(self) -> None:
        ts = self._now_utc()
        fill = PositionFill(
            fill_timestamp=ts,
            fill_price=100.0,
            fill_quantity=10.0,
            slippage=0.01,
            fees=1.50,
        )
        assert fill.fill_timestamp == ts
        assert fill.fill_price == 100.0

    def test_frozen(self) -> None:
        fill = PositionFill(
            fill_timestamp=self._now_utc(),
            fill_price=100.0,
            fill_quantity=10.0,
            slippage=0.01,
            fees=1.50,
        )
        with pytest.raises(ValidationError):
            fill.fill_price = 200.0

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(ValidationError):
            PositionFill.model_validate({"fill_price": 100.0})


class TestEquityPositionDetails:
    def test_long_construction(self) -> None:
        d = EquityPositionDetails(
            ticker="AAPL",
            share_count=100.0,
            average_cost_basis_per_share=150.0,
            borrow_rate_pct=None,
            locate_status=None,
            margin_held_usd=None,
        )
        assert d.ticker == "AAPL"
        assert d.borrow_rate_pct is None

    def test_short_construction(self) -> None:
        d = EquityPositionDetails(
            ticker="AAPL",
            share_count=100.0,
            average_cost_basis_per_share=150.0,
            borrow_rate_pct=0.5,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=5000.0,
        )
        assert d.borrow_rate_pct == 0.5
        assert d.locate_status == LocateStatus.LOCATED

    def test_frozen(self) -> None:
        d = EquityPositionDetails(
            ticker="AAPL",
            share_count=100.0,
            average_cost_basis_per_share=150.0,
        )
        with pytest.raises(ValidationError):
            d.ticker = "MSFT"

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(ValidationError):
            EquityPositionDetails.model_validate({"ticker": "AAPL"})


_GREEKS = OptionGreeks(delta=0.5, gamma=0.05, theta=-0.01, vega=0.2)
_EXP = date(2025, 6, 20)


def _make_options_details(**overrides: object) -> OptionsPositionDetails:
    kwargs: dict[str, object] = {
        "underlying_ticker": "AAPL",
        "strike_price": 200.0,
        "expiration_date": _EXP,
        "contract_type": OptionContractType.CALL,
        "contract_count": 2.0,
        "contract_multiplier": 100.0,
        "premium_paid_per_contract": 5.0,
        "greeks": _GREEKS,
    }
    kwargs.update(overrides)
    return OptionsPositionDetails.model_validate(kwargs)


class TestOptionsPositionDetails:
    def test_valid_construction(self) -> None:
        d = _make_options_details()
        assert d.underlying_ticker == "AAPL"
        assert d.contract_type == OptionContractType.CALL
        assert d.greeks.delta == 0.5

    def test_frozen(self) -> None:
        d = _make_options_details()
        with pytest.raises(ValidationError):
            d.underlying_ticker = "MSFT"

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(ValidationError):
            OptionsPositionDetails.model_validate({"underlying_ticker": "AAPL"})


def _make_strategy_leg(**overrides: object) -> StrategyLeg:
    kwargs: dict[str, object] = {
        "leg_id": "leg-1",
        "options": _make_options_details(),
    }
    kwargs.update(overrides)
    return StrategyLeg.model_validate(kwargs)


class TestStrategyLeg:
    def test_valid_construction(self) -> None:
        leg = _make_strategy_leg()
        assert leg.leg_id == "leg-1"
        assert leg.options.underlying_ticker == "AAPL"

    def test_frozen(self) -> None:
        leg = _make_strategy_leg()
        with pytest.raises(ValidationError):
            leg.leg_id = "leg-2"


class TestStrategyPositionDetails:
    def test_valid_construction(self) -> None:
        leg = _make_strategy_leg()
        d = StrategyPositionDetails(
            strategy_type_label="bull_call_spread",
            legs=(leg,),
            net_premium_usd=-500.0,
            max_profit_usd=1000.0,
            max_loss_usd=500.0,
            breakeven_levels=(205.0,),
            strategy_greeks=_GREEKS,
        )
        assert d.strategy_type_label == "bull_call_spread"
        assert len(d.legs) == 1

    def test_frozen(self) -> None:
        leg = _make_strategy_leg()
        d = StrategyPositionDetails(
            strategy_type_label="bull_call_spread",
            legs=(leg,),
            net_premium_usd=-500.0,
            max_profit_usd=1000.0,
            max_loss_usd=500.0,
            breakeven_levels=(205.0,),
            strategy_greeks=_GREEKS,
        )
        with pytest.raises(ValidationError):
            d.strategy_type_label = "other"


# ---------------------------------------------------------------------------
# PositionRecord helpers
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
    ticker="AAPL",
    share_count=100.0,
    average_cost_basis_per_share=150.0,
)
_SHORT_EQUITY = EquityPositionDetails(
    ticker="AAPL",
    share_count=100.0,
    average_cost_basis_per_share=150.0,
    borrow_rate_pct=0.5,
    locate_status=LocateStatus.LOCATED,
    margin_held_usd=5000.0,
)
_OPTIONS_DETAILS = _make_options_details()


def _make_position(**overrides: object) -> PositionRecord:
    """Build a valid open long equity PositionRecord."""
    kwargs: dict[str, object] = {
        "position_id": "POS-AAPL-001",
        "thesis_id": "THESIS-001",
        "bracket_id": None,
        "status": PositionStatus.OPEN,
        "direction": Direction.LONG,
        "entry_timestamp": _NOW,
        "instrument_type": InstrumentType.EQUITY,
        "equity_details": _LONG_EQUITY,
        "options_details": None,
        "strategy_details": None,
        "execution_history": (_FILL,),
        "realized_pnl_to_date_usd": None,
        "current_market_value_usd": 15000.0,
        "unrealized_pnl_usd": 500.0,
        "unrealized_pnl_pct": 3.4,
        "position_weight_pct": 10.0,
        "position_age_hours": 24.0,
        "notional_exposure_usd": 15000.0,
        "delta_adjusted_exposure_usd": 15000.0,
        "distance_to_target_usd": None,
        "distance_to_stop_usd": None,
        "risk_reward_at_current": None,
        "corporate_action_adjustment_needed": False,
        "parent_position_id": None,
        "origin": None,
    }
    kwargs.update(overrides)
    return PositionRecord.model_validate(kwargs)


# ---------------------------------------------------------------------------
# Discriminator tests
# ---------------------------------------------------------------------------


class TestPositionRecordDiscriminator:
    def test_equity_with_equity_details_passes(self) -> None:
        p = _make_position()
        assert p.instrument_type == InstrumentType.EQUITY
        assert p.equity_details is not None

    def test_equity_with_options_details_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(equity_details=None, options_details=_OPTIONS_DETAILS)
        assert "instrument_type" in str(exc_info.value) or "details" in str(exc_info.value)

    def test_two_details_populated_simultaneously_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_position(options_details=_OPTIONS_DETAILS)

    def test_no_details_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_position(equity_details=None)

    def test_options_with_options_details_passes(self) -> None:
        p = _make_position(
            instrument_type=InstrumentType.OPTIONS,
            equity_details=None,
            options_details=_OPTIONS_DETAILS,
            execution_history=(_FILL,),
        )
        assert p.options_details is not None

    def test_strategy_with_strategy_details_passes(self) -> None:
        leg = _make_strategy_leg()
        strat = StrategyPositionDetails(
            strategy_type_label="bull_call_spread",
            legs=(leg,),
            net_premium_usd=-500.0,
            max_profit_usd=1000.0,
            max_loss_usd=500.0,
            breakeven_levels=(205.0,),
            strategy_greeks=_GREEKS,
        )
        p = _make_position(
            instrument_type=InstrumentType.STRATEGY,
            equity_details=None,
            strategy_details=strat,
        )
        assert p.strategy_details is not None


# ---------------------------------------------------------------------------
# Status validation tests
# ---------------------------------------------------------------------------


class TestPendingStatusRules:
    def test_pending_with_no_timestamp_passes(self) -> None:
        p = _make_position(
            status=PositionStatus.PENDING,
            entry_timestamp=None,
            execution_history=(),
        )
        assert p.status == PositionStatus.PENDING

    def test_pending_with_filled_execution_history_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                status=PositionStatus.PENDING,
                entry_timestamp=None,
                execution_history=(_FILL,),
            )
        assert "execution_history" in str(exc_info.value)


class TestOpenStatusRules:
    def test_open_with_fills_passes(self) -> None:
        p = _make_position(execution_history=(_FILL,))
        assert p.status == PositionStatus.OPEN

    def test_open_with_empty_execution_history_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(status=PositionStatus.OPEN, execution_history=())
        assert "execution_history" in str(exc_info.value)


class TestClosedStatusRules:
    def test_closed_with_pnl_and_zero_market_value_passes(self) -> None:
        p = _make_position(
            status=PositionStatus.CLOSED,
            realized_pnl_to_date_usd=500.0,
            current_market_value_usd=0.0,
            unrealized_pnl_usd=0.0,
        )
        assert p.status == PositionStatus.CLOSED

    def test_closed_without_realized_pnl_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                status=PositionStatus.CLOSED,
                realized_pnl_to_date_usd=None,
                current_market_value_usd=0.0,
                unrealized_pnl_usd=0.0,
            )
        assert "realized_pnl_to_date_usd" in str(exc_info.value)

    def test_closed_with_nonzero_market_value_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                status=PositionStatus.CLOSED,
                realized_pnl_to_date_usd=500.0,
                current_market_value_usd=100.0,
                unrealized_pnl_usd=0.0,
            )
        assert "current_market_value_usd" in str(exc_info.value)

    def test_closed_with_nonzero_unrealized_pnl_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                status=PositionStatus.CLOSED,
                realized_pnl_to_date_usd=500.0,
                current_market_value_usd=0.0,
                unrealized_pnl_usd=50.0,
            )
        assert "unrealized_pnl_usd" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Direction / equity short-only fields tests
# ---------------------------------------------------------------------------


class TestDirectionShortFields:
    def test_long_with_no_short_fields_passes(self) -> None:
        p = _make_position(direction=Direction.LONG, equity_details=_LONG_EQUITY)
        assert p.equity_details is not None
        assert p.equity_details.borrow_rate_pct is None

    def test_long_with_short_fields_populated_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(direction=Direction.LONG, equity_details=_SHORT_EQUITY)
        assert "borrow_rate_pct" in str(exc_info.value) or "locate_status" in str(exc_info.value)

    def test_short_with_all_short_fields_passes(self) -> None:
        p = _make_position(direction=Direction.SHORT, equity_details=_SHORT_EQUITY)
        assert p.equity_details is not None
        assert p.equity_details.locate_status == LocateStatus.LOCATED

    def test_short_with_missing_borrow_rate_fails(self) -> None:
        partial = EquityPositionDetails(
            ticker="AAPL",
            share_count=100.0,
            average_cost_basis_per_share=150.0,
            borrow_rate_pct=None,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=5000.0,
        )
        with pytest.raises(ValidationError) as exc_info:
            _make_position(direction=Direction.SHORT, equity_details=partial)
        assert "borrow_rate_pct" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Range constraint tests
# ---------------------------------------------------------------------------


class TestRangeConstraints:
    # position_weight_pct: finite-only, any sign (shorts negative, leveraged >100%)
    def test_position_weight_pct_short_negative_passes(self) -> None:
        p = _make_position(position_weight_pct=-3.5)
        assert p.position_weight_pct == -3.5

    def test_position_weight_pct_leveraged_above_100_passes(self) -> None:
        p = _make_position(position_weight_pct=145.0)
        assert p.position_weight_pct == 145.0

    def test_position_weight_pct_inf_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_position(position_weight_pct=float("inf"))

    def test_position_weight_pct_at_bounds_passes(self) -> None:
        p0 = _make_position(position_weight_pct=0.0)
        p100 = _make_position(position_weight_pct=100.0)
        assert p0.position_weight_pct == 0.0
        assert p100.position_weight_pct == 100.0

    def test_position_age_hours_negative_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(position_age_hours=-1.0)
        assert "position_age_hours" in str(exc_info.value)

    def test_position_age_hours_zero_passes(self) -> None:
        p = _make_position(position_age_hours=0.0)
        assert p.position_age_hours == 0.0

    def test_notional_exposure_usd_negative_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(notional_exposure_usd=-100.0)
        assert "notional_exposure_usd" in str(exc_info.value)

    def test_notional_exposure_usd_zero_passes(self) -> None:
        p = _make_position(notional_exposure_usd=0.0)
        assert p.notional_exposure_usd == 0.0

    # delta_adjusted_exposure_usd: signed (any finite float)
    def test_delta_adjusted_exposure_usd_negative_passes(self) -> None:
        p = _make_position(delta_adjusted_exposure_usd=-50000.0)
        assert p.delta_adjusted_exposure_usd == -50000.0

    def test_delta_adjusted_exposure_usd_inf_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_position(delta_adjusted_exposure_usd=float("inf"))


# ---------------------------------------------------------------------------
# Spin-off invariant tests
# ---------------------------------------------------------------------------


class TestSpinOffInvariant:
    def test_origin_without_parent_position_id_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                origin="SPIN",
                parent_position_id=None,
                corporate_action_adjustment_needed=True,
            )
        assert "parent_position_id" in str(exc_info.value)

    def test_origin_without_corporate_action_flag_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_position(
                origin="SPIN",
                parent_position_id="POS-AAPL-000",
                corporate_action_adjustment_needed=False,
            )
        assert "corporate_action_adjustment_needed" in str(exc_info.value)

    def test_all_spinoff_fields_set_passes(self) -> None:
        p = _make_position(
            origin="SPIN",
            parent_position_id="POS-AAPL-000",
            corporate_action_adjustment_needed=True,
        )
        assert p.origin == "SPIN"
        assert p.parent_position_id == "POS-AAPL-000"
