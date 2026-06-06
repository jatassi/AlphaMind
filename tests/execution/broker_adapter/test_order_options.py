"""Tests for ``alphamind.execution.broker_adapter.order_options``.

Story ALP-381 — single-leg options OMS-command translation, OCC symbol
construction, and submission via ``TradingClient.submit_order(...)`` wrapped
by ``submit_with_retry``. Covers Alpaca's options-only constraints:
``time_in_force=DAY`` (only TIF Alpaca accepts on options),
``OrderClass.SIMPLE`` (no brackets/OCO/OTO), and the four options-specific
permanent rejection codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLimitOrderRequest,
)

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    AddCommand,
    BracketOrderParameters,
    CapitalProtectionFloor,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    EventCondition,
    EventLeg,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
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
from alphamind.config.models.execution import (
    OrderType as ExecOrderType,
)
from alphamind.execution.broker_adapter import (
    GatewaySubmissionFailed,
    OptionsSubmission,
    PermanentRejection,
    Submitted,
    build_occ_symbol,
    submit_options_add,
    submit_options_close,
    submit_options_open,
)
from alphamind.execution.broker_adapter.order_options import (
    PermanentRejectionError,
    submit_options_capital_floor,
)
from alphamind.portfolio_state.records.positions import OptionContractType

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_VALID_PM_COMMAND_ID = (
    "inv-2026-04-23T14-30Z.ENV-REC-1.1.1~the-THE-NVDA-0123456789abcdef0123456789abcdef"
)


def _execution_config(window_seconds: int = 30) -> ExecutionConfig:
    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=15, move_trigger_pct=0.02),
        conservative_delta_buffer_pct=0.05,
        submission_retry_window_seconds=window_seconds,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                ExecOrderType.market: 0.0001,
                ExecOrderType.limit: 0.0001,
                ExecOrderType.stop: 0.0001,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.05,
    )


def _option_instrument(
    *,
    underlying: str = "NVDA",
    strike: float = 800.0,
    expiration: str = "2024-03-15",
    contract_type: str = "call",
    direction: str = "long",
) -> OptionInstrument:
    return OptionInstrument(
        asset_type="option",
        underlying=underlying,
        strike=price(strike),
        expiration=expiration,
        contract_type=contract_type,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
    )


def _equity_instrument() -> EquityInstrument:
    return EquityInstrument(asset_type="equity", ticker=Symbol("NVDA"), direction="long")


def _thesis() -> Thesis:
    return Thesis(
        summary="Long NVDA calls.",
        nature="directional",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference="NVDA",
                narrative="Capex tailwind drives 30-day momentum.",
                key_assumptions=("Capex stays elevated.",),
            ),
        ),
    )


def _hard_price_leg(*, trigger_price: float = 750.0) -> PriceLeg:
    return PriceLeg(
        type="price",
        is_hard=True,
        trigger_signal="underlying_price",
        condition=PriceCondition(
            underlying_trigger="NVDA",
            comparator="<=",
            trigger_price=price(trigger_price),
        ),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


def _hard_event_legs() -> tuple[Any, ...]:
    """A pair of legs satisfying the hard-backstop requirement (price + soft event)."""
    return (
        _hard_price_leg(),
        EventLeg(
            type="event",
            is_hard=False,
            condition=EventCondition(event_description="Surprise downgrade."),
        ),
    )


def _open_options_command(
    *,
    instrument: OptionInstrument | None = None,
    entry_type: str = "limit",
    limit_price: float | None = 12.50,
    stop_price: float | None = None,
    quantity: float = 5.0,
) -> OpenCommand:
    # ALP-866: an options OPEN entry must rest (limit / stop_limit) — a market
    # entry is rejected at the command boundary. The default is a valid limit
    # entry so every caller that doesn't care about entry type gets a
    # constructible options OPEN.
    return OpenCommand(
        command_type="open",
        instrument=instrument or _option_instrument(),
        entry_order=EntryOrder(
            type=entry_type,  # type: ignore[arg-type]
            limit_price=None if limit_price is None else price(limit_price),
            stop_price=None if stop_price is None else price(stop_price),
        ),
        position_size=PositionSize(quantity=quantity, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_price_leg(),),
        thesis=_thesis(),
        capital_protection_floor=CapitalProtectionFloor(max_loss=money(2_000.0)),
    )


def _add_options_command(
    *,
    underlying: str = "NVDA",
    entry_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    quantity: float = 3.0,
) -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=PositionId("POS-1"),
        additional_quantity=quantity,
        additional_dollar_value=money(5_000.0),
        entry_order=EntryOrder(
            type=entry_type,  # type: ignore[arg-type]
            limit_price=None if limit_price is None else price(limit_price),
            stop_price=None if stop_price is None else price(stop_price),
        ),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="entry",
            instrument_reference=underlying,
            narrative="Adding on momentum continuation.",
            key_assumptions=("Trend intact.",),
        ),
    )


def _close_options_command(
    *,
    quantity: float | str = 5.0,
    order_type: str = "market",
    limit_price: float | None = None,
) -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId("POS-1"),
        quantity=quantity,  # type: ignore[arg-type]
        order_type=order_type,  # type: ignore[arg-type]
        limit_price=None if limit_price is None else price(limit_price),
        close_rationale_type="target_reached",
    )


def _fake_alpaca_order(
    *,
    order_id: str = "alpaca-order-uuid",
    client_order_id: str = _VALID_PM_COMMAND_ID,
    status: str = "accepted",
    order_class: str = "simple",
) -> MagicMock:
    """Return a MagicMock shaped like ``alpaca.trading.models.Order``.

    Only the four fields the translator reads (``id``, ``client_order_id``,
    ``status.value``, ``order_class.value``) are populated; everything else
    is the MagicMock default.
    """
    order = MagicMock()
    order.id = order_id
    order.client_order_id = client_order_id
    order.status = MagicMock(value=status)
    order.order_class = MagicMock(value=order_class)
    return order


def _capturing_client(
    *,
    order: MagicMock | None = None,
    raises: Iterable[BaseException] = (),
) -> tuple[MagicMock, list[OrderRequest]]:
    """Build a TradingClient mock that captures each ``submit_order`` call.

    Returns the client and a list that grows with each captured request. If
    ``raises`` is non-empty, the client raises those in order before falling
    through to returning ``order``.
    """
    captured: list[OrderRequest] = []
    raises_iter = iter(raises)

    def _submit(request: OrderRequest) -> MagicMock:
        captured.append(request)
        try:
            exc = next(raises_iter)
        except StopIteration:
            return order if order is not None else _fake_alpaca_order()
        raise exc

    client = MagicMock()
    client.submit_order.side_effect = _submit
    return client, captured


def _make_api_error(status: int, message: str) -> APIError:
    """Build an ``APIError`` whose ``.status_code`` and ``.message`` resolve.

    Mirrors ``tests/execution/broker_adapter/test_errors.py::_make_api_error``.
    """
    body = json.dumps({"code": 42, "message": message})
    fake_http_error = MagicMock()
    fake_http_error.response.status_code = status
    return cast(APIError, cast(Any, APIError)(body, http_error=fake_http_error))


# ---------------------------------------------------------------------------
# build_occ_symbol — OCC 21-character standard format
# ---------------------------------------------------------------------------


def test_build_occ_symbol_call_integer_strike() -> None:
    """Per the story example: NVDA 2024-03-15 800-strike call.

    21-character OCC: 6-char root (space-padded) + YYMMDD + C/P + 8-digit
    strike-in-thousandths.
    """
    symbol = build_occ_symbol("NVDA", date(2024, 3, 15), OptionContractType.CALL, 800.0)

    assert len(symbol) == 21
    assert symbol.endswith("240315C00800000")


def test_build_occ_symbol_fractional_strike() -> None:
    """Strike of 12.50 → ``00012500`` (thousandths, zero-padded to 8 digits)."""
    symbol = build_occ_symbol("F", date(2024, 6, 21), OptionContractType.CALL, 12.50)

    assert len(symbol) == 21
    assert symbol.endswith("240621C00012500")


def test_build_occ_symbol_put() -> None:
    """PUT contract type emits ``P`` in position 13."""
    symbol = build_occ_symbol("AAPL", date(2025, 1, 17), OptionContractType.PUT, 150.0)

    assert len(symbol) == 21
    assert symbol == "AAPL  250117P00150000"


@pytest.mark.parametrize(
    ("ticker", "expected_root"),
    [
        ("F", "F     "),  # 1-char root: padded to 6
        ("GE", "GE    "),  # 2-char root
        ("BRK", "BRK   "),  # 3-char root
        ("NVDA", "NVDA  "),  # 4-char root
        ("GOOGL", "GOOGL "),  # 5-char root
        ("AABBCC", "AABBCC"),  # 6-char root: no padding
    ],
)
def test_build_occ_symbol_root_padding(ticker: str, expected_root: str) -> None:
    """Underlying root is left-aligned and space-padded to six characters."""
    symbol = build_occ_symbol(ticker, date(2024, 3, 15), OptionContractType.CALL, 100.0)

    assert symbol[:6] == expected_root
    assert len(symbol) == 21


# ---------------------------------------------------------------------------
# submit_options_open — order-type translation and constraint enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_options_open_constructs_request_with_occ_symbol() -> None:
    """An options OPEN produces a request carrying the OCC-21 symbol,
    qty=position_size.quantity, side=BUY (long), DAY TIF, SIMPLE class, and the
    supplied client_order_id; the OptionsSubmission payload mirrors the OCC symbol.

    Entry type is incidental to the OCC/symbol/side/class wiring asserted here, so
    the constructible resting (limit) entry is used (ALP-866 forbids a market
    options OPEN). The ``market → MarketOrderRequest`` mapping in
    ``order_options._build_request`` is exercised separately by
    ``test_submit_options_add_constructs_simple_market_request`` — an options ADD
    carries no capital floor, so a market entry is valid (and still reached) there.
    """
    command = _open_options_command()
    client, captured = _capturing_client()

    outcome = await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    assert isinstance(outcome, Submitted)
    assert outcome.attempt_count == 1
    assert isinstance(outcome.payload, OptionsSubmission)
    assert outcome.payload.order_class == "simple"
    assert outcome.payload.client_order_id == _VALID_PM_COMMAND_ID

    [request] = captured
    assert request.symbol == "NVDA  240315C00800000"
    assert request.qty == 5.0
    assert request.side is OrderSide.BUY
    assert request.time_in_force is TimeInForce.DAY
    assert request.order_class is OrderClass.SIMPLE
    assert request.client_order_id == _VALID_PM_COMMAND_ID
    # OptionsSubmission.occ_symbol mirrors the request's symbol.
    assert outcome.payload.occ_symbol == "NVDA  240315C00800000"


@pytest.mark.asyncio
async def test_submit_options_open_short_direction_uses_sell_side() -> None:
    """A short OPEN (sell-to-open) maps to OrderSide.SELL."""
    command = _open_options_command(
        instrument=_option_instrument(direction="short", contract_type="put")
    )
    client, captured = _capturing_client()

    await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert request.side is OrderSide.SELL


@pytest.mark.asyncio
async def test_submit_options_open_limit_constructs_limit_order_request() -> None:
    """A limit OPEN constructs a LimitOrderRequest with limit_price."""
    command = _open_options_command(entry_type="limit", limit_price=12.50)
    client, captured = _capturing_client()

    await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert isinstance(request, LimitOrderRequest)
    assert request.limit_price == 12.50
    assert request.time_in_force is TimeInForce.DAY
    assert request.order_class is OrderClass.SIMPLE


@pytest.mark.asyncio
async def test_submit_options_open_stop_limit_constructs_stop_limit_order_request() -> None:
    """A stop_limit OPEN constructs a StopLimitOrderRequest with both prices."""
    command = _open_options_command(entry_type="stop_limit", limit_price=12.50, stop_price=12.00)
    client, captured = _capturing_client()

    await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert isinstance(request, StopLimitOrderRequest)
    assert request.limit_price == 12.50
    assert request.stop_price == 12.00


@pytest.mark.asyncio
async def test_submit_options_open_always_simple_regardless_of_bracket_shape() -> None:
    """OPEN with bracket-implying invalidation legs still submits as SIMPLE.

    Per ``broker-adapter.md § Order classes``: Alpaca rejects bracket / OCO /
    OTO on options. The translator hard-codes SIMPLE; the continuous monitor
    arms the protective leg via the underlying equity stream.
    """
    command = OpenCommand(
        command_type="open",
        instrument=_option_instrument(),
        # ALP-866: an options OPEN entry must rest (limit / stop_limit). Entry
        # type is incidental here — this test asserts the SIMPLE order class.
        entry_order=EntryOrder(type="limit", limit_price=price(12.50), stop_price=None),
        position_size=PositionSize(quantity=5.0, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=_hard_event_legs(),  # multi-leg bracket structure
        thesis=_thesis(),
        capital_protection_floor=CapitalProtectionFloor(max_loss=money(2_000.0)),
    )
    client, captured = _capturing_client()

    await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert request.order_class is OrderClass.SIMPLE
    # No take_profit / stop_loss children threaded onto an options request.
    assert request.take_profit is None
    assert request.stop_loss is None


# ---------------------------------------------------------------------------
# submit_options_capital_floor — resting GTC stop_limit capital floor (04c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_options_capital_floor_is_gtc_stop_limit() -> None:
    """An options OPEN's capital floor is a single-leg GTC ``stop_limit``.

    Per ADR-0003 / W3c: the broker-enforced floor uses ``stop_limit`` (not
    ``stop_market``) to bound bad fills, is GTC (always-on, survives a monitor
    wedge), and SIMPLE class (no complex order classes on options).
    """
    command = _open_options_command(quantity=5.0)
    client, captured = _capturing_client()

    outcome = await submit_options_capital_floor(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    assert isinstance(outcome, Submitted)
    [request] = captured
    assert isinstance(request, StopLimitOrderRequest)
    assert request.time_in_force is TimeInForce.GTC
    assert request.order_class is OrderClass.SIMPLE
    assert request.symbol == "NVDA  240315C00800000"


@pytest.mark.asyncio
async def test_submit_options_capital_floor_long_sells_at_pnl_level() -> None:
    """A long floor SELLs at the PnL-denominated trigger price.

    The floor closes the position, so a long (BUY-to-open) floor is a SELL. The
    trigger price is the PM-authored floor: planned premium per contract
    ``dollar_value / (qty * mult)`` minus the loss-per-contract
    ``max_loss / (qty * mult)`` — here ``(10_000 - 2_000) / (5 * 100) = 16.0``.
    """
    command = _open_options_command(quantity=5.0)  # long, $10k, max_loss $2k
    client, captured = _capturing_client()

    await submit_options_capital_floor(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert isinstance(request, StopLimitOrderRequest)
    assert request.side is OrderSide.SELL
    assert request.qty == 5.0
    assert request.stop_price == 16.0
    assert request.limit_price == 16.0


@pytest.mark.asyncio
async def test_submit_options_capital_floor_short_buys_to_close() -> None:
    """A short (SELL-to-open) floor BUYs to close."""
    command = _open_options_command(
        instrument=_option_instrument(direction="short", contract_type="put"),
        quantity=5.0,
    )
    client, captured = _capturing_client()

    await submit_options_capital_floor(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    [request] = captured
    assert request.side is OrderSide.BUY


@pytest.mark.asyncio
async def test_submit_options_close_buy_to_close_position_uses_buy_side() -> None:
    """A position whose closing intent is ``buy_to_close`` (i.e., it was opened
    short) closes via OrderSide.BUY.
    """
    command = _close_options_command()
    client, captured = _capturing_client()

    await submit_options_close(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        occ_symbol="NVDA  240315C00800000",
        position_qty=5.0,
        position_intent="buy_to_close",
    )

    [request] = captured
    assert request.side is OrderSide.BUY
    assert request.qty == 5.0
    assert request.symbol == "NVDA  240315C00800000"


@pytest.mark.asyncio
async def test_submit_options_close_sell_to_close_position_uses_sell_side() -> None:
    """A position whose closing intent is ``sell_to_close`` (i.e., it was opened
    long) closes via OrderSide.SELL.
    """
    command = _close_options_command()
    client, captured = _capturing_client()

    await submit_options_close(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        occ_symbol="NVDA  240315C00800000",
        position_qty=5.0,
        position_intent="sell_to_close",
    )

    [request] = captured
    assert request.side is OrderSide.SELL


@pytest.mark.asyncio
async def test_submit_options_close_quantity_all_uses_position_qty() -> None:
    """``quantity="all"`` resolves to the threaded ``position_qty``."""
    command = _close_options_command(quantity="all")
    client, captured = _capturing_client()

    await submit_options_close(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        occ_symbol="NVDA  240315C00800000",
        position_qty=7.0,
        position_intent="sell_to_close",
    )

    [request] = captured
    assert request.qty == 7.0


@pytest.mark.asyncio
async def test_submit_options_close_partial_quantity_threads_command_quantity() -> None:
    """A numeric ``quantity`` overrides the threaded position_qty."""
    command = _close_options_command(quantity=2.0)
    client, captured = _capturing_client()

    await submit_options_close(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        occ_symbol="NVDA  240315C00800000",
        position_qty=10.0,
        position_intent="sell_to_close",
    )

    [request] = captured
    assert request.qty == 2.0


@pytest.mark.asyncio
async def test_submit_options_close_limit_order_constructs_limit_request() -> None:
    """A limit CLOSE constructs a LimitOrderRequest with limit_price."""
    command = _close_options_command(order_type="limit", limit_price=11.25)
    client, captured = _capturing_client()

    await submit_options_close(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        occ_symbol="NVDA  240315C00800000",
        position_qty=5.0,
        position_intent="sell_to_close",
    )

    [request] = captured
    assert isinstance(request, LimitOrderRequest)
    assert request.limit_price == 11.25


# ---------------------------------------------------------------------------
# submit_options_add — translation of ADD on an options position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_options_add_constructs_simple_market_request() -> None:
    """ADD threads instrument + direction from portfolio state and submits SIMPLE."""
    command = _add_options_command()
    client, captured = _capturing_client()

    outcome = await submit_options_add(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
        instrument=_option_instrument(),
        direction="long",
    )

    assert isinstance(outcome, Submitted)
    [request] = captured
    assert isinstance(request, MarketOrderRequest)
    assert request.symbol == "NVDA  240315C00800000"
    assert request.qty == 3.0  # additional_quantity
    assert request.side is OrderSide.BUY
    assert request.order_class is OrderClass.SIMPLE
    assert request.time_in_force is TimeInForce.DAY


# ---------------------------------------------------------------------------
# Permanent rejection re-raise — each options-specific code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "expected_code"),
    [
        (
            403,
            "options level not approved for this strategy",
            "options_level_not_approved",
        ),
        (422, "contract expired", "contract_expired"),
        (403, "underlying is in regulatory halt", "underlying_halted"),
        (403, "asset is not tradable: bad OCC symbol", "asset_not_tradable"),
    ],
)
@pytest.mark.asyncio
async def test_submit_options_open_reraises_permanent_rejection(
    status: int, message: str, expected_code: str
) -> None:
    """Each documented options-specific rejection code surfaces as
    PermanentRejectionError carrying the typed PermanentRejection.
    """
    command = _open_options_command()
    api_error = _make_api_error(status, message)
    client, _ = _capturing_client(raises=(api_error,))

    with pytest.raises(PermanentRejectionError) as excinfo:
        await submit_options_open(
            command,
            client=client,
            execution=_execution_config(),
            client_order_id=_VALID_PM_COMMAND_ID,
        )

    assert isinstance(excinfo.value.rejection, PermanentRejection)
    assert excinfo.value.rejection.code == expected_code
    assert excinfo.value.rejection.http_status == status


# ---------------------------------------------------------------------------
# Retry exhaustion + retry success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_options_open_returns_gateway_failed_on_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent transient errors past the retry window return
    GatewaySubmissionFailed instead of raising.
    """
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    async def fake_sleep(seconds: float) -> None:
        fake_now[0] += seconds

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    command = _open_options_command()

    def _always_transient(_: OrderRequest) -> object:
        fake_now[0] += 0.05
        raise httpx.ConnectError("dns failed")

    client = MagicMock()
    client.submit_order.side_effect = _always_transient

    outcome = await submit_options_open(
        command,
        client=client,
        execution=_execution_config(window_seconds=2),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    assert isinstance(outcome, GatewaySubmissionFailed)
    assert outcome.attempt_count >= 2
    assert outcome.last_error_class == "ConnectError"


@pytest.mark.asyncio
async def test_submit_options_open_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient followed by success returns Submitted with attempt_count=2."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    command = _open_options_command()
    client, _ = _capturing_client(raises=(httpx.ConnectError("dns flap"),))

    outcome = await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=_VALID_PM_COMMAND_ID,
    )

    assert isinstance(outcome, Submitted)
    assert outcome.attempt_count == 2
    assert sleeps == [pytest.approx(0.5)]


# ---------------------------------------------------------------------------
# Asset-type mismatch (precondition)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_options_open_with_equity_instrument_raises_type_error() -> None:
    """The translator's precondition: dispatcher must route equity vs. options.

    Per the story, the dispatcher (story 03e) is responsible for routing; this
    function asserts its precondition with TypeError.
    """
    command = OpenCommand(
        command_type="open",
        instrument=_equity_instrument(),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_price_leg(),),
        thesis=_thesis(),
    )
    client, _ = _capturing_client()

    with pytest.raises(TypeError, match="OptionInstrument"):
        await submit_options_open(
            command,
            client=client,
            execution=_execution_config(),
            client_order_id=_VALID_PM_COMMAND_ID,
        )


# ---------------------------------------------------------------------------
# client_order_id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",  # empty
        "random-id-not-in-format",
        "abc-123",
        " inv-foo",  # leading whitespace
    ],
)
@pytest.mark.asyncio
async def test_submit_options_open_rejects_malformed_client_order_id(bad_id: str) -> None:
    """A client_order_id that does not match the canonical OMS pattern is
    rejected before any SDK call.
    """
    command = _open_options_command()
    client, captured = _capturing_client()

    with pytest.raises(ValueError):
        await submit_options_open(
            command,
            client=client,
            execution=_execution_config(),
            client_order_id=bad_id,
        )

    assert captured == []  # no SDK call was made


@pytest.mark.parametrize(
    "good_id",
    [
        "inv-2026-04-23T14-30Z.ENV-REC-1.1.1~the-THE-NVDA-0123456789abcdef0123456789abcdef",
        "MON.NVDA.42.7~the-THE-NVDA-fedcba9876543210fedcba9876543210~inv-X",
    ],
)
@pytest.mark.asyncio
async def test_submit_options_open_accepts_canonical_client_order_id(good_id: str) -> None:
    """Both PM-originated (``inv-``) and engine-originated (``MON.``) IDs pass."""
    command = _open_options_command()
    client, captured = _capturing_client()

    outcome = await submit_options_open(
        command,
        client=client,
        execution=_execution_config(),
        client_order_id=good_id,
    )

    assert isinstance(outcome, Submitted)
    [request] = captured
    assert request.client_order_id == good_id
