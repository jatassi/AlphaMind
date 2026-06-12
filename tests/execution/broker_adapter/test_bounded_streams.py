"""Tests for ``broker_adapter.bounded_streams`` (ALP-946).

alpaca-py's stream-mutator methods (``subscribe_quotes`` /
``unsubscribe_quotes`` / ``subscribe_trade_updates``) are loop-affined
synchronous APIs: on a running stream they marshal the send onto the stream's
own event loop via ``run_coroutine_threadsafe`` and block the calling thread
on an untimed ``Future.result()``. Called from the loop's own thread that is
a guaranteed self-deadlock — the production wedge behind ALP-946.

``bounded_call`` and the facade classes make the safe calling convention the
only one expressible: every vendor sync call runs on a worker thread (so the
scheduled coroutine can drain on the free loop) under an ``asyncio.wait_for``
bound (so the await always resolves). These tests pin the loop-affinity
contract — off-loop-thread execution with identical arguments — so no
per-call-site thread assertion is needed elsewhere.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from alphamind.execution.broker_adapter import bounded_call
from alphamind.execution.broker_adapter.bounded_streams import (
    BoundedStockDataStream,
    BoundedTradingStream,
)


class _RecordingVendorStockStream:
    """Fake vendor ``StockDataStream`` recording call thread ids + arguments."""

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[int, tuple[object, ...]]] = []
        self.unsubscribe_calls: list[tuple[int, tuple[str, ...]]] = []
        self.run_forever_called = False
        self.stop_ws_called = False

    def subscribe_quotes(self, handler: Callable[[Any], Awaitable[None]], *symbols: str) -> None:
        self.subscribe_calls.append((threading.get_ident(), (handler, *symbols)))

    def unsubscribe_quotes(self, *symbols: str) -> None:
        self.unsubscribe_calls.append((threading.get_ident(), symbols))

    async def _run_forever(self) -> None:
        self.run_forever_called = True

    async def stop_ws(self) -> None:
        self.stop_ws_called = True


class TestBoundedCall:
    async def test_runs_callable_off_loop_thread_passing_args_and_return(self) -> None:
        loop_thread_id = threading.get_ident()
        seen: dict[str, object] = {}

        def record(*args: object) -> str:
            seen["thread_id"] = threading.get_ident()
            seen["args"] = args
            return "payload"

        result = await bounded_call(record, "AAPL", 7, timeout_seconds=5.0)

        assert result == "payload"
        assert seen["args"] == ("AAPL", 7)
        assert seen["thread_id"] != loop_thread_id

    async def test_raises_timeout_error_when_callable_blocks_past_bound(self) -> None:
        release = threading.Event()

        def blocks_until_released() -> None:
            release.wait()

        try:
            with pytest.raises(TimeoutError):
                await bounded_call(blocks_until_released, timeout_seconds=0.05)
        finally:
            # Unwind the orphaned worker thread so it doesn't outlive the test.
            release.set()


class TestBoundedStockDataStream:
    async def test_subscribe_quotes_invokes_vendor_off_loop_thread_with_args(self) -> None:
        vendor = _RecordingVendorStockStream()
        facade = BoundedStockDataStream(vendor)

        async def handler(_payload: Any) -> None: ...

        await facade.subscribe_quotes(handler, "SPY", "AAPL")

        ((thread_id, args),) = vendor.subscribe_calls
        assert thread_id != threading.get_ident()
        assert args == (handler, "SPY", "AAPL")

    async def test_unsubscribe_quotes_invokes_vendor_off_loop_thread_with_args(self) -> None:
        vendor = _RecordingVendorStockStream()
        facade = BoundedStockDataStream(vendor)

        await facade.unsubscribe_quotes("SPY", "AAPL")

        ((thread_id, symbols),) = vendor.unsubscribe_calls
        assert thread_id != threading.get_ident()
        assert symbols == ("SPY", "AAPL")

    async def test_run_forever_and_stop_ws_delegate(self) -> None:
        vendor = _RecordingVendorStockStream()
        facade = BoundedStockDataStream(vendor)

        await facade._run_forever()
        await facade.stop_ws()

        assert vendor.run_forever_called
        assert vendor.stop_ws_called


class _RecordingVendorTradingStream:
    """Fake vendor ``TradingStream`` recording call thread ids + arguments."""

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[int, object]] = []
        self.run_forever_called = False

    def subscribe_trade_updates(self, handler: Any) -> None:
        self.subscribe_calls.append((threading.get_ident(), handler))

    async def _run_forever(self) -> None:
        self.run_forever_called = True


class TestBoundedTradingStream:
    async def test_subscribe_trade_updates_invokes_vendor_off_loop_thread(self) -> None:
        vendor = _RecordingVendorTradingStream()
        facade = BoundedTradingStream(vendor)

        async def handler(_update: Any) -> None: ...

        await facade.subscribe_trade_updates(handler)

        ((thread_id, seen_handler),) = vendor.subscribe_calls
        assert thread_id != threading.get_ident()
        assert seen_handler is handler

    async def test_run_forever_delegates(self) -> None:
        vendor = _RecordingVendorTradingStream()
        facade = BoundedTradingStream(vendor)

        await facade._run_forever()

        assert vendor.run_forever_called
