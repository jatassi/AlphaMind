"""Tests for order and bracket records (story 03c)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    OrderId,
    Symbol,
)
from alphamind._kernel.money import price
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    EventTrigger,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PLAnchorSpec,
    PriceParameters,
    PriceTrigger,
    StrategyInstrumentSpec,
    TimeTrigger,
    direction_to_side,
    order_direction,
)
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
TODAY = date(2025, 6, 15)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _equity_spec() -> EquityInstrumentSpec:
    return EquityInstrumentSpec(ticker=Symbol("AAPL"))


def _options_spec() -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=Symbol("AAPL"),
        strike=150.0,
        expiration=TODAY,
        contract_type=OptionContractType.CALL,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
    )


def _strategy_spec() -> StrategyInstrumentSpec:
    return StrategyInstrumentSpec(legs=(_options_spec(),))


def _market_price_params() -> PriceParameters:
    return PriceParameters(limit_price=None, stop_trigger_price=None)


def _make_order(**overrides: object) -> OrderRecord:
    """Build a valid MARKET OrderRecord, with optional overrides."""
    base: dict[str, Any] = {
        "order_id": "ord-1",
        "position_id": None,
        "bracket_id": "brk-1",
        "role": OrderRole.ENTRY,
        "instrument_spec": _equity_spec(),
        "direction": OrderDirection.BUY,
        "order_type": OrderType.MARKET,
        "price_parameters": _market_price_params(),
        "quantity": 10.0,
        "duration": OrderDuration.DAY,
        "status": OrderStatus.PENDING,
        "alpaca_order_id": "alp-1",
        "alpaca_order_id_chain": ("alp-1",),
        "submission_timestamp": NOW,
        "last_update_timestamp": NOW,
        "filled_quantity": 0.0,
        "avg_fill_price": None,
        "remaining_quantity": 10.0,
        "modification_count": 0,
        "originating_thesis_id": None,
        "originating_pm_command_id": None,
        "age_hours": 1.0,
    }
    base.update(overrides)
    return OrderRecord(**base)


def _trigger_for(leg_type: BracketLegType) -> PriceTrigger | TimeTrigger | EventTrigger:
    """Build the canonical typed trigger payload for a given leg_type."""
    if leg_type == BracketLegType.TAKE_PROFIT:
        return PriceTrigger(underlying_ticker=Symbol("AAPL"), threshold_usd=160.0, direction="GTE")
    if leg_type == BracketLegType.PRICE_STOP:
        return PriceTrigger(underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE")
    if leg_type == BracketLegType.TIME_EXPIRATION:
        return TimeTrigger(deadline=datetime(2025, 6, 1, 16, 0, tzinfo=UTC))
    return EventTrigger(description="thesis invalidated")


def _make_mechanical_leg(
    leg_id: str = "leg-1",
    leg_type: BracketLegType = BracketLegType.PRICE_STOP,
    status: BracketLegStatus = BracketLegStatus.PENDING_ACTIVATION,
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=leg_type,
        order_id=OrderId("ord-stop"),
        trigger=_trigger_for(leg_type),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=status,
    )


def _make_modification() -> BracketLegModification:
    return BracketLegModification(
        timestamp=NOW,
        pm_command_id=None,
        source="fill-anchor-recalculation",
        field_changed="trigger",
        old_value="price < 140.0",
        new_value="price < 138.5",
        rationale="fill anchor recalculated after entry fill",
    )


def _make_bracket(**overrides: object) -> BracketRecord:
    base: dict[str, Any] = {
        "bracket_id": "brk-1",
        "position_id": "pos-1",
        "status": BracketStatus.PENDING_ENTRY,
        "entry_order_id": "ord-entry",
        "protective_legs": (_make_mechanical_leg(),),
        "modification_history": (),
        "corporate_action_cancellation_reason": None,
    }
    base.update(overrides)
    return BracketRecord(**base)


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestOrderClass:
    def test_members(self) -> None:
        assert set(OrderClass) == {
            OrderClass.SIMPLE,
            OrderClass.BRACKET,
            OrderClass.OCO,
            OrderClass.OTO,
            OrderClass.MLEG,
        }


class TestOrderRole:
    def test_members(self) -> None:
        assert set(OrderRole) == {
            OrderRole.ENTRY,
            OrderRole.TAKE_PROFIT,
            OrderRole.PRICE_STOP,
            OrderRole.TIME_STOP,
            OrderRole.CLOSE,
            OrderRole.ADD_ENTRY,
        }


class TestOrderType:
    def test_members(self) -> None:
        assert set(OrderType) == {
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.STOP,
            OrderType.STOP_LIMIT,
        }


class TestOrderDirection:
    def test_members(self) -> None:
        assert set(OrderDirection) == {
            OrderDirection.BUY,
            OrderDirection.SELL,
            OrderDirection.BUY_TO_OPEN,
            OrderDirection.SELL_TO_OPEN,
            OrderDirection.BUY_TO_CLOSE,
            OrderDirection.SELL_TO_CLOSE,
        }


class TestOrderDuration:
    def test_members(self) -> None:
        assert set(OrderDuration) == {
            OrderDuration.DAY,
            OrderDuration.GTC,
            OrderDuration.GTD,
        }

    def test_exact_member_set(self) -> None:
        """Pinned set — any re-addition of IOC/FOK or new member trips this test."""
        assert {m.value for m in OrderDuration} == {"DAY", "GTC", "GTD"}


class TestOrderStatus:
    def test_members(self) -> None:
        assert set(OrderStatus) == {
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }


class TestBracketStatus:
    def test_members(self) -> None:
        assert set(BracketStatus) == {
            BracketStatus.PENDING_ENTRY,
            BracketStatus.ACTIVE,
            BracketStatus.COMPLETED,
            BracketStatus.DISSOLVED,
        }


class TestBracketLegType:
    def test_members(self) -> None:
        assert set(BracketLegType) == {
            BracketLegType.TAKE_PROFIT,
            BracketLegType.PRICE_STOP,
            BracketLegType.TIME_EXPIRATION,
            BracketLegType.EVENT_INVALIDATION,
        }

    def test_importable_from_orders(self) -> None:
        """BracketLegType must be importable from orders module (canonical declaration site)."""
        from alphamind.portfolio_state.records.orders import BracketLegType

        assert BracketLegType.TAKE_PROFIT is BracketLegType.TAKE_PROFIT


class TestBracketLegEnforcement:
    def test_members(self) -> None:
        assert set(BracketLegEnforcement) == {
            BracketLegEnforcement.MECHANICAL,
            BracketLegEnforcement.ADVISORY,
        }


class TestBracketLegStatus:
    def test_members(self) -> None:
        assert set(BracketLegStatus) == {
            BracketLegStatus.PENDING_ACTIVATION,
            BracketLegStatus.ACTIVE,
            BracketLegStatus.TRIGGERED,
            BracketLegStatus.CANCELLED,
        }


# ---------------------------------------------------------------------------
# InstrumentSpec discriminator tests
# ---------------------------------------------------------------------------


class TestInstrumentSpecDiscriminator:
    def test_equity_passes(self) -> None:
        spec = _equity_spec()
        assert isinstance(spec, EquityInstrumentSpec)
        assert spec.instrument_type == InstrumentType.EQUITY
        assert spec.ticker == "AAPL"

    def test_options_passes(self) -> None:
        spec = _options_spec()
        assert isinstance(spec, OptionsInstrumentSpec)
        assert spec.instrument_type == InstrumentType.OPTIONS
        assert spec.underlying == "AAPL"
        assert spec.strike == 150.0

    def test_strategy_passes(self) -> None:
        spec = _strategy_spec()
        assert isinstance(spec, StrategyInstrumentSpec)
        assert spec.instrument_type == InstrumentType.STRATEGY
        assert spec.legs is not None
        assert len(spec.legs) == 1

    def test_equity_without_ticker_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EquityInstrumentSpec(instrument_type="EQUITY")  # type: ignore[call-arg]

    def test_strategy_with_empty_legs_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            StrategyInstrumentSpec(legs=())

    def test_strategy_with_none_legs_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            StrategyInstrumentSpec(instrument_type="STRATEGY")  # type: ignore[call-arg]

    def test_bogus_discriminator_no_longer_validated_at_construction(self) -> None:
        """Post-Pydantic dataclass: union discriminator parsing now lives in the
        codec layer (``state/tables/orders_codec.py``); callers construct each
        variant via its concrete dataclass directly."""
        # The codec raises on bogus discriminators.
        from alphamind.state.tables.orders_codec import _instrument_spec_from_dict

        with pytest.raises((ValueError, TypeError)):
            _instrument_spec_from_dict({"instrument_type": "BOGUS"})


# ---------------------------------------------------------------------------
# PriceParameters cross-validation (enforced at OrderRecord level)
# ---------------------------------------------------------------------------


class TestPriceParametersCrossValidation:
    def test_market_both_none_passes(self) -> None:
        order = _make_order(order_type=OrderType.MARKET, price_parameters=_market_price_params())
        assert order.order_type == OrderType.MARKET

    def test_limit_with_limit_price_passes(self) -> None:
        pp = PriceParameters(limit_price=price("150.0"), stop_trigger_price=None)
        order = _make_order(order_type=OrderType.LIMIT, price_parameters=pp)
        assert order.price_parameters.limit_price == price("150.0")

    def test_stop_with_stop_trigger_passes(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=price("140.0"))
        order = _make_order(order_type=OrderType.STOP, price_parameters=pp)
        assert order.price_parameters.stop_trigger_price == price("140.0")

    def test_stop_limit_both_set_passes(self) -> None:
        pp = PriceParameters(limit_price=price("149.0"), stop_trigger_price=price("148.0"))
        order = _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)
        assert order.price_parameters.limit_price == price("149.0")
        assert order.price_parameters.stop_trigger_price == price("148.0")

    def test_limit_missing_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=None)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(order_type=OrderType.LIMIT, price_parameters=pp)

    def test_stop_missing_stop_trigger_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=None)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(order_type=OrderType.STOP, price_parameters=pp)

    def test_market_with_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=price("150.0"), stop_trigger_price=None)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(order_type=OrderType.MARKET, price_parameters=pp)

    def test_stop_limit_missing_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=price("148.0"))
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)

    def test_stop_limit_missing_stop_trigger_fails(self) -> None:
        pp = PriceParameters(limit_price=price("149.0"), stop_trigger_price=None)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)


# ---------------------------------------------------------------------------
# Quantity constraints
# ---------------------------------------------------------------------------


class TestQuantityConstraints:
    def test_quantity_zero_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(quantity=0.0, remaining_quantity=0.0)

    def test_quantity_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(quantity=-1.0, remaining_quantity=-1.0)

    def test_filled_quantity_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(filled_quantity=-0.1, remaining_quantity=10.1)

    def test_remaining_quantity_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(remaining_quantity=-1.0, filled_quantity=11.0)

    def test_modification_count_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(modification_count=-1)


# ---------------------------------------------------------------------------
# Quantity invariant
# ---------------------------------------------------------------------------


class TestQuantityInvariant:
    def test_non_terminal_filled_plus_remaining_equals_quantity_passes(self) -> None:
        order = _make_order(
            status=OrderStatus.PARTIALLY_FILLED,
            quantity=10.0,
            filled_quantity=4.0,
            remaining_quantity=6.0,
            avg_fill_price=151.0,
        )
        assert order.filled_quantity + order.remaining_quantity == order.quantity

    def test_non_terminal_invariant_violated_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_order(
                status=OrderStatus.PARTIALLY_FILLED,
                quantity=10.0,
                filled_quantity=4.0,
                remaining_quantity=5.0,  # should be 6.0
                avg_fill_price=151.0,
            )

    def test_rejected_filled_zero_remaining_equals_quantity_passes(self) -> None:
        order = _make_order(
            status=OrderStatus.REJECTED,
            quantity=10.0,
            filled_quantity=0.0,
            remaining_quantity=10.0,
        )
        assert order.status == OrderStatus.REJECTED

    def test_rejected_filled_nonzero_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_order(
                status=OrderStatus.REJECTED,
                quantity=10.0,
                filled_quantity=2.0,
                remaining_quantity=8.0,
            )

    def test_cancelled_partial_fill_passes(self) -> None:
        order = _make_order(
            status=OrderStatus.CANCELLED,
            quantity=10.0,
            filled_quantity=3.0,
            remaining_quantity=7.0,
            avg_fill_price=149.0,
        )
        assert order.filled_quantity + order.remaining_quantity == order.quantity


# ---------------------------------------------------------------------------
# Alpaca order ID chain rules
# ---------------------------------------------------------------------------


class TestAlpacaOrderIdChain:
    def test_non_empty_chain_passes(self) -> None:
        order = _make_order(
            alpaca_order_id=AlpacaOrderId("alp-2"), alpaca_order_id_chain=("alp-1", "alp-2")
        )
        assert order.alpaca_order_id == "alp-2"
        assert order.alpaca_order_id_chain[-1] == "alp-2"

    def test_empty_chain_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(alpaca_order_id=AlpacaOrderId("alp-1"), alpaca_order_id_chain=())

    def test_mismatch_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_order(
                alpaca_order_id=AlpacaOrderId("alp-1"),
                alpaca_order_id_chain=("alp-1", "alp-2"),  # last is alp-2, not alp-1
            )


# ---------------------------------------------------------------------------
# age_hours constraint
# ---------------------------------------------------------------------------


class TestAgeHours:
    def test_age_hours_zero_passes(self) -> None:
        order = _make_order(age_hours=0.0)
        assert order.age_hours == 0.0

    def test_age_hours_positive_passes(self) -> None:
        order = _make_order(age_hours=2.5)
        assert order.age_hours == 2.5

    def test_age_hours_negative_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_order(age_hours=-0.1)


# ---------------------------------------------------------------------------
# BracketLeg EVENT_INVALIDATION rule
# ---------------------------------------------------------------------------


class TestBracketLegEventInvalidation:
    def test_event_invalidation_with_none_order_id_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=EventTrigger(description="thesis invalidation event"),
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        assert leg.order_id is None

    def test_event_invalidation_with_non_none_order_id_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id=OrderId("ord-1"),  # must be None for EVENT_INVALIDATION
                trigger=EventTrigger(description="thesis invalidation event"),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.PENDING_ACTIVATION,
            )

    def test_non_event_invalidation_may_have_order_id(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord-stop"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        assert leg.order_id == "ord-stop"


# ---------------------------------------------------------------------------
# BracketRecord protective_legs non-empty rule
# ---------------------------------------------------------------------------


class TestBracketRecordProtectiveLegsNonEmpty:
    def test_non_empty_passes(self) -> None:
        bracket = _make_bracket()
        assert len(bracket.protective_legs) == 1

    def test_empty_fails(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_bracket(protective_legs=())


# ---------------------------------------------------------------------------
# Hard-backstop rule
# ---------------------------------------------------------------------------


class TestHardBackstopRule:
    def test_mechanical_price_stop_passes(self) -> None:
        bracket = _make_bracket(
            protective_legs=(_make_mechanical_leg(leg_type=BracketLegType.PRICE_STOP),)
        )
        assert bracket.bracket_id == "brk-1"

    def test_mechanical_take_profit_passes(self) -> None:
        bracket = _make_bracket(
            protective_legs=(_make_mechanical_leg(leg_type=BracketLegType.TAKE_PROFIT),)
        )
        assert bracket.bracket_id == "brk-1"

    def test_mechanical_time_expiration_passes(self) -> None:
        bracket = _make_bracket(
            protective_legs=(_make_mechanical_leg(leg_type=BracketLegType.TIME_EXPIRATION),)
        )
        assert bracket.bracket_id == "brk-1"

    def test_only_event_invalidation_advisory_fails(self) -> None:
        advisory_event_leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=EventTrigger(description="thesis invalidated"),
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_bracket(protective_legs=(advisory_event_leg,))

    def test_only_advisory_mechanical_type_fails(self) -> None:
        advisory_stop = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord-stop"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.ADVISORY,  # advisory, not mechanical
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_bracket(protective_legs=(advisory_stop,))

    def test_mixed_with_at_least_one_mechanical_passes(self) -> None:
        advisory_event_leg = BracketLeg(
            leg_id="leg-2",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=EventTrigger(description="thesis invalidated"),
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        bracket = _make_bracket(
            protective_legs=(
                _make_mechanical_leg("leg-1"),
                advisory_event_leg,
            )
        )
        assert len(bracket.protective_legs) == 2


# ---------------------------------------------------------------------------
# PENDING_ENTRY rule
# ---------------------------------------------------------------------------


class TestPendingEntryRule:
    def test_all_pending_activation_passes(self) -> None:
        bracket = _make_bracket(
            status=BracketStatus.PENDING_ENTRY,
            protective_legs=(_make_mechanical_leg(status=BracketLegStatus.PENDING_ACTIVATION),),
        )
        assert bracket.status == BracketStatus.PENDING_ENTRY

    def test_leg_not_pending_activation_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_bracket(
                status=BracketStatus.PENDING_ENTRY,
                protective_legs=(_make_mechanical_leg(status=BracketLegStatus.ACTIVE),),
            )


# ---------------------------------------------------------------------------
# DISSOLVED rule
# ---------------------------------------------------------------------------


class TestDissolvedRule:
    def test_all_cancelled_passes(self) -> None:
        bracket = _make_bracket(
            status=BracketStatus.DISSOLVED,
            protective_legs=(_make_mechanical_leg(status=BracketLegStatus.CANCELLED),),
        )
        assert bracket.status == BracketStatus.DISSOLVED

    def test_leg_not_cancelled_fails(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_bracket(
                status=BracketStatus.DISSOLVED,
                protective_legs=(_make_mechanical_leg(status=BracketLegStatus.TRIGGERED),),
            )


# ---------------------------------------------------------------------------
# OrderRecord order_class field and MLEG cross-validation
# ---------------------------------------------------------------------------


class TestOrderRecordOrderClass:
    def test_default_order_class_is_simple(self) -> None:
        order = _make_order()
        assert order.order_class == OrderClass.SIMPLE

    def test_mleg_with_equity_spec_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _make_order(order_class=OrderClass.MLEG, instrument_spec=_equity_spec(), direction=None)
        assert "STRATEGY" in str(exc_info.value)

    def test_mleg_with_strategy_spec_passes(self) -> None:
        order = _make_order(
            order_class=OrderClass.MLEG, instrument_spec=_strategy_spec(), direction=None
        )
        assert order.order_class == OrderClass.MLEG
        assert order.direction is None

    def test_bracket_with_equity_spec_passes(self) -> None:
        order = _make_order(order_class=OrderClass.BRACKET, instrument_spec=_equity_spec())
        assert order.order_class == OrderClass.BRACKET

    def test_oco_with_equity_spec_passes(self) -> None:
        order = _make_order(order_class=OrderClass.OCO, instrument_spec=_equity_spec())
        assert order.order_class == OrderClass.OCO

    def test_oto_with_equity_spec_passes(self) -> None:
        order = _make_order(order_class=OrderClass.OTO, instrument_spec=_equity_spec())
        assert order.order_class == OrderClass.OTO


# ---------------------------------------------------------------------------
# entry_window_deadline field (ALP-341)
# ---------------------------------------------------------------------------


class TestBracketRecordEntryWindowDeadline:
    def test_none_default(self) -> None:
        bracket = _make_bracket()
        assert bracket.entry_window_deadline is None

    def test_tz_aware_datetime_accepted(self) -> None:
        deadline = datetime(2026, 5, 6, 18, 0, tzinfo=UTC)
        bracket = _make_bracket(entry_window_deadline=deadline)
        assert bracket.entry_window_deadline == deadline

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_bracket(entry_window_deadline=datetime(2026, 5, 6, 18, 0))  # noqa: DTZ001

    def test_pending_entry_with_deadline_succeeds(self) -> None:
        deadline = datetime(2026, 5, 6, 18, 0, tzinfo=UTC)
        bracket = _make_bracket(
            status=BracketStatus.PENDING_ENTRY,
            entry_window_deadline=deadline,
        )
        assert bracket.status == BracketStatus.PENDING_ENTRY
        assert bracket.entry_window_deadline == deadline

    def test_active_status_with_deadline_informational(self) -> None:
        deadline = datetime(2026, 5, 6, 18, 0, tzinfo=UTC)
        bracket = _make_bracket(
            status=BracketStatus.ACTIVE,
            protective_legs=(_make_mechanical_leg(status=BracketLegStatus.ACTIVE),),
            entry_window_deadline=deadline,
        )
        assert bracket.status == BracketStatus.ACTIVE
        assert bracket.entry_window_deadline == deadline


# ---------------------------------------------------------------------------
# Typed trigger payload classes (ALP-345)
# ---------------------------------------------------------------------------


_LEG_PRICE_TRIGGER_TYPES = (BracketLegType.TAKE_PROFIT, BracketLegType.PRICE_STOP)


class TestPriceTrigger:
    def test_construct_with_valid_fields(self) -> None:
        trigger = PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=800.0, direction="LTE"
        )
        assert trigger.trigger_type == "price"
        assert trigger.underlying_ticker == "NVDA"
        assert trigger.threshold_usd == 800.0
        assert trigger.direction == "LTE"

    def test_negative_threshold_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PriceTrigger(underlying_ticker=Symbol("NVDA"), threshold_usd=-5.0, direction="LTE")

    def test_zero_threshold_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PriceTrigger(underlying_ticker=Symbol("NVDA"), threshold_usd=0.0, direction="LTE")

    def test_empty_ticker_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PriceTrigger(underlying_ticker=Symbol(""), threshold_usd=10.0, direction="LTE")


class TestTimeTrigger:
    def test_construct_with_tz_aware(self) -> None:
        deadline = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
        trigger = TimeTrigger(deadline=deadline)
        assert trigger.trigger_type == "time"
        assert trigger.deadline == deadline

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0))  # noqa: DTZ001


class TestEventTrigger:
    def test_construct_with_description(self) -> None:
        trigger = EventTrigger(description="thesis invalidated")
        assert trigger.trigger_type == "event"
        assert trigger.description == "thesis invalidated"
        assert trigger.condition_evaluator_id is None

    def test_construct_with_condition_evaluator_id(self) -> None:
        trigger = EventTrigger(
            description="thesis invalidated", condition_evaluator_id="evaluator-1"
        )
        assert trigger.condition_evaluator_id == "evaluator-1"

    def test_empty_description_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            EventTrigger(description="")


class TestBracketLegTriggerLegTypeValidator:
    """leg_type ↔ trigger_type cross-validation."""

    def test_take_profit_with_price_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=OrderId("ord-1"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=200.0, direction="GTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, PriceTrigger)

    def test_price_stop_with_price_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord-1"),
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"), threshold_usd=160.0, direction="LTE"
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, PriceTrigger)

    def test_time_expiration_with_time_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId("ord-1"),
            trigger=TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, TimeTrigger)

    def test_event_invalidation_with_event_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=EventTrigger(description="thesis invalidated"),
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, EventTrigger)

    @pytest.mark.parametrize("leg_type", _LEG_PRICE_TRIGGER_TYPES)
    def test_price_leg_with_time_trigger_rejected(self, leg_type: BracketLegType) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=leg_type,
                order_id=OrderId("ord-1"),
                trigger=TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    @pytest.mark.parametrize("leg_type", _LEG_PRICE_TRIGGER_TYPES)
    def test_price_leg_with_event_trigger_rejected(self, leg_type: BracketLegType) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=leg_type,
                order_id=OrderId("ord-1"),
                trigger=EventTrigger(description="qualitative"),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_time_expiration_with_price_trigger_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.TIME_EXPIRATION,
                order_id=OrderId("ord-1"),
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("NVDA"), threshold_usd=10.0, direction="GTE"
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_time_expiration_with_event_trigger_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.TIME_EXPIRATION,
                order_id=OrderId("ord-1"),
                trigger=EventTrigger(description="qualitative"),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_event_invalidation_with_price_trigger_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("NVDA"), threshold_usd=10.0, direction="GTE"
                ),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.ACTIVE,
            )

    def test_event_invalidation_with_time_trigger_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id=None,
                trigger=TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.ACTIVE,
            )


class TestBracketLegTriggerDiscriminatedUnionRoundTrip:
    """Trigger variants survive an in-memory ``dataclasses.replace`` round-trip.

    The wire round-trip lives in ``state/tables/brackets_codec.py``; these
    tests cover the in-memory contract that callers construct each variant via
    its concrete dataclass.
    """

    def test_price_trigger_in_memory(self) -> None:
        trigger = PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=160.0, direction="LTE"
        )
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId("ord-1"),
            trigger=trigger,
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, PriceTrigger)
        assert leg.trigger.threshold_usd == 160.0

    def test_time_trigger_in_memory(self) -> None:
        deadline = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
        trigger = TimeTrigger(deadline=deadline)
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId("ord-1"),
            trigger=trigger,
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, TimeTrigger)
        assert leg.trigger.deadline == deadline

    def test_event_trigger_in_memory(self) -> None:
        trigger = EventTrigger(description="thesis invalidated")
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=trigger,
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, EventTrigger)
        assert leg.trigger.description == "thesis invalidated"


# ---------------------------------------------------------------------------
# Typed pl_based payload (PLAnchorSpec) — ALP-346
# ---------------------------------------------------------------------------


class TestPLAnchorSpec:
    """PLAnchorSpec captures the P/L percentage spec, planned-entry anchor, and
    fill-recalc state per orders-and-brackets.md § P/L-based bracket legs."""

    def test_construct_target_spec(self) -> None:
        spec = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        assert spec.spec_type == "target"
        assert spec.pct == 0.80
        assert spec.planned_entry_price == 18.50
        assert spec.actual_entry_price is None
        assert spec.recalculated_at_fill is False

    def test_construct_stop_spec(self) -> None:
        spec = PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50)
        assert spec.spec_type == "stop"
        assert spec.pct == 0.30

    def test_construct_recalculated_spec(self) -> None:
        spec = PLAnchorSpec(
            spec_type="target",
            pct=0.80,
            planned_entry_price=18.50,
            actual_entry_price=17.80,
            recalculated_at_fill=True,
        )
        assert spec.actual_entry_price == 17.80
        assert spec.recalculated_at_fill is True

    def test_zero_pct_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PLAnchorSpec(spec_type="target", pct=0.0, planned_entry_price=18.50)

    def test_negative_pct_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PLAnchorSpec(spec_type="target", pct=-0.10, planned_entry_price=18.50)

    def test_pct_above_cap_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PLAnchorSpec(spec_type="target", pct=11.0, planned_entry_price=18.50)

    def test_pct_at_cap_passes(self) -> None:
        spec = PLAnchorSpec(spec_type="target", pct=10.0, planned_entry_price=18.50)
        assert spec.pct == 10.0

    def test_negative_planned_entry_price_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=-5.0)

    def test_zero_planned_entry_price_rejected(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=0.0)

    def test_recalculated_without_actual_price_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PLAnchorSpec(
                spec_type="target",
                pct=0.80,
                planned_entry_price=18.50,
                recalculated_at_fill=True,
            )

    def test_actual_price_without_recalculated_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PLAnchorSpec(
                spec_type="target",
                pct=0.80,
                planned_entry_price=18.50,
                actual_entry_price=17.80,
                recalculated_at_fill=False,
            )

    def test_round_trip(self) -> None:
        spec = PLAnchorSpec(
            spec_type="stop",
            pct=0.30,
            planned_entry_price=18.50,
            actual_entry_price=17.80,
            recalculated_at_fill=True,
        )
        # PLAnchorSpec has only scalar fields, so asdict-rehydrate is faithful.
        rehydrated = PLAnchorSpec(**dataclasses.asdict(spec))
        assert rehydrated == spec


def _make_leg_with_anchor(
    leg_type: BracketLegType,
    pl_anchor: PLAnchorSpec | None,
) -> BracketLeg:
    """Build a BracketLeg with the canonical trigger for leg_type plus pl_anchor."""
    return BracketLeg(
        leg_id="leg-1",
        leg_type=leg_type,
        order_id=None if leg_type == BracketLegType.EVENT_INVALIDATION else OrderId("ord-1"),
        trigger=_trigger_for(leg_type),
        enforcement=(
            BracketLegEnforcement.ADVISORY
            if leg_type == BracketLegType.EVENT_INVALIDATION
            else BracketLegEnforcement.MECHANICAL
        ),
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=pl_anchor,
    )


class TestBracketLegPLAnchor:
    """BracketLeg.pl_anchor field replacing pl_based: bool (ALP-346)."""

    def test_default_pl_anchor_is_none(self) -> None:
        leg = _make_leg_with_anchor(BracketLegType.PRICE_STOP, pl_anchor=None)
        assert leg.pl_anchor is None

    def test_take_profit_with_target_anchor_passes(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        leg = _make_leg_with_anchor(BracketLegType.TAKE_PROFIT, pl_anchor=anchor)
        assert leg.pl_anchor is not None
        assert leg.pl_anchor.spec_type == "target"

    def test_price_stop_with_stop_anchor_passes(self) -> None:
        anchor = PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50)
        leg = _make_leg_with_anchor(BracketLegType.PRICE_STOP, pl_anchor=anchor)
        assert leg.pl_anchor is not None
        assert leg.pl_anchor.spec_type == "stop"

    def test_take_profit_with_stop_spec_type_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="stop", pct=0.30, planned_entry_price=18.50)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_leg_with_anchor(BracketLegType.TAKE_PROFIT, pl_anchor=anchor)

    def test_price_stop_with_target_spec_type_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_leg_with_anchor(BracketLegType.PRICE_STOP, pl_anchor=anchor)

    def test_time_expiration_with_pl_anchor_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_leg_with_anchor(BracketLegType.TIME_EXPIRATION, pl_anchor=anchor)

    def test_event_invalidation_with_pl_anchor_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_leg_with_anchor(BracketLegType.EVENT_INVALIDATION, pl_anchor=anchor)

    def test_in_memory_with_pl_anchor(self) -> None:
        """BracketLeg with pl_anchor is constructible and exposes the typed anchor.

        The wire round-trip (which has to handle the discriminated trigger and
        anchor JSON shapes) is exercised in ``state/tables/brackets_codec.py``.
        """
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        leg = _make_leg_with_anchor(BracketLegType.TAKE_PROFIT, pl_anchor=anchor)
        assert leg.pl_anchor is not None
        assert leg.pl_anchor.spec_type == "target"
        assert leg.pl_anchor.pct == 0.80


# ---------------------------------------------------------------------------
# Buy/sell-side mapping
# ---------------------------------------------------------------------------


class TestDirectionToSide:
    """Buy-vs-sell partition of OrderDirection consumed by phase1 cash-movement
    and the paper-evaluation harness side dispatch."""

    @pytest.mark.parametrize(
        "direction",
        [OrderDirection.BUY, OrderDirection.BUY_TO_OPEN, OrderDirection.BUY_TO_CLOSE],
    )
    def test_buy_directions_map_to_buy(self, direction: OrderDirection) -> None:
        assert direction_to_side(direction) == "buy"

    @pytest.mark.parametrize(
        "direction",
        [OrderDirection.SELL, OrderDirection.SELL_TO_OPEN, OrderDirection.SELL_TO_CLOSE],
    )
    def test_sell_directions_map_to_sell(self, direction: OrderDirection) -> None:
        assert direction_to_side(direction) == "sell"

    def test_every_variant_is_mapped(self) -> None:
        """Iterating ``OrderDirection`` through ``direction_to_side`` must
        succeed for every variant — an unmapped variant raises ``KeyError``
        on the missing dict key, which is the partition's exhaustiveness
        check. The set-equality assertion catches the secondary failure mode
        where a future variant is added to the mapping with a third side."""
        sides = {direction_to_side(d) for d in OrderDirection}
        assert sides == {"buy", "sell"}


# ---------------------------------------------------------------------------
# order_direction() accessor + MLEG ↔ None binding (ALP-614)
# ---------------------------------------------------------------------------


class TestOrderDirectionAccessor:
    """``order_direction()`` is the canonical order-level direction read."""

    def test_equity_order_returns_direction(self) -> None:
        order = _make_order(direction=OrderDirection.BUY)
        assert order_direction(order) == OrderDirection.BUY

    def test_single_leg_options_order_returns_direction(self) -> None:
        order = _make_order(
            direction=OrderDirection.SELL_TO_OPEN,
            instrument_spec=_options_spec(),
        )
        assert order_direction(order) == OrderDirection.SELL_TO_OPEN

    def test_strategy_mleg_order_returns_none(self) -> None:
        """A multi-leg strategy MLEG envelope is neither buy nor sell."""
        order = _make_order(
            order_class=OrderClass.MLEG,
            instrument_spec=_strategy_spec(),
            direction=None,
        )
        assert order_direction(order) is None


class TestOrderDirectionValidator:
    """``direction is None`` iff ``order_class == OrderClass.MLEG``."""

    def test_mleg_with_none_passes(self) -> None:
        order = _make_order(
            order_class=OrderClass.MLEG,
            instrument_spec=_strategy_spec(),
            direction=None,
        )
        assert order.direction is None

    def test_mleg_with_non_none_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="None for an MLEG"):
            _make_order(
                order_class=OrderClass.MLEG,
                instrument_spec=_strategy_spec(),
                direction=OrderDirection.BUY,
            )

    def test_non_mleg_with_none_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="non-None for a non-MLEG"):
            _make_order(order_class=OrderClass.BRACKET, direction=None)

    def test_non_mleg_with_non_none_direction_passes(self) -> None:
        order = _make_order(order_class=OrderClass.SIMPLE, direction=OrderDirection.SELL)
        assert order.direction == OrderDirection.SELL


# ---------------------------------------------------------------------------
# Static-only validation (five collapsed markers — ALP-795)
# ---------------------------------------------------------------------------
#
# Post-Pydantic dataclasses: Literal/type enforcement that Pydantic previously
# applied at construction now lives in mypy (static) or at the codec boundary.
# Each row below confirms the dataclass stores the out-of-contract value
# unchanged — i.e. no runtime guard at construction. This is the single
# canonical record of that design note instead of five scattered dead-green
# copies.
#
# Row IDs mirror the five rows enumerated in §4 of ALP-795.


def _static_only_strategy_legs() -> object:
    """orders / strategy_legs: leg-type enforcement is mypy-only."""
    spec = StrategyInstrumentSpec(legs=(_equity_spec(),))  # type: ignore[arg-type]
    return spec.legs


def _static_only_orders_direction() -> object:
    """orders / direction: Literal['GTE','LTE'] is mypy-only."""
    trigger = PriceTrigger(
        underlying_ticker=Symbol("NVDA"),
        threshold_usd=10.0,
        direction="ABOVE",  # type: ignore[arg-type]
    )
    return trigger.direction


def _static_only_orders_spec_type() -> object:
    """orders / spec_type: Literal['target','stop'] is mypy-only."""
    spec = PLAnchorSpec(
        spec_type="limit",  # type: ignore[arg-type]
        pct=0.80,
        planned_entry_price=18.50,
    )
    return spec.spec_type


def _static_only_positions_construction_from_dict() -> object:
    """positions / construction_from_dict: codec layer owns dict→variant parsing."""
    from datetime import UTC, datetime

    from alphamind._kernel.ids import PositionId as _PositionId
    from alphamind._kernel.ids import ThesisId as _ThesisId
    from alphamind._kernel.money import money, price, signed_money
    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        PositionFill,
        PositionRecord,
        PositionStatus,
    )

    _now = datetime.now(tz=UTC)
    _fill = PositionFill(
        fill_timestamp=_now,
        fill_price=price(150.0),
        fill_quantity=100.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )
    details_dict: Any = {
        "instrument_type": "EQUITY",
        "ticker": "AAPL",
        "share_count": 100.0,
        "average_cost_basis_per_share": 150.0,
    }
    p = PositionRecord(
        position_id=_PositionId("POS-AAPL-001"),
        thesis_id=_ThesisId("THESIS-001"),
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_now,
        details=details_dict,
        execution_history=(_fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    # The dataclass accepts the dict but it stays a dict — no auto-conversion.
    return not isinstance(p.details, EquityPositionDetails)


def _static_only_capital_zone() -> object:
    """capital / zone: zone-string validation lives at the codec boundary."""
    from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetEntry

    entry = RiskBudgetEntry(
        rule_id="r",
        rule_label="R",
        current_value=1.0,
        limit_value=10.0,
        headroom=9.0,
        headroom_pct_of_limit=90.0,
        zone="UNKNOWN_ZONE",  # type: ignore[arg-type]
        unit="pct",
        cumulative_invocation_impact_value=0.0,
    )
    return entry.zone


_StaticOnlyRow = Callable[[], object]

_STATIC_ONLY_ROWS: list[tuple[str, _StaticOnlyRow]] = [
    ("orders/strategy_legs", _static_only_strategy_legs),
    ("orders/direction", _static_only_orders_direction),
    ("orders/spec_type", _static_only_orders_spec_type),
    ("positions/construction_from_dict", _static_only_positions_construction_from_dict),
    ("capital/zone", _static_only_capital_zone),
]


@pytest.mark.parametrize(
    ("row_id", "call"),
    _STATIC_ONLY_ROWS,
)
def test_static_only_validation(row_id: str, call: _StaticOnlyRow) -> None:
    """Post-Pydantic dataclasses store out-of-contract values unchanged.

    Each row confirms that Literal/type enforcement happens at the mypy layer
    (or at the codec boundary), not at construction time. The dataclass
    constructor accepts the bad value and returns it unmodified.
    """
    result = call()
    # The call must not raise — that is the entire contract being tested.
    # The returned value (the stored out-of-contract payload) must be truthy
    # to confirm storage occurred.
    assert result is not None, f"row {row_id!r}: expected stored value, got None"
