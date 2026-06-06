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
from pydantic import TypeAdapter

# Import portfolio_manager.models first to break the latent cycle between
# alphamind.execution.oms (engine-stub MCP) and alphamind.decision.portfolio_manager
# (harness imports back from the OMS). Mirrors test_submit_envelope_mcp.py.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import (
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    BracketAdjustment,
    BracketOrderParameters,
    CancelCommand,
    CapitalProtectionFloor,
    CloseCommand,
    EntryOrder,
    EntryWindow,
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
    return EquityInstrument(asset_type="equity", ticker=Symbol("AAPL"), direction="long")


def _option_instrument() -> OptionInstrument:
    return OptionInstrument(
        asset_type="option",
        underlying=Symbol("AAPL"),
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
        underlying=Symbol("AAPL"),
        legs=(_strategy_leg(150.0, "call"), _strategy_leg(155.0, "call")),
    )


def _entry_order_market() -> EntryOrder:
    return EntryOrder(type="market")


def _entry_order_limit() -> EntryOrder:
    return EntryOrder(type="limit", limit_price=price(5.0))


def _entry_order_stop_limit() -> EntryOrder:
    return EntryOrder(type="stop_limit", limit_price=price(5.0), stop_price=price(4.5))


def _position_size() -> PositionSize:
    return PositionSize(quantity=100.0, dollar_value=money(15_000.0), premium_at_risk=None)


def _target_absolute() -> Target:
    return Target(target_type="absolute_price", price=price(170.0), order_type="limit")


def _price_leg(trigger_price: float = 140.0, trigger_signal: str = "underlying_price") -> PriceLeg:
    return PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal=trigger_signal,  # type: ignore[arg-type]
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


def _thesis(nature: str = "directional") -> Thesis:
    return Thesis(
        summary="long AAPL on momentum",
        nature=nature,  # type: ignore[arg-type]
        components=(
            _thesis_component("entry_rationale"),
            _thesis_component("target_rationale"),
            _thesis_component("invalidation_rationale"),
        ),
    )


def _capital_protection_floor(loss_limit: float = 500.0) -> CapitalProtectionFloor:
    return CapitalProtectionFloor(max_loss=money(loss_limit))


# Sentinel distinguishing "caller omitted the floor (use the default)" from
# "caller explicitly passed ``None``" (the floorless-rejection test case).
_FLOOR_DEFAULT = object()


def _option_open_command(
    *,
    capital_protection_floor: CapitalProtectionFloor | None | object = _FLOOR_DEFAULT,
    nature: str = "directional",
    invalidation_legs: tuple[PriceLeg, ...] | None = None,
    entry_order: EntryOrder | None = None,
) -> OpenCommand:
    floor = (
        _capital_protection_floor()
        if capital_protection_floor is _FLOOR_DEFAULT
        else capital_protection_floor
    )
    return OpenCommand(
        command_type="open",
        instrument=_option_instrument(),
        entry_order=entry_order or _entry_order_limit(),
        position_size=PositionSize(
            quantity=10.0, dollar_value=money(1_000.0), premium_at_risk=money(1_000.0)
        ),
        target=_target_absolute(),
        invalidation_legs=invalidation_legs or (_price_leg(),),
        thesis=_thesis(nature=nature),
        capital_protection_floor=floor,  # type: ignore[arg-type]
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
        with pytest.raises((ValueError, TypeError)):
            StrategyInstrument(
                asset_type="strategy",
                strategy_type="vertical_spread",
                underlying=Symbol("AAPL"),
                legs=(_strategy_leg(),),
            )


class TestEntryOrder:
    def test_market_constructs(self) -> None:
        EntryOrder(type="market")

    def test_limit_requires_limit_price(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EntryOrder(type="limit")
        EntryOrder(type="limit", limit_price=price(100.0))

    def test_stop_limit_requires_both_prices(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EntryOrder(type="stop_limit", limit_price=price(100.0))
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
            Target(target_type="absolute_price", order_type="limit")
        Target(target_type="absolute_price", price=price(170.0), order_type="limit")

    def test_pl_percentage_requires_pct_and_price(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            Target(target_type="pl_percentage", price=price(170.0), order_type="limit")
        with pytest.raises((ValueError, TypeError)):
            Target(target_type="pl_percentage", pl_percentage=80.0, order_type="limit")
        Target(
            target_type="pl_percentage",
            pl_percentage=80.0,
            price=price(170.0),
            order_type="limit",
        )

    def test_pl_dollar_requires_dollar_and_price(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            Target(target_type="pl_dollar", price=price(170.0), order_type="limit")
        with pytest.raises((ValueError, TypeError)):
            Target(target_type="pl_dollar", pl_dollar=money(500.0), order_type="limit")
        Target(
            target_type="pl_dollar", pl_dollar=money(500.0), price=price(170.0), order_type="limit"
        )


# ---------------------------------------------------------------------------
# Invalidation legs
# ---------------------------------------------------------------------------


class TestInvalidationLegs:
    def test_price_leg_requires_is_hard_true(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PriceLeg(
                type="price",
                is_hard=False,  # type: ignore[arg-type]
                trigger_signal="underlying_price",
                condition=PriceCondition(
                    underlying_trigger="AAPL", comparator="<=", trigger_price=price(140.0)
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            )

    def test_time_leg_requires_is_hard_true(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            TimeLeg(
                type="time",
                is_hard=False,  # type: ignore[arg-type]
                condition=TimeCondition(deadline=_DEADLINE),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            )

    def test_event_leg_requires_is_hard_false(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EventLeg(
                type="event",
                is_hard=True,  # type: ignore[arg-type]
                condition=EventCondition(event_description="x"),
            )

    def test_event_leg_disallows_order_parameters(self) -> None:
        # EventLeg should not accept order_parameters at all (extra=forbid).
        with pytest.raises((ValueError, TypeError)):
            EventLeg(
                type="event",
                is_hard=False,
                condition=EventCondition(event_description="x"),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Command variants — happy paths and per-variant model_validators
# ---------------------------------------------------------------------------


class TestEntryWindow:
    def test_constructs_with_required_fields(self) -> None:
        # Mirrors the analyst Recommendation.entry_window shape (deadline +
        # decay_type + rationale) so the PM copies it through verbatim — the
        # ALP-737 "map, not strip" reconciliation with ALP-736.
        window = EntryWindow(
            deadline=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
            decay_type="gradual",
            rationale="shorting into an active relief bounce; 24-hour gradual decay",
        )
        assert window.deadline == datetime(2026, 5, 29, 17, 30, tzinfo=UTC)
        assert window.decay_type == "gradual"

    def test_rejects_naive_deadline(self) -> None:
        with pytest.raises((ValueError, TypeError)) as exc_info:
            EntryWindow(
                deadline=datetime(2026, 5, 29, 17, 30),  # noqa: DTZ001 — intentionally naive
                decay_type="binary",
                rationale="catalyst at the open",
            )
        assert "tz-aware" in str(exc_info.value)

    def test_rejects_empty_rationale(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            EntryWindow(
                deadline=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
                decay_type="binary",
                rationale="",
            )


class TestOpenCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _open_command()
        assert cmd.command_type == "open"
        assert cmd.command_id is None

    def test_entry_window_defaults_to_none(self) -> None:
        # An OPEN command without a window is the common case; the field is
        # optional and absent by default.
        assert _open_command().entry_window is None

    def test_accepts_entry_window(self) -> None:
        # ALP-737: OpenCommand now carries the analyst's entry_window so the
        # patient-limit deadline survives the PM authoring boundary instead of
        # being rejected by extra="forbid" (ALP-736's MRVL loss).
        window = EntryWindow(
            deadline=datetime(2026, 5, 29, 17, 30, tzinfo=UTC),
            decay_type="gradual",
            rationale="overnight gap retest window",
        )
        cmd = OpenCommand(
            command_type="open",
            instrument=_equity_instrument(),
            entry_order=_entry_order_market(),
            position_size=_position_size(),
            target=_target_absolute(),
            invalidation_legs=(_price_leg(),),
            thesis=_thesis(),
            entry_window=window,
        )
        assert cmd.entry_window is not None
        assert cmd.entry_window.deadline == datetime(2026, 5, 29, 17, 30, tzinfo=UTC)

    def test_rejects_no_hard_legs(self) -> None:
        # Soft-only invalidation_legs (event leg) → reject.
        with pytest.raises((ValueError, TypeError)) as exc_info:
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

    def test_strategy_instrument_requires_pl_percentage_target(self) -> None:
        # ALP-611 (option 2): a strategy take-profit references the strategy's
        # net P/L, so it must be a pl_percentage target. pl_dollar and
        # absolute_price are rejected at the command boundary — they never
        # reach the OPEN write path's _strategy_target_to_bracket_leg.
        strategy_kwargs = {
            "command_type": "open",
            "instrument": _strategy_instrument(),
            "entry_order": _entry_order_limit(),
            "position_size": _position_size(),
            "invalidation_legs": (_price_leg(),),
            "thesis": _thesis(),
            "capital_protection_floor": _capital_protection_floor(),
        }
        # pl_percentage strategy target constructs.
        OpenCommand(
            target=Target(
                target_type="pl_percentage",
                pl_percentage=80.0,
                price=price(170.0),
                order_type="limit",
            ),
            **strategy_kwargs,  # type: ignore[arg-type]
        )
        # pl_dollar on a strategy → reject with a pl_percentage-naming error.
        with pytest.raises((ValueError, TypeError)) as pl_dollar_exc:
            OpenCommand(
                target=Target(
                    target_type="pl_dollar",
                    pl_dollar=money(500.0),
                    price=price(170.0),
                    order_type="limit",
                ),
                **strategy_kwargs,  # type: ignore[arg-type]
            )
        pl_dollar_msg = str(pl_dollar_exc.value)
        assert "strategy" in pl_dollar_msg and "pl_percentage" in pl_dollar_msg
        # absolute_price on a strategy → reject.
        with pytest.raises((ValueError, TypeError)) as abs_exc:
            OpenCommand(target=_target_absolute(), **strategy_kwargs)  # type: ignore[arg-type]
        abs_msg = str(abs_exc.value)
        assert "strategy" in abs_msg and "pl_percentage" in abs_msg

    def test_non_strategy_instrument_allows_any_target_type(self) -> None:
        # The strategy constraint must not regress equity / single-option OPENs —
        # they keep their full absolute_price | pl_percentage | pl_dollar range.
        # An option OPEN additionally carries the mandatory capital-protection
        # floor; an equity OPEN does not (native-bracket protection).
        for instrument in (_equity_instrument(), _option_instrument()):
            is_equity = isinstance(instrument, EquityInstrument)
            floor = None if is_equity else _capital_protection_floor()
            # Equity may use a market entry (native bracket protects it); an
            # options OPEN must rest (ALP-866), so use a limit entry there.
            entry_order = _entry_order_market() if is_equity else _entry_order_limit()
            OpenCommand(
                command_type="open",
                instrument=instrument,
                entry_order=entry_order,
                position_size=_position_size(),
                target=Target(
                    target_type="pl_dollar",
                    pl_dollar=money(500.0),
                    price=price(170.0),
                    order_type="limit",
                ),
                invalidation_legs=(_price_leg(),),
                thesis=_thesis(),
                capital_protection_floor=floor,
            )

    def test_allows_short_equity_instrument(self) -> None:
        # ALP-717: short-equity OpenCommand construction is first-class once
        # Phase 1 grows the SHORT entry-fill helper + borrow-cost modeling on
        # EquityPositionDetails (Story 01). The OMS-boundary guard from
        # ALP-644 is retired; SHORT equity now mirrors the existing
        # SHORT-option allowance.
        short_equity = EquityInstrument(
            asset_type="equity", ticker=Symbol("AAPL"), direction="short"
        )
        OpenCommand(
            command_type="open",
            instrument=short_equity,
            entry_order=_entry_order_market(),
            position_size=_position_size(),
            target=_target_absolute(),
            invalidation_legs=(_price_leg(),),
            thesis=_thesis(),
        )

    def test_allows_short_option_instrument(self) -> None:
        # The short-equity restriction must not regress single-leg short options,
        # which Phase 1 already supports as first-class (SELL_TO_OPEN routes to
        # _apply_options_entry_fill unconditionally).
        short_option = OptionInstrument(
            asset_type="option",
            underlying=Symbol("AAPL"),
            strike=price(150.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
        )
        OpenCommand(
            command_type="open",
            instrument=short_option,
            entry_order=_entry_order_limit(),
            position_size=_position_size(),
            target=_target_absolute(),
            invalidation_legs=(_price_leg(),),
            thesis=_thesis(),
            capital_protection_floor=_capital_protection_floor(),
        )


class TestThesisNature:
    """ALP-848: the thesis carries a directional / non-directional nature tag."""

    def test_directional_nature_constructs(self) -> None:
        thesis = _thesis(nature="directional")
        assert thesis.nature == "directional"

    def test_non_directional_nature_constructs(self) -> None:
        thesis = _thesis(nature="non_directional")
        assert thesis.nature == "non_directional"

    def test_rejects_unknown_nature(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            Thesis(
                summary="x",
                nature="sideways",  # type: ignore[arg-type]
                components=(_thesis_component("entry_rationale"),),
            )


class TestThesisShapedInvalidationSignal:
    """ALP-848: a price invalidation leg names the trigger signal it fires on."""

    def test_directional_leg_carries_underlying_signal(self) -> None:
        leg = _price_leg(trigger_signal="underlying_price")
        assert leg.trigger_signal == "underlying_price"

    def test_non_directional_leg_carries_option_price_signal(self) -> None:
        leg = _price_leg(trigger_signal="option_price")
        assert leg.trigger_signal == "option_price"

    def test_non_directional_leg_carries_net_mark_signal(self) -> None:
        leg = _price_leg(trigger_signal="net_mark")
        assert leg.trigger_signal == "net_mark"

    def test_rejects_unknown_trigger_signal(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PriceLeg(
                type="price",
                is_hard=True,
                trigger_signal="implied_vol",  # type: ignore[arg-type]
                condition=PriceCondition(
                    underlying_trigger="AAPL", comparator="<=", trigger_price=price(140.0)
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            )

    def test_directional_and_non_directional_open_carry_documented_signal(self) -> None:
        # AC2 end-to-end: a directional options OPEN carries the underlying
        # signal on its invalidation leg; a non-directional one carries the
        # option-price/net-mark signal. The thesis nature and the leg signal
        # coexist coherently on a fully-built OPEN.
        directional = _option_open_command(nature="directional")
        assert directional.thesis.nature == "directional"
        assert isinstance(directional.invalidation_legs[0], PriceLeg)
        assert directional.invalidation_legs[0].trigger_signal == "underlying_price"

        non_directional = _option_open_command(
            nature="non_directional",
            invalidation_legs=(_price_leg(trigger_signal="option_price"),),
        )
        assert non_directional.thesis.nature == "non_directional"
        assert isinstance(non_directional.invalidation_legs[0], PriceLeg)
        assert non_directional.invalidation_legs[0].trigger_signal == "option_price"


class TestThesisNatureLegConsistency:
    """ALP-848: OpenCommand cross-field validator pairs nature ⟺ trigger_signal.

    A directional thesis is invalidated by an underlying price level, so every
    PriceLeg must fire on ``underlying_price``; a non-directional (vol/spread)
    thesis is nonlinear in the underlying, so its PriceLegs must fire on
    ``option_price`` / ``net_mark``. An inconsistent OPEN silently mis-wires the
    03d stop, so the command boundary rejects it.
    """

    def test_directional_with_underlying_signal_accepts(self) -> None:
        cmd = _option_open_command(
            nature="directional",
            invalidation_legs=(_price_leg(trigger_signal="underlying_price"),),
        )
        assert cmd.thesis.nature == "directional"

    def test_non_directional_with_option_price_signal_accepts(self) -> None:
        cmd = _option_open_command(
            nature="non_directional",
            invalidation_legs=(_price_leg(trigger_signal="option_price"),),
        )
        assert cmd.thesis.nature == "non_directional"

    def test_directional_with_non_underlying_signal_rejects(self) -> None:
        with pytest.raises(ValueError, match="directional"):
            _option_open_command(
                nature="directional",
                invalidation_legs=(_price_leg(trigger_signal="option_price"),),
            )

    def test_non_directional_with_underlying_signal_rejects(self) -> None:
        with pytest.raises(ValueError, match="non_directional"):
            _option_open_command(
                nature="non_directional",
                invalidation_legs=(_price_leg(trigger_signal="underlying_price"),),
            )


class TestCapitalProtectionFloor:
    """ALP-848: a PnL-denominated, PM-authored, options-mandatory capital floor."""

    def test_floor_is_pnl_denominated(self) -> None:
        floor = CapitalProtectionFloor(max_loss=money(750.0))
        assert floor.max_loss == 750.0

    def test_floor_rejects_non_positive_max_loss(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            CapitalProtectionFloor(max_loss=money(0.0))

    def test_option_open_requires_floor(self) -> None:
        # An options OPEN missing the floor is rejected at the command boundary.
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _option_open_command(capital_protection_floor=None)
        assert "capital_protection_floor" in str(exc_info.value)

    def test_option_open_with_floor_constructs(self) -> None:
        cmd = _option_open_command()
        assert cmd.capital_protection_floor is not None
        assert cmd.capital_protection_floor.max_loss == 500.0

    def test_strategy_open_requires_floor(self) -> None:
        # The mandatory floor is options-scoped — a multi-leg strategy is an
        # options position too, so a floorless strategy OPEN is rejected. Use a
        # resting entry so the floor is the *only* violation: a market entry would
        # also fail (ALP-866), and which error surfaces would then depend on
        # validator declaration order rather than the floor invariant under test.
        with pytest.raises((ValueError, TypeError)) as exc_info:
            OpenCommand(
                command_type="open",
                instrument=_strategy_instrument(),
                entry_order=_entry_order_limit(),
                position_size=_position_size(),
                target=Target(
                    target_type="pl_percentage",
                    pl_percentage=80.0,
                    price=price(170.0),
                    order_type="limit",
                ),
                invalidation_legs=(_price_leg(),),
                thesis=_thesis(),
                capital_protection_floor=None,
            )
        assert "capital_protection_floor" in str(exc_info.value)

    def test_equity_open_validates_without_floor(self) -> None:
        # Equity OPENs are protected by the native bracket — the floor field is
        # options-scoped, so an equity OPEN validates with it absent.
        cmd = _open_command()
        assert cmd.capital_protection_floor is None

    def test_equity_open_rejects_floor(self) -> None:
        # The floor is options-only: attaching it to an equity OPEN is a
        # category error and is rejected.
        with pytest.raises((ValueError, TypeError)) as exc_info:
            OpenCommand(
                command_type="open",
                instrument=_equity_instrument(),
                entry_order=_entry_order_market(),
                position_size=_position_size(),
                target=_target_absolute(),
                invalidation_legs=(_price_leg(),),
                thesis=_thesis(),
                capital_protection_floor=_capital_protection_floor(),
            )
        assert "capital_protection_floor" in str(exc_info.value)

    def test_floor_below_dollar_value_constructs(self) -> None:
        # A floor whose max_loss is strictly below the position's planned
        # outlay derives a positive per-contract stop price — the only coherent
        # floor (ALP-856 / FL2). ``_option_open_command`` uses dollar_value=1_000.
        cmd = _option_open_command(
            capital_protection_floor=_capital_protection_floor(loss_limit=999.0)
        )
        assert cmd.capital_protection_floor is not None
        assert cmd.capital_protection_floor.max_loss == 999.0

    def test_floor_equal_to_dollar_value_rejected(self) -> None:
        # max_loss == dollar_value → floor stop price 0 → Alpaca 422 (FL2). The
        # floor must keep some capital at stake, so equality is rejected at the
        # command boundary rather than surfacing as a broker rejection.
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _option_open_command(
                capital_protection_floor=_capital_protection_floor(loss_limit=1_000.0)
            )
        assert "max_loss" in str(exc_info.value)

    def test_floor_exceeding_dollar_value_rejected(self) -> None:
        # max_loss > dollar_value → negative floor stop price → Alpaca 422 (FL2).
        with pytest.raises((ValueError, TypeError)) as exc_info:
            _option_open_command(
                capital_protection_floor=_capital_protection_floor(loss_limit=1_500.0)
            )
        assert "max_loss" in str(exc_info.value)


class TestOptionsEntryResting:
    """ALP-866: an options OPEN entry must rest (limit / stop_limit), never market.

    A market options entry can fill at the broker before/while the broker-enforced
    capital floor (ALP-856) submits; if the floor then fails, the FL1 cancel is a
    no-op on the already-filled entry, leaving a live, floorless options position
    (the husk class the broker-boundary redesign targets). The command boundary
    forbids a market options entry so the entry always rests and ``_cancel_live_entry``
    can retract it. Equity OPENs are unaffected — the native bracket protects them.
    """

    def test_option_open_market_entry_rejected(self) -> None:
        with pytest.raises(ValueError, match="rest"):
            _option_open_command(entry_order=_entry_order_market())

    def test_strategy_open_market_entry_rejected(self) -> None:
        # A multi-leg strategy is an options position too — a market entry on a
        # strategy OPEN is rejected for the same fill-before-floor reason.
        with pytest.raises(ValueError, match="rest"):
            OpenCommand(
                command_type="open",
                instrument=_strategy_instrument(),
                entry_order=_entry_order_market(),
                position_size=_position_size(),
                target=Target(
                    target_type="pl_percentage",
                    pl_percentage=80.0,
                    price=price(170.0),
                    order_type="limit",
                ),
                invalidation_legs=(_price_leg(),),
                thesis=_thesis(),
                capital_protection_floor=_capital_protection_floor(),
            )

    def test_option_open_limit_entry_constructs(self) -> None:
        cmd = _option_open_command(entry_order=_entry_order_limit())
        assert cmd.entry_order.type == "limit"

    def test_option_open_stop_limit_entry_constructs(self) -> None:
        cmd = _option_open_command(entry_order=_entry_order_stop_limit())
        assert cmd.entry_order.type == "stop_limit"

    def test_equity_open_market_entry_constructs(self) -> None:
        # The resting constraint is options-scoped: an equity OPEN keeps its
        # market entry (the native bracket protects it). Guards against the
        # validator being mis-scoped to equity.
        cmd = _open_command()
        assert isinstance(cmd.instrument, EquityInstrument)
        assert cmd.entry_order.type == "market"


class TestCloseCommand:
    def test_constructs_happy_path(self) -> None:
        cmd = _close_command()
        assert cmd.command_type == "close"
        assert cmd.quantity == "all"

    def test_close_rationale_type_excludes_tactical_exit(self) -> None:
        # Parent decision (E): canonical enum is the four design values; no tactical_exit.
        with pytest.raises((ValueError, TypeError)):
            CloseCommand(
                command_type="close",
                position_id=PositionId("pos-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="tactical_exit",  # type: ignore[arg-type]
            )

    def test_limit_order_requires_limit_price(self) -> None:
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
            AddCommand(
                command_type="add",
                position_id=PositionId("pos-1"),
                additional_quantity=10.0,
                additional_dollar_value=money(1500.0),
                entry_order=_entry_order_market(),
                thesis_addition_component=_thesis_component("target_rationale"),
            )

    def test_bracket_adjustment_requires_at_least_one_field(self) -> None:
        with pytest.raises((ValueError, TypeError)):
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
        with pytest.raises((ValueError, TypeError)):
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
