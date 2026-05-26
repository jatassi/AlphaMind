"""Async pub/sub multiplexer for the SSE event stream (ALP-669, story 04b).

The multiplexer owns:

* A registry of subscriber queues — one bounded ``asyncio.Queue`` per
  connected browser ``EventSource``. The browser-facing route handler
  acquires a queue via :meth:`EventMultiplexer.subscribe`, drains it
  while the SSE connection is open, and lets the context manager
  deregister on close.
* The :meth:`EventMultiplexer.publish` fan-out — every event the
  upstream-consumer tasks parse is fanned to every subscriber's queue.
* Slow-consumer protection — a saturated queue triggers a *drop-oldest*
  + WARNING log. Live state is transient screen state per ALP-128
  pre-resolved (G); dropping the oldest preserves the most-recent N
  events the operator's UI cares about. Dropping newest would freeze
  the UI on stale state.

The multiplexer carries the frozen-dataclass events
(:class:`PipelineEvent` / :class:`MonitorEvent`) — Pydantic stays at
the boundary in :mod:`alphamind.command_center.events.models`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.events.clients import (
    MonitorEventsClient,
    PipelineEventsClient,
)

__all__ = [
    "BACKOFF_CAP_SECONDS",
    "BACKOFF_INITIAL_SECONDS",
    "DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE",
    "CombinedEvent",
    "EventMultiplexer",
    "monitor_consumer_task",
    "pipeline_consumer_task",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type alias — the tagged union the SSE route fans out.
# ---------------------------------------------------------------------------


type CombinedEvent = PipelineEvent | MonitorEvent


# ---------------------------------------------------------------------------
# Tunables — mirror the 01b/01c per-subscriber-queue + backoff patterns.
# ---------------------------------------------------------------------------


DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE: Final = 256
"""Per-subscriber queue capacity.

Mirrors the upstream pipeline + monitor SSE producers' queue cap.
A subscriber whose queue saturates has its OLDEST event dropped
rather than blocking the publish path; live state is transient
screen state, not history (ALP-128 pre-resolved G)."""


BACKOFF_INITIAL_SECONDS: Final = 1.0
BACKOFF_CAP_SECONDS: Final = 30.0
_JITTER_FRACTION: Final = 0.25


def _backoff_delay(attempt: int, *, rng: random.Random) -> float:
    """Compute the next reconnect delay with ±25% jitter.

    ``attempt`` starts at 0 for the first retry; the unjittered
    schedule is 1, 2, 4, 8, 16, 30, 30, 30 … (capped). The ±25%
    jitter prevents synchronized reconnect storms when both upstreams
    drop simultaneously (e.g., operator restarts both services).
    """
    base: float = min(BACKOFF_INITIAL_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
    factor: float = 1.0 + _JITTER_FRACTION * (2.0 * rng.random() - 1.0)
    return base * factor


# ---------------------------------------------------------------------------
# Multiplexer
# ---------------------------------------------------------------------------


class EventMultiplexer:
    """Per-process pub/sub over :class:`CombinedEvent`.

    The lifespan constructs one instance and shares it across:

    * The two upstream-consumer tasks (which call :meth:`publish`).
    * Every connected SSE route handler (which calls :meth:`subscribe`
      and drains the yielded queue).
    * Story 05a's alert engine, when it lands (also via :meth:`subscribe`).
    """

    def __init__(
        self,
        *,
        subscriber_queue_maxsize: int = DEFAULT_SUBSCRIBER_QUEUE_MAXSIZE,
    ) -> None:
        if subscriber_queue_maxsize < 1:
            msg = "subscriber_queue_maxsize must be >= 1"
            raise ValueError(msg)
        self._subscriber_queue_maxsize = subscriber_queue_maxsize
        self._subscribers: set[asyncio.Queue[CombinedEvent]] = set()
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        """Number of currently-active subscribers."""
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[CombinedEvent]]:
        """Register a fresh subscriber queue; deregister on exit.

        Usage from the SSE route handler:

        ::

            async with mux.subscribe() as queue:
                while True:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield format_sse_frame_from(event)
        """
        queue: asyncio.Queue[CombinedEvent] = asyncio.Queue(maxsize=self._subscriber_queue_maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def publish(self, event: CombinedEvent) -> None:
        """Fan ``event`` to every subscriber's queue.

        On full queue: drop the OLDEST event for that subscriber + log
        WARNING. Doing the drop synchronously (rather than awaiting
        space) keeps one slow consumer from stalling the fan-out for
        every other subscriber — and protects the upstream-consumer
        task from back-pressure that would lengthen the SSE producer's
        own queue.

        A snapshot of the subscriber set is taken under the lock so a
        concurrent subscribe/unsubscribe doesn't mutate the iteration.
        Publishing onto a stale-snapshot queue is benign: a queue that
        was just unsubscribed will be garbage-collected after the
        handler's task drains.
        """
        async with self._lock:
            snapshot = tuple(self._subscribers)
        for queue in snapshot:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop-oldest: pop the head, then put the new event.
                # Re-raising QueueFull from put_nowait after get_nowait
                # would indicate concurrent fill, which can't happen
                # because no other producer publishes to this queue.
                try:
                    dropped = queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — defensive
                    dropped = None
                # Defensive ``suppress``: a concurrent put can't happen
                # in our single-producer model, but the guard keeps the
                # publisher loop intact if it ever does.
                with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover
                    queue.put_nowait(event)
                log.warning(
                    "subscriber queue full; dropped oldest event (dropped_type=%s, new_type=%s)",
                    type(dropped).__name__,
                    type(event).__name__,
                )


# ---------------------------------------------------------------------------
# Upstream consumer task — one body, two registrations.
# ---------------------------------------------------------------------------


async def _handle_one_frame[E: CombinedEvent](
    *,
    raw_name: str,
    raw_data: Mapping[str, Any],
    name: str,
    event_type_enum: type[PipelineEventType] | type[MonitorEventType],
    event_factory: Callable[[PipelineEventType | MonitorEventType, dict[str, Any]], E],
    multiplexer: EventMultiplexer,
) -> bool:
    """Process one parsed frame from an upstream SSE stream.

    Returns ``True`` if the frame was published (i.e. the attempt
    counter should reset), ``False`` if the frame was dropped at the
    enum-validation or payload-type gate (wire-drift surfacing).
    """
    try:
        event_type = event_type_enum(raw_name)
    except ValueError:
        log.warning("%s consumer: unknown event-type %r; dropping", name, raw_name)
        return False
    typed_event = event_factory(event_type, dict(raw_data))
    await multiplexer.publish(typed_event)
    return True


async def _sleep_or_stop(*, delay: float, stop_event: asyncio.Event | None) -> bool:
    """Sleep ``delay`` seconds; return ``True`` if stop signalled mid-wait."""
    if stop_event is None:
        await asyncio.sleep(delay)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


async def _upstream_consumer_loop[E: CombinedEvent](
    *,
    name: str,
    client_stream: Callable[
        [], AbstractAsyncContextManager[AsyncIterator[tuple[str, Mapping[str, Any]]]]
    ],
    event_type_enum: type[PipelineEventType] | type[MonitorEventType],
    event_factory: Callable[[PipelineEventType | MonitorEventType, dict[str, Any]], E],
    multiplexer: EventMultiplexer,
    rng: random.Random,
    stop_event: asyncio.Event | None = None,
) -> None:
    """The shared consumer loop — parameterized over event type + factory.

    Loop:

    1. Open the client's SSE stream.
    2. For each parsed frame, dispatch to :func:`_handle_one_frame` —
       publishes typed events to the multiplexer; logs + drops
       unknown event-types and non-dict payloads (wire-drift surfacing).
    3. On any exception (connection drop, parse error escaping the
       client) — log WARNING and back off with jitter; the sibling
       consumer keeps running unaffected.

    Reset the backoff attempt counter on the first successfully-published
    event of a new connection — not on connect alone, because a
    connection can succeed and then fail before delivering any bytes.

    ``stop_event``: optional; when set, the loop exits at the next
    backoff window. Tests inject this to avoid leaning on task
    cancellation for shutdown.
    """
    attempt = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            async with client_stream() as frames:
                async for raw_name, raw_data in frames:
                    published = await _handle_one_frame(
                        raw_name=raw_name,
                        raw_data=raw_data,
                        name=name,
                        event_type_enum=event_type_enum,
                        event_factory=event_factory,
                        multiplexer=multiplexer,
                    )
                    if published:
                        attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s consumer: upstream stream errored (%s); backing off", name, exc)
        delay = _backoff_delay(attempt, rng=rng)
        attempt += 1
        log.info("%s consumer: reconnecting in %.2fs (attempt=%d)", name, delay, attempt)
        stopped = await _sleep_or_stop(delay=delay, stop_event=stop_event)
        if stopped:
            return


def _make_pipeline_event(
    event_type: PipelineEventType | MonitorEventType,
    payload: dict[str, Any],
) -> PipelineEvent:
    assert isinstance(event_type, PipelineEventType)
    return PipelineEvent(event_type=event_type, payload=payload)


def _make_monitor_event(
    event_type: PipelineEventType | MonitorEventType,
    payload: dict[str, Any],
) -> MonitorEvent:
    assert isinstance(event_type, MonitorEventType)
    return MonitorEvent(event_type=event_type, payload=payload)


async def pipeline_consumer_task(
    *,
    client: PipelineEventsClient,
    multiplexer: EventMultiplexer,
    rng: random.Random | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running pipeline-events consumer; registers on the supervisor.

    Reconnects independently of the monitor consumer (per ALP-128
    pre-resolved G — "independent reconnect per upstream"). Construct
    one of these via :func:`functools.partial` or a small closure
    inside ``app.py``'s lifespan, then register on the
    :class:`CommandCenterSupervisor`.
    """
    await _upstream_consumer_loop(
        name="pipeline",
        client_stream=client.stream,
        event_type_enum=PipelineEventType,
        event_factory=_make_pipeline_event,
        multiplexer=multiplexer,
        rng=rng or random.Random(),
        stop_event=stop_event,
    )


async def monitor_consumer_task(
    *,
    client: MonitorEventsClient,
    multiplexer: EventMultiplexer,
    rng: random.Random | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running monitor-events consumer; registers on the supervisor."""
    await _upstream_consumer_loop(
        name="monitor",
        client_stream=client.stream,
        event_type_enum=MonitorEventType,
        event_factory=_make_monitor_event,
        multiplexer=multiplexer,
        rng=rng or random.Random(),
        stop_event=stop_event,
    )
