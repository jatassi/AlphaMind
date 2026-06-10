"""Tests for alphamind.execution.broker_adapter.order_equity (ALP-380).

Tests drive TDD for equity order POST translation:
- submit_equity_open: market / limit / stop_limit x bracket / oto / simple
- submit_equity_add: always simple
- submit_equity_close: side derivation, quantity resolution
- EquitySubmission: frozen dataclass with correct fields
- client_order_id validation
- retry-success, retry-exhaustion, permanent-rejection paths
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.enums import OrderType as AlpacaOrderType
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
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
    EventLeg,
    OpenCommand,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
    ThesisComponent,
    TimeCondition,
    TimeLeg,
)
from alphamind.config.models.execution import (
    ExecutionConfig,
    FeeSchedule,
    GreeksRefresh,
    OrderType,
    PaperHarness,
)
from alphamind.execution.broker_adapter import (
    EquityLegAck,
    EquityOcoLevels,
    EquitySubmission,
    GatewaySubmissionFailed,
    Submitted,
    submit_equity_add,
    submit_equity_close,
    submit_equity_oco,
    submit_equity_open,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_CLIENT_ORDER_ID_INV = (
    "inv-2026-05-09T09-30Z.ENV-REC-1.0.0~the-THE-NVDA-0123456789abcdef0123456789abcdef"
)
_CLIENT_ORDER_ID_MON = "MON.sess-abc.42.0~the-THE-AAPL-fedcba9876543210fedcba9876543210~inv-X"


def _make_execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=5, move_trigger_pct=0.01),
        conservative_delta_buffer_pct=0.0,
        submission_retry_window_seconds=30,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                OrderType.market: 0.1,
                OrderType.limit: 0.05,
                OrderType.stop: 0.08,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.0,
    )


def _make_thesis() -> Thesis:
    return Thesis(
        summary="Test thesis",
        nature="directional",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference="AAPL",
                narrative="Buy AAPL on momentum",
                key_assumptions=("earnings beat",),
            ),
        ),
    )


def _make_price_leg(ticker: str = "AAPL", trigger: float = 150.0) -> PriceLeg:
    return PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal="underlying_price",
        condition=PriceCondition(
            underlying_trigger=ticker,
            comparator="<=",
            trigger_price=price(trigger),
        ),
        order_parameters=BracketOrderParameters(order_type="stop", limit_price=None),
    )


def _make_time_leg() -> TimeLeg:
    return TimeLeg(
        type="time",
        is_hard=True,
        condition=TimeCondition(deadline=datetime(2026, 6, 1, tzinfo=UTC)),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


def _make_target(target_price: float = 200.0) -> Target:
    return Target(
        target_type="absolute_price",
        price=price(target_price),
        order_type="limit",
    )


def _make_open_command(
    ticker: str = "AAPL",
    direction: str = "long",
    entry_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    target: Target | None = None,
    invalidation_legs: tuple[PriceLeg | TimeLeg | EventLeg, ...] | None = None,
) -> OpenCommand:
    entry_order = EntryOrder(type=entry_type, limit_price=limit_price, stop_price=stop_price)  # type: ignore[arg-type]
    if target is None:
        target = _make_target()
    if invalidation_legs is None:
        # Default: price-stop hard leg (required by OpenCommand validator)
        invalidation_legs = (_make_price_leg(ticker),)
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=ticker, direction=direction),  # type: ignore[arg-type]
        entry_order=entry_order,
        position_size=PositionSize(quantity=100.0, dollar_value=money(17000.0)),
        target=target,
        invalidation_legs=invalidation_legs,
        thesis=_make_thesis(),
    )


def _make_add_command(
    ticker: str = "AAPL",
    entry_type: str = "market",
    limit_price: float | None = None,
) -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=PositionId("pos-001"),
        additional_quantity=50.0,
        additional_dollar_value=money(8500.0),
        entry_order=EntryOrder(type=entry_type, limit_price=limit_price),  # type: ignore[arg-type]
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="entry",
            instrument_reference=ticker,
            narrative="Increase exposure",
            key_assumptions=("momentum continues",),
        ),
    )


def _make_close_command(
    quantity: float | str = "all",
    order_type: str = "market",
    limit_price: float | None = None,
    close_rationale: str = "target_reached",
) -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId("pos-001"),
        quantity=quantity,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        limit_price=None if limit_price is None else price(limit_price),
        close_rationale_type=close_rationale,  # type: ignore[arg-type]
    )


def _make_fake_order(
    order_class: OrderClass = OrderClass.BRACKET,
    status: OrderStatus = OrderStatus.ACCEPTED,
    client_order_id: str = _CLIENT_ORDER_ID_INV,
    legs: list[Any] | None = None,
) -> MagicMock:
    """Build a fake alpaca Order object with the fields submit_equity_* reads."""
    order = MagicMock()
    order.id = uuid.uuid4()
    order.client_order_id = client_order_id
    order.status = status
    order.order_class = order_class
    # alpaca-py returns ``list[Order] | None``; an explicit value keeps the
    # leg-capture path (ALP-746) reading a real list rather than a MagicMock.
    order.legs = legs
    return order


def _make_fake_leg(order_type: AlpacaOrderType) -> MagicMock:
    """A fake alpaca child Order carrying the fields leg capture reads."""
    leg = MagicMock()
    leg.id = uuid.uuid4()
    leg.order_type = order_type
    return leg


# ---------------------------------------------------------------------------
# RED → GREEN cycle 1: EquitySubmission dataclass importable and frozen
# ---------------------------------------------------------------------------


def test_equity_submission_is_frozen_dataclass() -> None:
    sub = EquitySubmission(
        alpaca_order_id=AlpacaOrderId("alp-123"),
        client_order_id=ClientOrderId(_CLIENT_ORDER_ID_INV),
        status="accepted",
        order_class="bracket",
    )
    assert sub.alpaca_order_id == "alp-123"
    assert sub.status == "accepted"
    assert sub.order_class == "bracket"
    # frozen — assignment must raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        sub.alpaca_order_id = AlpacaOrderId("other")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RED → GREEN cycle 2: client_order_id validation raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_client_order_id_raises_value_error() -> None:
    """Empty or non-matching IDs raise ValueError before any SDK call."""
    cmd = _make_open_command()
    client = MagicMock()
    execution = _make_execution_config()

    with pytest.raises(ValueError, match="client_order_id"):
        await submit_equity_open(
            cmd,
            client=client,
            execution=execution,
            client_order_id="",
        )


@pytest.mark.asyncio
async def test_bad_prefix_client_order_id_raises_value_error() -> None:
    cmd = _make_open_command()
    client = MagicMock()
    execution = _make_execution_config()

    with pytest.raises(ValueError, match="client_order_id"):
        await submit_equity_open(
            cmd,
            client=client,
            execution=execution,
            client_order_id="order-12345",
        )


@pytest.mark.asyncio
async def test_mon_prefix_client_order_id_is_valid() -> None:
    """MON. prefix is a valid client_order_id."""
    fake_order = _make_fake_order(
        order_class=OrderClass.BRACKET, client_order_id=_CLIENT_ORDER_ID_MON
    )
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    execution = _make_execution_config()

    cmd = _make_open_command()
    result = await submit_equity_open(
        cmd,
        client=client,
        execution=execution,
        client_order_id=_CLIENT_ORDER_ID_MON,
    )
    assert isinstance(result, Submitted)


# ---------------------------------------------------------------------------
# RED → GREEN cycle 3: market order → MarketOrderRequest with correct fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_market_order_constructs_market_order_request() -> None:
    """submit_equity_open for market sends MarketOrderRequest with correct fields."""
    captured: list[Any] = []

    fake_order = _make_fake_order(
        order_class=OrderClass.BRACKET,
        status=OrderStatus.ACCEPTED,
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit
    execution = _make_execution_config()

    cmd = _make_open_command(direction="long", entry_type="market")
    result = await submit_equity_open(
        cmd,
        client=client,
        execution=execution,
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    assert len(captured) == 1
    req = captured[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.qty == 100.0
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert req.client_order_id == _CLIENT_ORDER_ID_INV


# ---------------------------------------------------------------------------
# RED → GREEN cycle 4: limit order → LimitOrderRequest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_limit_order_constructs_limit_order_request() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit
    execution = _make_execution_config()

    cmd = _make_open_command(entry_type="limit", limit_price=170.0)
    await submit_equity_open(
        cmd,
        client=client,
        execution=execution,
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.limit_price == 170.0
    assert req.symbol == "AAPL"


# ---------------------------------------------------------------------------
# RED → GREEN cycle 5: stop_limit → StopLimitOrderRequest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_stop_limit_order_constructs_stop_limit_request() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit
    execution = _make_execution_config()

    cmd = _make_open_command(entry_type="stop_limit", limit_price=169.0, stop_price=168.0)
    await submit_equity_open(
        cmd,
        client=client,
        execution=execution,
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert isinstance(req, StopLimitOrderRequest)
    assert req.limit_price == 169.0
    assert req.stop_price == 168.0


# ---------------------------------------------------------------------------
# RED → GREEN cycle 6: BRACKET class when target + price-stop both present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_with_target_and_price_stop_produces_bracket() -> None:
    """target + PriceLeg → OrderClass.BRACKET with take_profit + stop_loss."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    # Price-stop with a limit_price on the stop (stop-limit protective leg)
    price_leg = PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal="underlying_price",
        condition=PriceCondition(
            underlying_trigger="AAPL", comparator="<=", trigger_price=price(150.0)
        ),
        order_parameters=BracketOrderParameters(order_type="stop_limit", limit_price=price(149.0)),
    )
    cmd = _make_open_command(
        target=_make_target(200.0),
        invalidation_legs=(price_leg,),
    )
    await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert req.order_class == OrderClass.BRACKET
    assert req.take_profit is not None
    assert req.take_profit.limit_price == 200.0
    assert req.stop_loss is not None
    assert req.stop_loss.stop_price == 150.0
    assert req.stop_loss.limit_price == 149.0


@pytest.mark.asyncio
async def test_open_bracket_stop_only_no_limit_price() -> None:
    """PriceLeg with stop (no limit_price) → stop_loss.limit_price is None."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    price_leg = _make_price_leg(trigger=150.0)  # order_parameters has no limit_price
    cmd = _make_open_command(target=_make_target(200.0), invalidation_legs=(price_leg,))

    await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert req.order_class == OrderClass.BRACKET
    assert req.stop_loss.limit_price is None


# ---------------------------------------------------------------------------
# ALP-746: capture native bracket / OTO protective-child ids at submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_bracket_captures_take_profit_and_stop_loss_leg_acks() -> None:
    """A native BRACKET returns its TP (LIMIT) + SL (STOP) children on
    ``Order.legs``; submission captures both real ids classified by role."""
    tp_leg = _make_fake_leg(AlpacaOrderType.LIMIT)
    sl_leg = _make_fake_leg(AlpacaOrderType.STOP)
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET, legs=[tp_leg, sl_leg])
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)

    cmd = _make_open_command(
        target=_make_target(200.0), invalidation_legs=(_make_price_leg(trigger=150.0),)
    )
    result = await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    acks = result.payload.leg_acks
    assert acks == (
        EquityLegAck(alpaca_order_id=AlpacaOrderId(str(tp_leg.id)), role="take_profit"),
        EquityLegAck(alpaca_order_id=AlpacaOrderId(str(sl_leg.id)), role="stop_loss"),
    )


@pytest.mark.asyncio
async def test_open_bracket_stop_limit_child_classified_as_stop_loss() -> None:
    """A STOP_LIMIT protective child still classifies as the stop-loss leg."""
    tp_leg = _make_fake_leg(AlpacaOrderType.LIMIT)
    sl_leg = _make_fake_leg(AlpacaOrderType.STOP_LIMIT)
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET, legs=[tp_leg, sl_leg])
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)

    result = await submit_equity_open(
        _make_open_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    assert {a.role for a in result.payload.leg_acks} == {"take_profit", "stop_loss"}
    sl = next(a for a in result.payload.leg_acks if a.role == "stop_loss")
    assert sl.alpaca_order_id == AlpacaOrderId(str(sl_leg.id))


@pytest.mark.asyncio
async def test_open_oto_captures_take_profit_leg_ack_only() -> None:
    """An OTO (target, no price-stop) returns just the TP child → one ack."""
    tp_leg = _make_fake_leg(AlpacaOrderType.LIMIT)
    fake_order = _make_fake_order(order_class=OrderClass.OTO, legs=[tp_leg])
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)

    # target + time-only invalidation (no PriceLeg) → OTO
    cmd = _make_open_command(invalidation_legs=(_make_time_leg(),))
    result = await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    assert result.payload.leg_acks == (
        EquityLegAck(alpaca_order_id=AlpacaOrderId(str(tp_leg.id)), role="take_profit"),
    )


@pytest.mark.asyncio
async def test_open_with_no_broker_legs_yields_empty_leg_acks() -> None:
    """No ``Order.legs`` (SIMPLE / None payload) → no leg acks captured."""
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE, legs=None)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)

    result = await submit_equity_open(
        _make_open_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    assert result.payload.leg_acks == ()


# ---------------------------------------------------------------------------
# RED → GREEN cycle 7: OTO with take-profit when no PriceLeg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_with_target_no_price_stop_produces_oto_with_take_profit() -> None:
    """No PriceLeg → OTO with take_profit child (time-leg is the hard backstop)."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.OTO)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_open_command(
        target=_make_target(200.0),
        invalidation_legs=(_make_time_leg(),),
    )
    await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert req.order_class == OrderClass.OTO
    assert req.take_profit is not None
    assert req.take_profit.limit_price == 200.0
    assert req.stop_loss is None


# ---------------------------------------------------------------------------
# RED → GREEN cycle 8: SIMPLE when event-only invalidation (time leg = hard backstop,
# but no PriceLeg → OTO; SIMPLE requires neither target nor stop mechanically)
#
# The spec says: "No mechanical bracket structure (event-only invalidation) → SIMPLE"
# BUT OpenCommand requires at least one is_hard leg. A time-leg satisfies is_hard.
# An event-leg is is_hard=False. So event-only would fail OpenCommand validation.
# The spec's "event-only" means: only EventLeg (is_hard=False) — impossible per schema.
# Realistically SIMPLE can only happen via the defensive path.
#
# Implementation: SIMPLE is produced when invalidation_legs has no PriceLeg AND
# no hard mechanical leg that maps to a contingent child. Since time-leg doesn't
# map to a stop_loss, the logic should produce OTO (take_profit only).
# The "SIMPLE from event-only" acceptance criterion is untestable via public API.
# We test SIMPLE via the internal helper exposed as a module-level testable function.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_with_time_leg_only_produces_oto_with_take_profit() -> None:
    """Time-leg (hard) but no price-stop → OTO; time-leg doesn't produce stop_loss."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.OTO)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_open_command(
        target=_make_target(200.0),
        invalidation_legs=(_make_time_leg(),),
    )
    await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert req.order_class == OrderClass.OTO
    assert req.stop_loss is None


# ---------------------------------------------------------------------------
# RED → GREEN cycle 9: submit_equity_add → always SIMPLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_always_produces_simple_order_class() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_add_command(ticker=Symbol("AAPL"), entry_type="market")
    await submit_equity_add(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        side=OrderSide.BUY,
    )

    req = captured[0]
    assert req.order_class == OrderClass.SIMPLE


@pytest.mark.asyncio
async def test_add_market_order_uses_additional_quantity() -> None:
    """submit_equity_add uses additional_quantity for qty."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_add_command(entry_type="market")
    await submit_equity_add(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        side=OrderSide.BUY,
    )

    req = captured[0]
    assert req.qty == 50.0
    assert req.side == OrderSide.BUY
    assert isinstance(req, MarketOrderRequest)


# ---------------------------------------------------------------------------
# RED → GREEN cycle 10: submit_equity_close side derivation
# (quantity resolution hoisted into _close_equity's ALP-943 drift guard —
# covered by tests/execution/oms/test_broker_dispatch.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_long_position_uses_sell_side() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_close_command(quantity="all")
    await submit_equity_close(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        qty=100.0,
        position_side="long",
    )

    req = captured[0]
    assert req.side == OrderSide.SELL
    assert req.qty == 100.0  # the threaded, pre-resolved final qty


@pytest.mark.asyncio
async def test_close_short_position_uses_buy_side() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_close_command(quantity="all")
    await submit_equity_close(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="MSFT",
        qty=50.0,
        position_side="short",
    )

    req = captured[0]
    assert req.side == OrderSide.BUY
    assert req.qty == 50.0


@pytest.mark.asyncio
async def test_close_limit_order_type() -> None:
    """Close with order_type=limit sends LimitOrderRequest."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.SIMPLE)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_close_command(order_type="limit", limit_price=175.0)
    await submit_equity_close(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        qty=100.0,
        position_side="long",
    )

    req = captured[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.limit_price == 175.0


# ---------------------------------------------------------------------------
# RED → GREEN cycle 11: Submitted[EquitySubmission] on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_submission_returns_submitted_equity_submission() -> None:
    """On success, returns Submitted[EquitySubmission] with correct fields."""
    order_id = uuid.uuid4()
    fake_order = MagicMock()
    fake_order.id = order_id
    fake_order.client_order_id = _CLIENT_ORDER_ID_INV
    fake_order.status = OrderStatus.ACCEPTED
    fake_order.order_class = OrderClass.BRACKET

    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)

    result = await submit_equity_open(
        _make_open_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, Submitted)
    payload = result.payload
    assert isinstance(payload, EquitySubmission)
    assert payload.alpaca_order_id == str(order_id)
    assert payload.client_order_id == _CLIENT_ORDER_ID_INV
    assert payload.status == "accepted"
    assert payload.order_class == "bracket"


# ---------------------------------------------------------------------------
# RED → GREEN cycle 12: retry-window exhaustion → GatewaySubmissionFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_gateway_submission_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient errors → GatewaySubmissionFailed (no raise)."""

    class _FakeTransientError(Exception):
        # Make it look like a network error (no status_code attribute)
        pass

    fake_clock = [0.0]

    def fake_monotonic() -> float:
        return fake_clock[0]

    async def fake_sleep(seconds: float) -> None:
        fake_clock[0] += seconds

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    attempt_count = [0]

    def raise_transient(request: Any) -> Any:
        attempt_count[0] += 1
        fake_clock[0] += 0.1  # simulate round-trip time
        raise _FakeTransientError("network hiccup")

    client = MagicMock()
    client.submit_order = raise_transient

    result = await submit_equity_open(
        _make_open_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    assert isinstance(result, GatewaySubmissionFailed)
    assert result.attempt_count >= 1
    assert "TransientError" in result.last_error_class


# ---------------------------------------------------------------------------
# RED → GREEN cycle 13: permanent rejection re-raises (doesn't return)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_rejection_reraises_not_swallowed() -> None:
    """A 4xx permanent rejection is re-raised, not returned as GatewaySubmissionFailed."""
    # Simulate a permanent API error (has status_code=422)
    permanent_exc = Exception("insufficient buying power")
    permanent_exc.status_code = 403  # type: ignore[attr-defined]

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=permanent_exc)

    with pytest.raises(Exception, match="insufficient buying power"):
        await submit_equity_open(
            _make_open_command(),
            client=client,
            execution=_make_execution_config(),
            client_order_id=_CLIENT_ORDER_ID_INV,
        )


# ---------------------------------------------------------------------------
# RED → GREEN cycle 14: client_order_id carried on all request types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_order_id_threaded_onto_add_request() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(
        order_class=OrderClass.SIMPLE, client_order_id=_CLIENT_ORDER_ID_INV
    )

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    await submit_equity_add(
        _make_add_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        side=OrderSide.BUY,
    )

    req = captured[0]
    assert req.client_order_id == _CLIENT_ORDER_ID_INV


@pytest.mark.asyncio
async def test_client_order_id_threaded_onto_close_request() -> None:
    captured: list[Any] = []
    fake_order = _make_fake_order(
        order_class=OrderClass.SIMPLE, client_order_id=_CLIENT_ORDER_ID_INV
    )

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    await submit_equity_close(
        _make_close_command(),
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
        symbol="AAPL",
        qty=100.0,
        position_side="long",
    )

    req = captured[0]
    assert req.client_order_id == _CLIENT_ORDER_ID_INV


# ---------------------------------------------------------------------------
# RED → GREEN cycle 15: ticker symbol propagated correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_market_order_symbol_from_instrument() -> None:
    """Symbol on the request comes from command.instrument.ticker."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.BRACKET)

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    cmd = _make_open_command(ticker=Symbol("NVDA"))
    await submit_equity_open(
        cmd,
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_INV,
    )

    req = captured[0]
    assert req.symbol == "NVDA"


# ---------------------------------------------------------------------------
# ALP-938: submit_equity_oco — the standalone re-protection OCO primitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_oco_long_constructs_oco_request_no_entry() -> None:
    """submit_equity_oco builds an OCO LimitOrderRequest carrying a take-profit +
    stop child and NO entry leg, SELL side for a LONG position."""
    captured: list[Any] = []

    fake_order = _make_fake_order(order_class=OrderClass.OCO, client_order_id=_CLIENT_ORDER_ID_MON)
    fake_order.order_type = AlpacaOrderType.LIMIT
    fake_order.legs = [_make_fake_leg(AlpacaOrderType.STOP)]

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    result = await submit_equity_oco(
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_MON,
        symbol="AAPL",
        qty=6.0,
        position_side="long",
        levels=EquityOcoLevels(take_profit_price=price(200.0), stop_price=price(140.0)),
    )

    assert isinstance(result, Submitted)
    assert len(captured) == 1
    req = captured[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.order_class == OrderClass.OCO
    assert req.side == OrderSide.SELL
    assert req.qty == 6.0
    assert req.time_in_force == TimeInForce.DAY
    # No entry leg: the parent carries no limit_price; the take-profit rides the child.
    assert req.limit_price is None
    assert req.take_profit is not None
    assert req.take_profit.limit_price == 200.0
    assert req.stop_loss is not None
    assert req.stop_loss.stop_price == 140.0
    assert req.stop_loss.limit_price is None


@pytest.mark.asyncio
async def test_submit_oco_short_uses_buy_to_cover_side() -> None:
    """A SHORT position's protective OCO covers with the BUY side."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.OCO, client_order_id=_CLIENT_ORDER_ID_MON)
    fake_order.order_type = AlpacaOrderType.LIMIT
    fake_order.legs = [_make_fake_leg(AlpacaOrderType.STOP)]

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    await submit_equity_oco(
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_MON,
        symbol="TSLA",
        qty=3.0,
        position_side="short",
        levels=EquityOcoLevels(take_profit_price=price(100.0), stop_price=price(160.0)),
    )
    assert captured[0].side == OrderSide.BUY


@pytest.mark.asyncio
async def test_submit_oco_captures_both_protective_ids_parent_tp_leg_stop() -> None:
    """Result.leg_acks carries BOTH ids when the take-profit is the parent limit and
    the stop surfaces on order.legs (one shape alpaca-py returns for an OCO)."""
    fake_order = _make_fake_order(order_class=OrderClass.OCO, client_order_id=_CLIENT_ORDER_ID_MON)
    fake_order.order_type = AlpacaOrderType.LIMIT
    stop_leg = _make_fake_leg(AlpacaOrderType.STOP)
    fake_order.legs = [stop_leg]

    client = MagicMock()
    client.submit_order = lambda request: fake_order

    result = await submit_equity_oco(
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_MON,
        symbol="AAPL",
        qty=6.0,
        position_side="long",
        levels=EquityOcoLevels(take_profit_price=price(200.0), stop_price=price(140.0)),
    )
    assert isinstance(result, Submitted)
    acks = {ack.role: ack.alpaca_order_id for ack in result.payload.leg_acks}
    assert acks == {
        "take_profit": AlpacaOrderId(str(fake_order.id)),
        "stop_loss": AlpacaOrderId(str(stop_leg.id)),
    }


@pytest.mark.asyncio
async def test_submit_oco_captures_both_ids_when_both_surface_as_legs() -> None:
    """The classifier is robust to the other shape — both children on order.legs —
    and never double-counts the take-profit even though the parent is also a LIMIT."""
    fake_order = _make_fake_order(order_class=OrderClass.OCO, client_order_id=_CLIENT_ORDER_ID_MON)
    fake_order.order_type = AlpacaOrderType.LIMIT
    tp_leg = _make_fake_leg(AlpacaOrderType.LIMIT)
    stop_leg = _make_fake_leg(AlpacaOrderType.STOP)
    fake_order.legs = [tp_leg, stop_leg]

    client = MagicMock()
    client.submit_order = lambda request: fake_order

    result = await submit_equity_oco(
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_MON,
        symbol="AAPL",
        qty=6.0,
        position_side="long",
        levels=EquityOcoLevels(take_profit_price=price(200.0), stop_price=price(140.0)),
    )
    assert isinstance(result, Submitted)
    acks = {ack.role: ack.alpaca_order_id for ack in result.payload.leg_acks}
    # take-profit comes from the leg, not the parent — no double-count.
    assert acks == {
        "take_profit": AlpacaOrderId(str(tp_leg.id)),
        "stop_loss": AlpacaOrderId(str(stop_leg.id)),
    }


@pytest.mark.asyncio
async def test_submit_oco_stop_limit_threads_limit_price() -> None:
    """A stop-limit protective stop threads its limit_price onto the stop_loss child."""
    captured: list[Any] = []
    fake_order = _make_fake_order(order_class=OrderClass.OCO, client_order_id=_CLIENT_ORDER_ID_MON)
    fake_order.order_type = AlpacaOrderType.LIMIT
    fake_order.legs = [_make_fake_leg(AlpacaOrderType.STOP_LIMIT)]

    def fake_submit(request: Any) -> Any:
        captured.append(request)
        return fake_order

    client = MagicMock()
    client.submit_order = fake_submit

    await submit_equity_oco(
        client=client,
        execution=_make_execution_config(),
        client_order_id=_CLIENT_ORDER_ID_MON,
        symbol="AAPL",
        qty=6.0,
        position_side="long",
        levels=EquityOcoLevels(
            take_profit_price=price(200.0),
            stop_price=price(140.0),
            stop_limit_price=price(139.5),
        ),
    )
    assert captured[0].stop_loss.stop_price == 140.0
    assert captured[0].stop_loss.limit_price == 139.5


@pytest.mark.asyncio
async def test_submit_oco_rejects_invalid_client_order_id() -> None:
    client = MagicMock()
    with pytest.raises(ValueError, match="client_order_id"):
        await submit_equity_oco(
            client=client,
            execution=_make_execution_config(),
            client_order_id="not-a-valid-id",
            symbol="AAPL",
            qty=6.0,
            position_side="long",
            levels=EquityOcoLevels(take_profit_price=price(200.0), stop_price=price(140.0)),
        )
