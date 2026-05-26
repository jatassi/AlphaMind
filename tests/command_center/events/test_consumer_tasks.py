"""Tests for the upstream consumer tasks (ALP-669, story 04b).

The consumer tasks live inside the supervisor's TaskGroup. They open
the upstream events client, parse incoming frames against the matching
StrEnum, construct the typed event dataclass, and publish to the
multiplexer. Connection drop / parse failure triggers a jittered
exponential backoff and a fresh client.stream() call — independent of
the sibling consumer.

The fake clients from ``clients.py`` provide canned-frame sequences;
tests inject a seeded ``random.Random`` to make the jitter
deterministic and a ``stop_event`` to terminate the loop without
relying on task cancellation.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.events.clients import (
    FakeMonitorEventsClient,
    FakePipelineEventsClient,
)
from alphamind.command_center.events.multiplexer import (
    BACKOFF_CAP_SECONDS,
    BACKOFF_INITIAL_SECONDS,
    EventMultiplexer,
    monitor_consumer_task,
    pipeline_consumer_task,
)


class TestPipelineConsumerParsesAndPublishes:
    async def test_publishes_each_pipeline_event_type(self) -> None:
        mux = EventMultiplexer()
        # One frame per pipeline event type (9 members).
        frames = [
            ("invocation_started", {"invocation_id": "inv-1", "run_type": "market_hours_rolling"}),
            ("phase_transition", {"invocation_id": "inv-1", "phase": "distill"}),
            ("agent_started", {"invocation_id": "inv-1", "agent_name": "analyst"}),
            ("agent_succeeded", {"invocation_id": "inv-1", "agent_name": "analyst"}),
            ("agent_retrying", {"invocation_id": "inv-1", "agent_name": "analyst", "attempt": 2}),
            ("agent_failed", {"invocation_id": "inv-1", "agent_name": "analyst", "failure_mode": "timeout"}),
            ("invocation_ended", {"invocation_id": "inv-1", "status": "completed", "commands_issued": 1}),
            ("next_trigger_changed", {"next_trigger_at": "2026-05-26T00:00:00+00:00", "next_trigger_type": "pre_close"}),
            ("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"}),
        ]
        client = FakePipelineEventsClient(frames=frames)
        stop = asyncio.Event()
        # Capture all 9 events as they're published.
        received: list[PipelineEvent | MonitorEvent] = []

        async def collect() -> None:
            async with mux.subscribe() as queue:
                for _ in range(9):
                    received.append(await asyncio.wait_for(queue.get(), timeout=2))
                stop.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(collect())
            # Let the subscribe land before publishing.
            await asyncio.sleep(0)
            tg.create_task(
                pipeline_consumer_task(
                    client=client,
                    multiplexer=mux,
                    rng=random.Random(0),
                    stop_event=stop,
                )
            )

        assert len(received) == 9
        # All wrapped as PipelineEvent with their distinct event_type.
        types = [r.event_type for r in received]
        assert types == [
            PipelineEventType.INVOCATION_STARTED,
            PipelineEventType.PHASE_TRANSITION,
            PipelineEventType.AGENT_STARTED,
            PipelineEventType.AGENT_SUCCEEDED,
            PipelineEventType.AGENT_RETRYING,
            PipelineEventType.AGENT_FAILED,
            PipelineEventType.INVOCATION_ENDED,
            PipelineEventType.NEXT_TRIGGER_CHANGED,
            PipelineEventType.HEARTBEAT,
        ]


class TestMonitorConsumerParsesAndPublishes:
    async def test_publishes_each_monitor_event_type(self) -> None:
        mux = EventMultiplexer()
        frames = [
            ("websocket_connected", {"timestamp": "2026-05-26T00:00:00+00:00"}),
            ("websocket_disconnected", {"timestamp": "2026-05-26T00:00:01+00:00", "reason": "idle"}),
            ("fill_received", {"order_id": "o-1", "position_id": "p-1", "fill_price": 1.0, "fill_qty": 1}),
            ("breach_detected", {"rule": "per_position_max_size", "current_value": 10.0, "limit": 5.0, "response_classification": "immediate"}),
            ("emergency_invocation_triggered", {"reason": "multi_rule_breach"}),
            ("greeks_refreshed", {"underlying": "AAPL", "refreshed_at": "2026-05-26T00:00:00+00:00"}),
            ("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"}),
        ]
        client = FakeMonitorEventsClient(frames=frames)
        stop = asyncio.Event()
        received: list[PipelineEvent | MonitorEvent] = []

        async def collect() -> None:
            async with mux.subscribe() as queue:
                for _ in range(7):
                    received.append(await asyncio.wait_for(queue.get(), timeout=2))
                stop.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(collect())
            await asyncio.sleep(0)
            tg.create_task(
                monitor_consumer_task(
                    client=client,
                    multiplexer=mux,
                    rng=random.Random(0),
                    stop_event=stop,
                )
            )

        assert len(received) == 7
        assert all(isinstance(r, MonitorEvent) for r in received)
        types = [r.event_type for r in received]
        assert types == [
            MonitorEventType.WEBSOCKET_CONNECTED,
            MonitorEventType.WEBSOCKET_DISCONNECTED,
            MonitorEventType.FILL_RECEIVED,
            MonitorEventType.BREACH_DETECTED,
            MonitorEventType.EMERGENCY_INVOCATION_TRIGGERED,
            MonitorEventType.GREEKS_REFRESHED,
            MonitorEventType.HEARTBEAT,
        ]


class TestConsumerSkipsUnknownEventTypes:
    async def test_unknown_event_type_is_logged_and_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        mux = EventMultiplexer()
        frames = [
            ("totally_unknown_event", {"some": "payload"}),
            ("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"}),
        ]
        client = FakePipelineEventsClient(frames=frames)
        stop = asyncio.Event()
        received: list[PipelineEvent | MonitorEvent] = []

        async def collect() -> None:
            async with mux.subscribe() as queue:
                received.append(await asyncio.wait_for(queue.get(), timeout=2))
                stop.set()

        with caplog.at_level(logging.WARNING, logger="alphamind.command_center.events"):
            async with asyncio.TaskGroup() as tg:
                tg.create_task(collect())
                await asyncio.sleep(0)
                tg.create_task(
                    pipeline_consumer_task(
                        client=client,
                        multiplexer=mux,
                        rng=random.Random(0),
                        stop_event=stop,
                    )
                )

        # The unknown event was dropped; heartbeat survived.
        assert len(received) == 1
        assert received[0].event_type == PipelineEventType.HEARTBEAT
        # WARNING log mentions the unknown event.
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("totally_unknown_event" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# Reconnect + independence tests
# ---------------------------------------------------------------------------


class _FlakyClient:
    """Test double that raises on the first stream() then yields canned frames.

    Used to exercise the consumer task's backoff + reconnect path.
    """

    def __init__(
        self,
        *,
        raise_first_n: int,
        frames_after_recovery: list[tuple[str, Mapping[str, Any]]],
    ) -> None:
        self._raise_first_n = raise_first_n
        self._frames = frames_after_recovery
        self.connect_count = 0

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[AsyncIterator[tuple[str, Mapping[str, Any]]]]:
        self.connect_count += 1
        if self.connect_count <= self._raise_first_n:

            async def fail_gen() -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
                raise ConnectionError(f"simulated drop #{self.connect_count}")
                yield  # unreachable; satisfies the generator protocol  # noqa

            yield fail_gen()
        else:

            async def good_gen() -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
                for name, data in self._frames:
                    yield name, dict(data)

            yield good_gen()


class TestConsumerReconnectsAfterDrop:
    async def test_drops_and_reconnects_with_short_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the backoff helper to return ~0 so the test runs in ms.
        import alphamind.command_center.events.multiplexer as mod

        monkeypatch.setattr(mod, "_backoff_delay", lambda attempt, rng: 0.001)

        mux = EventMultiplexer()
        client = _FlakyClient(
            raise_first_n=2,  # two drops then succeed
            frames_after_recovery=[("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"})],
        )
        stop = asyncio.Event()
        received: list[PipelineEvent | MonitorEvent] = []

        async def collect() -> None:
            async with mux.subscribe() as queue:
                received.append(await asyncio.wait_for(queue.get(), timeout=3))
                stop.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(collect())
            await asyncio.sleep(0)
            tg.create_task(
                pipeline_consumer_task(
                    client=client,  # type: ignore[arg-type]
                    multiplexer=mux,
                    rng=random.Random(0),
                    stop_event=stop,
                )
            )

        assert len(received) == 1
        assert client.connect_count >= 3, "expected 2 drops + 1 successful connect"


class TestSiblingIndependence:
    """A drop on the pipeline upstream does NOT kill the monitor
    consumer's connection, and vice versa. The two consumer tasks
    each run their own backoff loop.
    """

    async def test_pipeline_drop_leaves_monitor_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import alphamind.command_center.events.multiplexer as mod

        monkeypatch.setattr(mod, "_backoff_delay", lambda attempt, rng: 0.001)

        mux = EventMultiplexer()
        pipeline = _FlakyClient(
            raise_first_n=1,
            frames_after_recovery=[("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"})],
        )
        # The monitor never drops — its first stream() yields cleanly.
        monitor = FakeMonitorEventsClient(
            frames=[("heartbeat", {"timestamp": "2026-05-26T00:00:01+00:00"})]
        )

        stop = asyncio.Event()
        received: list[PipelineEvent | MonitorEvent] = []

        async def collect() -> None:
            async with mux.subscribe() as queue:
                # Wait for both event types — proves both consumers
                # delivered through their independent reconnect paths.
                while True:
                    received.append(await asyncio.wait_for(queue.get(), timeout=3))
                    has_pipeline = any(isinstance(r, PipelineEvent) for r in received)
                    has_monitor = any(isinstance(r, MonitorEvent) for r in received)
                    if has_pipeline and has_monitor:
                        stop.set()
                        return

        async with asyncio.TaskGroup() as tg:
            tg.create_task(collect())
            await asyncio.sleep(0)
            tg.create_task(
                pipeline_consumer_task(
                    client=pipeline,  # type: ignore[arg-type]
                    multiplexer=mux,
                    rng=random.Random(0),
                    stop_event=stop,
                )
            )
            tg.create_task(
                monitor_consumer_task(
                    client=monitor,
                    multiplexer=mux,
                    rng=random.Random(0),
                    stop_event=stop,
                )
            )

        # Both PipelineEvent + MonitorEvent observed in the subscriber's
        # queue — proves the monitor consumer was NOT impacted by the
        # pipeline's drop.
        assert any(isinstance(r, PipelineEvent) for r in received)
        assert any(isinstance(r, MonitorEvent) for r in received)


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


class TestBackoffDelaySchedule:
    """The backoff helper: 1, 2, 4, 8, 16, 30s cap with ±25% jitter.

    Tests pin the schedule with a deterministic Random — the
    multiplicative jitter factor is bounded by 0.75 .. 1.25 of the
    unjittered base.
    """

    def test_unjittered_base_doubles_then_caps(self) -> None:
        from alphamind.command_center.events.multiplexer import _backoff_delay

        # rng.random() == 0.5 => factor 1.0 => return base.
        class _MidRng:
            def random(self) -> float:
                return 0.5

        rng = _MidRng()
        assert _backoff_delay(0, rng=rng) == BACKOFF_INITIAL_SECONDS
        assert _backoff_delay(1, rng=rng) == 2 * BACKOFF_INITIAL_SECONDS
        assert _backoff_delay(2, rng=rng) == 4 * BACKOFF_INITIAL_SECONDS
        assert _backoff_delay(3, rng=rng) == 8 * BACKOFF_INITIAL_SECONDS
        assert _backoff_delay(4, rng=rng) == 16 * BACKOFF_INITIAL_SECONDS
        # Cap at 30s — attempt=5 would be 32s without cap.
        assert _backoff_delay(5, rng=rng) == BACKOFF_CAP_SECONDS
        assert _backoff_delay(10, rng=rng) == BACKOFF_CAP_SECONDS

    def test_jitter_stays_within_bounds(self) -> None:
        from alphamind.command_center.events.multiplexer import _backoff_delay

        rng = random.Random(0)
        for attempt in range(8):
            base = min(BACKOFF_INITIAL_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
            for _ in range(100):
                delay = _backoff_delay(attempt, rng=rng)
                assert 0.75 * base <= delay <= 1.25 * base
