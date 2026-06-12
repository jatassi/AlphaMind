"""Bounded async facades over alpaca-py's synchronous seams (ALP-946).

alpaca-py is synchronous in two distinct ways and both can freeze the calling
event loop:

* **REST calls** issue blocking ``requests`` round-trips with no socket
  timeout — a hung connection blocks the calling thread forever (the ALP-841
  wedge: a bare-sync ``get_orders`` froze the monitor loop and its watchdog
  for ~4.5h).
* **Stream mutators** (``subscribe_quotes`` / ``unsubscribe_quotes`` /
  ``subscribe_trade_updates``) are *loop-affined*: on a running stream they
  marshal the actual send onto the stream's own event loop via
  ``run_coroutine_threadsafe`` and block the calling thread on an untimed
  ``Future.result()``. Called from that loop's own thread the scheduled
  coroutine can never run — a guaranteed self-deadlock (the ALP-946 wedge:
  every position-set-changing invocation froze the monitor for ~150 s).

:func:`bounded_call` is the one safe calling convention for both: offloading
to a worker thread keeps the loop turning (and gives the loop-affined
mutators a free loop to marshal onto), and the :func:`asyncio.wait_for`
bound guarantees the await resolves even if the callable never returns —
raising :class:`TimeoutError` for the caller's reconnect/retry machinery.
The client factory's socket-level timeout remains defence-in-depth: it lets
the orphaned worker thread eventually unwind rather than leak.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

# Wall-clock budget (seconds) for one vendor stream-mutator call run on a
# worker thread. Mirrors ``queries._REST_TIMEOUT_SECONDS`` (30s) and sits
# below the client factory's 60s socket-timeout floor, so this loop-level
# bound is the one that normally fires and a wedged send surfaces as a
# TimeoutError-driven reconnect rather than a frozen await.
_STREAM_CALL_TIMEOUT_SECONDS = 30.0


async def bounded_call[T](fn: Callable[..., T], /, *args: object, timeout_seconds: float) -> T:
    """Run a synchronous alpaca-py call off the event loop, time-bounded.

    Offloading to a worker thread keeps the calling loop running — for REST
    calls that means the loop survives a hung socket (ALP-841); for the
    loop-affined stream mutators it means the send they marshal back onto
    the loop can actually drain (ALP-946). The :func:`asyncio.wait_for`
    bound guarantees the await resolves within *timeout_seconds* even if the
    callable never returns — raising :class:`TimeoutError`, which callers
    treat as transient (retry on the next sweep interval, or tear down and
    rebuild the stream). The client factory's socket-level timeout is the
    matching thread-side floor so the orphaned worker eventually unwinds.
    """
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout_seconds)


QuoteHandler = Callable[[Any], Awaitable[None]]


class _SyncStockDataStream(Protocol):
    """The vendor ``StockDataStream`` surface the facade wraps.

    The mutators are the loop-affined synchronous APIs; ``_run_forever`` is
    alpaca-py's documented async entry point (the underscore prefix is the
    library's own convention) and ``stop_ws`` its async teardown.
    """

    def subscribe_quotes(self, handler: QuoteHandler, *symbols: str) -> None: ...

    def unsubscribe_quotes(self, *symbols: str) -> None: ...

    async def _run_forever(self) -> None: ...

    async def stop_ws(self) -> None: ...


class BoundedStockDataStream:
    """Async facade over a vendor ``StockDataStream`` (ALP-946).

    The vendor mutators must run on a thread *other than* the stream's own
    event-loop thread (see module docstring); the facade routes every call
    through :func:`bounded_call` so callers can simply ``await`` from the
    loop. ``_run_forever`` / ``stop_ws`` are already async — plain awaited
    delegations.
    """

    def __init__(
        self,
        stream: _SyncStockDataStream,
        *,
        timeout_seconds: float = _STREAM_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._stream = stream
        self._timeout_seconds = timeout_seconds

    async def subscribe_quotes(self, handler: QuoteHandler, *symbols: str) -> None:
        await bounded_call(
            self._stream.subscribe_quotes, handler, *symbols, timeout_seconds=self._timeout_seconds
        )

    async def unsubscribe_quotes(self, *symbols: str) -> None:
        await bounded_call(
            self._stream.unsubscribe_quotes, *symbols, timeout_seconds=self._timeout_seconds
        )

    async def _run_forever(self) -> None:
        await self._stream._run_forever()

    async def stop_ws(self) -> None:
        await self._stream.stop_ws()


class _SyncTradingStream(Protocol):
    """The vendor ``TradingStream`` surface the facade wraps."""

    def subscribe_trade_updates(self, handler: Any) -> None: ...

    async def _run_forever(self) -> None: ...


class BoundedTradingStream:
    """Async facade over a vendor ``TradingStream`` (ALP-946).

    ``subscribe_trade_updates`` carries the same loop-affined blocking branch
    as the stock-stream mutators (``alpaca/trading/stream.py``); the facade
    routes it through :func:`bounded_call` so the fill stream's subscribe can
    never block the event loop, however the stream's run state interleaves.
    """

    def __init__(
        self,
        stream: _SyncTradingStream,
        *,
        timeout_seconds: float = _STREAM_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._stream = stream
        self._timeout_seconds = timeout_seconds

    async def subscribe_trade_updates(self, handler: Any) -> None:
        await bounded_call(
            self._stream.subscribe_trade_updates, handler, timeout_seconds=self._timeout_seconds
        )

    async def _run_forever(self) -> None:
        await self._stream._run_forever()
