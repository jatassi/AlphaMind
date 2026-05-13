"""Tests for canonical OMS command models — ALP-370 / story 01a.

Verifies the broker-grade Pydantic models translate
``docs/design/05-execution-layer/oms-command-schema.md`` faithfully:

* every variant constructs successfully on the happy path
* every model is frozen
* the discriminated union round-trips per ``command_type``
* every per-design ``model_validator`` raises ``ValidationError`` on
  the negative cases enumerated in the story's acceptance criteria
* :func:`oms_command_schema` returns a JSON Schema dict whose top level
  is a ``oneOf`` over the five variants discriminated by ``command_type``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

# Import portfolio_manager.models first to break the latent cycle between
# alphamind.execution.oms (engine-stub MCP) and alphamind.decision.portfolio_manager
# (harness imports back from the OMS). Mirrors test_submit_envelope_mcp.py.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import (
    OrderId,
    PositionId,
)
from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    BracketAdjustment,
    BracketOrderParameters,
    CancelCommand,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    EventCondition,
    EventLeg,
    NewEventInvalidation,
    NewStopLevel,
    NewTargetLevel,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    StrategyInstrument,
    StrategyLeg,
    Target,
    Thesis,
    ThesisComponent,
    TimeCondition,
    TimeLeg,
    oms_command_schema,
)

# ---------------------------------------------------------------------------
# Builder helpers — return minimal valid variants for tests to compose.
# ---------------------------------------------------------------------------


def _equity_instrument() -> EquityInstrument:
    return EquityInstrument(asset_type="equity", ticker="AAPL", direction="long")


def _option_instrument() -> OptionInstrument:
    return OptionInstrument(
        asset_type="option",
        underlying="AAPL",
        strike=price(150.0),
        expiration="2026-06-19",
        contract_type="call",
        direction="long",
    )


def _strategy_leg(strike: float = 150.0, contract_type: str = "call") -> StrategyLeg:
    return StrategyLeg(
        strike=price(strike),
        expiration="2026-06-19",
        contract_type=contract_type,  # type: ignore[arg-type]
        direction="long",
        quantity_ratio=1,
    )


def _strategy_instrument() -> StrategyInstrument:
    return StrategyInstrument(
        asset_type="strategy",
        strategy_type="vertical_spread",
        underlying="AAPL",
        legs=(_strategy_leg(150.0, "call"), _strategy_leg(155.0, "call")),
    )


def _entry_order_market() -> EntryOrder:
    return EntryOrder(type="market")


def _position_size() -> PositionSize:
    return PositionSize(quantity=100.0, dollar_value=money(15_000.0), premium_at_risk=None)


def _target_absolute() -> Target:
    return Target(target_type="absolute_price", price=price(170.0), order_type="limit")


def _price_leg(trigger_price: float = 140.0) -> PriceLeg:
    return PriceLeg(
        type="price",
        is_hard=True,
        condition=PriceCondition(
            underlying_trigger="AAPL", comparator="<=", trigger_price=price(trigger_price)
        ),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


_DEADLINE = datetime(2026, 6, 19, 16, 0, tzinfo=UTC)


def _time_leg() -> TimeLeg:
    return TimeLeg(
        type="time",
        is_hard=True,
        condition=TimeCondition(deadline=_DEADLINE),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


def _event_leg() -> EventLeg:
    return EventLeg(
        type="event",
        is_hard=False,
        condition=EventCondition(event_description="thesis-relevant macro"),
    )


def _thesis_component(component_type: str = "entry_rationale") -> ThesisComponent:
    return ThesisComponent(
        component_type=component_type,  # type: ignore[arg-type]
        linked_leg="entry",
        instrument_reference="AAPL",
        narrative="signals support the entry",
        key_assumptions=("assumption-1",),
    )


def _thesis() -> Thesis:
    return Thesis(
        summary="long AAPL on momentum",
        components=(
            _thesis_component("entry_rationale"),
            _thesis_component("target_rationale"),
            _thesis_component("invalidation_rationale"),
        ),
    )


def _open_command() -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=_equity_instrument(),
        entry_order=_entry_order_market(),
        position_size=_position_size(),
        target=_target_absolute(),
        invalidation_legs=(_price_leg(),),
        thesis=_thesis(),
    )


def _close_command() -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity="all",
        order_type="market",
        close_rationale_type="target_reached",
    )


def _adjust_command() -> AdjustCommand:
    return AdjustCommand(
        command_type="adjust",
        position_id=PositionId("pos-1"),
        adjustment_rationale="bracket revision",
        new_stop_level=NewStopLevel(trigger_price=price(130.0), order_type="market"),
    )


def _cancel_command() -> CancelCommand:
    return CancelCommand(
        command_type="cancel",
        order_id=OrderId("ord-1"),
        cancel_reason="thesis no longer valid",
    )


def _add_command() -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=PositionId("pos-1"),
        additional_quantity=10.0,
        additional_dollar_value=money(1500.0),
        entry_order=_entry_order_market(),
        thesis_addition_component=_thesis_component("entry_rationale"),
    )


# ---------------------------------------------------------------------------
# Sub-record happy-path constructions
# ---------------------------------------------------------------------------


class TestEquityInstrument:
    def test_constructs_with_required_fields(self) -> None:
        inst = _equity_instrument()
        assert inst.asset_type == "equity"
        assert inst.ticker == "AAPL"
        assert inst.direction == "long"


class TestOptionInstrument:
    def test_constructs_with_required_fields(self) -> None:
        inst = _option_instrument()
        assert inst.asset_type == "option"
        assert inst.contract_type == "call"


class TestStrategyInstrument:
    def test_constructs_with_two_legs(self) -> None:
        inst = _strategy_instrument()
        assert len(inst.legs) == 2

    def test_rejects_single_leg(self) -> None:
        with pytest.raises(ValidationError):
            StrategyInstrument(
                asset_type="strategy",
                strategy_type="vertical_spread",
                underlying="AAPL",
                legs=(_strategy_leg(),),
            )


class TestEntryOrder:
    def test_market_constructs(self) -> None:
        EntryOrder(type="market")

    def test_limit_requires_limit_price(self) -> None:
        with pytest.raises(ValidationError):
            EntryOrder(type="limit")
        EntryOrder(type="limit", limit_price=price(100.0))

    def test_stop_limit_requires_both_prices(self) -> None:
        with pytest.raises(ValidationError):
            EntryOrder(type="stop_limit", limit_price=price(100.0))
        with pytest.raises(ValidationError):
            EntryOrder(type="stop_limit", stop_price=price(100.0))
        EntryOrder(type="stop_limit", limit_price=price(100.0), stop_price=price(99.0))


class TestPositionSize:
    def test_constructs_without_sector_field(self) -> None:
        ps = PositionSize(quantity=100.0, dollar_value=money(15_000.0), premium_at_risk=None)
        # Parent decision (B): no sector field on canonical PositionSize.
        assert not hasattr(ps, "sector")

    def test_premium_at_risk_optional(self) -> None:
        ps = PositionSize(quantity=10.0, dollar_value=money(1_000.0), premium_at_risk=money(500.0))
        assert ps.premium_at_risk == 500.0


class TestTarget:
    def test_absolute_price_requires_price(self) -> None:
        with pytest.raises(ValidationError):
            Target(target_type="absolute_price", order_type="limit")
        Target(target_type="absolute_price", price=price(170.0), order_type="limit")

    def test_pl_percentage_requires_pct_and_price(self) -> None:
        with pytest.raises(ValidationError):
            Target(target_type="pl_percentage", price=price(170.0), order_type="limit")
        with pytest.raises(ValidationError):
            Target(target_type="pl_percentage", pl_percentage=80.0, order_type="limit")
        Target(
            target_type="pl_percentage",
            pl_percentage=80.0,
            price=price(170.0),
            order_type="limit",
        )

    def test_pl_dollar_requires_dollar_and_price(self) -> None:
        with pytest.raises(ValidationError):
            Target(target_type="pl_dollar", price=price(170.0), order_type="limit")
        with pytest.raises(ValidationError):
            Target(target_type="pl_dollar", pl_dollar=money(500.0), order_type="limit")
        Target(
            target_type="pl_dollar", pl_dollar=money(500.0), price=price(170.0), order_type="limit"
        )


# ---------------------------------------------------------------------------
# Invalidation legs
# ---------------------------------------------------------------------------


class TestInvalidationLegs:
    def test_price_leg_requires_is_hard_true(self) -> None:
        with pytest.raises(ValidationError):
            PriceLeg(
                type="price",
                is_hard=False,  # type: ignore[arg-type]
                condition=PriceCondition(
                    underlying_trigger="AAPL", comparator="<=", trigger_price=price(140.0)
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            )

    def test_time_leg_requires_is_hard_true(self) -> None:
        with pytest.raises(ValidationError):
            TimeLeg(
                type="time",
                is_hard=False,  # type: ignore[arg-type]
                condition=TimeCondition(deadline=_DEADLINE),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            )

    def test_event_leg_requires_is_hard_false(self) -> None:
        with pytest.raises(ValidationError):
            EventLeg(
                type="event",
                is_hard=True,  # type: ignore[arg-type]
                condition=EventCondition(event_description="x"),
            )

    def test_event_leg_disallows_order_parameters(self) -> None:
        # EventLeg should not accept order_parameters at all (extra=forbid).
        with pytest.raises(ValidationError):
            EventLeg(
                type="event",
                is_hard=False,
                condition=EventCondition(event_description="x"),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Command variants — happy paths and per-variant model_validators
# ---------------------------------------------------------------------------


class TestOpenCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _open_command()
        assert cmd.command_type == "open"
        assert cmd.command_id is None

    def test_rejects_no_hard_legs(self) -> None:
        # Soft-only invalidation_legs (event leg) → reject.
        with pytest.raises(ValidationError) as exc_info:
            OpenCommand(
                command_type="open",
                instrument=_equity_instrument(),
                entry_order=_entry_order_market(),
                position_size=_position_size(),
                target=_target_absolute(),
                invalidation_legs=(_event_leg(),),
                thesis=_thesis(),
            )
        assert "is_hard" in str(exc_info.value).lower() or "hard" in str(exc_info.value).lower()


class TestCloseCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _close_command()
        assert cmd.command_type == "close"
        assert cmd.quantity == "all"

    def test_close_rationale_type_excludes_tactical_exit(self) -> None:
        # Parent decision (E): canonical enum is the four design values; no tactical_exit.
        with pytest.raises(ValidationError):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="tactical_exit",  # type: ignore[arg-type]
            )

    def test_limit_order_requires_limit_price(self) -> None:
        with pytest.raises(ValidationError):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="limit",
                close_rationale_type="target_reached",
            )
        CloseCommand(
            command_type="close",
            position_id=PositionId("pos-1"),
            quantity="all",
            order_type="limit",
            limit_price=price(170.0),
            close_rationale_type="target_reached",
        )

    def test_thesis_invalidated_requires_invalidation_reason(self) -> None:
        with pytest.raises(ValidationError):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="thesis_invalidated",
            )
        CloseCommand(
            command_type="close",
            position_id=PositionId("pos-1"),
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
            invalidation_reason="signal X flipped",
        )

    def test_risk_management_requires_subtype(self) -> None:
        with pytest.raises(ValidationError):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="risk_management",
            )
        CloseCommand(
            command_type="close",
            position_id=PositionId("pos-1"),
            quantity="all",
            order_type="market",
            close_rationale_type="risk_management",
            risk_management_subtype="pm_directed",
        )

    def test_conviction_reduced_forbids_quantity_all(self) -> None:
        with pytest.raises(ValidationError):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="conviction_reduced",
            )
        CloseCommand(
            command_type="close",
            position_id=PositionId("pos-1"),
            quantity=10.0,
            order_type="market",
            close_rationale_type="conviction_reduced",
        )


class TestAdjustCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _adjust_command()
        assert cmd.command_type == "adjust"

    def test_requires_at_least_one_change(self) -> None:
        with pytest.raises(ValidationError):
            AdjustCommand(
                command_type="adjust",
                position_id=PositionId("pos-1"),
                adjustment_rationale="x",
            )

    def test_accepts_each_change_field(self) -> None:
        AdjustCommand(
            command_type="adjust",
            position_id=PositionId("pos-1"),
            adjustment_rationale="x",
            new_target_level=NewTargetLevel(
                target_type="absolute_price", price=price(170.0), order_type="limit"
            ),
        )
        AdjustCommand(
            command_type="adjust",
            position_id=PositionId("pos-1"),
            adjustment_rationale="x",
            new_time_expiration=_DEADLINE,
        )
        AdjustCommand(
            command_type="adjust",
            position_id=PositionId("pos-1"),
            adjustment_rationale="x",
            new_event_invalidation=NewEventInvalidation(event_description="ev"),
        )
        AdjustCommand(
            command_type="adjust",
            position_id=PositionId("pos-1"),
            adjustment_rationale="x",
            thesis_component_updates=(_thesis_component(),),
        )


class TestCancelCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _cancel_command()
        assert cmd.command_type == "cancel"


class TestAddCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _add_command()
        assert cmd.command_type == "add"

    def test_thesis_addition_component_must_be_entry_rationale(self) -> None:
        with pytest.raises(ValidationError):
            AddCommand(
                command_type="add",
                position_id=PositionId("pos-1"),
                additional_quantity=10.0,
                additional_dollar_value=money(1500.0),
                entry_order=_entry_order_market(),
                thesis_addition_component=_thesis_component("target_rationale"),
            )

    def test_bracket_adjustment_requires_at_least_one_field(self) -> None:
        with pytest.raises(ValidationError):
            BracketAdjustment()
        BracketAdjustment(
            new_stop_level=NewStopLevel(trigger_price=price(130.0), order_type="market")
        )


# ---------------------------------------------------------------------------
# Frozen / immutable invariant
# ---------------------------------------------------------------------------


class TestFrozenModels:
    @pytest.mark.parametrize(
        "instance",
        [
            _equity_instrument(),
            _option_instrument(),
            _strategy_instrument(),
            _entry_order_market(),
            _position_size(),
            _target_absolute(),
            _price_leg(),
            _time_leg(),
            _event_leg(),
            _thesis_component(),
            _thesis(),
            _open_command(),
            _close_command(),
            _adjust_command(),
            _cancel_command(),
            _add_command(),
        ],
    )
    def test_models_are_frozen(self, instance: object) -> None:
        # Pydantic raises ValidationError on mutation of a frozen model.
        field_names = list(instance.__class__.model_fields.keys())  # type: ignore[attr-defined]
        with pytest.raises(ValidationError):
            setattr(instance, field_names[0], None)


# ---------------------------------------------------------------------------
# Discriminated-union round-trip
# ---------------------------------------------------------------------------


class TestDiscriminatedUnion:
    @pytest.mark.parametrize(
        ("payload", "expected_cls"),
        [
            (_open_command().model_dump(), OpenCommand),
            (_close_command().model_dump(), CloseCommand),
            (_adjust_command().model_dump(), AdjustCommand),
            (_cancel_command().model_dump(), CancelCommand),
            (_add_command().model_dump(), AddCommand),
        ],
    )
    def test_round_trip(self, payload: dict[str, object], expected_cls: type) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        instance = adapter.validate_python(payload)
        assert isinstance(instance, expected_cls)


# ---------------------------------------------------------------------------
# Schema export accessor
# ---------------------------------------------------------------------------


class TestSchemaExport:
    def test_returns_one_of_over_five_variants(self) -> None:
        schema = oms_command_schema()
        assert isinstance(schema, dict)
        assert "oneOf" in schema
        assert len(schema["oneOf"]) == 5

    def test_discriminator_keyed_on_command_type(self) -> None:
        schema = oms_command_schema()
        assert "discriminator" in schema
        assert schema["discriminator"]["propertyName"] == "command_type"


# ---------------------------------------------------------------------------
# Public surface re-exports
# ---------------------------------------------------------------------------


class TestPackageReExports:
    def test_canonical_models_importable_from_commands_package(self) -> None:
        """After ALP-458 the OMS command discriminated union lives in
        :mod:`alphamind.commands`; the public surface is re-exported from the
        kernel ``__init__`` for one-line consumer imports.
        """
        from alphamind.commands import (
            AddCommand as PkgAddCommand,
        )
        from alphamind.commands import (
            AdjustCommand as PkgAdjustCommand,
        )
        from alphamind.commands import (
            CancelCommand as PkgCancelCommand,
        )
        from alphamind.commands import (
            CloseCommand as PkgCloseCommand,
        )
        from alphamind.commands import (
            OMSCommand as PkgOMSCommand,
        )
        from alphamind.commands import (
            OpenCommand as PkgOpenCommand,
        )
        from alphamind.commands import (
            oms_command_schema as pkg_oms_command_schema,
        )

        assert PkgOpenCommand is OpenCommand
        assert PkgCloseCommand is CloseCommand
        assert PkgAdjustCommand is AdjustCommand
        assert PkgCancelCommand is CancelCommand
        assert PkgAddCommand is AddCommand
        assert PkgOMSCommand is OMSCommand
        assert pkg_oms_command_schema is oms_command_schema

    def test_submission_result_re_exported_from_commands(self) -> None:
        # SubmissionResult lives in commands/submission_results after ALP-458.
        from alphamind.commands import SubmissionResult  # noqa: F401
