"""Tests for order and bracket records (story 03c)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EventTrigger,
    InstrumentSpec,
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
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import InstrumentType, OptionContractType

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
TODAY = date(2025, 6, 15)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _equity_spec() -> InstrumentSpec:
    return InstrumentSpec(instrument_type=InstrumentType.EQUITY, ticker="AAPL")


def _options_spec() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_type=InstrumentType.OPTIONS,
        underlying="AAPL",
        strike=150.0,
        expiration=TODAY,
        contract_type=OptionContractType.CALL,
        contract_multiplier=100.0,
    )


def _strategy_spec() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_type=InstrumentType.STRATEGY,
        legs=(_equity_spec(),),
    )


def _market_price_params() -> PriceParameters:
    return PriceParameters(limit_price=None, stop_trigger_price=None)


def _make_order(**overrides: object) -> OrderRecord:
    """Build a valid MARKET OrderRecord, with optional overrides."""
    base: dict[str, object] = {
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
    return OrderRecord.model_validate(base)


def _trigger_for(leg_type: BracketLegType) -> PriceTrigger | TimeTrigger | EventTrigger:
    """Build the canonical typed trigger payload for a given leg_type."""
    if leg_type == BracketLegType.TAKE_PROFIT:
        return PriceTrigger(underlying_ticker="AAPL", threshold_usd=160.0, direction="GTE")
    if leg_type == BracketLegType.PRICE_STOP:
        return PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE")
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
        order_id="ord-stop",
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
    base: dict[str, object] = {
        "bracket_id": "brk-1",
        "position_id": "pos-1",
        "status": BracketStatus.PENDING_ENTRY,
        "entry_order_id": "ord-entry",
        "protective_legs": (_make_mechanical_leg(),),
        "modification_history": (),
        "corporate_action_cancellation_reason": None,
    }
    base.update(overrides)
    return BracketRecord.model_validate(base)


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

    def test_string_values(self) -> None:
        assert OrderClass.SIMPLE == "SIMPLE"
        assert OrderClass.BRACKET == "BRACKET"
        assert OrderClass.OCO == "OCO"
        assert OrderClass.OTO == "OTO"
        assert OrderClass.MLEG == "MLEG"

    def test_strenum_semantics(self) -> None:
        assert isinstance(OrderClass.MLEG, str)


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

    def test_string_values(self) -> None:
        assert OrderRole.ENTRY == "ENTRY"
        assert OrderRole.TAKE_PROFIT == "TAKE_PROFIT"
        assert OrderRole.PRICE_STOP == "PRICE_STOP"
        assert OrderRole.TIME_STOP == "TIME_STOP"
        assert OrderRole.CLOSE == "CLOSE"
        assert OrderRole.ADD_ENTRY == "ADD_ENTRY"


class TestOrderType:
    def test_members(self) -> None:
        assert set(OrderType) == {
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.STOP,
            OrderType.STOP_LIMIT,
        }

    def test_string_values(self) -> None:
        assert OrderType.MARKET == "MARKET"
        assert OrderType.LIMIT == "LIMIT"
        assert OrderType.STOP == "STOP"
        assert OrderType.STOP_LIMIT == "STOP_LIMIT"


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

    def test_string_values(self) -> None:
        assert OrderDirection.BUY == "BUY"
        assert OrderDirection.SELL == "SELL"
        assert OrderDirection.BUY_TO_OPEN == "BUY_TO_OPEN"
        assert OrderDirection.SELL_TO_OPEN == "SELL_TO_OPEN"
        assert OrderDirection.BUY_TO_CLOSE == "BUY_TO_CLOSE"
        assert OrderDirection.SELL_TO_CLOSE == "SELL_TO_CLOSE"


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

    def test_string_values(self) -> None:
        assert OrderDuration.DAY == "DAY"
        assert OrderDuration.GTC == "GTC"
        assert OrderDuration.GTD == "GTD"


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

    def test_string_values(self) -> None:
        assert OrderStatus.PENDING == "PENDING"
        assert OrderStatus.PARTIALLY_FILLED == "PARTIALLY_FILLED"
        assert OrderStatus.FILLED == "FILLED"
        assert OrderStatus.CANCELLED == "CANCELLED"
        assert OrderStatus.EXPIRED == "EXPIRED"
        assert OrderStatus.REJECTED == "REJECTED"


class TestBracketStatus:
    def test_members(self) -> None:
        assert set(BracketStatus) == {
            BracketStatus.PENDING_ENTRY,
            BracketStatus.ACTIVE,
            BracketStatus.COMPLETED,
            BracketStatus.DISSOLVED,
        }

    def test_string_values(self) -> None:
        assert BracketStatus.PENDING_ENTRY == "PENDING_ENTRY"
        assert BracketStatus.ACTIVE == "ACTIVE"
        assert BracketStatus.COMPLETED == "COMPLETED"
        assert BracketStatus.DISSOLVED == "DISSOLVED"


class TestBracketLegType:
    def test_members(self) -> None:
        assert set(BracketLegType) == {
            BracketLegType.TAKE_PROFIT,
            BracketLegType.PRICE_STOP,
            BracketLegType.TIME_EXPIRATION,
            BracketLegType.EVENT_INVALIDATION,
        }

    def test_string_values(self) -> None:
        assert BracketLegType.TAKE_PROFIT == "TAKE_PROFIT"
        assert BracketLegType.PRICE_STOP == "PRICE_STOP"
        assert BracketLegType.TIME_EXPIRATION == "TIME_EXPIRATION"
        assert BracketLegType.EVENT_INVALIDATION == "EVENT_INVALIDATION"

    def test_importable_from_orders(self) -> None:
        """BracketLegType must be importable from orders module (canonical declaration site)."""
        from alphamind.portfolio_state.records.orders import BracketLegType

        assert BracketLegType.TAKE_PROFIT == "TAKE_PROFIT"


class TestBracketLegEnforcement:
    def test_members(self) -> None:
        assert set(BracketLegEnforcement) == {
            BracketLegEnforcement.MECHANICAL,
            BracketLegEnforcement.ADVISORY,
        }

    def test_string_values(self) -> None:
        assert BracketLegEnforcement.MECHANICAL == "MECHANICAL"
        assert BracketLegEnforcement.ADVISORY == "ADVISORY"


class TestBracketLegStatus:
    def test_members(self) -> None:
        assert set(BracketLegStatus) == {
            BracketLegStatus.PENDING_ACTIVATION,
            BracketLegStatus.ACTIVE,
            BracketLegStatus.TRIGGERED,
            BracketLegStatus.CANCELLED,
        }

    def test_string_values(self) -> None:
        assert BracketLegStatus.PENDING_ACTIVATION == "PENDING_ACTIVATION"
        assert BracketLegStatus.ACTIVE == "ACTIVE"
        assert BracketLegStatus.TRIGGERED == "TRIGGERED"
        assert BracketLegStatus.CANCELLED == "CANCELLED"


# ---------------------------------------------------------------------------
# InstrumentSpec discriminator tests
# ---------------------------------------------------------------------------


class TestInstrumentSpecDiscriminator:
    def test_equity_passes(self) -> None:
        spec = _equity_spec()
        assert spec.instrument_type == InstrumentType.EQUITY
        assert spec.ticker == "AAPL"

    def test_options_passes(self) -> None:
        spec = _options_spec()
        assert spec.instrument_type == InstrumentType.OPTIONS
        assert spec.underlying == "AAPL"
        assert spec.strike == 150.0

    def test_strategy_passes(self) -> None:
        spec = _strategy_spec()
        assert spec.instrument_type == InstrumentType.STRATEGY
        assert spec.legs is not None
        assert len(spec.legs) == 1

    def test_equity_with_options_field_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec(
                instrument_type=InstrumentType.EQUITY,
                ticker="AAPL",
                underlying="AAPL",  # options field on equity spec
            )

    def test_equity_without_ticker_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec.model_validate({"instrument_type": "EQUITY"})

    def test_options_with_ticker_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec(
                instrument_type=InstrumentType.OPTIONS,
                ticker="AAPL",  # equity field on options spec
                underlying="AAPL",
                strike=150.0,
                expiration=TODAY,
                contract_type=OptionContractType.CALL,
                contract_multiplier=100.0,
            )

    def test_strategy_with_empty_legs_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec(instrument_type=InstrumentType.STRATEGY, legs=())

    def test_strategy_with_none_legs_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec.model_validate({"instrument_type": "STRATEGY"})

    def test_strategy_with_equity_field_fails(self) -> None:
        with pytest.raises(ValidationError):
            InstrumentSpec(
                instrument_type=InstrumentType.STRATEGY,
                ticker="AAPL",  # equity field on strategy spec
                legs=(_equity_spec(),),
            )


# ---------------------------------------------------------------------------
# PriceParameters cross-validation (enforced at OrderRecord level)
# ---------------------------------------------------------------------------


class TestPriceParametersCrossValidation:
    def test_market_both_none_passes(self) -> None:
        order = _make_order(order_type=OrderType.MARKET, price_parameters=_market_price_params())
        assert order.order_type == OrderType.MARKET

    def test_limit_with_limit_price_passes(self) -> None:
        pp = PriceParameters(limit_price=150.0, stop_trigger_price=None)
        order = _make_order(order_type=OrderType.LIMIT, price_parameters=pp)
        assert order.price_parameters.limit_price == 150.0

    def test_stop_with_stop_trigger_passes(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=140.0)
        order = _make_order(order_type=OrderType.STOP, price_parameters=pp)
        assert order.price_parameters.stop_trigger_price == 140.0

    def test_stop_limit_both_set_passes(self) -> None:
        pp = PriceParameters(limit_price=149.0, stop_trigger_price=148.0)
        order = _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)
        assert order.price_parameters.limit_price == 149.0
        assert order.price_parameters.stop_trigger_price == 148.0

    def test_limit_missing_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=None)
        with pytest.raises(ValidationError):
            _make_order(order_type=OrderType.LIMIT, price_parameters=pp)

    def test_stop_missing_stop_trigger_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=None)
        with pytest.raises(ValidationError):
            _make_order(order_type=OrderType.STOP, price_parameters=pp)

    def test_market_with_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=150.0, stop_trigger_price=None)
        with pytest.raises(ValidationError):
            _make_order(order_type=OrderType.MARKET, price_parameters=pp)

    def test_stop_limit_missing_limit_price_fails(self) -> None:
        pp = PriceParameters(limit_price=None, stop_trigger_price=148.0)
        with pytest.raises(ValidationError):
            _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)

    def test_stop_limit_missing_stop_trigger_fails(self) -> None:
        pp = PriceParameters(limit_price=149.0, stop_trigger_price=None)
        with pytest.raises(ValidationError):
            _make_order(order_type=OrderType.STOP_LIMIT, price_parameters=pp)


# ---------------------------------------------------------------------------
# Quantity constraints
# ---------------------------------------------------------------------------


class TestQuantityConstraints:
    def test_quantity_zero_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(quantity=0.0, remaining_quantity=0.0)

    def test_quantity_negative_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(quantity=-1.0, remaining_quantity=-1.0)

    def test_filled_quantity_negative_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(filled_quantity=-0.1, remaining_quantity=10.1)

    def test_remaining_quantity_negative_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(remaining_quantity=-1.0, filled_quantity=11.0)

    def test_modification_count_negative_fails(self) -> None:
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
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
        order = _make_order(alpaca_order_id="alp-2", alpaca_order_id_chain=("alp-1", "alp-2"))
        assert order.alpaca_order_id == "alp-2"
        assert order.alpaca_order_id_chain[-1] == "alp-2"

    def test_empty_chain_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(alpaca_order_id="alp-1", alpaca_order_id_chain=())

    def test_mismatch_fails(self) -> None:
        with pytest.raises(ValidationError):
            _make_order(
                alpaca_order_id="alp-1",
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
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id="ord-1",  # must be None for EVENT_INVALIDATION
                trigger=EventTrigger(description="thesis invalidation event"),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.PENDING_ACTIVATION,
            )

    def test_non_event_invalidation_may_have_order_id(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-stop",
            trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
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
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
            _make_bracket(protective_legs=(advisory_event_leg,))

    def test_only_advisory_mechanical_type_fails(self) -> None:
        advisory_stop = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-stop",
            trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
            enforcement=BracketLegEnforcement.ADVISORY,  # advisory, not mechanical
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError) as exc_info:
            _make_order(order_class=OrderClass.MLEG, instrument_spec=_equity_spec())
        assert "STRATEGY" in str(exc_info.value)

    def test_mleg_with_strategy_spec_passes(self) -> None:
        order = _make_order(order_class=OrderClass.MLEG, instrument_spec=_strategy_spec())
        assert order.order_class == OrderClass.MLEG

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
# OrderRecord round-trip / frozen check
# ---------------------------------------------------------------------------


class TestOrderRecordFrozen:
    def test_order_record_is_frozen(self) -> None:
        order = _make_order()
        with pytest.raises((AttributeError, ValidationError)):
            order.order_id = "changed"


# ---------------------------------------------------------------------------
# BracketRecord round-trip / frozen check
# ---------------------------------------------------------------------------


class TestBracketRecordFrozen:
    def test_bracket_record_is_frozen(self) -> None:
        bracket = _make_bracket()
        with pytest.raises((AttributeError, ValidationError)):
            bracket.bracket_id = "changed"


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
        with pytest.raises(ValidationError):
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
        trigger = PriceTrigger(underlying_ticker="NVDA", threshold_usd=800.0, direction="LTE")
        assert trigger.trigger_type == "price"
        assert trigger.underlying_ticker == "NVDA"
        assert trigger.threshold_usd == 800.0
        assert trigger.direction == "LTE"

    def test_negative_threshold_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriceTrigger(underlying_ticker="NVDA", threshold_usd=-5.0, direction="LTE")

    def test_zero_threshold_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriceTrigger(underlying_ticker="NVDA", threshold_usd=0.0, direction="LTE")

    def test_empty_ticker_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriceTrigger(underlying_ticker="", threshold_usd=10.0, direction="LTE")

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PriceTrigger.model_validate(
                {"underlying_ticker": "NVDA", "threshold_usd": 10.0, "direction": "ABOVE"}
            )

    def test_frozen(self) -> None:
        trigger = PriceTrigger(underlying_ticker="NVDA", threshold_usd=10.0, direction="GTE")
        with pytest.raises((AttributeError, ValidationError)):
            trigger.threshold_usd = 20.0


class TestTimeTrigger:
    def test_construct_with_tz_aware(self) -> None:
        deadline = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
        trigger = TimeTrigger(deadline=deadline)
        assert trigger.trigger_type == "time"
        assert trigger.deadline == deadline

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError):
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
        with pytest.raises(ValidationError):
            EventTrigger(description="")


class TestBracketLegTriggerLegTypeValidator:
    """leg_type ↔ trigger_type cross-validation."""

    def test_take_profit_with_price_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id="ord-1",
            trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=200.0, direction="GTE"),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, PriceTrigger)

    def test_price_stop_with_price_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-1",
            trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=160.0, direction="LTE"),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        assert isinstance(leg.trigger, PriceTrigger)

    def test_time_expiration_with_time_trigger_passes(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id="ord-1",
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
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=leg_type,
                order_id="ord-1",
                trigger=TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    @pytest.mark.parametrize("leg_type", _LEG_PRICE_TRIGGER_TYPES)
    def test_price_leg_with_event_trigger_rejected(self, leg_type: BracketLegType) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=leg_type,
                order_id="ord-1",
                trigger=EventTrigger(description="qualitative"),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_time_expiration_with_price_trigger_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.TIME_EXPIRATION,
                order_id="ord-1",
                trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=10.0, direction="GTE"),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_time_expiration_with_event_trigger_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.TIME_EXPIRATION,
                order_id="ord-1",
                trigger=EventTrigger(description="qualitative"),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            )

    def test_event_invalidation_with_price_trigger_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id=None,
                trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=10.0, direction="GTE"),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.ACTIVE,
            )

    def test_event_invalidation_with_time_trigger_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id=None,
                trigger=TimeTrigger(deadline=datetime(2026, 6, 1, 16, 0, tzinfo=UTC)),
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.ACTIVE,
            )


class TestBracketLegTriggerDiscriminatedUnionRoundTrip:
    """Pydantic discriminator-keyed deserialization round-trips."""

    def test_price_trigger_round_trip(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-1",
            trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=160.0, direction="LTE"),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        dumped = leg.model_dump()
        rehydrated = BracketLeg.model_validate(dumped)
        assert isinstance(rehydrated.trigger, PriceTrigger)
        assert rehydrated.trigger.threshold_usd == 160.0

    def test_time_trigger_round_trip(self) -> None:
        deadline = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id="ord-1",
            trigger=TimeTrigger(deadline=deadline),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        dumped = leg.model_dump()
        rehydrated = BracketLeg.model_validate(dumped)
        assert isinstance(rehydrated.trigger, TimeTrigger)
        assert rehydrated.trigger.deadline == deadline

    def test_event_trigger_round_trip(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger=EventTrigger(description="thesis invalidated"),
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.ACTIVE,
        )
        dumped = leg.model_dump()
        rehydrated = BracketLeg.model_validate(dumped)
        assert isinstance(rehydrated.trigger, EventTrigger)
        assert rehydrated.trigger.description == "thesis invalidated"


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
        with pytest.raises(ValidationError):
            PLAnchorSpec(spec_type="target", pct=0.0, planned_entry_price=18.50)

    def test_negative_pct_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(spec_type="target", pct=-0.10, planned_entry_price=18.50)

    def test_pct_above_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(spec_type="target", pct=11.0, planned_entry_price=18.50)

    def test_pct_at_cap_passes(self) -> None:
        spec = PLAnchorSpec(spec_type="target", pct=10.0, planned_entry_price=18.50)
        assert spec.pct == 10.0

    def test_negative_planned_entry_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=-5.0)

    def test_zero_planned_entry_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=0.0)

    def test_invalid_spec_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec.model_validate(
                {"spec_type": "limit", "pct": 0.80, "planned_entry_price": 18.50}
            )

    def test_recalculated_without_actual_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(
                spec_type="target",
                pct=0.80,
                planned_entry_price=18.50,
                recalculated_at_fill=True,
            )

    def test_actual_price_without_recalculated_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PLAnchorSpec(
                spec_type="target",
                pct=0.80,
                planned_entry_price=18.50,
                actual_entry_price=17.80,
                recalculated_at_fill=False,
            )

    def test_frozen(self) -> None:
        spec = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises((AttributeError, ValidationError)):
            spec.pct = 0.50

    def test_round_trip(self) -> None:
        spec = PLAnchorSpec(
            spec_type="stop",
            pct=0.30,
            planned_entry_price=18.50,
            actual_entry_price=17.80,
            recalculated_at_fill=True,
        )
        rehydrated = PLAnchorSpec.model_validate(spec.model_dump())
        assert rehydrated == spec


def _make_leg_with_anchor(
    leg_type: BracketLegType,
    pl_anchor: PLAnchorSpec | None,
) -> BracketLeg:
    """Build a BracketLeg with the canonical trigger for leg_type plus pl_anchor."""
    return BracketLeg(
        leg_id="leg-1",
        leg_type=leg_type,
        order_id=None if leg_type == BracketLegType.EVENT_INVALIDATION else "ord-1",
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
        with pytest.raises(ValidationError):
            _make_leg_with_anchor(BracketLegType.TAKE_PROFIT, pl_anchor=anchor)

    def test_price_stop_with_target_spec_type_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises(ValidationError):
            _make_leg_with_anchor(BracketLegType.PRICE_STOP, pl_anchor=anchor)

    def test_time_expiration_with_pl_anchor_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises(ValidationError):
            _make_leg_with_anchor(BracketLegType.TIME_EXPIRATION, pl_anchor=anchor)

    def test_event_invalidation_with_pl_anchor_rejected(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        with pytest.raises(ValidationError):
            _make_leg_with_anchor(BracketLegType.EVENT_INVALIDATION, pl_anchor=anchor)

    def test_round_trip_with_pl_anchor(self) -> None:
        anchor = PLAnchorSpec(spec_type="target", pct=0.80, planned_entry_price=18.50)
        leg = _make_leg_with_anchor(BracketLegType.TAKE_PROFIT, pl_anchor=anchor)
        rehydrated = BracketLeg.model_validate(leg.model_dump())
        assert rehydrated.pl_anchor is not None
        assert rehydrated.pl_anchor.spec_type == "target"
        assert rehydrated.pl_anchor.pct == 0.80
