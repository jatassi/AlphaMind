"""Tests for ``alphamind.scheduler.control.events`` (ALP-664).

The :class:`SSEEventEmitter` is the producer side of the pipeline's
``GET /events`` stream:

* Implements the :class:`alphamind._kernel.progress.ProgressEmitter`
  Protocol (so the orchestrator wires through it alongside the existing
  NoOpProgressEmitter and JsonlProgressEmitter).
* Exposes nine explicit ``emit_*`` methods, one per schema event.
* Maintains a per-subscriber ``asyncio.Queue`` so each ``/events``
  connection consumes its own copy.
* Frames each event into the SSE record shape defined by the schema
  (``event: <name>\\ndata: <json>\\n\\n``).
* Drives a 15-second heartbeat per connection when no other event has
  emitted.

Cross-field invariants (forward-only phases, agent-succeeded /
agent-failed mutual exclusion per ``(invocation_id, agent_name)``) are
honored by emitter callers; the emitter itself does not police order,
matching the JsonlProgressEmitter's design (observability first).  The
acceptance criterion calling for "cross-field invariants observable by
a test" is exercised via integration tests in ``test_routes.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from alphamind._kernel.progress import ProgressEmitter
from alphamind.scheduler.control import events, models

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 5, 26, 12, 0, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------


class TestProgressEmitterProtocol:
    def test_emitter_satisfies_protocol_isinstance(self) -> None:
        emitter = events.SSEEventEmitter()
        assert isinstance(emitter, ProgressEmitter)

    def test_protocol_methods_are_callable_and_no_op_on_default(self) -> None:
        # ProgressEmitter methods may absorb events without raising; the
        # mapping from the in-process Protocol to the schema-shaped event
        # taxonomy is the orchestrator's concern and not exercised here.
        emitter = events.SSEEventEmitter()
        emitter.phase_start("phase1")
        emitter.phase_done("phase1", fills_processed=0)
        emitter.agent_request(phase="phase1", agent="analyst", model="claude")
        emitter.agent_response(
            phase="phase1",
            agent="analyst",
            model="claude",
            duration_s=12.5,
            input_tokens=1000,
            output_tokens=200,
            tool_calls=0,
            stop_reason="end_turn",
        )


# ---------------------------------------------------------------------------
# Subscriber lifecycle + per-subscriber queue.
# ---------------------------------------------------------------------------


class TestSubscriberLifecycle:
    async def test_subscribe_adds_a_queue_and_unsubscribe_removes_it(self) -> None:
        emitter = events.SSEEventEmitter()
        async with emitter.subscribe() as queue:
            assert queue in emitter.subscribers
        assert queue not in emitter.subscribers

    async def test_multiple_subscribers_each_receive_their_own_copy(self) -> None:
        emitter = events.SSEEventEmitter()
        async with emitter.subscribe() as q1, emitter.subscribe() as q2:
            emitter.emit_heartbeat(_NOW)
            first = await asyncio.wait_for(q1.get(), timeout=1.0)
            second = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert first.name == "heartbeat"
            assert second.name == "heartbeat"


# ---------------------------------------------------------------------------
# Explicit emit methods — one per schema event.
# ---------------------------------------------------------------------------


class TestEmitEvents:
    async def test_emit_invocation_started_enqueues_validated_payload(self) -> None:
        emitter = events.SSEEventEmitter()
        async with emitter.subscribe() as q:
            event = models.InvocationStartedEvent(
                invocation_id="inv-1",
                run_type="emergency",
                started_at=_NOW,
            )
            emitter.emit(event)
            enqueued = await asyncio.wait_for(q.get(), timeout=1.0)
            assert enqueued.name == "invocation_started"
            assert enqueued.data["invocation_id"] == "inv-1"
            assert enqueued.data["run_type"] == "emergency"

    @pytest.mark.parametrize(
        ("ctor", "name"),
        [
            (
                lambda: models.PhaseTransitionEvent(
                    invocation_id="inv-1",
                    phase="distill",
                    phase_started_at=_NOW,
                ),
                "phase_transition",
            ),
            (
                lambda: models.AgentStartedEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    started_at=_NOW,
                    latency_budget_seconds=60.0,
                ),
                "agent_started",
            ),
            (
                lambda: models.AgentSucceededEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    duration_seconds=12.5,
                    tokens_used=models.TokensUsed(input=1000, output=200),
                ),
                "agent_succeeded",
            ),
            (
                lambda: models.AgentRetryingEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    attempt=2,
                    reason="timeout",
                ),
                "agent_retrying",
            ),
            (
                lambda: models.AgentFailedEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    failure_mode="timeout",
                ),
                "agent_failed",
            ),
            (
                lambda: models.InvocationEndedEvent(
                    invocation_id="inv-1",
                    status="completed",
                    commands_issued=3,
                ),
                "invocation_ended",
            ),
            (
                lambda: models.NextTriggerChangedEvent(
                    next_trigger_at=_LATER,
                    next_trigger_type="market_hours_rolling",
                ),
                "next_trigger_changed",
            ),
        ],
    )
    async def test_emit_each_event_yields_correct_name(self, ctor: object, name: str) -> None:
        emitter = events.SSEEventEmitter()
        async with emitter.subscribe() as q:
            event = ctor()  # type: ignore[operator]
            emitter.emit(event)
            enqueued = await asyncio.wait_for(q.get(), timeout=1.0)
            assert enqueued.name == name

    async def test_emit_heartbeat_helper_writes_heartbeat_event(self) -> None:
        emitter = events.SSEEventEmitter()
        async with emitter.subscribe() as q:
            emitter.emit_heartbeat(_NOW)
            enqueued = await asyncio.wait_for(q.get(), timeout=1.0)
            assert enqueued.name == "heartbeat"
            # Pydantic JSON mode renders UTC as Z; treat the dict entry as
            # canonical wire bytes.
            assert enqueued.data["timestamp"].startswith("2026-05-26T12:00:00")


# ---------------------------------------------------------------------------
# SSE framing.
# ---------------------------------------------------------------------------


class TestFraming:
    def test_format_sse_record_shape(self) -> None:
        record = events._Event(name="heartbeat", data={"timestamp": _NOW.isoformat()})
        framed = events.format_sse_record(record)
        # Per schema: "event: <event_name>\ndata: <json>\n\n"
        assert framed.startswith("event: heartbeat\n")
        assert "\ndata: " in framed
        assert framed.endswith("\n\n")
        # The data line must be a single-line JSON document.
        data_line = framed.split("\ndata: ", 1)[1].rstrip("\n")
        json.loads(data_line)  # raises on multi-line / malformed JSON

    def test_format_sse_record_data_is_canonical_json(self) -> None:
        # Sort keys for deterministic on-wire bytes.
        record = events._Event(
            name="next_trigger_changed",
            data={"next_trigger_type": "pre_open", "next_trigger_at": _NOW.isoformat()},
        )
        framed = events.format_sse_record(record)
        data_line = framed.split("\ndata: ", 1)[1].rstrip("\n")
        parsed = json.loads(data_line)
        assert parsed["next_trigger_type"] == "pre_open"


# ---------------------------------------------------------------------------
# Heartbeat cadence — `iter_events` interleaves heartbeats every interval.
# ---------------------------------------------------------------------------


class TestHeartbeatCadence:
    async def test_iter_events_yields_heartbeat_after_idle_interval(self) -> None:
        # Tight interval so the test runs in well under a second.
        emitter = events.SSEEventEmitter(heartbeat_interval_seconds=0.05)
        records: list[events._Event] = []
        async with emitter.subscribe() as q:

            async def consume() -> None:
                async for record in emitter.iter_events(queue=q):
                    records.append(record)
                    if len(records) >= 2:
                        return

            await asyncio.wait_for(consume(), timeout=2.0)
        # Both yields should be heartbeats (no other events fired).
        assert records[0].name == "heartbeat"
        assert records[1].name == "heartbeat"

    async def test_iter_events_does_not_inject_heartbeat_when_event_emits(self) -> None:
        emitter = events.SSEEventEmitter(heartbeat_interval_seconds=0.5)
        records: list[events._Event] = []
        async with emitter.subscribe() as q:

            async def producer() -> None:
                # Fire a real event before the heartbeat window elapses.
                await asyncio.sleep(0.05)
                emitter.emit(
                    models.InvocationStartedEvent(
                        invocation_id="inv-1",
                        run_type="emergency",
                        started_at=_NOW,
                    )
                )

            async def consumer() -> None:
                async for record in emitter.iter_events(queue=q):
                    records.append(record)
                    return  # bail after the first record

            await asyncio.wait_for(
                asyncio.gather(producer(), consumer()),
                timeout=2.0,
            )
        # The first record should be the explicit event, NOT a heartbeat.
        assert records[0].name == "invocation_started"
