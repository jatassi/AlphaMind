"""Tests for position records (story 03a)."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LiveExecutionEstimate,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
    resolve_ticker,
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
        with pytest.raises(FrozenInstanceError):
            g.delta = 0.9  # type: ignore[misc]

    def test_required_fields_enforced(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            OptionGreeks(delta=0.5)  # type: ignore[call-arg]

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
        with pytest.raises((ValueError, TypeError)):
            OptionGreeks(
                delta=0.5,
                gamma=0.1,
                theta=-0.02,
                vega=0.3,
                as_of_timestamp=datetime.now(),  # noqa: DTZ005
            )

    def test_tz_aware_datetime_accepted(self) -> None:
        """(c) tz-aware datetime is accepted."""
        ts = datetime.now(tz=UTC)
        g = OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, as_of_timestamp=ts)
        assert g.as_of_timestamp == ts

    def test_zero_iv_used_rejected(self) -> None:
        """(d) iv_used=0.0 raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            OptionGreeks(delta=0.5, gamma=0.1, theta=-0.02, vega=0.3, iv_used=0.0)

    def test_negative_iv_used_rejected(self) -> None:
        """(d) iv_used=-0.1 raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
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
        with pytest.raises(FrozenInstanceError):
            g.as_of_timestamp = datetime.now(tz=UTC)  # type: ignore[misc]

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
            fill_price=price(100.0),
            fill_quantity=10.0,
            slippage=signed_money(0.01),
            fees=money(1.50),
        )
        assert fill.fill_timestamp == ts
        assert fill.fill_price == 100.0

    def test_frozen(self) -> None:
        fill = PositionFill(
            fill_timestamp=self._now_utc(),
            fill_price=price(100.0),
            fill_quantity=10.0,
            slippage=signed_money(0.01),
            fees=money(1.50),
        )
        with pytest.raises(FrozenInstanceError):
            fill.fill_price = price(200.0)  # type: ignore[misc]

    def test_required_fields_enforced(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PositionFill(fill_price=price(100.0))  # type: ignore[call-arg]

    def test_live_execution_estimate_defaults_to_none(self) -> None:
        """(a) live_execution_estimate defaults to None (live-mode case)."""
        fill = PositionFill(
            fill_timestamp=self._now_utc(),
            fill_price=price(100.0),
            fill_quantity=10.0,
            slippage=signed_money(0.01),
            fees=money(1.50),
        )
        assert fill.live_execution_estimate is None

    def test_live_execution_estimate_populated_succeeds(self) -> None:
        """(b) Populating live_execution_estimate (paper-mode case) succeeds."""
        est = _make_live_estimate()
        fill = PositionFill(
            fill_timestamp=self._now_utc(),
            fill_price=price(150.0),
            fill_quantity=10.0,
            slippage=signed_money(0.01),
            fees=money(0.50),
            live_execution_estimate=est,
        )
        assert fill.live_execution_estimate is est

    def test_negative_fees_rejected(self) -> None:
        """(c) Negative fees raises ValidationError (newly enforced constraint)."""
        with pytest.raises((ValueError, TypeError)):
            PositionFill(
                fill_timestamp=self._now_utc(),
                fill_price=price(100.0),
                fill_quantity=10.0,
                slippage=signed_money(0.01),
                fees=money(-1.0),
            )

    def test_docstring_includes_slippage_sign_convention(self) -> None:
        """PositionFill docstring includes slippage and fees sign-convention paragraphs."""
        doc = PositionFill.__doc__ or ""
        assert "slippage" in doc
        assert "fees" in doc


class TestEquityPositionDetails:
    def test_long_construction(self) -> None:
        d = EquityPositionDetails(
            ticker=Symbol("AAPL"),
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
            ticker=Symbol("AAPL"),
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
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
        )
        with pytest.raises(FrozenInstanceError):
            d.ticker = Symbol("MSFT")  # type: ignore[misc]

    def test_required_fields_enforced(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EquityPositionDetails(ticker=Symbol("AAPL"))  # type: ignore[call-arg]


_GREEKS = OptionGreeks(delta=0.5, gamma=0.05, theta=-0.01, vega=0.2)
_EXP = date(2025, 6, 20)


def _make_options_details(**overrides: object) -> OptionsPositionDetails:
    kwargs: dict[str, Any] = {
        "underlying_ticker": "AAPL",
        "strike_price": 200.0,
        "expiration_date": _EXP,
        "contract_type": OptionContractType.CALL,
        "contract_count": 2.0,
        "contract_multiplier": LISTED_OPTION_CONTRACT_MULTIPLIER,
        "premium_paid_per_contract": 5.0,
        "greeks": _GREEKS,
    }
    kwargs.update(overrides)
    return OptionsPositionDetails(**kwargs)


class TestOptionsPositionDetails:
    def test_valid_construction(self) -> None:
        d = _make_options_details()
        assert d.underlying_ticker == "AAPL"
        assert d.contract_type == OptionContractType.CALL
        assert d.greeks.delta == 0.5

    def test_frozen(self) -> None:
        d = _make_options_details()
        with pytest.raises(FrozenInstanceError):
            d.underlying_ticker = Symbol("MSFT")  # type: ignore[misc]

    def test_required_fields_enforced(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            OptionsPositionDetails(underlying_ticker=Symbol("AAPL"))  # type: ignore[call-arg]


def _make_strategy_leg(**overrides: object) -> StrategyLeg:
    kwargs: dict[str, Any] = {
        "leg_id": "leg-1",
        "options": _make_options_details(),
    }
    kwargs.update(overrides)
    return StrategyLeg(**kwargs)


class TestStrategyLeg:
    def test_valid_construction(self) -> None:
        leg = _make_strategy_leg()
        assert leg.leg_id == "leg-1"
        assert leg.options.underlying_ticker == "AAPL"

    def test_frozen(self) -> None:
        leg = _make_strategy_leg()
        with pytest.raises(FrozenInstanceError):
            leg.leg_id = "leg-2"  # type: ignore[misc]


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
        with pytest.raises(FrozenInstanceError):
            d.strategy_type_label = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PositionRecord helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_FILL = PositionFill(
    fill_timestamp=_NOW,
    fill_price=price(150.0),
    fill_quantity=100.0,
    slippage=signed_money(0.01),
    fees=money(1.0),
)
_LONG_EQUITY = EquityPositionDetails(
    ticker=Symbol("AAPL"),
    share_count=100.0,
    average_cost_basis_per_share=150.0,
)
_SHORT_EQUITY = EquityPositionDetails(
    ticker=Symbol("AAPL"),
    share_count=100.0,
    average_cost_basis_per_share=150.0,
    borrow_rate_pct=0.5,
    locate_status=LocateStatus.LOCATED,
    margin_held_usd=5000.0,
)
_OPTIONS_DETAILS = _make_options_details()


def _make_position(**overrides: object) -> PositionRecord:
    """Build a valid open long equity PositionRecord (post-05a — persistent fields only)."""
    kwargs: dict[str, Any] = {
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


# ---------------------------------------------------------------------------
# Discriminator tests
# ---------------------------------------------------------------------------


class TestPositionRecordDiscriminator:
    def test_equity_with_equity_details_passes(self) -> None:
        p = _make_position()
        assert isinstance(p.details, EquityPositionDetails)
        assert p.details.instrument_type == InstrumentType.EQUITY

    def test_options_with_options_details_passes(self) -> None:
        p = _make_position(
            details=_OPTIONS_DETAILS,
            direction=Direction.LONG,
            execution_history=(_FILL,),
        )
        assert isinstance(p.details, OptionsPositionDetails)

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
        p = _make_position(details=strat, direction=Direction.LONG)
        assert isinstance(p.details, StrategyPositionDetails)

    def test_construction_from_dict_no_longer_supported(self) -> None:
        """Post-Pydantic dataclass: dict-payload discriminator parsing now lives in
        the codec layer (``state/tables/positions_codec.py``); callers construct
        each variant via its concrete dataclass directly. Documenting the
        boundary so this regression is intentional, not silent."""
        # The dataclass accepts the dict but it stays a dict (no auto-conversion).
        p = _make_position(
            details={
                "instrument_type": "EQUITY",
                "ticker": "AAPL",
                "share_count": 100.0,
                "average_cost_basis_per_share": 150.0,
            }
        )
        assert not isinstance(p.details, EquityPositionDetails)

    def test_concrete_instance_passes_through(self) -> None:
        """The dataclass stores the variant the caller hands it without conversion."""
        equity = EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
        )
        p = _make_position(details=equity)
        assert isinstance(p.details, EquityPositionDetails)
        assert p.details.ticker == "AAPL"


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
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(
                status=PositionStatus.PENDING,
                entry_timestamp=None,
                execution_history=(_FILL,),
            )
        assert "execution_history" in str(exc_info.value)

    def test_pending_strategy_with_per_leg_fills_passes(self) -> None:
        """Strategy positions accumulate per-leg fills in execution_history while
        the parent stays PENDING — the atomic PENDING → OPEN transition fires
        only when every leg has reached `filled` status (broker-adapter.md §
        Multi-leg fill events § Atomicity)."""
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
            status=PositionStatus.PENDING,
            entry_timestamp=None,
            details=strat,
            direction=Direction.LONG,
            execution_history=(_FILL,),
        )
        assert p.status == PositionStatus.PENDING
        assert len(p.execution_history) == 1


class TestOpenStatusRules:
    def test_open_with_fills_passes(self) -> None:
        p = _make_position(execution_history=(_FILL,))
        assert p.status == PositionStatus.OPEN

    def test_open_with_empty_execution_history_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(status=PositionStatus.OPEN, execution_history=())
        assert "execution_history" in str(exc_info.value)


class TestClosedStatusRules:
    def test_closed_with_realized_pnl_passes(self) -> None:
        p = _make_position(
            status=PositionStatus.CLOSED,
            realized_pnl_to_date_usd=500.0,
        )
        assert p.status == PositionStatus.CLOSED

    def test_closed_without_realized_pnl_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(
                status=PositionStatus.CLOSED,
                realized_pnl_to_date_usd=None,
            )
        assert "realized_pnl_to_date_usd" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Direction / equity short-only fields tests
# ---------------------------------------------------------------------------


class TestDirectionShortFields:
    def test_long_with_no_short_fields_passes(self) -> None:
        p = _make_position(direction=Direction.LONG, details=_LONG_EQUITY)
        assert isinstance(p.details, EquityPositionDetails)
        assert p.details.borrow_rate_pct is None

    def test_long_with_short_fields_populated_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(direction=Direction.LONG, details=_SHORT_EQUITY)
        assert "borrow_rate_pct" in str(exc_info.value) or "locate_status" in str(exc_info.value)

    def test_short_with_all_short_fields_passes(self) -> None:
        p = _make_position(direction=Direction.SHORT, details=_SHORT_EQUITY)
        assert isinstance(p.details, EquityPositionDetails)
        assert p.details.locate_status == LocateStatus.LOCATED

    def test_short_with_missing_borrow_rate_fails(self) -> None:
        partial = EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
            borrow_rate_pct=None,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=5000.0,
        )
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(direction=Direction.SHORT, details=partial)
        assert "borrow_rate_pct" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Range constraint tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spin-off invariant tests
# ---------------------------------------------------------------------------


class TestSpinOffInvariant:
    def test_origin_without_parent_position_id_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(
                origin="SPIN",
                parent_position_id=None,
                corporate_action_adjustment_needed=True,
            )
        assert "parent_position_id" in str(exc_info.value)

    def test_origin_without_corporate_action_flag_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_position(
                origin="SPIN",
                parent_position_id=PositionId("POS-AAPL-000"),
                corporate_action_adjustment_needed=False,
            )
        assert "corporate_action_adjustment_needed" in str(exc_info.value)

    def test_all_spinoff_fields_set_passes(self) -> None:
        p = _make_position(
            origin="SPIN",
            parent_position_id=PositionId("POS-AAPL-000"),
            corporate_action_adjustment_needed=True,
        )
        assert p.origin == "SPIN"
        assert p.parent_position_id == "POS-AAPL-000"


# ---------------------------------------------------------------------------
# LiveExecutionEstimate tests
# ---------------------------------------------------------------------------


def _make_live_estimate(**overrides: object) -> LiveExecutionEstimate:
    kwargs: dict[str, Any] = {
        "estimated_spread_usd": 0.05,
        "estimated_impact_usd": 0.02,
        "estimated_regulatory_fees_usd": 0.01,
        "live_adjusted_fill_price": 149.92,
    }
    kwargs.update(overrides)
    return LiveExecutionEstimate(**kwargs)


class TestLiveExecutionEstimate:
    def test_happy_path(self) -> None:
        """(a) Valid construction succeeds with all four fields."""
        est = _make_live_estimate()
        assert est.estimated_spread_usd == 0.05
        assert est.estimated_impact_usd == 0.02
        assert est.estimated_regulatory_fees_usd == 0.01
        assert est.live_adjusted_fill_price == 149.92

    def test_negative_spread_rejected(self) -> None:
        """(b) Negative estimated_spread_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_spread_usd=money(-0.1))

    def test_negative_impact_rejected(self) -> None:
        """(b) Negative estimated_impact_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_impact_usd=money(-0.01))

    def test_negative_regulatory_fees_rejected(self) -> None:
        """(b) Negative estimated_regulatory_fees_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_regulatory_fees_usd=money(-0.005))

    def test_nan_spread_rejected(self) -> None:
        """(c) nan in estimated_spread_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_spread_usd=money(float("nan")))

    def test_inf_impact_rejected(self) -> None:
        """(c) inf in estimated_impact_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_impact_usd=money(float("inf")))

    def test_nan_regulatory_fees_rejected(self) -> None:
        """(c) nan in estimated_regulatory_fees_usd raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(estimated_regulatory_fees_usd=money(float("nan")))

    def test_inf_live_adjusted_fill_price_rejected(self) -> None:
        """(c) inf in live_adjusted_fill_price raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(live_adjusted_fill_price=price(float("inf")))

    def test_nan_live_adjusted_fill_price_rejected(self) -> None:
        """(c) nan in live_adjusted_fill_price raises ValidationError."""
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_live_estimate(live_adjusted_fill_price=price(float("nan")))

    def test_frozen(self) -> None:
        """(d) Model is frozen — mutation raises FrozenInstanceError."""
        est = _make_live_estimate()
        with pytest.raises(FrozenInstanceError):
            est.estimated_spread_usd = money(1.0)  # type: ignore[misc]

    def test_zero_cost_fields_accepted(self) -> None:
        """Cost fields accept exactly zero (ge=0.0 boundary)."""
        est = _make_live_estimate(
            estimated_spread_usd=money(0.0),
            estimated_impact_usd=money(0.0),
            estimated_regulatory_fees_usd=money(0.0),
        )
        assert est.estimated_spread_usd == 0.0

    def test_negative_live_adjusted_fill_price_rejected(self) -> None:
        """ALP-489 — live_adjusted_fill_price is ``Price`` (strictly positive)."""
        with pytest.raises(ValueError, match="strictly positive"):
            _make_live_estimate(live_adjusted_fill_price=price(-5.0))

    def test_docstring_includes_sign_convention(self) -> None:
        """LiveExecutionEstimate docstring documents cost/sign convention."""
        doc = LiveExecutionEstimate.__doc__ or ""
        assert "positive" in doc
        assert "estimated_spread_usd" in doc
        assert "live_adjusted_fill_price" in doc


# ---------------------------------------------------------------------------
# resolve_ticker tests (ALP-406)
# ---------------------------------------------------------------------------


class TestResolveTicker:
    def test_equity_returns_ticker(self) -> None:
        details = EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=100.0,
            average_cost_basis_per_share=150.0,
        )
        assert resolve_ticker(details) == "AAPL"

    def test_options_returns_underlying_ticker(self) -> None:
        details = _make_options_details(underlying_ticker=Symbol("MSFT"))
        assert resolve_ticker(details) == "MSFT"

    def test_strategy_returns_first_leg_underlying_ticker(self) -> None:
        leg = _make_strategy_leg(
            options=_make_options_details(underlying_ticker=Symbol("SPY")),
        )
        details = StrategyPositionDetails(
            strategy_type_label="bull_call_spread",
            legs=(leg,),
            net_premium_usd=-100.0,
            max_profit_usd=200.0,
            max_loss_usd=-100.0,
            breakeven_levels=(450.0,),
            strategy_greeks=_GREEKS,
        )
        assert resolve_ticker(details) == "SPY"

    def test_strategy_with_empty_legs_returns_none(self) -> None:
        details = StrategyPositionDetails(
            strategy_type_label="iron_condor",
            legs=(),
            net_premium_usd=-100.0,
            max_profit_usd=200.0,
            max_loss_usd=-500.0,
            breakeven_levels=(),
            strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )
        assert resolve_ticker(details) is None
