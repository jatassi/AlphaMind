"""Equity order POST translation — ALP-380.

Translates canonical OMS commands carrying equity instruments into alpaca-py
order request objects, then submits via ``TradingClient.submit_order``
wrapped by ``submit_with_retry``.

Covered order types: market, limit, stop_limit.
Covered order classes: bracket, oco, oto, simple.

Public API:
- :class:`EquitySubmission` — typed acknowledgment for a submitted equity order.
- :func:`submit_equity_open` — translate + submit an OPEN command.
- :func:`submit_equity_add` — translate + submit an ADD command (always SIMPLE).
- :func:`submit_equity_close` — translate + submit a CLOSE command (always SIMPLE).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal, cast

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    SubmissionOutcome,
    Submitted,
    submit_with_retry,
)
from alphamind.execution.oms.command_models import (
    AddCommand,
    CloseCommand,
    EquityInstrument,
    OpenCommand,
    PriceLeg,
)

# Pattern per oms-command-ids.md: PM-originated ("inv-…") or engine-originated ("MON.")
_CLIENT_ORDER_ID_PATTERN: re.Pattern[str] = re.compile(r"^(inv-|MON\.)")


def _require_equity_instrument(instrument: object, *, command_kind: str) -> EquityInstrument:
    """Assert the dispatched command carries an EquityInstrument.

    Routing equity / options / strategy is the dispatcher's job (story 03e);
    the equity translator's precondition is that the caller routed correctly.
    A wrong-asset instrument is a programming error — surface as ``TypeError``.
    """
    if not isinstance(instrument, EquityInstrument):
        msg = (
            f"submit_equity_{command_kind} requires EquityInstrument; "
            f"got {type(instrument).__name__}"
        )
        raise TypeError(msg)
    return instrument


@dataclass(frozen=True)
class EquitySubmission:
    """Alpaca's acknowledgment record for a submitted equity order.

    Fields mirror what the OMS needs for end-to-end correlation and
    order-record hydration (story 03e).
    """

    alpaca_order_id: str
    client_order_id: str
    status: str  # Alpaca's reported status: accepted | new | pending_new | …
    order_class: str  # simple | bracket | oco | oto


# ---------------------------------------------------------------------------
# Public submission functions
# ---------------------------------------------------------------------------


async def submit_equity_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[EquitySubmission]:
    """Translate an OPEN command targeting an equity instrument and submit.

    The order class is determined from the bracket shape:

    * target + price-stop invalidation leg → BRACKET (entry + TP + SL).
    * target only (no price-stop, time/event invalidation) → OTO with TP child.
    * price-stop only (no target — defensive, impossible via OpenCommand schema) →
      OTO with SL child.
    * neither (event-only — defensive, impossible via OpenCommand schema) → SIMPLE.

    OCO is not produced by OPEN — it is produced by ADJUST when the PM converts
    a position from bracket to OCO post-entry; that path lives in story 02e (PATCH).
    """
    _validate_client_order_id(client_order_id)

    instrument = _require_equity_instrument(command.instrument, command_kind="open")
    ticker: str = instrument.ticker
    side = _entry_side(instrument.direction)
    qty = command.position_size.quantity

    order_class, take_profit, stop_loss = _bracket_params(command)

    request = _build_entry_request(
        _EntryParams(
            entry_type=command.entry_order.type,
            symbol=ticker,
            qty=qty,
            side=side,
            limit_price=command.entry_order.limit_price,
            stop_price=command.entry_order.stop_price,
            order_class=order_class,
            take_profit=take_profit,
            stop_loss=stop_loss,
            client_order_id=client_order_id,
        )
    )
    return await _submit_and_map(request, client=client, execution=execution)


async def submit_equity_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    symbol: str,
    side: OrderSide,
) -> SubmissionOutcome[EquitySubmission]:
    """Translate an ADD command and submit.

    ADD is always SIMPLE — it is a fresh order on an existing position;
    bracket adjustments arrive separately via PATCH (story 02e).

    The ``symbol`` and ``side`` are threaded from the caller because
    ``AddCommand`` references the position by ID, not by ticker/direction.
    """
    _validate_client_order_id(client_order_id)

    request = _build_entry_request(
        _EntryParams(
            entry_type=command.entry_order.type,
            symbol=symbol,
            qty=command.additional_quantity,
            side=side,
            limit_price=command.entry_order.limit_price,
            stop_price=command.entry_order.stop_price,
            order_class=OrderClass.SIMPLE,
            take_profit=None,
            stop_loss=None,
            client_order_id=client_order_id,
        )
    )
    return await _submit_and_map(request, client=client, execution=execution)


async def submit_equity_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    symbol: str,
    position_qty: float,
    position_side: Literal["long", "short"],
) -> SubmissionOutcome[EquitySubmission]:
    """Translate a CLOSE command and submit.

    Always SIMPLE. Uses opposite side of the position:
    long → SELL, short → BUY (buy-to-cover).

    ``symbol``, ``position_qty``, and ``position_side`` are threaded from
    portfolio state because ``CloseCommand`` references the position by ID.
    """
    _validate_client_order_id(client_order_id)

    side = OrderSide.SELL if position_side == "long" else OrderSide.BUY
    qty = position_qty if command.quantity == "all" else float(command.quantity)

    if command.order_type == "limit":
        request: OrderRequest = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            limit_price=command.limit_price,
            client_order_id=client_order_id,
        )
    else:
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            client_order_id=client_order_id,
        )

    return await _submit_and_map(request, client=client, execution=execution)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_client_order_id(client_order_id: str) -> None:
    """Raise :exc:`ValueError` if *client_order_id* does not match the OMS format.

    Per ``oms-command-ids.md``: PM-originated IDs begin with ``inv-``; engine-
    originated IDs begin with ``MON.``. Any other prefix (or empty string) is
    a caller contract violation.
    """
    if not client_order_id or not _CLIENT_ORDER_ID_PATTERN.match(client_order_id):
        raise ValueError(
            f"client_order_id {client_order_id!r} must be non-empty and match "
            r"'^(inv-|MON\.)' per oms-command-ids.md"
        )


def _entry_side(direction: str) -> OrderSide:
    """Map an OMS instrument direction to an Alpaca order side for entries."""
    return OrderSide.BUY if direction == "long" else OrderSide.SELL


def _bracket_params(
    command: OpenCommand,
) -> tuple[OrderClass, TakeProfitRequest | None, StopLossRequest | None]:
    """Determine order class + contingent child params for an OPEN command.

    Returns ``(order_class, take_profit, stop_loss)``.
    """
    price_leg = next((leg for leg in command.invalidation_legs if isinstance(leg, PriceLeg)), None)
    # command.target is always present on OpenCommand (required field); price is
    # non-None for all target_type values (enforced by Target's model validator).
    assert command.target.price is not None, "OpenCommand target.price must not be None"
    tp = TakeProfitRequest(limit_price=command.target.price)

    if price_leg is not None:
        sl = StopLossRequest(
            stop_price=price_leg.condition.trigger_price,
            limit_price=price_leg.order_parameters.limit_price,
        )
        return OrderClass.BRACKET, tp, sl

    # No PriceLeg — OTO with take-profit child (time/event-only invalidation)
    return OrderClass.OTO, tp, None


@dataclass(frozen=True)
class _EntryParams:
    """Normalized parameters for building an alpaca-py entry order request."""

    entry_type: str
    symbol: str
    qty: float
    side: OrderSide
    limit_price: float | None
    stop_price: float | None
    order_class: OrderClass
    take_profit: TakeProfitRequest | None
    stop_loss: StopLossRequest | None
    client_order_id: str


def _build_entry_request(params: _EntryParams) -> OrderRequest:
    """Build an alpaca-py order request from normalized entry parameters."""
    common: dict[str, object] = {
        "symbol": params.symbol,
        "qty": params.qty,
        "side": params.side,
        "time_in_force": TimeInForce.DAY,
        "order_class": params.order_class,
        "client_order_id": params.client_order_id,
    }
    if params.take_profit is not None:
        common["take_profit"] = params.take_profit
    if params.stop_loss is not None:
        common["stop_loss"] = params.stop_loss

    if params.entry_type == "market":
        return MarketOrderRequest(**common)
    if params.entry_type == "limit":
        return LimitOrderRequest(limit_price=params.limit_price, **common)
    if params.entry_type == "stop_limit":
        return StopLimitOrderRequest(
            limit_price=params.limit_price, stop_price=params.stop_price, **common
        )
    raise ValueError(f"Unsupported entry order type: {params.entry_type!r}")


async def _submit_and_map(
    request: OrderRequest,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
) -> SubmissionOutcome[EquitySubmission]:
    """Wrap the SDK call with retry and map the result to :class:`EquitySubmission`."""

    async def _submit() -> Order:
        # alpaca-py declares submit_order → Union[Order, dict]; we always receive
        # Order for non-mleg equity submissions. cast() informs the type checker.
        return cast(Order, await asyncio.to_thread(client.submit_order, request))

    outcome = await submit_with_retry(
        _submit,
        window_seconds=execution.submission_retry_window_seconds,
    )
    match outcome:
        case Submitted(payload=order, attempt_count=n):
            return Submitted(
                EquitySubmission(
                    alpaca_order_id=str(order.id),
                    client_order_id=order.client_order_id,
                    status=order.status.value,
                    order_class=order.order_class.value,
                ),
                attempt_count=n,
            )
        case GatewaySubmissionFailed():
            return outcome
