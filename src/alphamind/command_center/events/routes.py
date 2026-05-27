"""FastAPI router for ``GET /api/events`` (ALP-669, story 04b).

Per-connection flow:

1. ``Depends(current_session)`` — 401 if no valid session cookie.
2. Subscribe to the per-process :class:`EventMultiplexer` (lives on
   ``app.state.event_multiplexer``, wired by the lifespan).
3. Return a :class:`StreamingResponse` whose body iterates:

   * Race ``queue.get()`` against the heartbeat timeout.
   * On event arrival: emit one SSE frame with event-name
     ``<source>:<event_name>`` and JSON payload.
   * On timeout: emit one ``heartbeat`` SSE frame with a fresh
     wall-clock timestamp.

The frame format helpers (:func:`format_sse_frame`,
:func:`format_heartbeat_frame`) are pure functions over the
:class:`BrowserEventEnvelope`. The route handler is the imperative
shell.

Per ALP-128 / parent issue:

* **Pydantic at boundaries only** — the envelope is Pydantic for
  JSON serialization clarity; the multiplexer's internal events are
  the frozen dataclasses.
* **Dual SSE event-name** — ``pipeline:heartbeat`` and
  ``monitor:heartbeat`` are distinct on the wire so the browser
  doesn't have to inspect the payload to know which upstream produced
  the frame. The route's own heartbeat (when no upstream event arrives
  within the window) is emitted as the unqualified ``heartbeat``
  event-name; the browser distinguishes "command-center alive" from
  "upstream alive" by event-name shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from alphamind.command_center._kernel.events import (
    AlertFiredEvent,
    ConfigReloadRequiresRestartEvent,
    MonitorEvent,
    PipelineEvent,
)
from alphamind.command_center.auth.dependencies import current_session
from alphamind.command_center.events.models import BrowserEventEnvelope
from alphamind.command_center.events.multiplexer import (
    CombinedEvent,
    EventMultiplexer,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_SSE_EVENT",
    "build_events_router",
    "format_heartbeat_frame",
    "format_sse_frame",
]

log = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 15.0
"""Idle window before the route emits a backend-originated heartbeat.

The browser's ``EventSource`` reconnects on its own when the
connection drops; the heartbeat keeps NSSM / Caddy / WireGuard
intermediaries from idling-out the connection. Matches the 15s
heartbeat the upstream SSE producers emit themselves."""

HEARTBEAT_SSE_EVENT: Final = "heartbeat"
"""SSE ``event:`` field for the route's own heartbeat frame.

Unqualified — distinguishes the backend's keep-alive from the
upstreams' ``pipeline:heartbeat`` / ``monitor:heartbeat`` event-name
shapes."""


# ---------------------------------------------------------------------------
# Pure framing helpers
# ---------------------------------------------------------------------------


def _envelope_for(event: CombinedEvent) -> BrowserEventEnvelope:
    """Wrap an internal multiplexer event in a Pydantic envelope.

    The envelope is the only Pydantic surface in the SSE path; it
    serializes to JSON for the ``data:`` line. The ``source`` field is
    derived structurally from the event's dataclass type — not a
    string-typed field on the internal record — so a future
    multiplexer-source addition lands here as a type error rather
    than a silent miscategorization.
    """
    source: str
    event_name: str
    payload: dict[str, object]
    if isinstance(event, PipelineEvent):
        source = "pipeline"
        event_name = str(event.event_type.value)
        payload = dict(event.payload)
    elif isinstance(event, MonitorEvent):
        source = "monitor"
        event_name = str(event.event_type.value)
        payload = dict(event.payload)
    elif isinstance(event, AlertFiredEvent):
        source = "cc"
        event_name = "alert_fired"
        payload = {
            "alert_id": event.alert_id,
            "rule_name": event.rule_name,
            "severity": event.severity,
            **dict(event.payload),
        }
    elif isinstance(event, ConfigReloadRequiresRestartEvent):
        # Story 06b — engine published this when the non-rules section
        # of a watched YAML changed; UI surfaces a "restart required"
        # banner so the operator triggers the daemon restart.
        source = "cc"
        event_name = "config_reload_requires_restart"
        payload = {
            "filename": event.filename,
            "reason": event.reason,
        }
    else:  # pragma: no cover — CombinedEvent is closed
        msg = f"unsupported CombinedEvent variant: {type(event).__name__}"
        raise TypeError(msg)
    return BrowserEventEnvelope(
        source=source,  # type: ignore[arg-type]
        event=event_name,
        data=payload,
    )


def format_sse_frame(envelope: BrowserEventEnvelope) -> str:
    """Frame a :class:`BrowserEventEnvelope` into SSE wire bytes.

    Format:

    ::

        event: <source>:<event_name>
        data: <key-sorted-json>
        <blank line>

    Key-sorted JSON makes the wire bytes deterministic across
    interpreter versions + dict-insertion orders.
    """
    payload = envelope.model_dump(mode="json")
    data_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sse_event_name = f"{envelope.source}:{envelope.event}"
    return f"event: {sse_event_name}\ndata: {data_json}\n\n"


def format_heartbeat_frame(*, now: datetime | None = None) -> str:
    """Frame the backend's own heartbeat SSE record.

    Distinguishable from upstream ``pipeline:heartbeat`` and
    ``monitor:heartbeat`` frames by the unqualified ``heartbeat``
    event-name. The timestamp is the moment of emission (not the idle
    deadline) so consumers can measure backend liveness.
    """
    ts = (now or datetime.now(UTC)).isoformat()
    data_json = json.dumps({"timestamp": ts}, sort_keys=True, separators=(",", ":"))
    return f"event: {HEARTBEAT_SSE_EVENT}\ndata: {data_json}\n\n"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def build_events_router() -> APIRouter:
    """Build the ``/api/events`` router.

    The route handler is an inner function so it can close over the
    router's dependency-injection seams. The handler pulls the
    multiplexer + heartbeat-interval off ``request.app.state``; the
    lifespan (or test fixture) wires both.
    """
    router = APIRouter(prefix="/api", tags=["events"])

    @router.get("/events")
    async def get_events(
        request: Request,
        _session_id: object = Depends(current_session),
    ) -> StreamingResponse:
        del _session_id  # Auth side-effect; we don't need the id here.

        multiplexer: EventMultiplexer = request.app.state.event_multiplexer
        heartbeat_interval = getattr(
            request.app.state,
            "event_heartbeat_interval_seconds",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        )

        async def streamer() -> AsyncIterator[str]:
            async with multiplexer.subscribe() as queue:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                    except TimeoutError:
                        yield format_heartbeat_frame()
                        continue
                    yield format_sse_frame(_envelope_for(event))

        return StreamingResponse(
            streamer(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return router
