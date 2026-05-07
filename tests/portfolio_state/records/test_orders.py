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
    InstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
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


def _make_mechanical_leg(
    leg_id: str = "leg-1",
    leg_type: BracketLegType = BracketLegType.PRICE_STOP,
    status: BracketLegStatus = BracketLegStatus.PENDING_ACTIVATION,
) -> BracketLeg:
    return BracketLeg(
        leg_id=leg_id,
        leg_type=leg_type,
        order_id="ord-stop",
        trigger_condition="price < 140.0",
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=status,
        pl_based=False,
    )


def _make_modification() -> BracketLegModification:
    return BracketLegModification(
        timestamp=NOW,
        pm_command_id=None,
        source="fill-anchor-recalculation",
        field_changed="trigger_condition",
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
            OrderDuration.IOC,
            OrderDuration.FOK,
        }

    def test_string_values(self) -> None:
        assert OrderDuration.DAY == "DAY"
        assert OrderDuration.GTC == "GTC"
        assert OrderDuration.GTD == "GTD"
        assert OrderDuration.IOC == "IOC"
        assert OrderDuration.FOK == "FOK"


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
            trigger_condition="thesis invalidation event",
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_based=False,
        )
        assert leg.order_id is None

    def test_event_invalidation_with_non_none_order_id_fails(self) -> None:
        with pytest.raises(ValidationError):
            BracketLeg(
                leg_id="leg-1",
                leg_type=BracketLegType.EVENT_INVALIDATION,
                order_id="ord-1",  # must be None for EVENT_INVALIDATION
                trigger_condition="thesis invalidation event",
                enforcement=BracketLegEnforcement.ADVISORY,
                status=BracketLegStatus.PENDING_ACTIVATION,
                pl_based=False,
            )

    def test_non_event_invalidation_may_have_order_id(self) -> None:
        leg = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-stop",
            trigger_condition="price < 140.0",
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_based=False,
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
            trigger_condition="thesis invalidated",
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_based=False,
        )
        with pytest.raises(ValidationError):
            _make_bracket(protective_legs=(advisory_event_leg,))

    def test_only_advisory_mechanical_type_fails(self) -> None:
        advisory_stop = BracketLeg(
            leg_id="leg-1",
            leg_type=BracketLegType.PRICE_STOP,
            order_id="ord-stop",
            trigger_condition="price < 140.0",
            enforcement=BracketLegEnforcement.ADVISORY,  # advisory, not mechanical
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_based=False,
        )
        with pytest.raises(ValidationError):
            _make_bracket(protective_legs=(advisory_stop,))

    def test_mixed_with_at_least_one_mechanical_passes(self) -> None:
        advisory_event_leg = BracketLeg(
            leg_id="leg-2",
            leg_type=BracketLegType.EVENT_INVALIDATION,
            order_id=None,
            trigger_condition="thesis invalidated",
            enforcement=BracketLegEnforcement.ADVISORY,
            status=BracketLegStatus.PENDING_ACTIVATION,
            pl_based=False,
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
