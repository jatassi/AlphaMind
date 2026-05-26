"""SSE event emitter for ``GET /events`` (ALP-664).

Implements the :class:`alphamind._kernel.progress.ProgressEmitter` Protocol
alongside the existing :class:`NoOpProgressEmitter` and the debug-e2e
``JsonlProgressEmitter``.  Additionally exposes nine explicit ``emit_*``
entry points — one per event type defined in
``docs/design/pipeline-control-and-events-schema.md`` — and a per-subscriber
``asyncio.Queue`` fan-out so each ``GET /events`` connection consumes its
own copy of every event.

Design notes:

* **Protocol-vs-schema asymmetry.** The in-process ``ProgressEmitter``
  Protocol's payload taxonomy (``phase_start`` / ``phase_done`` /
  ``agent_request`` / ``agent_response``) is broader and shaped for
  debug-time observability; the wire-event schema is the operator's
  view of the pipeline.  The mapping from one to the other is a
  caller-side concern (the orchestrator decides which Protocol callbacks
  correspond to schema events).  This emitter implements the Protocol
  as a no-op pass-through and accepts schema events through the
  explicit :meth:`emit` entry point.  Callers compose the two.
* **SSE framing** lives in :func:`format_sse_record`.  Pure function over
  the internal :class:`_Event` record; the route handler calls it.
* **Heartbeat** is a per-subscriber, per-connection concern: the
  :meth:`iter_events` coroutine races ``queue.get()`` against an
  ``asyncio.wait_for`` timeout and yields a fresh
  :class:`HeartbeatEvent`-derived record on timeout.  No background
  task; the heartbeat cadence is reset by every real event.

Per the ALP-128 architectural invariant (Pydantic at boundaries only),
the internal queue carries plain ``_Event`` records (frozen dataclass +
``dict[str, Any]`` payload); the route handler dumps Pydantic to dict
once at emit time and the queue thereafter sees only JSON-safe data.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel

from alphamind.scheduler.control.models import HeartbeatEvent

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "SSEEventEmitter",
    "format_sse_record",
]


DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 15.0


# Map every Pydantic event class onto the SSE ``event:`` name.  The
# emitter does the lookup once at emit time.
_EVENT_NAME_BY_CLASS: dict[type[BaseModel], str] = {}


def _register(name: str, cls: type[BaseModel]) -> None:
    _EVENT_NAME_BY_CLASS[cls] = name


# Populated below at module-load time once the model classes are
# imported.  Lazy to avoid circular import — models is already imported
# above; just iterate to keep the wire vocabulary in one place.
def _populate_registry() -> None:
    # Import here so the registry is keyed by the concrete classes the
    # emit() lookup sees at runtime.
    from alphamind.scheduler.control.models import (
        AgentFailedEvent,
        AgentRetryingEvent,
        AgentStartedEvent,
        AgentSucceededEvent,
        InvocationEndedEvent,
        InvocationStartedEvent,
        NextTriggerChangedEvent,
        PhaseTransitionEvent,
    )

    _register("invocation_started", InvocationStartedEvent)
    _register("phase_transition", PhaseTransitionEvent)
    _register("agent_started", AgentStartedEvent)
    _register("agent_succeeded", AgentSucceededEvent)
    _register("agent_retrying", AgentRetryingEvent)
    _register("agent_failed", AgentFailedEvent)
    _register("invocation_ended", InvocationEndedEvent)
    _register("next_trigger_changed", NextTriggerChangedEvent)
    _register("heartbeat", HeartbeatEvent)


_populate_registry()


# ---------------------------------------------------------------------------
# Internal records.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Event:
    """Internal record enqueued onto each subscriber's ``asyncio.Queue``.

    ``data`` is the Pydantic event already serialized to a JSON-safe
    ``dict``; the route handler reads it back out via
    :func:`format_sse_record` without further validation.
    """

    name: str
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# SSE framing — pure function.
# ---------------------------------------------------------------------------


def format_sse_record(record: _Event) -> str:
    """Frame an :class:`_Event` into the SSE wire bytes.

    Per the schema's framing spec, each event is emitted as one record
    terminated by a blank line:

    ::

        event: <event_name>
        data: <json>
        <blank line>

    The ``data`` payload is a single-line, key-sorted JSON document so
    on-wire bytes are deterministic across Python versions + dict
    insertion orders.
    """
    data_json = json.dumps(record.data, sort_keys=True, separators=(",", ":"))
    return f"event: {record.name}\ndata: {data_json}\n\n"


# ---------------------------------------------------------------------------
# Emitter.
# ---------------------------------------------------------------------------


class SSEEventEmitter:
    """Per-process emitter; lifespan creates one instance and shares it.

    Implements :class:`alphamind._kernel.progress.ProgressEmitter` as a
    no-op so the orchestrator can pass an instance into the existing
    Protocol injection point without changes.  Schema-shaped events are
    emitted via :meth:`emit` (one of the nine Pydantic event classes)
    or the convenience helper :meth:`emit_heartbeat`.
    """

    def __init__(
        self,
        *,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            msg = "heartbeat_interval_seconds must be positive"
            raise ValueError(msg)
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._subscribers: set[asyncio.Queue[_Event]] = set()

    # ------------------------------------------------------------------
    # ProgressEmitter Protocol — no-op pass-through.
    #
    # The Protocol carries phase + agent diagnostics shaped for
    # debug-e2e observation; the schema events are the operator view.
    # Mapping from one to the other is a caller-side concern — the
    # orchestrator decides which Protocol callback corresponds to a
    # schema-shaped event (e.g. ``phase_start("phase1")`` is NOT the
    # same as the schema's ``phase_transition(phase="collect")``).
    # ------------------------------------------------------------------

    def phase_start(self, phase: str) -> None:
        del phase

    def phase_done(self, phase: str, **fields: Any) -> None:
        del phase, fields

    def agent_request(self, **fields: Any) -> None:
        del fields

    def agent_response(self, **fields: Any) -> None:
        del fields

    # ------------------------------------------------------------------
    # Schema-shaped emit entry points.
    # ------------------------------------------------------------------

    @property
    def subscribers(self) -> frozenset[asyncio.Queue[_Event]]:
        """Read-only snapshot of currently-subscribed queues."""
        return frozenset(self._subscribers)

    def emit(self, event: BaseModel) -> None:
        """Enqueue ``event`` onto every subscriber's queue.

        ``event`` must be one of the nine event Pydantic classes
        registered at module-load time.  Unknown classes raise
        :class:`KeyError` — wire-vocabulary drift is a developer-facing
        bug, not a runtime condition to tolerate silently.
        """
        name = _EVENT_NAME_BY_CLASS[type(event)]
        data = event.model_dump(mode="json")
        record = _Event(name=name, data=data)
        for queue in self._subscribers:
            queue.put_nowait(record)

    def emit_heartbeat(self, now: datetime) -> None:
        """Convenience wrapper that constructs a :class:`HeartbeatEvent`."""
        self.emit(HeartbeatEvent(timestamp=now))

    # ------------------------------------------------------------------
    # Subscriber lifecycle.
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[_Event]]:
        """Register a fresh queue; deregister on exit.

        Designed for use inside the ``GET /events`` route handler:

        ::

            async with emitter.subscribe() as queue:
                async for record in emitter.iter_events(queue=queue):
                    yield format_sse_record(record)

        The queue has unbounded capacity; the connection is responsible
        for draining at the rate it can sustain.
        """
        queue: asyncio.Queue[_Event] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def iter_events(self, *, queue: asyncio.Queue[_Event]) -> AsyncIterator[_Event]:
        """Yield events from ``queue``; inject a heartbeat every idle interval.

        On each iteration:

        * If an event arrives within ``heartbeat_interval_seconds``,
          yield it.
        * Otherwise, yield a synthesized heartbeat (real wall-clock
          time at emission, not the timeout deadline) and continue.

        The loop terminates only when the consumer breaks out — the
        FastAPI ``StreamingResponse`` does so on connection close.
        """
        while True:
            try:
                record = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._heartbeat_interval_seconds,
                )
                yield record
            except TimeoutError:
                # Synthesize a heartbeat for this connection without
                # touching siblings — every subscriber drives its own
                # cadence per the schema's connection-scoped semantics.
                from datetime import UTC

                yield _Event(
                    name="heartbeat",
                    data={"timestamp": datetime.now(UTC).isoformat()},
                )
