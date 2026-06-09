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

from collections.abc import Callable
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

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId
from alphamind._kernel.money import Price
from alphamind.commands.command_models import (
    AddCommand,
    CloseCommand,
    EquityInstrument,
    OpenCommand,
    PriceLeg,
)
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    SubmissionOutcome,
    Submitted,
    bounded_broker_call,
    submit_with_retry,
)
from alphamind.execution.oms.command_ids import is_engine_originated, is_pm_originated


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


LegRole = Literal["take_profit", "stop_loss"]


@dataclass(frozen=True)
class EquityLegAck:
    """Real Alpaca id for a protective child of a native BRACKET / OTO order.

    A native equity ``order_class=BRACKET`` returns its take-profit (a LIMIT
    child) and price-stop (a STOP / STOP_LIMIT child) on ``Order.legs``; an OTO
    returns just the take-profit child. Alpaca generates each child's id and
    ``client_order_id`` server-side — the OMS never sends one and the child
    orders never round-trip an OMS id — so the children's real ids must be
    captured here at submission, classified by ``order_type`` (LIMIT →
    take-profit, STOP / STOP_LIMIT → stop-loss). The command execution OPEN writeback
    then stamps each captured id onto the matching protective-leg ``orders``
    row, which carries a NULL ``alpaca_order_id`` until backfilled (ALP-847 — no
    synthetic ``alp-…`` placeholder), so a later protective fill / OCO
    sibling-cancel resolves to the local row (ALP-746).
    """

    alpaca_order_id: AlpacaOrderId
    role: LegRole


@dataclass(frozen=True)
class EquityOcoLevels:
    """The protective price geometry for a standalone re-protection OCO (ALP-938).

    Groups the take-profit limit, the stop trigger, and the optional stop-limit
    price — the cohesive unit the re-bracket step reads off the original (now
    cancelled) protective order rows and replays onto a fresh OCO.
    """

    take_profit_price: Price
    stop_price: Price
    stop_limit_price: Price | None = None


@dataclass(frozen=True)
class EquitySubmission:
    """Alpaca's acknowledgment record for a submitted equity order.

    Fields mirror what the OMS needs for end-to-end correlation and
    order-record hydration (story 03e).

    ``leg_acks`` carries the real ids of any native BRACKET / OTO protective
    children read off ``Order.legs`` at submission (empty for SIMPLE orders and
    for instruments with no broker-side protective child); the OMS threads them
    onto the persisted protective-leg rows (ALP-746).
    """

    alpaca_order_id: AlpacaOrderId
    client_order_id: ClientOrderId
    status: str  # Alpaca's reported status: accepted | new | pending_new | …
    order_class: str  # simple | bracket | oco | oto
    leg_acks: tuple[EquityLegAck, ...] = ()


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
        if command.limit_price is None:
            msg = "CloseCommand order_type=limit requires limit_price"
            raise ValueError(msg)
        request: OrderRequest = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            # Price → float at the Alpaca SDK boundary.
            limit_price=float(command.limit_price),
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


async def submit_equity_oco(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    symbol: str,
    qty: float,
    position_side: Literal["long", "short"],
    levels: EquityOcoLevels,
) -> SubmissionOutcome[EquitySubmission]:
    """Translate + submit a standalone protective OCO (take-profit + stop, NO entry).

    The auto re-bracket primitive (ALP-938 — the deferred ALP-937 (E)). Unlike a
    native bracket it re-protects an *existing* OPEN position's remaining shares, so
    there is no entry leg. The protective side is the close side: SELL for a LONG
    position, BUY (buy-to-cover) for a SHORT.

    Built as a ``LimitOrderRequest`` with ``order_class=OCO`` carrying a
    ``take_profit`` (the limit child) and a ``stop_loss`` (the stop / stop-limit
    child). alpaca-py exempts an OCO ``LimitOrderRequest`` from the
    parent-``limit_price`` requirement precisely because the take-profit price rides
    the ``take_profit`` child. The result's ``leg_acks`` carry BOTH protective broker
    ids (:func:`_classify_oco_leg_acks`).
    """
    _validate_client_order_id(client_order_id)

    side = OrderSide.SELL if position_side == "long" else OrderSide.BUY
    request = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.OCO,
        # Price → float at the Alpaca SDK boundary.
        take_profit=TakeProfitRequest(limit_price=float(levels.take_profit_price)),
        stop_loss=StopLossRequest(
            stop_price=float(levels.stop_price),
            limit_price=(
                float(levels.stop_limit_price) if levels.stop_limit_price is not None else None
            ),
        ),
        client_order_id=client_order_id,
    )
    return await _submit_and_map(
        request, client=client, execution=execution, classify_legs=_classify_oco_leg_acks
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_client_order_id(client_order_id: str) -> None:
    """Raise :exc:`ValueError` if *client_order_id* does not match the OMS format.

    Delegates to :func:`oms.command_ids.is_pm_originated` /
    :func:`is_engine_originated` so the broker adapter shares one pattern
    definition with the OMS intake.
    """
    if not client_order_id:
        msg = "client_order_id must be non-empty"
        raise ValueError(msg)
    if not (is_pm_originated(client_order_id) or is_engine_originated(client_order_id)):
        msg = (
            f"client_order_id {client_order_id!r} does not match the canonical "
            f"OMS command-ID pattern (PM-originated 'inv-...' or "
            f"engine-originated 'MON....')"
        )
        raise ValueError(msg)


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
    # ALP-462 — Price → float at the Alpaca SDK boundary.
    tp = TakeProfitRequest(limit_price=float(command.target.price))

    if price_leg is not None:
        sl = StopLossRequest(
            stop_price=float(price_leg.condition.trigger_price),
            limit_price=(
                float(price_leg.order_parameters.limit_price)
                if price_leg.order_parameters.limit_price is not None
                else None
            ),
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
    limit_price: Price | None
    stop_price: Price | None
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

    # Price → float at the Alpaca SDK boundary.
    limit_price_float = float(params.limit_price) if params.limit_price is not None else None
    stop_price_float = float(params.stop_price) if params.stop_price is not None else None
    if params.entry_type == "market":
        return MarketOrderRequest(**common)
    if params.entry_type == "limit":
        return LimitOrderRequest(limit_price=limit_price_float, **common)
    if params.entry_type == "stop_limit":
        return StopLimitOrderRequest(
            limit_price=limit_price_float, stop_price=stop_price_float, **common
        )
    raise ValueError(f"Unsupported entry order type: {params.entry_type!r}")


async def _submit_and_map(
    request: OrderRequest,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    classify_legs: Callable[[Order], tuple[EquityLegAck, ...]] | None = None,
) -> SubmissionOutcome[EquitySubmission]:
    """Wrap the SDK call with retry and map the result to :class:`EquitySubmission`.

    ``classify_legs`` defaults to the native-bracket/OTO classifier (the entry
    parent is excluded; only ``order.legs`` children are captured). The standalone
    OCO path (ALP-938) passes :func:`_classify_oco_leg_acks`, which also captures
    the top-level order because an OCO has no entry parent to exclude.
    """

    async def _submit() -> Order:
        # alpaca-py declares submit_order → Union[Order, dict]; we always receive
        # Order for non-mleg equity submissions. cast() informs the type checker.
        # ``bounded_broker_call`` time-bounds the offloaded sync call so a hung
        # socket cannot park the caller for the full client-factory socket timeout.
        return cast(Order, await bounded_broker_call(lambda: client.submit_order(request)))

    outcome = await submit_with_retry(
        _submit,
        window_seconds=execution.submission_retry_window_seconds,
    )
    match outcome:
        case Submitted(payload=order, attempt_count=n):
            acks = (
                classify_legs(order)
                if classify_legs is not None
                else _classify_leg_acks(order.legs)
            )
            return Submitted(
                EquitySubmission(
                    alpaca_order_id=AlpacaOrderId(str(order.id)),
                    client_order_id=ClientOrderId(order.client_order_id),
                    status=order.status.value,
                    order_class=order.order_class.value,
                    leg_acks=acks,
                ),
                attempt_count=n,
            )
        case GatewaySubmissionFailed():
            return outcome


# alpaca-py ``OrderType`` value → OMS protective-leg classification. A native
# bracket / OTO returns its take-profit as a LIMIT child and its price-stop as
# a STOP / STOP_LIMIT child; nothing else is a protective leg we capture.
_LEG_ROLE_BY_ORDER_TYPE: dict[str, LegRole] = {
    "limit": "take_profit",
    "stop": "stop_loss",
    "stop_limit": "stop_loss",
}


def _classify_leg_acks(legs: object) -> tuple[EquityLegAck, ...]:
    """Capture real ids for the protective children on a native BRACKET / OTO.

    ``order.legs`` is alpaca-py ``list[Order] | None``; defensively narrowed to
    ``list`` (a SIMPLE order has no children, and a fake/None payload yields no
    acks). Each child is classified by ``order_type`` — LIMIT is the take-profit,
    STOP / STOP_LIMIT is the price-stop; any other child type is skipped rather
    than misclassified.

    A native bracket carries exactly one take-profit + one price-stop child and
    an OTO exactly one take-profit; the parent/entry order is NOT in ``legs``.
    So each role appears at most once, which is the invariant the role-keyed
    ``leg_alpaca_order_ids`` map in ``broker_dispatch._wrap_equity`` relies on
    (a second ack of the same role would silently overwrite).
    """
    if not isinstance(legs, list):
        return ()
    acks: list[EquityLegAck] = []
    for leg in legs:
        order_type = getattr(leg.order_type, "value", leg.order_type)
        role = _LEG_ROLE_BY_ORDER_TYPE.get(order_type)
        if role is None:
            continue
        acks.append(EquityLegAck(alpaca_order_id=AlpacaOrderId(str(leg.id)), role=role))
    return tuple(acks)


def _classify_oco_leg_acks(order: Order) -> tuple[EquityLegAck, ...]:
    """Capture BOTH protective ids for a standalone OCO submission (ALP-938).

    A native bracket / OTO returns its entry as the top-level order and its
    protective children on ``order.legs``, so :func:`_classify_leg_acks` (children
    only) suffices. A standalone OCO has NO entry parent — depending on alpaca-py's
    shape the take-profit may surface as the top-level order itself (with the stop
    child on ``order.legs``) or both may surface as legs. So classify the children
    first, then fill any role the children did NOT supply from the top-level order.
    Filling only the *missing* role never double-counts when both already surface
    as legs, and recovers the take-profit when it is the parent limit.
    """
    acks: list[EquityLegAck] = list(_classify_leg_acks(order.legs))
    present_roles = {ack.role for ack in acks}
    parent_order_type = getattr(order.order_type, "value", order.order_type)
    parent_role = (
        _LEG_ROLE_BY_ORDER_TYPE.get(parent_order_type)
        if isinstance(parent_order_type, str)
        else None
    )
    if parent_role is not None and parent_role not in present_roles:
        acks.append(EquityLegAck(alpaca_order_id=AlpacaOrderId(str(order.id)), role=parent_role))
    return tuple(acks)
