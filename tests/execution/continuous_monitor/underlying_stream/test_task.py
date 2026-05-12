"""Tests for ``run_underlying_stream`` (story 02b / ALP-434).

The task is the run-forever consumer the supervisor registers. It:

1. Constructs a ``StockDataStream(feed=DataFeed.IEX)`` via an injectable
   factory (real factory wraps alpaca-py; tests inject a fake).
2. Subscribes to the initial target underlying set returned by
   ``compute_target_underlyings(reader)``.
3. Drains the alpaca-py handler queue, translating each ``Quote`` into an
   ``UnderlyingQuote`` (mid-price, tz-aware UTC timestamp) and writing it to
   the ``UnderlyingPriceCache``.
4. Re-evaluates the target set every ``subscription_refresh_seconds``;
   computes added / removed deltas; issues subscribe / unsubscribe calls.
5. On synthetic disconnect, retries with exponential backoff up to
   ``max_reconnect_attempts``; the cache survives.
6. When the reconnect budget is exhausted, raises so the supervisor's exit
   logging records the failure.
7. On ``asyncio.CancelledError``, calls ``stream.stop_ws()`` and exits.

Tests use a fake ``AlpacaStreamFactory`` + fake ``StockDataStream`` — no live
websocket calls. The fake stream exposes hooks so tests can inject quotes,
disconnects, and reconnect-budget exhaustion through the same surface that
alpaca-py uses at runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import (
    AlpacaStreamFactory,
    StockDataStreamProtocol,
    run_underlying_stream,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)


def _equity_position(*, position_id: str, ticker: str) -> PositionRecord:
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_now(),
        details=EquityPositionDetails(
            ticker=ticker, share_count=10.0, average_cost_basis_per_share=100.0
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_now(),
                fill_price=100.0,
                fill_quantity=10.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class _MutableReader:
    """Test reader whose open-positions set can be flipped mid-run."""

    def __init__(self, positions: tuple[PositionRecord, ...]) -> None:
        self._positions = positions

    def set_positions(self, positions: tuple[PositionRecord, ...]) -> None:
        self._positions = positions

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        return self._positions


# ---------------------------------------------------------------------------
# Fake stream + factory
# ---------------------------------------------------------------------------


class _QuotePayload:
    """Mirror the alpaca-py ``Quote`` surface ``run_underlying_stream`` reads."""

    def __init__(
        self,
        *,
        symbol: str,
        bid_price: float,
        ask_price: float,
        timestamp: datetime,
    ) -> None:
        self.symbol = symbol
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.timestamp = timestamp


class _FakeStream:
    """In-memory ``StockDataStream`` substitute.

    Records ``subscribe_quotes`` / ``unsubscribe_quotes`` calls and exposes
    ``feed_quote`` so tests can push synthetic ``Quote`` payloads through the
    registered handler.
    """

    def __init__(self) -> None:
        self.subscribed: set[str] = set()
        self.subscribe_calls: list[tuple[str, ...]] = []
        self.unsubscribe_calls: list[tuple[str, ...]] = []
        self.stop_ws_called = False
        self.run_should_raise: BaseException | None = None
        self._handler: Callable[[Any], Awaitable[None]] | None = None
        self._run_started = asyncio.Event()
        self._stop_run = asyncio.Event()

    def subscribe_quotes(self, handler: Callable[[Any], Awaitable[None]], *symbols: str) -> None:
        # alpaca-py uses the handler from the first ``subscribe_quotes`` call.
        if self._handler is None:
            self._handler = handler
        self.subscribed.update(symbols)
        self.subscribe_calls.append(tuple(symbols))

    def unsubscribe_quotes(self, *symbols: str) -> None:
        self.subscribed.difference_update(symbols)
        self.unsubscribe_calls.append(tuple(symbols))

    async def _run_forever(self) -> None:
        self._run_started.set()
        await self._stop_run.wait()
        if self.run_should_raise is not None:
            exc, self.run_should_raise = self.run_should_raise, None
            raise exc

    async def stop_ws(self) -> None:
        self.stop_ws_called = True
        self._stop_run.set()

    async def wait_until_running(self) -> None:
        await self._run_started.wait()

    async def feed_quote(self, payload: _QuotePayload) -> None:
        assert self._handler is not None, "subscribe_quotes was never called"
        await self._handler(payload)

    def trigger_disconnect(self, exc: BaseException) -> None:
        """Cause the current ``_run_forever`` to raise ``exc``."""
        self.run_should_raise = exc
        # Wake the running task so it exits with our injected exception.
        self._stop_run.set()


class _FakeFactory:
    """Yields a fresh ``_FakeStream`` on each ``build`` call.

    Reconnect cycles create a new ``_FakeStream``; tests can inspect the
    full sequence via ``streams``.
    """

    def __init__(self) -> None:
        self.streams: list[_FakeStream] = []
        self.build_calls: list[str] = []  # mode tags for assertions

    def build(self, *, mode: str) -> _FakeStream:
        self.build_calls.append(mode)
        stream = _FakeStream()
        self.streams.append(stream)
        return stream


def _config(
    *,
    subscription_refresh_seconds: int = 30,
    max_reconnect_attempts: int = 3,
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        subscription_refresh_seconds=subscription_refresh_seconds,
        max_reconnect_attempts=max_reconnect_attempts,
        supervisor_shutdown_timeout_seconds=5,
    )


def _session(mode: str = "paper") -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T143000Z-deadbeef",
        started_at=_now(),
        mode=mode,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFactoryProtocol:
    def test_fake_factory_satisfies_protocol(self) -> None:
        factory = _FakeFactory()
        assert isinstance(factory, AlpacaStreamFactory)

    def test_fake_stream_satisfies_protocol(self) -> None:
        stream = _FakeStream()
        assert isinstance(stream, StockDataStreamProtocol)


class TestStartupSubscribesInitialTargets:
    async def test_initial_subscribe_quotes_called_with_open_position_underlyings(
        self,
    ) -> None:
        reader = _MutableReader(
            (
                _equity_position(position_id="p1", ticker="SPY"),
                _equity_position(position_id="p2", ticker="AAPL"),
            )
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()

        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            # Wait for the first stream to be running + first subscribe call.
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream = factory.streams[0]
            await asyncio.wait_for(stream.wait_until_running(), timeout=1.0)
            await asyncio.wait_for(
                _eventually(lambda: stream.subscribed == {"SPY", "AAPL"}),
                timeout=1.0,
            )
            assert factory.build_calls == ["paper"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_live_mode_passed_to_factory(self) -> None:
        reader = _MutableReader(())
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(mode="live"),
                _config(),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.build_calls)), timeout=1.0)
            assert factory.build_calls[0] == "live"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestQuoteToCache:
    async def test_quote_writes_mid_price_with_tz_aware_timestamp(self) -> None:
        reader = _MutableReader((_equity_position(position_id="p1", ticker="SPY"),))
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream = factory.streams[0]
            await asyncio.wait_for(
                _eventually(lambda: "SPY" in stream.subscribed),
                timeout=1.0,
            )
            ts = datetime(2026, 5, 11, 14, 30, 5, tzinfo=UTC)
            await stream.feed_quote(
                _QuotePayload(symbol="SPY", bid_price=499.5, ask_price=500.5, timestamp=ts)
            )
            await asyncio.wait_for(
                _eventually(lambda: cache.get("SPY") is not None),
                timeout=1.0,
            )
            quote = cache.get("SPY")
            assert quote is not None
            assert quote.ticker == "SPY"
            assert quote.price == pytest.approx(500.0)
            assert quote.as_of == ts
            assert quote.as_of.tzinfo is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestSubscriptionRediffOnCadence:
    async def test_added_position_triggers_subscribe_quotes(self) -> None:
        reader = _MutableReader((_equity_position(position_id="p1", ticker="SPY"),))
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                # Make the cadence aggressive so the test runs fast.
                _config(subscription_refresh_seconds=1),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream = factory.streams[0]
            await asyncio.wait_for(
                _eventually(lambda: stream.subscribed == {"SPY"}),
                timeout=1.0,
            )
            # Simulate a position being opened on AAPL.
            reader.set_positions(
                (
                    _equity_position(position_id="p1", ticker="SPY"),
                    _equity_position(position_id="p2", ticker="AAPL"),
                )
            )
            await asyncio.wait_for(
                _eventually(lambda: "AAPL" in stream.subscribed),
                timeout=3.0,
            )
            # AAPL was added via a fresh subscribe call.
            assert any("AAPL" in call for call in stream.subscribe_calls)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_removed_position_triggers_unsubscribe_quotes(self) -> None:
        reader = _MutableReader(
            (
                _equity_position(position_id="p1", ticker="SPY"),
                _equity_position(position_id="p2", ticker="AAPL"),
            )
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=1),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream = factory.streams[0]
            await asyncio.wait_for(
                _eventually(lambda: stream.subscribed == {"SPY", "AAPL"}),
                timeout=1.0,
            )
            # Close the AAPL position.
            reader.set_positions((_equity_position(position_id="p1", ticker="SPY"),))
            await asyncio.wait_for(
                _eventually(lambda: "AAPL" not in stream.subscribed),
                timeout=3.0,
            )
            assert any("AAPL" in call for call in stream.unsubscribe_calls)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestReconnect:
    async def test_disconnect_triggers_reconnect_and_cache_survives(self) -> None:
        reader = _MutableReader((_equity_position(position_id="p1", ticker="SPY"),))
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60, max_reconnect_attempts=3),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)
            # Seed the cache before disconnect.
            await stream1.feed_quote(
                _QuotePayload(
                    symbol="SPY",
                    bid_price=499.5,
                    ask_price=500.5,
                    timestamp=datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC),
                )
            )
            await asyncio.wait_for(
                _eventually(lambda: cache.get("SPY") is not None),
                timeout=1.0,
            )

            # Force a disconnect.
            stream1.trigger_disconnect(ConnectionError("websocket closed"))

            # A second stream is built on reconnect.
            await asyncio.wait_for(_eventually(lambda: len(factory.streams) >= 2), timeout=3.0)
            stream2 = factory.streams[1]
            await asyncio.wait_for(stream2.wait_until_running(), timeout=2.0)
            await asyncio.wait_for(
                _eventually(lambda: "SPY" in stream2.subscribed),
                timeout=2.0,
            )
            # Cache value from before disconnect is still readable.
            assert cache.get("SPY") is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_reconnect_budget_exhausted_propagates(self) -> None:
        reader = _MutableReader((_equity_position(position_id="p1", ticker="SPY"),))
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        # Budget=1: the first connect counts as attempt 1, the first disconnect
        # exhausts the budget and the task raises.
        config = _config(subscription_refresh_seconds=60, max_reconnect_attempts=1)
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                config,
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
        stream = factory.streams[0]
        await asyncio.wait_for(stream.wait_until_running(), timeout=1.0)
        stream.trigger_disconnect(ConnectionError("websocket closed"))
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, timeout=3.0)


class TestCancellation:
    async def test_cancel_calls_stop_ws_and_exits_cleanly(self) -> None:
        reader = _MutableReader((_equity_position(position_id="p1", ticker="SPY"),))
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
        stream = factory.streams[0]
        await asyncio.wait_for(stream.wait_until_running(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.stop_ws_called


# ---------------------------------------------------------------------------
# Polling helper — drives event-loop tests without sleep-spinning a fixed time
# ---------------------------------------------------------------------------

# Each test wraps ``_eventually`` in ``asyncio.wait_for`` with a hard timeout
# so the bounded loop here just sets the polling granularity (10 ms) — the
# determinate exit makes ASYNC110 happy.
_POLL_BUDGET_ITERATIONS = 1000
_POLL_GRANULARITY_SECONDS = 0.01


async def _eventually(condition: Callable[[], bool]) -> None:
    """Yield to the loop until *condition()* returns true."""
    for _ in range(_POLL_BUDGET_ITERATIONS):
        if condition():
            return
        await asyncio.sleep(_POLL_GRANULARITY_SECONDS)
    msg = "polling budget exhausted; condition still false"
    raise AssertionError(msg)
