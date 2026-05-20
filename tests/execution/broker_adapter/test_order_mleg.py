"""Tests for ``alphamind.execution.broker_adapter.order_mleg`` (story ALP-382).

Translates canonical OMS commands carrying ``StrategyInstrument`` into
alpaca-py ``OrderClass.MLEG`` requests with up to 4 legs, per-leg
``position_intent``, and ratio_qty in simplified form (GCD = 1). Submits via
``TradingClient.submit_order(...)`` wrapped by ``submit_with_retry``.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    OptionLegRequest,
)

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
    CommandId,
    OccSymbol,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    AddCommand,
    BracketOrderParameters,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    StrategyInstrument,
    StrategyLeg,
    StrategyType,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.config.models.execution import (
    ExecutionConfig,
    FeeSchedule,
    GreeksRefresh,
    PaperHarness,
)
from alphamind.config.models.execution import OrderType as ExecOrderType
from alphamind.execution.broker_adapter import (
    GatewaySubmissionFailed,
    MLEGLegAck,
    MLEGSubmission,
    Submitted,
    submit_mleg_add,
    submit_mleg_close,
    submit_mleg_open,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _execution_config(window_seconds: int = 5) -> ExecutionConfig:
    """Return a minimal valid ExecutionConfig with the given retry window."""
    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=15, move_trigger_pct=0.02),
        conservative_delta_buffer_pct=0.05,
        submission_retry_window_seconds=window_seconds,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.001,
            impact_coefficients={
                ExecOrderType.market: 0.001,
                ExecOrderType.limit: 0.0005,
                ExecOrderType.stop: 0.0015,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.01,
    )


def _strategy_open_command(
    *,
    strategy_type: StrategyType = "vertical_spread",
    underlying: str = "NVDA",
    legs: tuple[StrategyLeg, ...] | None = None,
    quantity: float = 1.0,
    entry_type: str = "market",
    limit_price: float | None = None,
    command_id: str = "inv-test.ENV-SA-1.1.1",
) -> OpenCommand:
    """Build an OpenCommand carrying a StrategyInstrument."""
    if legs is None:
        legs = (
            StrategyLeg(
                strike=price(800.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="long",
                quantity_ratio=1,
            ),
            StrategyLeg(
                strike=price(820.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="short",
                quantity_ratio=1,
            ),
        )
    instrument = StrategyInstrument(
        asset_type="strategy",
        strategy_type=strategy_type,
        underlying=underlying,
        legs=legs,
    )
    entry = EntryOrder(type=entry_type, limit_price=limit_price)  # type: ignore[arg-type]
    target = Target(target_type="absolute_price", price=price(850.0), order_type="limit")
    invalidation = (
        PriceLeg(
            type="price",
            is_hard=True,
            condition=PriceCondition(
                underlying_trigger=underlying, comparator="<=", trigger_price=price(780.0)
            ),
            order_parameters=BracketOrderParameters(order_type="market"),
        ),
    )
    thesis = Thesis(
        summary="test thesis",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference=underlying,
                narrative="bullish setup",
                key_assumptions=("earnings beat",),
            ),
        ),
    )
    return OpenCommand(
        command_id=CommandId(command_id),
        command_type="open",
        instrument=instrument,
        entry_order=entry,
        position_size=PositionSize(quantity=quantity, dollar_value=money(1000.0)),
        target=target,
        invalidation_legs=invalidation,
        thesis=thesis,
    )


def _vertical_spread_legs() -> tuple[StrategyLeg, ...]:
    return (
        StrategyLeg(
            strike=price(800.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(820.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
            quantity_ratio=1,
        ),
    )


def _iron_condor_legs() -> tuple[StrategyLeg, ...]:
    return (
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="short",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(830.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="long",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(900.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(920.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
    )


def _make_api_error(status: int, code: int | str, message: str) -> APIError:
    """Build an ``APIError`` whose ``.status_code`` and ``.message`` resolve."""
    body = json.dumps({"code": code, "message": message})
    fake_http_error = MagicMock()
    fake_http_error.response.status_code = status
    return cast(APIError, cast(Any, APIError)(body, http_error=fake_http_error))


def _fake_alpaca_order(
    *,
    order_id: str = "alpaca-mleg-1",
    client_order_id: str,
    status: str = "accepted",
    legs: list[Any] | None = None,
) -> Any:
    """Return a duck-typed Alpaca Order surrogate sufficient for our translator."""
    order = MagicMock()
    order.id = order_id
    order.client_order_id = client_order_id
    order.status = MagicMock(value=status)
    order.order_class = MagicMock(value="mleg")
    order.legs = legs
    return order


class _CapturingClient:
    """Stub alpaca-py TradingClient that records the request it receives."""

    def __init__(
        self,
        *,
        response: Any | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.captured_request: Any = None
        self._response = response
        self._raise_exc = raise_exc

    def submit_order(self, request: Any) -> Any:
        self.captured_request = request
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_module_exports_public_symbols() -> None:
    """All five public symbols re-export from the package root."""
    import alphamind.execution.broker_adapter as adapter

    for name in (
        "submit_mleg_open",
        "submit_mleg_add",
        "submit_mleg_close",
        "MLEGSubmission",
        "MLEGLegAck",
    ):
        assert hasattr(adapter, name), f"missing public symbol: {name}"
        assert name in adapter.__all__, f"{name} missing from __all__"


def test_mleg_submission_is_frozen_dataclass() -> None:
    submission = MLEGSubmission(
        alpaca_order_id=AlpacaOrderId("x"),
        client_order_id=ClientOrderId("inv-y"),
        status="accepted",
        legs=(),
        strategy_type="vertical_spread",
    )
    with pytest.raises(FrozenInstanceError):
        cast(Any, submission).status = "filled"


def test_mleg_leg_ack_is_frozen_dataclass() -> None:
    leg = MLEGLegAck(
        occ_symbol=OccSymbol("NVDA  260619C00800000"),
        side="buy",
        ratio_qty=1,
        position_intent="buy_to_open",
    )
    with pytest.raises(FrozenInstanceError):
        cast(Any, leg).side = "sell"


# ---------------------------------------------------------------------------
# submit_mleg_open — request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_vertical_spread_constructs_mleg_request_with_two_legs() -> None:
    """A 2-leg vertical-spread OPEN produces an OrderClass.MLEG request."""
    command = _strategy_open_command(strategy_type="vertical_spread", legs=_vertical_spread_legs())
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    outcome = await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    assert isinstance(outcome, Submitted)
    request = client.captured_request
    assert request is not None
    assert request.order_class == OrderClass.MLEG
    assert request.time_in_force == TimeInForce.DAY
    assert request.client_order_id == "inv-test.ENV-SA-1.1.1"
    assert request.qty == 1.0
    assert request.legs is not None
    assert len(request.legs) == 2
    leg0, leg1 = request.legs
    assert isinstance(leg0, OptionLegRequest)
    assert isinstance(leg1, OptionLegRequest)
    # Each leg has symbol + ratio_qty + position_intent
    assert leg0.symbol  # OCC symbol
    assert leg0.ratio_qty > 0
    assert leg0.position_intent in {
        PositionIntent.BUY_TO_OPEN,
        PositionIntent.SELL_TO_OPEN,
    }


@pytest.mark.asyncio
async def test_open_more_than_four_legs_rejected_before_sdk_call() -> None:
    """Strategy with > 4 legs raises ValueError before SDK touch."""
    five_legs = (
        StrategyLeg(
            strike=price(float(800 + i * 10)),
            expiration="2026-06-19",
            contract_type="call",
            direction="long" if i % 2 == 0 else "short",
            quantity_ratio=1,
        )
        for i in range(5)
    )
    command = _strategy_open_command(strategy_type="custom", legs=tuple(five_legs))
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="up to 4 legs"):
        await submit_mleg_open(
            command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
        )

    assert client.captured_request is None  # SDK never called


# ---------------------------------------------------------------------------
# All five named strategy types — basic construction
# ---------------------------------------------------------------------------


def _calendar_spread_legs() -> tuple[StrategyLeg, ...]:
    """Same strike, different expiration — long the longer-dated leg."""
    return (
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-09-18",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
    )


def _straddle_legs() -> tuple[StrategyLeg, ...]:
    """Long call + long put at the same strike + expiration."""
    return (
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="long",
            quantity_ratio=1,
        ),
    )


def _strangle_legs() -> tuple[StrategyLeg, ...]:
    """Long call + long put at different strikes (otm both)."""
    return (
        StrategyLeg(
            strike=price(900.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(800.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="long",
            quantity_ratio=1,
        ),
    )


def _custom_legs() -> tuple[StrategyLeg, ...]:
    """Three-leg custom combination."""
    return (
        StrategyLeg(
            strike=price(800.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
        StrategyLeg(
            strike=price(820.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
            quantity_ratio=2,
        ),
        StrategyLeg(
            strike=price(840.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=1,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy_type", "legs_factory", "expected_count"),
    [
        ("vertical_spread", _vertical_spread_legs, 2),
        ("calendar_spread", _calendar_spread_legs, 2),
        ("straddle", _straddle_legs, 2),
        ("strangle", _strangle_legs, 2),
        ("iron_condor", _iron_condor_legs, 4),
        ("custom", _custom_legs, 3),
    ],
)
async def test_open_constructs_each_named_strategy_type(
    strategy_type: StrategyType,
    legs_factory: Any,
    expected_count: int,
) -> None:
    """Each of the five named strategy types + custom builds an mleg request."""
    command = _strategy_open_command(strategy_type=strategy_type, legs=legs_factory())
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    outcome = await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    assert isinstance(outcome, Submitted)
    request = client.captured_request
    assert request.order_class == OrderClass.MLEG
    assert len(request.legs) == expected_count
    assert outcome.payload.strategy_type == strategy_type


# ---------------------------------------------------------------------------
# strategy_legs_to_close_acks — the single open→close inversion seam
# ---------------------------------------------------------------------------


def _persisted_strategy_leg(
    *,
    leg_id: str,
    direction: Any,
    strike: float = 800.0,
    contract_type: Any = None,
) -> Any:
    """Build a portfolio-state ``StrategyLeg`` carrying an open-side direction."""
    from datetime import date as _date

    from alphamind.portfolio_state.records.positions import (
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
    )
    from alphamind.portfolio_state.records.positions import (
        StrategyLeg as PersistedStrategyLeg,
    )

    return PersistedStrategyLeg(
        leg_id=leg_id,
        direction=direction,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=strike,
            expiration_date=_date(2026, 6, 19),
            contract_type=contract_type or OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=8.0,
            greeks=OptionGreeks(delta=0.5, gamma=0.02, theta=-0.1, vega=0.3, iv_used=0.25),
        ),
    )


def test_strategy_legs_to_close_acks_inverts_each_open_direction() -> None:
    """A LONG-opened leg closes sell/sell_to_close; a SHORT-opened leg closes buy/buy_to_close."""
    from alphamind.execution.broker_adapter.order_mleg import strategy_legs_to_close_acks
    from alphamind.portfolio_state.records.positions import Direction

    legs = (
        _persisted_strategy_leg(leg_id="leg-1", direction=Direction.LONG, strike=800.0),
        _persisted_strategy_leg(leg_id="leg-2", direction=Direction.SHORT, strike=820.0),
    )

    acks = strategy_legs_to_close_acks(legs)

    assert [ack.side for ack in acks] == ["sell", "buy"]
    assert [ack.position_intent for ack in acks] == ["sell_to_close", "buy_to_close"]
    assert [ack.occ_symbol for ack in acks] == [
        "NVDA  260619C00800000",
        "NVDA  260619C00820000",
    ]


def test_strategy_legs_to_close_acks_rejects_leg_without_direction() -> None:
    """A leg whose ``direction`` is unset cannot be reversed — raises ValueError."""
    from alphamind.execution.broker_adapter.order_mleg import strategy_legs_to_close_acks

    legs = (_persisted_strategy_leg(leg_id="leg-x", direction=None),)

    with pytest.raises(ValueError, match="direction"):
        strategy_legs_to_close_acks(legs)


# ---------------------------------------------------------------------------
# submit_mleg_close
# ---------------------------------------------------------------------------


def _close_legs_nvda_vertical() -> tuple[MLEGLegAck, ...]:
    """Close-side legs for a NVDA vertical spread (long call, short call).

    A LONG-opened leg closes ``sell`` / ``sell_to_close``; a SHORT-opened leg
    closes ``buy`` / ``buy_to_close``. ``submit_mleg_close`` receives these
    close-side legs directly — the inversion happens upstream at the seam.
    """
    return (
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00800000"),
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_close",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00820000"),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_close",
        ),
    )


@pytest.mark.asyncio
async def test_close_translates_close_side_legs_straight() -> None:
    """CLOSE submits the close-side legs it receives without re-inverting them."""
    close_legs = _close_legs_nvda_vertical()
    close_command = CloseCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity="all",
        order_type="market",
        close_rationale_type="target_reached",
    )
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    outcome = await submit_mleg_close(
        close_command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
        close_legs=close_legs,
        strategy_type="vertical_spread",
        position_units=2.0,
    )

    assert isinstance(outcome, Submitted)
    request = client.captured_request
    assert request.qty == 2.0
    assert request.order_class == OrderClass.MLEG
    assert request.time_in_force == TimeInForce.DAY
    intents = [leg.position_intent for leg in request.legs]
    sides = [leg.side for leg in request.legs]
    assert intents == [PositionIntent.SELL_TO_CLOSE, PositionIntent.BUY_TO_CLOSE]
    assert sides == [OrderSide.SELL, OrderSide.BUY]
    # OCC symbols carry over unchanged from the close legs.
    assert [leg.symbol for leg in request.legs] == [
        "NVDA  260619C00800000",
        "NVDA  260619C00820000",
    ]
    # Strategy_type echoes from the close call.
    assert outcome.payload.strategy_type == "vertical_spread"


@pytest.mark.asyncio
async def test_close_rejects_non_close_position_intent() -> None:
    """A leg carrying an open-side intent is rejected — close legs must be ``*_to_close``."""
    open_side_legs = (
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00800000"),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00820000"),
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_open",
        ),
    )
    close_command = CloseCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity=1.0,
        order_type="market",
        close_rationale_type="target_reached",
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="non-close position_intent"):
        await submit_mleg_close(
            close_command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
            close_legs=open_side_legs,
            strategy_type="vertical_spread",
        )


@pytest.mark.asyncio
async def test_close_with_multi_underlying_close_legs_rejected() -> None:
    """Close legs that span multiple underlyings raise ValueError."""
    close_legs = (
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00800000"),
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_close",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol("AAPL  260619C00150000"),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_close",
        ),
    )
    close_command = CloseCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity=1.0,
        order_type="market",
        close_rationale_type="target_reached",
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="multiple underlyings"):
        await submit_mleg_close(
            close_command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
            close_legs=close_legs,
            strategy_type="custom",
        )


@pytest.mark.asyncio
async def test_close_with_limit_price_uses_limit_order_request() -> None:
    """Close with a net-credit limit price submits a LimitOrderRequest mleg."""
    close_legs = _close_legs_nvda_vertical()
    close_command = CloseCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity=1.0,
        order_type="limit",
        limit_price=price(2.50),
        close_rationale_type="target_reached",
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1"))

    await submit_mleg_close(
        close_command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
        close_legs=close_legs,
        strategy_type="vertical_spread",
    )

    request = client.captured_request
    assert isinstance(request, LimitOrderRequest)
    assert request.limit_price == 2.50


@pytest.mark.asyncio
async def test_close_with_quantity_all_requires_position_units() -> None:
    """``quantity="all"`` without ``position_units`` raises ValueError."""
    close_legs = _close_legs_nvda_vertical()
    close_command = CloseCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="close",
        position_id=PositionId("pos-1"),
        quantity="all",
        order_type="market",
        close_rationale_type="target_reached",
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="position_units"):
        await submit_mleg_close(
            close_command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
            close_legs=close_legs,
            strategy_type="vertical_spread",
        )


# ---------------------------------------------------------------------------
# submit_mleg_add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_scales_ratios_by_additional_quantity_preserving_intent() -> None:
    """ADD multiplies each leg's ratio by additional_quantity then re-simplifies.

    Open legs (1, 1) scaled by 3 → (3, 3) → re-simplified (1, 1). Position
    intents stay buy_to_open / sell_to_open (no inversion on ADD).
    """
    open_legs = (
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00800000"),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00820000"),
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_open",
        ),
    )
    add_command = AddCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="add",
        position_id=PositionId("pos-1"),
        additional_quantity=3.0,
        additional_dollar_value=money(3000.0),
        entry_order=EntryOrder(type="market"),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="entry",
            instrument_reference="NVDA",
            narrative="x",
            key_assumptions=("x",),
        ),
    )
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    outcome = await submit_mleg_add(
        add_command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
        open_legs=open_legs,
        strategy_type="vertical_spread",
    )

    assert isinstance(outcome, Submitted)
    request = client.captured_request
    intents = [leg.position_intent for leg in request.legs]
    # No inversion: opens stay opens.
    assert intents == [PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN]
    # qty reflects the additional units the strategy is scaling by.
    assert request.qty == 3.0
    # Ratios re-simplified from (1*3, 1*3)=(3,3) → (1,1) — no-op via gcd.
    assert tuple(int(leg.ratio_qty) for leg in request.legs) == (1, 1)


@pytest.mark.asyncio
async def test_add_with_uneven_ratios_resimplifies() -> None:
    """ADD scales (1, 2) by 4 → (4, 8) → simplifies to (1, 2)."""
    open_legs = (
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00800000"),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol("NVDA  260619C00820000"),
            side="sell",
            ratio_qty=2,
            position_intent="sell_to_open",
        ),
    )
    add_command = AddCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="add",
        position_id=PositionId("pos-1"),
        additional_quantity=4.0,
        additional_dollar_value=money(4000.0),
        entry_order=EntryOrder(type="market"),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="entry",
            instrument_reference="NVDA",
            narrative="x",
            key_assumptions=("x",),
        ),
    )
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    await submit_mleg_add(
        add_command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
        open_legs=open_legs,
        strategy_type="vertical_spread",
    )

    request = client.captured_request
    assert tuple(int(leg.ratio_qty) for leg in request.legs) == (1, 2)


@pytest.mark.asyncio
async def test_open_retries_on_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient failures retry until success per ExecutionConfig window."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    command = _strategy_open_command(legs=_vertical_spread_legs())
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")

    attempts = [0]
    captured: list[Any] = []

    class _FlakyClient:
        def submit_order(self, request: Any) -> Any:
            captured.append(request)
            attempts[0] += 1
            if attempts[0] < 3:
                # 503-style transient: classified as None by classify_alpaca_error
                raise _make_api_error(503, code=1, message="service unavailable")
            return response

    outcome = await submit_mleg_open(
        command,
        client=cast(Any, _FlakyClient()),
        execution=_execution_config(window_seconds=10),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    assert isinstance(outcome, Submitted)
    assert outcome.attempt_count == 3
    assert len(captured) == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_open_returns_gateway_failure_on_window_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient failures past the window return GatewaySubmissionFailed."""
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    async def fake_sleep(seconds: float) -> None:
        fake_now[0] += seconds

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    command = _strategy_open_command(legs=_vertical_spread_legs())

    class _AlwaysFailsClient:
        def submit_order(self, request: Any) -> Any:
            fake_now[0] += 0.1
            raise _make_api_error(503, code=1, message="bad gateway")

    outcome = await submit_mleg_open(
        command,
        client=cast(Any, _AlwaysFailsClient()),
        execution=_execution_config(window_seconds=2),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    assert isinstance(outcome, GatewaySubmissionFailed)
    assert outcome.attempt_count >= 2


@pytest.mark.asyncio
async def test_open_invalid_legs_rejection_reraises_for_strategist_substitution() -> None:
    """``invalid_legs`` rejection re-raises so the strategist can substitute."""
    command = _strategy_open_command(legs=_vertical_spread_legs())
    invalid_legs_err = _make_api_error(422, code=42210000, message="invalid legs[1].ratio_qty")
    client = _CapturingClient(raise_exc=invalid_legs_err)

    with pytest.raises(APIError) as exc_info:
        await submit_mleg_open(
            command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
        )

    # The raised exception's message identifies the offending leg index per
    # broker-adapter.md § Options-specific rejections.
    assert "legs[1]" in exc_info.value.message
    # And it's still classified as invalid_legs by the existing taxonomy.
    from alphamind.execution.broker_adapter import classify_alpaca_error

    rejection = classify_alpaca_error(exc_info.value)
    assert rejection is not None
    assert rejection.code == "invalid_legs"


@pytest.mark.asyncio
async def test_open_with_equity_instrument_raises_type_error() -> None:
    """Equity instrument routes to submit_equity_*; mleg path rejects."""
    equity_open = OpenCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=Symbol("NVDA"), direction="long"),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=10, dollar_value=money(8000.0)),
        target=Target(target_type="absolute_price", price=price(850.0), order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger="NVDA", comparator="<=", trigger_price=price(780.0)
                ),
                order_parameters=BracketOrderParameters(order_type="market"),
            ),
        ),
        thesis=Thesis(
            summary="x",
            components=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference="NVDA",
                    narrative="x",
                    key_assumptions=("x",),
                ),
            ),
        ),
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(TypeError, match="StrategyInstrument"):
        await submit_mleg_open(
            equity_open,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
        )


@pytest.mark.asyncio
async def test_open_with_single_leg_option_instrument_raises_type_error() -> None:
    """Single-leg option routes to submit_options_*; mleg path rejects."""
    option_open = OpenCommand(
        command_id=CommandId("inv-test.ENV-SA-1.1.1"),
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying=Symbol("NVDA"),
            strike=price(800.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
        ),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=1, dollar_value=money(200.0)),
        target=Target(target_type="absolute_price", price=price(820.0), order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger="NVDA", comparator="<=", trigger_price=price(780.0)
                ),
                order_parameters=BracketOrderParameters(order_type="market"),
            ),
        ),
        thesis=Thesis(
            summary="x",
            components=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference="NVDA",
                    narrative="x",
                    key_assumptions=("x",),
                ),
            ),
        ),
    )
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(TypeError, match="StrategyInstrument"):
        await submit_mleg_open(
            option_open,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["", "garbage", "MOM.123", "inv", "monitor.x.1.1"],
)
async def test_open_rejects_malformed_client_order_id(bad_id: str) -> None:
    """``client_order_id`` must start with ``inv-`` or ``MON.``."""
    command = _strategy_open_command(legs=_vertical_spread_legs())
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="client_order_id"):
        await submit_mleg_open(
            command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id=bad_id,
        )

    assert client.captured_request is None


@pytest.mark.asyncio
async def test_open_constructs_occ_symbols_with_root_yymmdd_strike() -> None:
    """Each leg's OCC symbol encodes underlying + expiration + C/P + strike."""
    command = _strategy_open_command(underlying=Symbol("NVDA"), legs=_vertical_spread_legs())
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1"))

    await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    request = client.captured_request
    occ_symbols = [leg.symbol for leg in request.legs]
    # Format: NVDA  + 260619 + C + 00800000 (strike 800.0 in thousandths)
    assert occ_symbols[0] == "NVDA  260619C00800000"
    assert occ_symbols[1] == "NVDA  260619C00820000"
    # Each is 21 characters per OCC standard.
    for s in occ_symbols:
        assert len(s) == 21


@pytest.mark.asyncio
async def test_open_returns_submitted_with_mleg_submission_payload() -> None:
    """On success returns Submitted carrying parent ID, per-leg acks, strategy_type."""
    command = _strategy_open_command(legs=_vertical_spread_legs())
    response = _fake_alpaca_order(
        order_id=OrderId("alpaca-strategy-id-42"),
        client_order_id="inv-test.ENV-SA-1.1.1",
        status="accepted",
    )
    client = _CapturingClient(response=response)

    outcome = await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    assert isinstance(outcome, Submitted)
    payload = outcome.payload
    assert isinstance(payload, MLEGSubmission)
    assert payload.alpaca_order_id == "alpaca-strategy-id-42"
    assert payload.client_order_id == "inv-test.ENV-SA-1.1.1"
    assert payload.status == "accepted"
    assert payload.strategy_type == "vertical_spread"
    assert len(payload.legs) == 2
    assert payload.legs[0].position_intent == "buy_to_open"
    assert payload.legs[1].position_intent == "sell_to_open"
    assert payload.legs[0].side == "buy"
    assert payload.legs[1].side == "sell"


@pytest.mark.asyncio
async def test_open_position_intent_per_leg_direction() -> None:
    """OPEN: long leg → buy_to_open, short leg → sell_to_open."""
    legs = _vertical_spread_legs()  # (long call, short call)
    command = _strategy_open_command(legs=legs)
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1"))

    await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    request = client.captured_request
    intents = [leg.position_intent for leg in request.legs]
    sides = [leg.side for leg in request.legs]
    assert intents == [PositionIntent.BUY_TO_OPEN, PositionIntent.SELL_TO_OPEN]
    assert sides == [OrderSide.BUY, OrderSide.SELL]


@pytest.mark.asyncio
async def test_open_simplifies_ratios_with_common_factor_two() -> None:
    """Legs (2, 4, 2, 4) simplify to (1, 2, 1, 2) in the request."""
    legs = (
        StrategyLeg(
            strike=price(850.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="short",
            quantity_ratio=2,
        ),
        StrategyLeg(
            strike=price(830.0),
            expiration="2026-06-19",
            contract_type="put",
            direction="long",
            quantity_ratio=4,
        ),
        StrategyLeg(
            strike=price(900.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="short",
            quantity_ratio=2,
        ),
        StrategyLeg(
            strike=price(920.0),
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
            quantity_ratio=4,
        ),
    )
    command = _strategy_open_command(strategy_type="custom", legs=legs)
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1"))

    await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    request = client.captured_request
    assert tuple(int(leg.ratio_qty) for leg in request.legs) == (1, 2, 1, 2)


@pytest.mark.asyncio
async def test_open_simplifies_ratios_no_op_when_already_coprime() -> None:
    """Legs (1, 1) stay (1, 1) (gcd=1 already)."""
    command = _strategy_open_command(legs=_vertical_spread_legs())
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1"))

    await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    request = client.captured_request
    assert tuple(int(leg.ratio_qty) for leg in request.legs) == (1, 1)


@pytest.mark.asyncio
async def test_open_empty_underlying_rejected() -> None:
    """A strategy with no resolvable underlying raises ValueError before SDK call.

    Multi-underlying is structurally prevented by the canonical
    ``StrategyInstrument.underlying`` Pydantic constraint
    (``min_length=1``); the broker-adapter's defensive validator catches
    bypass attempts via ``model_construct`` (which skips validation) by
    asserting that the resolved underlying is non-empty.
    """
    legs = _vertical_spread_legs()
    # Bypass Pydantic validation to simulate a multi-underlying / empty
    # underlying construction the canonical schema would normally reject.
    instrument = StrategyInstrument.model_construct(
        asset_type="strategy",
        strategy_type="vertical_spread",
        underlying=Symbol(""),  # canonically rejected; bypass to exercise our guard
        legs=legs,
    )
    command = _strategy_open_command()
    command = command.model_copy(update={"instrument": instrument})
    client = _CapturingClient(response=_fake_alpaca_order(client_order_id="inv-x"))

    with pytest.raises(ValueError, match="underlying"):
        await submit_mleg_open(
            command,
            client=cast(Any, client),
            execution=_execution_config(),
            client_order_id="inv-test.ENV-SA-1.1.1",
        )

    assert client.captured_request is None


@pytest.mark.asyncio
async def test_open_iron_condor_constructs_four_legs() -> None:
    """A 4-leg iron-condor OPEN produces legs[] of length 4."""
    command = _strategy_open_command(strategy_type="iron_condor", legs=_iron_condor_legs())
    response = _fake_alpaca_order(client_order_id="inv-test.ENV-SA-1.1.1")
    client = _CapturingClient(response=response)

    await submit_mleg_open(
        command,
        client=cast(Any, client),
        execution=_execution_config(),
        client_order_id="inv-test.ENV-SA-1.1.1",
    )

    request = client.captured_request
    assert request is not None
    assert len(request.legs) == 4
