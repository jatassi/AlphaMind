"""``trade_updates`` fill-stream subscriber + translator (story 02f / ALP-384).

Wraps alpaca-py's ``TradingStream.subscribe_trade_updates`` in an async
generator the continuous monitor (ALP-123) can drain in its run-forever loop.
The pure ``translate_trade_update`` function maps each ``TradeUpdate`` into
one or more :class:`FillReport` records per
``architecture.md § Fill report contract``:

* equity / single-leg-options events emit a single report;
* ``mleg`` parent events emit the parent followed by per-leg children, each
  child carrying the parent's ``client_order_id`` and ``alpaca_order_id``
  for OMS-side correlation per ``broker-adapter.md § Multi-leg fill events``;
* alpaca-py event types not in the OMS vocabulary
  (``pending_new`` / ``pending_cancel`` / ``calculated`` / ``accepted`` /
  ``pending_replace``) translate to an empty tuple and are filtered before
  the generator yields.

The primitive itself yields events as they arrive; lifecycle (connect /
disconnect / reconnect / fill-buffer write / disconnect-recovery) lives in
the continuous monitor's run-loop (ALP-123 + story 03d).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

from alpaca.trading.models import Order, TradeUpdate
from pydantic import BaseModel, ConfigDict, field_validator

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, OccSymbol, make_occ_symbol

OrderStatus = Literal[
    "new",
    "filled",
    "partially_filled",
    "canceled",
    "expired",
    "replaced",
    "replace_rejected",
    "stopped",
    "rejected",
    "done_for_day",
]

PositionIntentLiteral = Literal[
    "buy_to_open",
    "sell_to_open",
    "buy_to_close",
    "sell_to_close",
]


class FillReport(BaseModel):
    """OMS-facing projection of an alpaca-py ``TradeUpdate`` event.

    Every ``TradeUpdate`` translates to zero or more ``FillReport`` records:
    equity / single-leg-options events produce one; ``mleg`` parent events
    produce one parent + N per-leg children; filtered alpaca-py events
    (e.g. ``pending_new``) produce none.

    The ``live_execution_estimate`` field documented on
    ``architecture.md § Fill report contract`` is intentionally NOT carried
    on this record — the paper-evaluation harness (ALP-130) attaches that
    metadata downstream of this primitive in the monitor's run-loop.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: ClientOrderId
    alpaca_order_id: AlpacaOrderId
    parent_client_order_id: ClientOrderId | None
    parent_alpaca_order_id: AlpacaOrderId | None
    event_type: OrderStatus
    fill_timestamp: datetime
    fill_price: float | None
    fill_quantity: float | None
    cumulative_filled_quantity: float
    remaining_quantity: float
    execution_venue: str | None
    occ_symbol: OccSymbol | None
    position_intent: PositionIntentLiteral | None
    raw_event_payload: dict[str, Any]

    @field_validator("fill_timestamp")
    @classmethod
    def _coerce_to_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


# alpaca-py event string -> OMS OrderStatus literal. ``order_replace_rejected``
# is an alias alpaca-py emits for ``replace_rejected``; both flow to the same
# OMS status. Anything not in this map (``pending_new`` / ``pending_cancel``
# / ``calculated`` / ``accepted`` / ``pending_replace``) is filtered.
_EVENT_TO_STATUS: Final[dict[str, OrderStatus]] = {
    "new": "new",
    "fill": "filled",
    "partial_fill": "partially_filled",
    "canceled": "canceled",
    "expired": "expired",
    "replaced": "replaced",
    "replace_rejected": "replace_rejected",
    "order_replace_rejected": "replace_rejected",
    "stopped": "stopped",
    "rejected": "rejected",
    "done_for_day": "done_for_day",
}

# alpaca-py event types whose ``price`` / ``qty`` fields populate the
# corresponding ``FillReport`` fields. Every other event type leaves
# ``fill_price`` / ``fill_quantity`` as ``None`` per ``broker-adapter.md
# § Fill stream § Event types consumed``.
_FILL_BEARING_EVENTS: Final[frozenset[str]] = frozenset({"fill", "partial_fill", "stopped"})


def translate_trade_update(update: TradeUpdate) -> tuple[FillReport, ...]:
    """Map an alpaca-py ``TradeUpdate`` into one or more ``FillReport`` records.

    For equity / single-leg options: returns a single-element tuple. For
    ``mleg`` parent events: returns the parent ``FillReport`` followed by
    per-leg ``FillReport`` children, each carrying the parent's
    ``client_order_id`` / ``alpaca_order_id``. For filtered alpaca-py events:
    returns an empty tuple.
    """
    event_str = _event_string(update.event)
    status = _EVENT_TO_STATUS.get(event_str)
    if status is None:
        return ()

    # Serialize the alpaca-py payload once; every parent + leg report on this
    # update shares the same raw_event_payload reference.
    raw_payload = update.model_dump(mode="json")
    parent = _build_parent_report(update, status, raw_payload)
    if not _is_mleg(update.order):
        return (parent,)

    children = tuple(
        _build_leg_report(leg, update, parent, status, raw_payload)
        for leg in (update.order.legs or [])
    )
    return (parent, *children)


def _event_string(event: object) -> str:
    """Coerce alpaca-py's ``TradeEvent | str`` field to its raw string value."""
    raw_value = getattr(event, "value", None)
    if isinstance(raw_value, str):
        return raw_value
    return str(event)


def _is_mleg(order: Order) -> bool:
    """Whether *order* is an mleg parent strategy with leg children."""
    order_class = getattr(order.order_class, "value", order.order_class)
    return order_class == "mleg" and bool(order.legs)


def _build_parent_report(
    update: TradeUpdate, status: OrderStatus, raw_payload: dict[str, Any]
) -> FillReport:
    """Build the single-event / mleg-parent report from *update*."""
    order = update.order
    fill_price, fill_quantity = _extract_fill_metrics(update)
    cumulative, remaining = _compute_quantities(order)
    occ = _occ_symbol_for_parent(order)
    return FillReport(
        client_order_id=ClientOrderId(order.client_order_id),
        alpaca_order_id=AlpacaOrderId(str(order.id)),
        parent_client_order_id=None,
        parent_alpaca_order_id=None,
        event_type=status,
        fill_timestamp=update.timestamp,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        cumulative_filled_quantity=cumulative,
        remaining_quantity=remaining,
        execution_venue=None,
        occ_symbol=make_occ_symbol(occ) if occ is not None else None,
        position_intent=None,
        raw_event_payload=raw_payload,
    )


def _build_leg_report(
    leg: Order,
    update: TradeUpdate,
    parent: FillReport,
    status: OrderStatus,
    raw_payload: dict[str, Any],
) -> FillReport:
    """Build a per-leg child report for an mleg parent event."""
    cumulative, remaining = _compute_quantities(leg)
    return FillReport(
        client_order_id=ClientOrderId(leg.client_order_id),
        alpaca_order_id=AlpacaOrderId(str(leg.id)),
        parent_client_order_id=parent.client_order_id,
        parent_alpaca_order_id=parent.alpaca_order_id,
        event_type=status,
        fill_timestamp=update.timestamp,
        fill_price=parent.fill_price,
        fill_quantity=parent.fill_quantity,
        cumulative_filled_quantity=cumulative,
        remaining_quantity=remaining,
        execution_venue=None,
        occ_symbol=make_occ_symbol(leg.symbol) if leg.symbol is not None else None,
        position_intent=_position_intent_for(leg),
        raw_event_payload=raw_payload,
    )


def _occ_symbol_for_parent(order: Order) -> str | None:
    """Return the ``symbol`` for single-leg options events; ``None`` otherwise.

    Mleg parents have ``symbol=None`` per alpaca-py's serialization (the
    symbol lives on each leg). Equities carry an underlying ticker we treat
    as the equity symbol — not OCC. Only single-leg options events surface a
    symbol that is OCC-shaped.
    """
    asset_class = getattr(order.asset_class, "value", order.asset_class)
    if asset_class == "us_option":
        return order.symbol
    return None


def _position_intent_for(leg: Order) -> PositionIntentLiteral | None:
    """Coerce alpaca-py's ``PositionIntent`` enum value to the OMS literal."""
    intent = leg.position_intent
    if intent is None:
        return None
    raw = getattr(intent, "value", intent)
    if raw in {"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"}:
        # Runtime membership check above narrows to the Literal alphabet.
        return cast(PositionIntentLiteral, raw)
    return None


def _extract_fill_metrics(update: TradeUpdate) -> tuple[float | None, float | None]:
    """Return ``(fill_price, fill_quantity)`` for *update*'s event type.

    Non-fill events (``new``, ``canceled``, ``expired``, ``replaced``,
    ``replace_rejected``, ``rejected``, ``done_for_day``) yield
    ``(None, None)`` per ``broker-adapter.md § Fill stream``.
    """
    event_str = _event_string(update.event)
    if event_str not in _FILL_BEARING_EVENTS:
        return None, None
    return update.price, update.qty


def _compute_quantities(order: Order) -> tuple[float, float]:
    """Return ``(cumulative_filled, remaining)`` from *order*'s alpaca-py fields.

    ``Order.qty`` and ``Order.filled_qty`` may arrive as ``str`` or ``float``
    per alpaca-py's loose schema; we coerce to ``float`` and treat missing
    values as zero so the OMS-facing record always has finite numbers.
    """
    cumulative = _coerce_float(order.filled_qty)
    total = _coerce_float(order.qty)
    return cumulative, max(total - cumulative, 0.0)


def _coerce_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Subscriber
# ---------------------------------------------------------------------------


class _SubscribableStream(Protocol):
    """Subset of ``alpaca.trading.stream.TradingStream`` we depend on.

    Defined as a protocol so the test suite can swap a fake without inheriting
    from the real (network-touching) class.
    """

    def subscribe_trade_updates(self, handler: Any) -> None: ...

    async def _run_forever(self) -> None: ...


class FillStreamStalledError(Exception):
    """A connected ``trade_updates`` stream stopped delivering frames during RTH.

    Raised by :func:`subscribe_trade_updates` when no frame arrives within
    ``frame_timeout`` while the market is open (ALP-819). The ``websockets``
    library reconnects internally on a transport error (e.g. ``WinError 121``)
    without raising or delivering frames, so the ALP-768 done-callback sentinel
    never fires and ``queue.get()`` would otherwise park forever. The consumer
    treats this as a budget-neutral signal to tear down and rebuild the stream.
    """


async def subscribe_trade_updates(
    stream: _SubscribableStream,
    *,
    frame_timeout: float | None = None,
    is_rth: Callable[[], bool] | None = None,
    beat: Callable[[], None] = lambda: None,
    poll_interval: float = 5.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncGenerator[FillReport]:
    """Subscribe to ``trade_updates`` and yield ``FillReport`` per event.

    Connects, authenticates, subscribes to the ``trade_updates`` channel via
    the supplied alpaca-py ``TradingStream``, and yields one ``FillReport``
    per ``TradeUpdate`` produced by ``translate_trade_update`` — equity /
    single-leg events emit one, ``mleg`` parents emit parent + per-leg
    children individually in order, filtered events emit nothing.

    The continuous monitor (ALP-123) wraps this generator with the run-forever
    lifecycle, fill-buffer durable write, reconnect orchestration, and
    ``GET /v2/orders since-recovery`` (story 03d). The primitive itself yields
    events as they arrive; on ``asyncio.CancelledError`` the generator exits
    cleanly and the background ``stream._run_forever()`` task is cancelled.
    Any exception raised during translation propagates to the consumer so the
    caller's run-loop can catch and trigger reconnect.

    Connected-but-silent detection (ALP-819): the consume loop waits on
    ``queue.get()`` in ``poll_interval`` slices rather than one unbounded
    ``await`` so it can (a) call ``beat()`` on every slice — feeding the
    monitor's stall watchdog a real fill-consumer liveness signal — and
    (b) raise :class:`FillStreamStalledError` when ``is_rth()`` is true and no
    frame has arrived for longer than ``frame_timeout``. That catches the
    library-internal-reconnect path the ALP-768 done-callback misses (the
    socket flaps, the library re-loops without raising, no sentinel fires).
    All four knobs are optional: with ``frame_timeout``/``is_rth`` unset the
    staleness branch is inert and the loop behaves as an ordinary drain.
    """
    # None is used as a sentinel: the done-callback puts it when run_task
    # finishes so queue.get() unblocks even if no fill events arrive.
    # The queue is unbounded (maxsize=0) — put_nowait() in the done-callback
    # must not raise QueueFull. Do not cap this queue.
    queue: asyncio.Queue[TradeUpdate | None] = asyncio.Queue()

    async def _handler(update: TradeUpdate) -> None:
        await queue.put(update)

    stream.subscribe_trade_updates(_handler)
    # ``asyncio.TaskGroup`` is incompatible with async-generator cleanup:
    # ``gen.aclose()`` injects ``GeneratorExit`` into the body, which a
    # surrounding ``async with TaskGroup()`` re-raises as
    # ``BaseExceptionGroup`` from ``__aexit__`` rather than propagating
    # cleanly. The bare :func:`asyncio.create_task` lets us manage the
    # background task's lifecycle through the generator's ``try/finally``
    # and suppress its cleanup-time exceptions so the consumer observes
    # only its own failures (translation errors or ``CancelledError``).
    #
    # The done-callback links run_task's lifecycle to the consumer: when
    # _run_forever() raises (e.g. OSError on a broken pipe) or returns
    # cleanly, the sentinel unblocks queue.get() so the consumer can
    # inspect run_task.result() and propagate the failure to the
    # monitor's reconnect loop (fill_stream_consumer/task.py:123-135).
    run_task = asyncio.create_task(stream._run_forever())
    run_task.add_done_callback(lambda _t: queue.put_nowait(None))

    last_frame_at = monotonic()
    try:
        while True:
            beat()
            try:
                update = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except TimeoutError:
                # No frame this slice. A genuinely-quiet stream and a
                # silently-wedged one look identical here, so force a reconnect
                # only when the market is open (fills are sparse off-hours) and
                # the silence exceeds frame_timeout. The consumer's reconnect
                # re-subscribes a fresh socket and REST-recovers any gap.
                if (
                    frame_timeout is not None
                    and is_rth is not None
                    and is_rth()
                    and monotonic() - last_frame_at > frame_timeout
                ):
                    msg = f"no trade_updates frame for >{frame_timeout:.0f}s during RTH"
                    raise FillStreamStalledError(msg) from None
                continue
            last_frame_at = monotonic()
            if update is None:
                # run_task finished (exception or clean close).
                # result() re-raises its exception; returns None on clean.
                run_task.result()
                return
            for report in translate_trade_update(update):
                yield report
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task
