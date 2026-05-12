"""Tests for the OMS broker_dispatch helper — story 03e (ALP-390).

Drives the canonical-command → broker-adapter dispatch helper that the
``submit_envelope_mcp`` and ``submit_engine_envelope`` write paths call once
the validators have accepted a command. Each test asserts:

* dispatch routes (command_type, instrument.asset_type) → the right
  ``submit_*`` function in the broker_adapter package;
* the resulting :class:`BrokerDispatchResult` carries the broker's real
  ``alpaca_order_id``, ``client_order_id``, and the dispatched submission's
  underlying typed ack.

Tests use ``unittest.mock.MagicMock`` for the alpaca-py client (mirrors the
sibling ``tests/execution/broker_adapter/test_order_*.py`` pattern) so the
suite runs offline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    BracketOrderParameters,
    CancelCommand,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    NewStopLevel,
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
)
from alphamind.config.models.execution import (
    ExecutionConfig,
    GreeksRefresh,
    OrderType,
    PaperHarness,
)
from alphamind.execution.broker_adapter import (
    EquitySubmission,
    GatewaySubmissionFailed,
    MLEGLegAck,
    MLEGSubmission,
    OptionsSubmission,
    Submitted,
)
from alphamind.execution.oms.broker_dispatch import (
    BrokerDispatchResult,
    dispatch_command_to_broker,
)

# ---------------------------------------------------------------------------
# Common fixture builders
# ---------------------------------------------------------------------------

_CLIENT_ORDER_ID = "inv-2026-05-09T09-30Z.ENV-REC-1.0.0"


def _execution_config() -> ExecutionConfig:
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
        ),
        pl_target_margin_pct=0.0,
    )


def _thesis(ticker: str = "AAPL") -> Thesis:
    return Thesis(
        summary="Test thesis",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference=ticker,
                narrative="Long thesis",
                key_assumptions=("setup intact",),
            ),
        ),
    )


def _equity_open_command(ticker: str = "AAPL") -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=ticker, direction="long"),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=10.0, dollar_value=1500.0),
        target=Target(target_type="absolute_price", price=200.0, order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=ticker,
                    comparator="<=",
                    trigger_price=140.0,
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_thesis(ticker),
    )


def _option_open_command(underlying: str = "NVDA") -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying=underlying,
            strike=900.0,
            expiration="2026-06-19",
            contract_type="call",
            direction="long",
        ),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=2.0, dollar_value=1500.0),
        target=Target(target_type="absolute_price", price=950.0, order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=850.0,
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_thesis(underlying),
    )


def _strategy_open_command(underlying: str = "SPY") -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=StrategyInstrument(
            asset_type="strategy",
            strategy_type="vertical_spread",
            underlying=underlying,
            legs=(
                StrategyLeg(
                    strike=400.0,
                    expiration="2026-06-19",
                    contract_type="call",
                    direction="long",
                    quantity_ratio=1,
                ),
                StrategyLeg(
                    strike=410.0,
                    expiration="2026-06-19",
                    contract_type="call",
                    direction="short",
                    quantity_ratio=1,
                ),
            ),
        ),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=1.0, dollar_value=500.0),
        target=Target(target_type="absolute_price", price=10.0, order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=395.0,
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_thesis(underlying),
    )


def _add_command(position_id: str = "POS-AAPL-001") -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=position_id,
        additional_quantity=5.0,
        additional_dollar_value=750.0,
        entry_order=EntryOrder(type="market"),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="entry",
            instrument_reference="AAPL",
            narrative="Pyramid into the position.",
            key_assumptions=("Setup intact.",),
        ),
    )


def _close_command(position_id: str = "POS-AAPL-001") -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=position_id,
        quantity="all",
        order_type="market",
        close_rationale_type="target_reached",
    )


def _adjust_command(position_id: str = "POS-AAPL-001") -> AdjustCommand:
    return AdjustCommand(
        command_type="adjust",
        position_id=position_id,
        adjustment_rationale="Tighten stop after run-up",
        new_stop_level=NewStopLevel(trigger_price=170.0, order_type="stop", limit_price=None),
    )


def _cancel_command(order_id: str = "alp-original-123") -> CancelCommand:
    return CancelCommand(
        command_type="cancel",
        order_id=order_id,
        cancel_reason="stale",
    )


def _make_fake_alpaca_order(
    *,
    order_class: OrderClass = OrderClass.SIMPLE,
    status: OrderStatus = OrderStatus.ACCEPTED,
    client_order_id: str = _CLIENT_ORDER_ID,
) -> MagicMock:
    order = MagicMock()
    order.id = uuid.uuid4()
    order.client_order_id = client_order_id
    order.status = status
    order.order_class = order_class
    return order


# ---------------------------------------------------------------------------
# 1. BrokerDispatchResult dataclass shape
# ---------------------------------------------------------------------------


def test_broker_dispatch_result_is_frozen_dataclass() -> None:
    """The unified result type the OMS persists onto an OrderRecord — frozen,
    carries the real Alpaca order id, the client_order_id, status, order_class,
    payload_kind, and the underlying typed submission."""
    inner = EquitySubmission(
        alpaca_order_id="alp-x",
        client_order_id=_CLIENT_ORDER_ID,
        status="accepted",
        order_class="bracket",
    )
    result = BrokerDispatchResult(
        alpaca_order_id="alp-x",
        client_order_id=_CLIENT_ORDER_ID,
        status="accepted",
        order_class="bracket",
        payload_kind="equity",
        raw_submission=inner,
    )

    assert result.alpaca_order_id == "alp-x"
    assert result.client_order_id == _CLIENT_ORDER_ID
    assert result.status == "accepted"
    assert result.order_class == "bracket"
    assert result.payload_kind == "equity"
    assert result.raw_submission is inner

    # frozen
    with pytest.raises(Exception, match=r"cannot assign|frozen"):
        result.alpaca_order_id = "alp-y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. OPEN equity routes to submit_equity_open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_open_equity_routes_to_submit_equity_open() -> None:
    """An ``OpenCommand`` carrying an ``EquityInstrument`` reaches
    ``submit_equity_open``; the BrokerDispatchResult mirrors the equity ack."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.BRACKET)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()  # not consulted on OPEN equity

    outcome = await dispatch_command_to_broker(
        _equity_open_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
    )

    assert isinstance(outcome, Submitted)
    result = outcome.payload
    assert isinstance(result, BrokerDispatchResult)
    assert result.payload_kind == "equity"
    assert result.alpaca_order_id == str(fake_order.id)
    assert result.client_order_id == _CLIENT_ORDER_ID
    assert isinstance(result.raw_submission, EquitySubmission)

    # submit_order was called with a bracket-class request whose symbol matches.
    submitted = client.submit_order.call_args[0][0]
    assert submitted.symbol == "AAPL"
    assert submitted.order_class == OrderClass.BRACKET
    assert submitted.side == OrderSide.BUY


# ---------------------------------------------------------------------------
# 3. OPEN options routes to submit_options_open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_open_options_routes_to_submit_options_open() -> None:
    """An ``OpenCommand`` carrying an ``OptionInstrument`` reaches
    ``submit_options_open``; the result is tagged ``payload_kind='options'``
    and the submitted request targets an OCC symbol."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _option_open_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
    )

    assert isinstance(outcome, Submitted)
    result = outcome.payload
    assert result.payload_kind == "options"
    assert isinstance(result.raw_submission, OptionsSubmission)
    submitted = client.submit_order.call_args[0][0]
    # OCC: NVDA padded to 6 chars + YYMMDD + C/P + 8-digit strike-thousandths.
    assert submitted.symbol == "NVDA  260619C00900000"
    assert submitted.order_class == OrderClass.SIMPLE
    assert submitted.time_in_force == TimeInForce.DAY


# ---------------------------------------------------------------------------
# 4. OPEN strategy routes to submit_mleg_open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_open_strategy_routes_to_submit_mleg_open() -> None:
    """An ``OpenCommand`` carrying a ``StrategyInstrument`` reaches
    ``submit_mleg_open``; the result is tagged ``payload_kind='mleg'``."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.MLEG)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _strategy_open_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
    )

    assert isinstance(outcome, Submitted)
    result = outcome.payload
    assert result.payload_kind == "mleg"
    assert isinstance(result.raw_submission, MLEGSubmission)
    submitted = client.submit_order.call_args[0][0]
    assert submitted.order_class == OrderClass.MLEG


# ---------------------------------------------------------------------------
# 5. ADD equity threads symbol/side from caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_add_equity_routes_to_submit_equity_add() -> None:
    """An ``AddCommand`` against an equity position threads ``symbol`` +
    ``side`` from caller-supplied portfolio state into ``submit_equity_add``."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _add_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_symbol="AAPL",
        position_side="long",
        position_asset_type="equity",
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "equity"
    assert isinstance(outcome.payload.raw_submission, EquitySubmission)
    submitted = client.submit_order.call_args[0][0]
    assert submitted.symbol == "AAPL"
    assert submitted.side == OrderSide.BUY  # long → BUY for ADD


# ---------------------------------------------------------------------------
# 6. CLOSE equity threads symbol/qty/side from caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_close_equity_routes_to_submit_equity_close() -> None:
    """A ``CloseCommand`` against an equity position threads
    ``symbol`` / ``position_qty`` / ``position_side`` from caller-supplied
    portfolio state into ``submit_equity_close``."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _close_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_symbol="AAPL",
        position_qty=10.0,
        position_side="long",
        position_asset_type="equity",
    )

    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "equity"
    submitted = client.submit_order.call_args[0][0]
    assert submitted.symbol == "AAPL"
    assert submitted.side == OrderSide.SELL  # long position → SELL to close
    assert submitted.qty == 10.0


# ---------------------------------------------------------------------------
# 7. CLOSE options threads OCC symbol + position_intent from caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_close_options_routes_to_submit_options_close() -> None:
    """A ``CloseCommand`` against an options position threads ``occ_symbol`` +
    ``position_intent`` from caller-supplied portfolio state into
    ``submit_options_close``."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _close_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_qty=2.0,
        position_asset_type="option",
        occ_symbol="NVDA  260619C00900000",
        position_intent="sell_to_close",
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "options"
    submitted = client.submit_order.call_args[0][0]
    assert submitted.symbol == "NVDA  260619C00900000"
    assert submitted.side == OrderSide.SELL


# ---------------------------------------------------------------------------
# 8. CLOSE strategy threads open_legs + strategy_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_close_strategy_routes_to_submit_mleg_close() -> None:
    """A ``CloseCommand`` against a strategy position threads ``open_legs`` +
    ``strategy_type`` (+ ``position_units`` when ``quantity == 'all'``)
    from caller-supplied portfolio state into ``submit_mleg_close``."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.MLEG)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    open_legs = (
        MLEGLegAck(
            occ_symbol="SPY   260619C00400000",
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol="SPY   260619C00410000",
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_open",
        ),
    )

    outcome = await dispatch_command_to_broker(
        _close_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_asset_type="strategy",
        open_legs=open_legs,
        strategy_type="vertical_spread",
        position_units=1.0,
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "mleg"
    submitted = client.submit_order.call_args[0][0]
    assert submitted.order_class == OrderClass.MLEG


# ---------------------------------------------------------------------------
# 9. ADD options threads instrument + direction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_add_options_routes_to_submit_options_add() -> None:
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    instrument = OptionInstrument(
        asset_type="option",
        underlying="NVDA",
        strike=900.0,
        expiration="2026-06-19",
        contract_type="call",
        direction="long",
    )

    outcome = await dispatch_command_to_broker(
        _add_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_asset_type="option",
        position_option_instrument=instrument,
        position_side="long",
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "options"


# ---------------------------------------------------------------------------
# 10. ADD strategy threads open_legs + strategy_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_add_strategy_routes_to_submit_mleg_add() -> None:
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.MLEG)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    open_legs = (
        MLEGLegAck(
            occ_symbol="SPY   260619C00400000",
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol="SPY   260619C00410000",
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_open",
        ),
    )

    outcome = await dispatch_command_to_broker(
        _add_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        position_asset_type="strategy",
        open_legs=open_legs,
        strategy_type="vertical_spread",
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "mleg"


# ---------------------------------------------------------------------------
# 11. ADJUST routes to submit_replace against target_alpaca_order_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_adjust_routes_to_submit_replace() -> None:
    """An ``AdjustCommand`` routes to ``submit_replace`` against the
    caller-threaded ``target_alpaca_order_id``; the dispatch result carries the
    NEW alpaca order id from Alpaca's cancel-and-replace response."""
    new_order_id = "alp-new-replacement-456"
    fake_replaced = MagicMock()
    fake_replaced.id = new_order_id
    fake_replaced.client_order_id = _CLIENT_ORDER_ID
    fake_replaced.status = OrderStatus.ACCEPTED

    client = MagicMock()
    client.replace_order_by_id = MagicMock(return_value=fake_replaced)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _adjust_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        target_alpaca_order_id="alp-original-123",
        target_asset_class="us_equity",
        target_order_class="simple",
    )
    assert isinstance(outcome, Submitted)
    result = outcome.payload
    assert result.payload_kind == "equity"  # asset_class us_equity
    assert result.alpaca_order_id == new_order_id
    client.replace_order_by_id.assert_called_once()
    args, _kwargs = client.replace_order_by_id.call_args
    assert args[0] == "alp-original-123"


# ---------------------------------------------------------------------------
# 12. CANCEL routes to submit_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_cancel_routes_to_submit_cancel() -> None:
    """A ``CancelCommand`` routes to ``submit_cancel`` against the threaded
    ``target_alpaca_order_id``."""
    client = MagicMock()
    client.cancel_order_by_id = MagicMock(return_value=None)
    queries = MagicMock()

    outcome = await dispatch_command_to_broker(
        _cancel_command(),
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id=_CLIENT_ORDER_ID,
        target_alpaca_order_id="alp-original-123",
    )
    assert isinstance(outcome, Submitted)
    result = outcome.payload
    # Cancel result still carries the canceled order id.
    assert result.alpaca_order_id == "alp-original-123"
    client.cancel_order_by_id.assert_called_once_with("alp-original-123")


# ---------------------------------------------------------------------------
# 13. Engine-guardrail CLOSE routes the same way as PM CLOSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_engine_guardrail_close_routes_to_submit_equity_close() -> None:
    """An engine-originated CLOSE (``risk_management_subtype=engine_guardrail``)
    dispatches identically to a PM-originated CLOSE — the dispatcher does not
    branch on the subtype."""
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    client = MagicMock()
    client.submit_order = MagicMock(return_value=fake_order)
    queries = MagicMock()

    engine_close = CloseCommand(
        command_type="close",
        position_id="POS-AAPL-001",
        quantity="all",
        order_type="market",
        close_rationale_type="risk_management",
        risk_management_subtype="engine_guardrail",
    )

    outcome = await dispatch_command_to_broker(
        engine_close,
        client=client,
        queries=queries,
        execution=_execution_config(),
        client_order_id="MON.session-abc.42.0",
        position_symbol="AAPL",
        position_qty=10.0,
        position_side="long",
        position_asset_type="equity",
    )
    assert isinstance(outcome, Submitted)
    assert outcome.payload.payload_kind == "equity"


# ---------------------------------------------------------------------------
# 14. Gateway-submission failure surfaces as GatewaySubmissionFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_returns_gateway_submission_failed_on_retry_exhaustion() -> None:
    """When the underlying submit_with_retry exhausts the window, the dispatcher
    surfaces ``GatewaySubmissionFailed`` rather than raising."""
    import httpx

    client = MagicMock()
    # Raise a transient-classified error every call → retry loop exhausts the window.
    client.submit_order = MagicMock(side_effect=httpx.ConnectError("network down"))
    queries = MagicMock()
    # Tight retry window so the test runs in <1s.
    cfg = ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=5, move_trigger_pct=0.01),
        conservative_delta_buffer_pct=0.0,
        submission_retry_window_seconds=1,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                OrderType.market: 0.1,
                OrderType.limit: 0.05,
                OrderType.stop: 0.08,
            },
        ),
        pl_target_margin_pct=0.0,
    )

    outcome = await dispatch_command_to_broker(
        _equity_open_command(),
        client=client,
        queries=queries,
        execution=cfg,
        client_order_id=_CLIENT_ORDER_ID,
    )
    assert isinstance(outcome, GatewaySubmissionFailed)
    assert outcome.attempt_count >= 1


# ---------------------------------------------------------------------------
# 15. Unsupported (asset_type, command_type) combo raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_raises_when_asset_type_missing_for_close() -> None:
    """CLOSE / ADD must thread ``position_asset_type`` because the canonical
    command references a position by id and carries no instrument; missing the
    asset-type kwarg is a programming error — surface as ValueError."""
    client = MagicMock()
    queries = MagicMock()

    with pytest.raises(ValueError, match="position_asset_type"):
        await dispatch_command_to_broker(
            _close_command(),
            client=client,
            queries=queries,
            execution=_execution_config(),
            client_order_id=_CLIENT_ORDER_ID,
            # position_asset_type omitted
        )


# ---------------------------------------------------------------------------
# Suppress unused-import warnings for fixtures pulled in for typing
# ---------------------------------------------------------------------------

# datetime/UTC/Any kept for future fixture parity; silence ruff noqa otherwise.
_ = (datetime, UTC, Any, LimitOrderRequest, MarketOrderRequest, OptionLegRequest)
