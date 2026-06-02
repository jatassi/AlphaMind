"""Tests for ``run_underlying_stream`` (story 02b / ALP-434; ALP-832 staleness).

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
8. RTH silence beyond ``underlying_stream_stale_timeout_seconds`` forces a
   budget-neutral reconnect (ALP-832); off-hours the check is disengaged.
9. The task beats the supervisor watchdog on every poll slice and declares the
   poll cadence at registration so a blocked writer trips ``os._exit(1)``.

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
from unittest.mock import patch

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import (
    AlpacaStreamFactory,
    StockDataStreamProtocol,
    run_underlying_stream,
)
from alphamind.execution.continuous_monitor.underlying_stream.wiring import (
    register_underlying_stream_task,
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
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_now(),
        details=EquityPositionDetails(
            ticker=Symbol(ticker), share_count=10.0, average_cost_basis_per_share=100.0
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_now(),
                fill_price=price(100.0),
                fill_quantity=10.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
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
    underlying_stream_stale_timeout_seconds: int = 60,
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        subscription_refresh_seconds=subscription_refresh_seconds,
        max_reconnect_attempts=max_reconnect_attempts,
        supervisor_shutdown_timeout_seconds=5,
        underlying_stream_stale_timeout_seconds=underlying_stream_stale_timeout_seconds,
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
                _equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),
                _equity_position(position_id=PositionId("p2"), ticker=Symbol("AAPL")),
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
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
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
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
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
                    _equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),
                    _equity_position(position_id=PositionId("p2"), ticker=Symbol("AAPL")),
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
                _equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),
                _equity_position(position_id=PositionId("p2"), ticker=Symbol("AAPL")),
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
            reader.set_positions(
                (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
            )
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
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
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
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
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


class TestCleanReturnReconnect:
    """ALP-770 — a clean ``_run_one_connection`` return must not exit the writer loop."""

    async def test_clean_return_triggers_reconnect(self) -> None:
        """When the stream exits cleanly (no exception), the writer reconnects.

        Before ALP-770 the ``else`` branch executed ``return``, which exited
        ``run_underlying_stream`` entirely — the writer appeared alive (the
        process didn't crash) but the cache was frozen. Now the ``else`` branch
        executes ``continue``, causing the outer while-loop to build a second
        connection.
        """
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60, max_reconnect_attempts=5),
                repository=reader,
                cache=cache,
                factory=factory,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)

            # Trigger a clean exit (no exception): call stop_ws so _run_forever
            # returns normally without raising.  The outer loop must reconnect.
            await stream1.stop_ws()

            # A second stream is built — proving the outer loop did not ``return``.
            await asyncio.wait_for(_eventually(lambda: len(factory.streams) >= 2), timeout=3.0)
            assert len(factory.streams) >= 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestCancellation:
    async def test_cancel_calls_stop_ws_and_exits_cleanly(self) -> None:
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
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
        await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
        stream = factory.streams[0]
        await asyncio.wait_for(stream.wait_until_running(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.stop_ws_called


# ---------------------------------------------------------------------------
# Fake monotonic clock for staleness-boundary tests
# ---------------------------------------------------------------------------


class _FakeClock:
    """Controllable monotonic clock for staleness tests.

    Starts at ``t=0.0`` and advances only when the test calls ``advance()``.
    Tests pair this with a ``is_rth`` callable to precisely control when
    the staleness threshold is crossed.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ---------------------------------------------------------------------------
# Staleness / RTH reconnect tests (ALP-832)
# ---------------------------------------------------------------------------


class TestRTHSilenceReconnect:
    """RTH silence beyond the threshold forces a budget-neutral reconnect."""

    async def test_rth_silence_beyond_threshold_forces_reconnect(self) -> None:
        """Connected-but-silent underlying stream during RTH forces a reconnect."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock(start=0.0)

        # is_rth=True always (market open); stale_timeout=10s; poll_interval=0.01s
        # so the test doesn't wait real seconds.
        beat_count: list[int] = [0]
        registered_cadences: list[float] = []

        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(
                    subscription_refresh_seconds=60,
                    max_reconnect_attempts=5,
                    underlying_stream_stale_timeout_seconds=10,
                ),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: True,
                beat=lambda: beat_count.__setitem__(0, beat_count[0] + 1),
                register_watch=lambda cadence: registered_cadences.append(cadence),
                monotonic=clock,
                stream_poll_interval=0.01,
            )
        )
        try:
            # Wait for first stream to start running.
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)

            # Advance clock past the stale threshold (no quotes delivered).
            clock.advance(11.0)

            # A second stream should be built (budget-neutral reconnect).
            await asyncio.wait_for(_eventually(lambda: len(factory.streams) >= 2), timeout=3.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_rth_silence_reconnect_is_budget_neutral(self) -> None:
        """Staleness reconnect does not consume max_reconnect_attempts."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock(start=0.0)

        # Budget=1: normal disconnects exhaust it, but staleness reconnects must not.
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(
                    subscription_refresh_seconds=60,
                    max_reconnect_attempts=1,
                    underlying_stream_stale_timeout_seconds=10,
                ),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: True,
                beat=lambda: None,
                register_watch=lambda _c: None,
                monotonic=clock,
                stream_poll_interval=0.01,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)

            # Trip staleness once — should reconnect without consuming budget.
            clock.advance(11.0)
            await asyncio.wait_for(_eventually(lambda: len(factory.streams) >= 2), timeout=3.0)

            stream2 = factory.streams[1]
            await asyncio.wait_for(stream2.wait_until_running(), timeout=1.0)

            # Trip staleness again — still budget-neutral; task still alive.
            clock.advance(11.0)
            await asyncio.wait_for(_eventually(lambda: len(factory.streams) >= 3), timeout=3.0)

            # Task is still running (budget not consumed).
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestOffHoursNoReconnect:
    """Off-hours: no reconnect and clock resets so opening gap is not charged."""

    async def test_off_hours_silence_does_not_force_reconnect(self) -> None:
        """When market is closed, extended silence does not trigger reconnect."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock(start=0.0)

        # Market always closed.
        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(
                    subscription_refresh_seconds=60,
                    max_reconnect_attempts=5,
                    underlying_stream_stale_timeout_seconds=10,
                ),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: False,
                beat=lambda: None,
                register_watch=lambda _c: None,
                monotonic=clock,
                stream_poll_interval=0.01,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)

            # Advance well past threshold — no reconnect expected.
            clock.advance(1000.0)

            # Give the poll loop a few real ticks to execute.
            await asyncio.sleep(0.1)

            # Only one stream — no budget-neutral reconnect triggered.
            assert len(factory.streams) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_clock_resets_off_hours_so_open_gap_not_charged(self) -> None:
        """Clock resets off-hours so the closed-market gap is not charged at RTH open."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock(start=0.0)

        # Market starts closed, then opens.
        market_open = [False]

        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(
                    subscription_refresh_seconds=60,
                    max_reconnect_attempts=5,
                    underlying_stream_stale_timeout_seconds=10,
                ),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: market_open[0],
                beat=lambda: None,
                register_watch=lambda _c: None,
                monotonic=clock,
                stream_poll_interval=0.01,
            )
        )
        try:
            await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=1.0)
            stream1 = factory.streams[0]
            await asyncio.wait_for(stream1.wait_until_running(), timeout=1.0)

            # Advance a large amount while market is closed — clock should reset each slice.
            clock.advance(10000.0)
            await asyncio.sleep(0.05)

            # Now open the market — should NOT immediately trigger a staleness reconnect
            # because the clock was reset during off-hours.
            market_open[0] = True

            # A brief RTH period (below threshold) — still no reconnect.
            clock.advance(5.0)
            await asyncio.sleep(0.05)

            assert len(factory.streams) == 1, (
                "Expected no staleness reconnect shortly after market open"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestStalenessBeatsWatchdog:
    """The underlying stream beats the watchdog and is bounded by poll_interval cadence."""

    async def test_register_watch_called_with_poll_interval_at_startup(self) -> None:
        """register_watch is called with stream_poll_interval at startup."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock()

        registered: list[float] = []

        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: True,
                beat=lambda: None,
                register_watch=lambda cadence: registered.append(cadence),
                monotonic=clock,
                stream_poll_interval=5.0,
            )
        )
        try:
            await asyncio.wait_for(
                _eventually(lambda: len(registered) >= 1),
                timeout=1.0,
            )
            assert registered[0] == pytest.approx(5.0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_beat_called_on_each_poll_slice(self) -> None:
        """beat() is called at least once per poll slice."""
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        clock = _FakeClock()

        beat_count: list[int] = [0]

        task = asyncio.create_task(
            run_underlying_stream(
                _session(),
                _config(subscription_refresh_seconds=60),
                repository=reader,
                cache=cache,
                factory=factory,
                is_market_open=lambda _dt: True,
                beat=lambda: beat_count.__setitem__(0, beat_count[0] + 1),
                register_watch=lambda _c: None,
                monotonic=clock,
                stream_poll_interval=0.01,
            )
        )
        try:
            await asyncio.wait_for(
                _eventually(lambda: beat_count[0] >= 3),
                timeout=2.0,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_blocked_writer_trips_watchdog_via_production_registration_path(
        self,
    ) -> None:
        """A genuinely-blocked writer that stops beating trips os._exit(1) via watchdog.

        This test exercises the PRODUCTION wiring path: ``register_underlying_stream_task``
        wires ``supervisor.beat`` and ``supervisor.register_watch`` so that the watchdog
        is properly BOUND at ``poll_interval`` cadence. We verify:

        1. The task registers a watch (positive bound) so the watchdog CAN trip it.
        2. Once a beat has been recorded, stopping the beat (by freezing the clock)
           and advancing past the stall bound causes os._exit(1).
        """
        reader = _MutableReader(
            (_equity_position(position_id=PositionId("p1"), ticker=Symbol("SPY")),)
        )
        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        # Shared fake clock: both the supervisor (monotonic) and the stream
        # (monotonic) use the same advancing clock so beats update last_beat
        # and then we can freeze it to simulate a stall.
        fake_clock = _FakeClock(start=1000.0)

        def _recording_mono() -> float:
            return fake_clock()

        config = _config(
            subscription_refresh_seconds=60,
            max_reconnect_attempts=5,
            underlying_stream_stale_timeout_seconds=60,
        )
        # Use a tight multiplier: poll_interval=0.01s → bound=0.02s.
        # The watchdog check_interval = max(1.0, 0.02/4) = 1.0s (floor).
        # Use a zero-delay fake sleep in the supervisor so the watchdog
        # loop ticks as fast as the event loop runs.
        config = config.model_copy(update={"watchdog_cadence_multiplier": 2.0})

        async def _fast_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        supervisor = MonitorSupervisor(
            session=_session(),
            config=config,
            monotonic=_recording_mono,
            sleep=_fast_sleep,
        )

        # Register via the PRODUCTION wiring path — this is the AC requirement.
        register_underlying_stream_task(
            supervisor,
            repository=reader,
            cache=cache,
            factory=factory,
            is_market_open=lambda _dt: True,
            stream_poll_interval=0.01,
        )

        # Patch os._exit so the test process doesn't actually die.
        with patch("os._exit") as mock_exit:
            supervisor_task = asyncio.create_task(supervisor.run())
            try:
                # Wait until the stream is up.
                await asyncio.wait_for(_eventually(lambda: bool(factory.streams)), timeout=2.0)
                stream = factory.streams[0]
                await asyncio.wait_for(stream.wait_until_running(), timeout=1.0)

                # Wait until the watchdog knows about this task's bound (register_watch called).
                # The task registers on startup before the first await.
                await asyncio.sleep(0.05)

                # Advance clock to give a beat a chance to land: the stream poll loop
                # will call on_slice() → beat() after poll_interval (0.01s real).
                await asyncio.wait_for(
                    _eventually(
                        lambda: (
                            supervisor._watch.get("underlying_stream") is not None
                            and supervisor._watch["underlying_stream"].last_beat is not None
                        )
                    ),
                    timeout=2.0,
                )

                # Now leap the fake clock past the stall bound.
                # bound = poll_interval * multiplier = 0.01 * 2.0 = 0.02s
                # Add a generous margin so the watchdog trips cleanly.
                fake_clock.advance(10.0)

                # Watchdog should now call os._exit(1).
                await asyncio.wait_for(
                    _eventually(lambda: mock_exit.called),
                    timeout=3.0,
                )
                assert mock_exit.call_args[0][0] == 1
            finally:
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)

    async def test_initial_connect_hang_trips_watchdog_via_entry_beat(self) -> None:
        """A hang during the FIRST connect/subscribe (before any quote) trips the watchdog.

        Regression (ALP-825 review). ``run_underlying_stream`` previously deferred
        its first ``beat`` to the first poll slice — which only runs after
        ``factory.build`` + ``compute_target_underlyings`` + ``subscribe_quotes``
        succeed and the staleness-watch sibling spins up. A hang in that initial
        connect/subscribe therefore left ``last_beat=None`` so the watchdog could
        only WARN (startup grace), never ``os._exit``-trip — unlike the fill
        consumer, which beats as its first statement.

        Here the open-positions read hangs forever (modelling a wedged
        connect/subscribe before the TaskGroup's beat loop starts). With the
        entry beat in place, ``last_beat`` is set, and once the clock leaps past
        the stall bound the watchdog trips. Drives the PRODUCTION wiring path.
        """

        class _HangingReader:
            """Reader whose open-positions read never returns (wedged connect)."""

            async def get_open_positions(self) -> tuple[PositionRecord, ...]:
                await asyncio.Event().wait()  # blocks forever; never resolves
                raise AssertionError("unreachable")  # pragma: no cover

        factory = _FakeFactory()
        cache = UnderlyingPriceCache()
        fake_clock = _FakeClock(start=1000.0)

        config = _config(
            subscription_refresh_seconds=60,
            max_reconnect_attempts=5,
            underlying_stream_stale_timeout_seconds=60,
        )
        # bound = poll_interval(0.01) * multiplier(2.0) = 0.02s.
        config = config.model_copy(update={"watchdog_cadence_multiplier": 2.0})

        async def _fast_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        supervisor = MonitorSupervisor(
            session=_session(),
            config=config,
            monotonic=lambda: fake_clock(),
            sleep=_fast_sleep,
        )
        register_underlying_stream_task(
            supervisor,
            repository=_HangingReader(),
            cache=cache,
            factory=factory,
            is_market_open=lambda _dt: True,
            stream_poll_interval=0.01,
        )

        with patch("os._exit") as mock_exit:
            supervisor_task = asyncio.create_task(supervisor.run())
            try:
                # The entry beat lands before the hanging connect, so last_beat
                # is set even though no stream ever finished building.
                await asyncio.wait_for(
                    _eventually(
                        lambda: (
                            supervisor._watch.get("underlying_stream") is not None
                            and supervisor._watch["underlying_stream"].last_beat is not None
                        )
                    ),
                    timeout=2.0,
                )
                # The connect is wedged — no stream ever ran (no poll-slice beat).
                assert not factory.streams or not factory.streams[0]._handler

                # Leap past the stall bound — no further beats arrive, so it trips.
                fake_clock.advance(10.0)
                await asyncio.wait_for(_eventually(lambda: mock_exit.called), timeout=3.0)
                assert mock_exit.call_args[0][0] == 1
            finally:
                supervisor_task.cancel()
                await asyncio.gather(supervisor_task, return_exceptions=True)


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
