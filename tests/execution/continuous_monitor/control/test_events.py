"""SSE event emitter tests.

The emitter is the structural producer the breach loop, fill consumer, greeks
refresher, and other monitor surfaces call to surface events on the
``GET /events`` SSE stream. The class fans out events onto per-subscriber
``asyncio.Queue`` instances; tests subscribe, fire structural callbacks, and
assert that subscribers observe the documented event payloads in order.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from alphamind.execution.continuous_monitor.control.events import (
    EmittedEvent,
    SSEEventEmitter,
)


@pytest.mark.asyncio
class TestEmitterFanout:
    async def test_emit_websocket_connected_reaches_subscriber(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_websocket_connected()
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "websocket_connected"
        assert "timestamp" in event.payload

    async def test_no_subscribers_means_no_buffer_growth(self) -> None:
        emitter = SSEEventEmitter()
        emitter.emit_websocket_connected()
        # Nothing to assert other than that the call doesn't raise; the
        # emitter must be no-op-safe when run without a subscriber.
        assert emitter.subscriber_count() == 0

    async def test_two_subscribers_each_observe_events(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as q1, emitter.subscribe() as q2:
            emitter.emit_websocket_connected()
            e1 = await asyncio.wait_for(q1.get(), timeout=0.5)
            e2 = await asyncio.wait_for(q2.get(), timeout=0.5)
        assert e1.name == "websocket_connected"
        assert e2.name == "websocket_connected"

    async def test_subscribe_unsubscribes_on_context_exit(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe():
            assert emitter.subscriber_count() == 1
        assert emitter.subscriber_count() == 0


@pytest.mark.asyncio
class TestPerEventCallbacks:
    async def test_websocket_disconnected_carries_reason(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_websocket_connected()
            await asyncio.wait_for(queue.get(), timeout=0.5)
            emitter.emit_websocket_disconnected(reason="network_error")
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "websocket_disconnected"
        assert event.payload["reason"] == "network_error"

    async def test_fill_received_carries_all_fields(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_fill_received(
                order_id="ord-1",
                position_id="pos-1",
                fill_price=100.5,
                fill_qty=10.0,
            )
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "fill_received"
        assert event.payload["order_id"] == "ord-1"
        assert event.payload["position_id"] == "pos-1"
        assert event.payload["fill_price"] == 100.5
        assert event.payload["fill_qty"] == 10.0

    async def test_breach_detected_carries_classification(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_breach_detected(
                rule="per_position_max_size",
                current_value=0.07,
                limit=0.05,
                response_classification="immediate",
            )
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "breach_detected"
        assert event.payload["response_classification"] == "immediate"

    async def test_emergency_invocation_triggered_carries_reason(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_emergency_invocation_triggered(reason="regime_jump")
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "emergency_invocation_triggered"
        assert event.payload["reason"] == "regime_jump"

    async def test_greeks_refreshed_carries_underlying(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            refreshed_at = datetime.now(UTC)
            emitter.emit_greeks_refreshed(underlying="AAPL", refreshed_at=refreshed_at)
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert event.name == "greeks_refreshed"
        assert event.payload["underlying"] == "AAPL"


@pytest.mark.asyncio
class TestCrossFieldInvariants:
    async def test_websocket_disconnected_required_after_connected(self) -> None:
        """Schema § Notes on cross-field invariants — connected and disconnected alternate."""
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_websocket_connected()
            # A second consecutive connected without an intervening
            # disconnect is a structural error per the schema.
            with pytest.raises(RuntimeError, match="alternate"):
                emitter.emit_websocket_connected()
            # Drain the first event so the test cleanup doesn't block.
            await asyncio.wait_for(queue.get(), timeout=0.5)

    async def test_websocket_disconnected_without_connected_is_structural_error(self) -> None:
        emitter = SSEEventEmitter()
        # A disconnect before any connect is a structural error.
        with pytest.raises(RuntimeError, match="alternate"):
            emitter.emit_websocket_disconnected(reason="network_error")

    async def test_websocket_connected_disconnected_connected_is_allowed(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as queue:
            emitter.emit_websocket_connected()
            emitter.emit_websocket_disconnected(reason="network_error")
            emitter.emit_websocket_connected()
            names = [
                (await asyncio.wait_for(queue.get(), timeout=0.5)).name for _ in range(3)
            ]
        assert names == [
            "websocket_connected",
            "websocket_disconnected",
            "websocket_connected",
        ]


@pytest.mark.asyncio
class TestQueueIsolation:
    async def test_subscriber_a_drain_does_not_affect_b(self) -> None:
        emitter = SSEEventEmitter()
        async with emitter.subscribe() as q1, emitter.subscribe() as q2:
            emitter.emit_websocket_connected()
            await asyncio.wait_for(q1.get(), timeout=0.5)
            # q2 still has the event pending.
            event = await asyncio.wait_for(q2.get(), timeout=0.5)
        assert event.name == "websocket_connected"


class TestEmittedEvent:
    def test_carries_name_and_payload(self) -> None:
        evt = EmittedEvent(name="heartbeat", payload={"timestamp": "2026-05-26T00:00:00Z"})
        assert evt.name == "heartbeat"
        assert evt.payload["timestamp"] == "2026-05-26T00:00:00Z"
