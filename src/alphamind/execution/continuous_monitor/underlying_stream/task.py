"""Underlying-price stream consumer task (story 02b / ALP-434; ALP-832 staleness).

Run-forever asyncio task that maintains a live Alpaca ``StockDataStream``
(IEX feed) subscription to the equity underlyings backing open positions,
draining quotes into the shared :class:`UnderlyingPriceCache`. Story 03a
(greeks refresh), 03b (breach evaluation), and 04c (options bracket-stop)
read through the cache; this task is the single writer.

Lifecycle:

* Build a fresh ``StockDataStream`` via the injected
  :class:`AlpacaStreamFactory`. The factory wraps credential resolution +
  alpaca-py instantiation so tests can substitute a fake.
* Subscribe to ``compute_target_underlyings(repository)``; register the
  quote handler that translates Alpaca's ``Quote`` payload into an
  :class:`UnderlyingQuote` and writes it to the cache.
* Drive the alpaca-py ``_run_forever()`` loop on a sibling task, with a
  staleness-watch sibling that beats the supervisor watchdog on every poll
  slice and raises :class:`~alphamind.execution.broker_adapter.StreamStalledError`
  on RTH silence beyond ``underlying_stream_stale_timeout_seconds`` (ALP-832).
* Every ``config.subscription_refresh_seconds``, diff the current target
  set against the live subscription; issue add / remove deltas.
* On websocket disconnect, the sibling task raises; catch, log, back off
  exponentially, and rebuild the stream. After ``max_reconnect_attempts``
  the exception propagates so the supervisor's exit logging records the
  failure.
* On :class:`StreamStalledError`, reconnect budget-neutrally: the stream
  is torn down and rebuilt without consuming ``max_reconnect_attempts`` so
  a persistently silent stream keeps recovering (ALP-832).
* On :class:`asyncio.CancelledError`, call ``stream.stop_ws()`` and exit.

The cache is shared across reconnect cycles — quotes seen before a
disconnect remain readable until a fresh quote overwrites them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter import StreamActivityMonitor, StreamStalledError
from alphamind.execution.continuous_monitor.session import MonitorMode, MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
    compute_target_underlyings,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendor surface — Protocols the task depends on
# ---------------------------------------------------------------------------


QuoteHandler = Callable[[Any], Awaitable[None]]


@runtime_checkable
class StockDataStreamProtocol(Protocol):
    """Async surface of the bounded stock-stream facade this task uses.

    Defined as a Protocol so the test suite can swap a fake without
    inheriting from alpaca-py's network-touching class. The runtime object
    is a :class:`~alphamind.execution.broker_adapter.bounded_streams.BoundedStockDataStream`
    wrapping the vendor stream. The mutators are ``async`` by contract
    (ALP-946): alpaca-py's bare sync mutators are loop-affined — calling one
    on the stream's own loop thread self-deadlocks the whole process loop —
    so the seam only admits the bounded off-thread form.
    """

    async def subscribe_quotes(self, handler: QuoteHandler, *symbols: str) -> None: ...

    async def unsubscribe_quotes(self, *symbols: str) -> None: ...

    async def _run_forever(self) -> None: ...

    async def stop_ws(self) -> None: ...


@runtime_checkable
class AlpacaStreamFactory(Protocol):
    """Factory that mints a fresh :class:`StockDataStreamProtocol` per connect.

    The real factory reads credentials from environment variables and
    constructs ``alpaca.data.live.stock.StockDataStream(feed=DataFeed.IEX)``.
    Tests pass a fake factory whose ``build`` records call history and yields
    in-memory stream substitutes.
    """

    def build(self, *, mode: str) -> StockDataStreamProtocol: ...


# ---------------------------------------------------------------------------
# Default factory — production wiring
# ---------------------------------------------------------------------------


class DefaultAlpacaStreamFactory:
    """Real :class:`AlpacaStreamFactory` — wraps alpaca-py's ``StockDataStream``.

    Resolves credentials from environment variables per parent issue ALP-123
    decision (B): ``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET`` in paper
    mode, ``ALPACA_LIVE_KEY`` / ``ALPACA_LIVE_SECRET`` in live mode. Raises
    :class:`RuntimeError` naming any unset env var so misconfiguration
    surfaces at startup rather than as silent zero-quote behavior.
    """

    def build(self, *, mode: str) -> StockDataStreamProtocol:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live.stock import StockDataStream

        from alphamind.execution.broker_adapter.bounded_streams import BoundedStockDataStream

        if mode == "live":
            key_env, secret_env = "ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET"
        else:
            key_env, secret_env = "ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"
        api_key = os.environ.get(key_env, "")
        api_secret = os.environ.get(secret_env, "")
        missing = [name for name, val in ((key_env, api_key), (secret_env, api_secret)) if not val]
        if missing:
            msg = (
                f"Alpaca {mode} credentials not set: environment variable(s) "
                f"{missing} are unset or empty"
            )
            raise RuntimeError(msg)
        return BoundedStockDataStream(
            StockDataStream(
                api_key=api_key,
                secret_key=api_secret,
                feed=DataFeed.IEX,
            )
        )


# ---------------------------------------------------------------------------
# Quote translation
# ---------------------------------------------------------------------------


def _quote_to_underlying(payload: Any) -> UnderlyingQuote:
    """Translate an alpaca-py ``Quote`` payload into an :class:`UnderlyingQuote`.

    Uses the mid-price ``(bid + ask) / 2`` as the underlying price reference
    per architecture.md § 4e (the underlying-equity feed drives trigger
    evaluation; mid is the standard reference for "where the underlying is
    trading right now"). ``timestamp`` is alpaca-py's parsed datetime —
    already tz-aware UTC.
    """
    ts: datetime = payload.timestamp
    if ts.tzinfo is None or ts.utcoffset() is None:
        ts = ts.replace(tzinfo=UTC)
    mid = (float(payload.bid_price) + float(payload.ask_price)) / 2.0
    return UnderlyingQuote(ticker=payload.symbol, price=mid, as_of=ts)


# ---------------------------------------------------------------------------
# Run-forever task
# ---------------------------------------------------------------------------


async def run_underlying_stream(  # noqa: PLR0913 — run-forever orchestrator surfaces each seam for injection
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    factory: AlpacaStreamFactory,
    is_market_open: Callable[[datetime], bool] | None = None,
    beat: Callable[[], None] = lambda: None,
    register_watch: Callable[[float], None] = lambda _cadence: None,
    monotonic: Callable[[], float] = time.monotonic,
    stream_poll_interval: float = 5.0,
) -> None:
    """Long-running consumer the supervisor registers as ``underlying_stream``.

    See module docstring for the lifecycle contract; raises on reconnect
    budget exhaustion so the supervisor's exit logging records the failure
    and NSSM's restart policy kicks in.

    Stall resilience (ALP-832). ``is_market_open`` + ``beat`` opt this task
    into the connected-but-silent detection and the supervisor's stall watchdog,
    mirroring how the fill consumer was wired in ALP-828. During RTH, if no
    quote arrives within ``config.underlying_stream_stale_timeout_seconds``, the
    :class:`~alphamind.execution.broker_adapter.StreamActivityMonitor` raises
    :class:`~alphamind.execution.broker_adapter.StreamStalledError`; that is a
    *budget-neutral* reconnect — a deliberate health refresh, not a failure — so
    a persistently silent stream keeps recovering. ``beat`` is fired on every
    poll slice so a genuinely-blocked writer stops beating and trips the watchdog.

    Watchdog binding (ALP-832). ``register_watch`` is called once at startup with
    ``stream_poll_interval`` so the watchdog derives a positive stall bound
    (``poll_interval * watchdog_cadence_multiplier``). Without this declaration a
    bare ``beat`` would leave the task watched-but-unbounded.

    Initial-connect trippability (ALP-825 review). ``beat`` is fired once
    immediately at entry — before ``factory.build`` / ``subscribe_quotes`` — so
    ``last_beat`` is set and a hang during the very first connect/subscribe
    becomes trippable. Mirrors the fill consumer, which beats as its first
    statement. Without this, the first beat would only land on the first poll
    slice (after connect succeeds), so an initial-connect wedge would leave
    ``last_beat=None`` and the watchdog could only WARN (startup grace), never
    ``os._exit``-trip.
    """
    register_watch(stream_poll_interval)
    beat()
    is_rth: Callable[[], bool] | None = (
        (lambda: is_market_open(datetime.now(UTC))) if is_market_open is not None else None
    )

    attempts_remaining = config.max_reconnect_attempts
    backoff_seconds = 1.0
    while True:
        try:
            await _run_one_connection(
                mode=session.mode,
                config=config,
                repository=repository,
                cache=cache,
                factory=factory,
                is_rth=is_rth,
                beat=beat,
                monotonic=monotonic,
                stream_poll_interval=stream_poll_interval,
            )
        except asyncio.CancelledError:
            raise
        except StreamStalledError:
            # Connected-but-silent stream detected during RTH (ALP-832): the
            # websockets library reconnect-looped internally without raising or
            # delivering frames. Tear down and rebuild a fresh stream. This is a
            # deliberate health refresh, NOT a failure — reset the reconnect budget
            # so a persistently silent stream keeps recovering rather than exhausting
            # the budget and exiting. The next connection gets a full
            # underlying_stream_stale_timeout_seconds window before it can go stale
            # again, so this never hot-loops in production.
            log.warning(
                "underlying_stream: no quotes for >%ds during RTH; "
                "forcing reconnect (budget-neutral)",
                config.underlying_stream_stale_timeout_seconds,
            )
            attempts_remaining = config.max_reconnect_attempts
            backoff_seconds = 1.0
            continue
        except BaseException as exc:
            # Reconnect-budget supervisor per runtime §G1: ``BaseException``
            # (vs ``Exception``) is intentional — alpaca-py raises raw
            # ``KeyboardInterrupt``-shaped failures in some websocket paths,
            # and the supervisor cancellation envelope must still count down
            # the reconnect budget. ``CancelledError`` re-raised above so
            # shutdown is honored.
            attempts_remaining -= 1
            if attempts_remaining <= 0:
                log.exception("underlying_stream reconnect budget exhausted; propagating")
                raise
            log.warning(
                "underlying_stream disconnect; %d reconnect attempts remaining; "
                "backoff %.1fs; reason=%r",
                attempts_remaining,
                backoff_seconds,
                exc,
            )
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2.0, 30.0)
        else:
            # ``_run_one_connection`` only returns on a clean finish (e.g.
            # ``stop_ws`` called externally). Reset the reconnect budget and
            # backoff so a healthy period of clean reconnects does not leave
            # the error-handling state in a degraded condition, then fall
            # through to the top of the loop so the writer never silently
            # exits while the process stays alive.
            log.info("underlying_stream clean connection exit; reconnecting")
            attempts_remaining = config.max_reconnect_attempts
            backoff_seconds = 1.0
            continue


async def _run_one_connection(  # noqa: PLR0913 — internal plumbing; each parameter is one injected seam
    *,
    mode: MonitorMode,
    config: ContinuousMonitorConfig,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    factory: AlpacaStreamFactory,
    is_rth: Callable[[], bool] | None,
    beat: Callable[[], None],
    monotonic: Callable[[], float],
    stream_poll_interval: float,
) -> None:
    """Run one connect → subscribe → drain → diff cycle.

    Returns cleanly only if the stream's background task exits normally.
    Any websocket failure raises out so the surrounding ``while True`` loop
    in :func:`run_underlying_stream` handles the reconnect budget.
    :class:`~alphamind.execution.broker_adapter.StreamStalledError` raised
    by the staleness-watch sibling propagates out budget-neutrally.
    """
    stream = factory.build(mode=mode)
    monitor = StreamActivityMonitor(
        frame_timeout=float(config.underlying_stream_stale_timeout_seconds),
        is_rth=is_rth,
        beat=beat,
        poll_interval=stream_poll_interval,
        monotonic=monotonic,
    )

    async def _handler(payload: Any) -> None:
        try:
            quote = _quote_to_underlying(payload)
        except (AttributeError, ValueError, TypeError):
            log.exception("underlying_stream quote translation failed; payload=%r", payload)
            return
        monitor.record_activity()
        await cache.update(quote)

    # Subscribe to the initial target set.
    current: frozenset[str] = await compute_target_underlyings(repository)
    if current:
        await stream.subscribe_quotes(_handler, *sorted(current))
    else:
        # ``subscribe_quotes`` with no symbols still registers the handler so
        # late-arriving subscribe calls reuse it.
        await stream.subscribe_quotes(_handler)

    # ``_run_forever`` is alpaca-py's documented async entry point — the
    # underscore-prefixed name is the library's own convention, not a
    # private method. See ``alpaca.data.live.stock``.
    #
    # Three sibling tasks live under an :class:`asyncio.TaskGroup`:
    # * ``run_task`` — the long-running websocket loop
    # * ``diff_task`` — the periodic subscription refresher
    # * ``watch_task`` — per-slice beat + staleness check (ALP-832)
    # The first failure cancels the others automatically; ``StreamStalledError``
    # from ``watch_task`` propagates as a budget-neutral reconnect signal.
    try:
        async with asyncio.TaskGroup() as tg:
            run_task: asyncio.Task[None] = tg.create_task(stream._run_forever())
            diff_task: asyncio.Task[None] = tg.create_task(
                _periodic_subscription_diff(
                    stream=stream,
                    handler=_handler,
                    repository=repository,
                    current=current,
                    cadence_seconds=config.subscription_refresh_seconds,
                )
            )
            watch_task: asyncio.Task[None] = tg.create_task(_staleness_watch_loop(monitor=monitor))
            # When ``run_task`` exits we treat it as the canonical
            # "connection ended" signal: cancel the siblings so the group
            # exits promptly.
            run_task.add_done_callback(lambda _t: diff_task.cancel())
            run_task.add_done_callback(lambda _t: watch_task.cancel())
    except asyncio.CancelledError:
        # Supervisor shutdown (external cancellation) — Python 3.13's
        # TaskGroup re-raises ``CancelledError`` bare rather than wrapping
        # in ``BaseExceptionGroup``. Best-effort socket close so alpaca-py
        # doesn't leak the connection; ``asyncio.shield`` over an
        # intermediate task is required because ``await`` from a cancelled
        # task re-raises immediately.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(asyncio.ensure_future(_safe_stop_ws(stream)))
        raise
    except BaseExceptionGroup as eg:
        run_task_exc = first_non_cancelled(eg)
        with contextlib.suppress(Exception):
            await stream.stop_ws()
        if run_task_exc is not None:
            raise run_task_exc from eg
        # Pure-cancellation group — treat as supervisor shutdown.
        raise asyncio.CancelledError from eg
    else:
        # Clean ``run_task`` exit — best-effort socket close to mirror the
        # disconnect path; ``stop_ws`` is idempotent.
        with contextlib.suppress(Exception):
            await stream.stop_ws()


async def _staleness_watch_loop(*, monitor: StreamActivityMonitor) -> None:
    """Heartbeat + staleness watch: one :meth:`on_slice` per ``poll_interval``.

    Runs as a sibling inside the connection's :class:`asyncio.TaskGroup`. On
    each iteration it sleeps ``monitor.poll_interval`` seconds then calls
    :meth:`~StreamActivityMonitor.on_slice`, which beats the watchdog and
    raises :class:`~alphamind.execution.broker_adapter.StreamStalledError`
    if RTH silence exceeds the configured threshold. The ``TaskGroup``
    propagates that exception out of :func:`_run_one_connection` so
    :func:`run_underlying_stream` performs a budget-neutral reconnect.
    """
    while True:
        await asyncio.sleep(monitor.poll_interval)
        monitor.on_slice()


async def _periodic_subscription_diff(
    *,
    stream: StockDataStreamProtocol,
    handler: QuoteHandler,
    repository: OpenPositionsReader,
    current: frozenset[str],
    cadence_seconds: int,
) -> None:
    """Re-compute the target set every cadence_seconds; issue subscribe deltas.

    ``current`` is the snapshot the connect-time subscribe call used; this
    task mutates a local copy as it issues add / remove deltas so its view
    matches the live socket's subscription state across iterations.
    """
    live = set(current)
    while True:
        await asyncio.sleep(cadence_seconds)
        target = await compute_target_underlyings(repository)
        added = target - live
        removed = live - target
        if added:
            await stream.subscribe_quotes(handler, *sorted(added))
        if removed:
            await stream.unsubscribe_quotes(*sorted(removed))
        live = set(target)


async def _safe_stop_ws(stream: StockDataStreamProtocol) -> None:
    """Wrapper around ``stream.stop_ws()`` that swallows secondary errors."""
    with contextlib.suppress(Exception):
        await stream.stop_ws()
