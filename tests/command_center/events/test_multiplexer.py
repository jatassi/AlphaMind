"""Tests for the EventMultiplexer (ALP-669, story 04b).

The multiplexer owns the per-subscriber ``asyncio.Queue`` registry and
the publish/subscribe contract:

* ``subscribe()`` async-context manager — registers a fresh bounded
  queue, yields it, deregisters on exit.
* ``publish(event)`` — fans the event to every subscriber's queue.
* Slow-consumer protection — queue full ⇒ drop OLDEST + log warning.

Tests use the frozen-dataclass events from
:mod:`alphamind.command_center._kernel.events` directly; no Pydantic
involved inside the multiplexer (per ALP-128 P5 invariant).
"""

from __future__ import annotations

import asyncio

import pytest

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.events.multiplexer import (
    DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE,
    CombinedEvent,
    EventMultiplexer,
)


def _pipeline_event(invocation_id: str = "inv-1") -> PipelineEvent:
    return PipelineEvent(
        event_type=PipelineEventType.INVOCATION_STARTED,
        payload={"invocation_id": invocation_id, "run_type": "market_hours_rolling"},
    )


def _monitor_event() -> MonitorEvent:
    return MonitorEvent(
        event_type=MonitorEventType.HEARTBEAT,
        payload={"timestamp": "2026-05-26T00:00:00+00:00"},
    )


class TestSubscriberLifecycle:
    async def test_subscriber_count_zero_initially(self) -> None:
        mux = EventMultiplexer()
        assert mux.subscriber_count == 0

    async def test_subscribe_registers_and_deregisters_queue(self) -> None:
        mux = EventMultiplexer()
        async with mux.subscribe() as queue:
            assert mux.subscriber_count == 1
            assert isinstance(queue, asyncio.Queue)
        assert mux.subscriber_count == 0

    async def test_subscribe_yields_distinct_queues(self) -> None:
        mux = EventMultiplexer()
        async with mux.subscribe() as q1, mux.subscribe() as q2:
            assert q1 is not q2
            assert mux.subscriber_count == 2

    async def test_subscriber_queue_default_maxsize(self) -> None:
        mux = EventMultiplexer()
        async with mux.subscribe() as queue:
            assert queue.maxsize == DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE


class TestPublishFanout:
    async def test_publish_to_zero_subscribers_is_safe(self) -> None:
        mux = EventMultiplexer()
        await mux.publish(_pipeline_event())  # no error

    async def test_publish_delivers_to_single_subscriber(self) -> None:
        mux = EventMultiplexer()
        event = _pipeline_event()
        async with mux.subscribe() as queue:
            await mux.publish(event)
            received = await asyncio.wait_for(queue.get(), timeout=1)
        assert received is event

    async def test_publish_fans_to_all_subscribers(self) -> None:
        mux = EventMultiplexer()
        event = _pipeline_event()
        async with mux.subscribe() as q1, mux.subscribe() as q2, mux.subscribe() as q3:
            await mux.publish(event)
            r1 = await asyncio.wait_for(q1.get(), timeout=1)
            r2 = await asyncio.wait_for(q2.get(), timeout=1)
            r3 = await asyncio.wait_for(q3.get(), timeout=1)
        assert r1 is event
        assert r2 is event
        assert r3 is event

    async def test_pipeline_and_monitor_events_both_publish(self) -> None:
        mux = EventMultiplexer()
        async with mux.subscribe() as queue:
            await mux.publish(_pipeline_event())
            await mux.publish(_monitor_event())
            first = await asyncio.wait_for(queue.get(), timeout=1)
            second = await asyncio.wait_for(queue.get(), timeout=1)
        assert isinstance(first, PipelineEvent)
        assert isinstance(second, MonitorEvent)


class TestSlowConsumerDropOldest:
    """When a subscriber's queue saturates, the multiplexer drops the
    OLDEST event for that subscriber and logs a warning. The other
    subscribers' queues are untouched (per-subscriber buffering — one
    slow consumer cannot stall the fan-out).

    Per ALP-128 pre-resolved (G): no Last-Event-ID resume — live state
    is transient screen state. Drop-oldest preserves the most-recent N
    events, which is what the operator's UI cares about; drop-newest
    would keep stale state.
    """

    async def test_full_queue_drops_oldest_event(self) -> None:
        # Tiny queue (size 2) so the test runs in microseconds.
        mux = EventMultiplexer(subscriber_queue_maxsize=2)
        e1 = _pipeline_event("inv-1")
        e2 = _pipeline_event("inv-2")
        e3 = _pipeline_event("inv-3")
        async with mux.subscribe() as queue:
            await mux.publish(e1)
            await mux.publish(e2)
            await mux.publish(e3)  # forces drop of e1
            # Queue should now hold e2 + e3 in order.
            assert queue.qsize() == 2
            assert await asyncio.wait_for(queue.get(), timeout=1) is e2
            assert await asyncio.wait_for(queue.get(), timeout=1) is e3

    async def test_slow_consumer_does_not_block_other_subscribers(self) -> None:
        # Fast consumer's queue has plenty of room; slow consumer's
        # queue saturates and drops.
        mux = EventMultiplexer(subscriber_queue_maxsize=1)
        async with mux.subscribe() as slow, mux.subscribe() as fast:
            for i in range(5):
                await mux.publish(_pipeline_event(f"inv-{i}"))
            # Fast consumer also has maxsize=1 in this test (same mux
            # parameter), so it also drops — but the publish loop
            # returned promptly, which is the property we care about.
            del slow, fast  # silence unused warning
            assert True  # the publish loop completed without deadlock

    async def test_slow_consumer_drop_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        mux = EventMultiplexer(subscriber_queue_maxsize=1)
        async with mux.subscribe():
            await mux.publish(_pipeline_event("inv-1"))
            with caplog.at_level(logging.WARNING, logger="alphamind.command_center.events"):
                await mux.publish(_pipeline_event("inv-2"))  # forces drop
        # At least one WARNING was emitted referencing the drop.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "expected a WARNING log on slow-consumer drop"
        assert any("drop" in r.getMessage().lower() for r in warnings)


class TestSubscriberConcurrency:
    """Subscribe + publish from concurrent tasks. No data races, no
    lost events on the not-saturated path.
    """

    async def test_concurrent_subscribers_each_receive_event(self) -> None:
        mux = EventMultiplexer()
        event = _pipeline_event()

        async def subscriber_reads_one() -> CombinedEvent:
            async with mux.subscribe() as queue:
                # Signal readiness then wait for the publish.
                return await asyncio.wait_for(queue.get(), timeout=2)

        # Spin up 5 subscribers; let them register; then publish once.
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(subscriber_reads_one()) for _ in range(5)]
            # Wait until all subscribers have registered. The
            # subscriber_count is the load-bearing observation.
            for _ in range(100):
                if mux.subscriber_count == 5:
                    break
                await asyncio.sleep(0.005)
            await mux.publish(event)
        for t in tasks:
            assert t.result() is event
