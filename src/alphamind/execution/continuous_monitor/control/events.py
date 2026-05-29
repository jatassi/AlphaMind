"""SSE event emitter for the monitor ``GET /events`` stream (ALP-665).

Exposes structural callbacks that the existing monitor loops invoke (breach
loop, fill-stream consumer, greeks refresher, websocket lifecycle hooks,
emergency-trigger emitter, cascade dispatcher). Each callback validates the
payload at the emitter boundary against the matching Pydantic model from
``models.py`` (so the SSE consumer only ever sees schema-conforming data),
serializes to a plain dict, and fans it out to every subscribed
``asyncio.Queue``.

Cross-field invariants per the schema's § Notes on cross-field invariants:

* ``websocket_connected`` and ``websocket_disconnected`` strictly alternate.
  The emitter enforces this by tracking the last websocket-state transition;
  emitting two consecutive ``connected`` events without an intervening
  ``disconnected`` (or vice versa) raises ``RuntimeError`` at the call site
  rather than producing an SSE record that violates the contract.
* Heartbeat cadence (15 s) is owned by the route handler in ``routes.py`` —
  not the emitter — because the cadence is per-connection (one subscriber
  with no events for 15 s emits one heartbeat to that subscriber, not the
  whole fanout).

The emitter is no-op-safe when run without any subscribers — a monitor that
boots into "degraded" mode (HTTP surface failing to bind, etc.) can still
invoke the callbacks without raising. Per architectural invariants, the
emitter only writes onto queues; it does not spawn background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alphamind.execution.continuous_monitor.control.models import (
    BreachDetectedEvent,
    BreachLoopDegradedEvent,
    BreachLoopRecoveredEvent,
    BreachResponseClassification,
    EmergencyInvocationTriggeredEvent,
    FillReceivedEvent,
    GreeksRefreshedEvent,
    HeartbeatEvent,
    WebsocketConnectedEvent,
    WebsocketDisconnectedEvent,
)

log = logging.getLogger(__name__)

# Per-subscriber queue capacity. SSE consumers should drain promptly; a
# slow consumer's queue grows up to this bound before put_nowait() raises
# QueueFull. Tests use small bounded scenarios so the default is generous.
_DEFAULT_QUEUE_MAXSIZE = 256


@dataclass(frozen=True, slots=True)
class EmittedEvent:
    """One event item placed on a subscriber's queue.

    The route handler converts each ``EmittedEvent`` into the SSE wire
    framing (``event: <name>\\ndata: <json>\\n\\n``) on the way out — see
    ``routes.py``.
    """

    name: str
    payload: dict[str, Any]


class SSEEventEmitter:
    """Fan-out SSE producer the monitor surfaces invoke.

    Each ``emit_*`` method validates its payload against the matching
    Pydantic model (so a typo in a producer surfaces immediately, not at
    SSE-write time), serializes to a dict, and pushes the
    :class:`EmittedEvent` onto every subscribed queue.

    Subscribers register through :meth:`subscribe`, an ``async with``
    context manager that yields a fresh :class:`asyncio.Queue` and
    unsubscribes on exit. The emitter is not threadsafe — all callers are
    on the same asyncio loop in production.
    """

    def __init__(self, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subscribers: list[asyncio.Queue[EmittedEvent]] = []
        self._queue_maxsize = queue_maxsize
        # Tracks the last websocket lifecycle transition. ``None`` =
        # initial state (neither connected nor disconnected observed). The
        # only legal transitions are None→connected, connected→disconnected,
        # disconnected→connected; everything else raises.
        self._last_websocket_state: str | None = None
        # Current breach-loop health (ALP-732). Flipped by
        # ``emit_breach_loop_degraded`` / ``emit_breach_loop_recovered`` so a
        # subscriber that joins after the transient transition event — or any
        # diagnostic surface — can read the loop's current health rather than
        # having to have caught the moment it flipped.
        self._breach_loop_degraded = False

    # ------------------------------------------------------------------
    # Subscriber management
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[EmittedEvent]]:
        """Register a fresh subscriber queue for the lifetime of the context."""
        queue: asyncio.Queue[EmittedEvent] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers.append(queue)
        try:
            yield queue
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)

    def subscriber_count(self) -> int:
        """Return the current number of subscribed queues. Used by tests + diagnostics."""
        return len(self._subscribers)

    def is_breach_loop_degraded(self) -> bool:
        """Return whether the breach loop is currently in the degraded state (ALP-732)."""
        return self._breach_loop_degraded

    # ------------------------------------------------------------------
    # Structural callbacks — one per documented event type
    # ------------------------------------------------------------------

    def emit_websocket_connected(self, *, timestamp: datetime | None = None) -> None:
        """The Alpaca ``trade_updates`` websocket established a connection.

        Updates the recorded last-state BEFORE fanning out so a fanout failure
        (e.g., model_dump raising) does not leave the emitter in a stale
        "disconnected" state inconsistent with the intent of this call (F7).
        Subscriber queues may carry partial fanout if one of them raises, but
        the next emit will still be processed under a coherent state.
        """
        if self._last_websocket_state == "connected":
            msg = (
                "websocket_connected emitted twice without an intervening "
                "websocket_disconnected (schema § Notes on cross-field "
                "invariants requires the events to alternate)"
            )
            raise RuntimeError(msg)
        event = WebsocketConnectedEvent(timestamp=timestamp or _utcnow())
        self._last_websocket_state = "connected"
        self._fanout("websocket_connected", event)

    def emit_websocket_disconnected(
        self, *, reason: str, timestamp: datetime | None = None
    ) -> None:
        """The websocket dropped or closed.

        Allows an initial ``None`` → ``disconnected`` transition so a boot-time
        pre-connect failure can record itself without crashing the producer;
        only ``disconnected`` → ``disconnected`` (a double-disconnect with no
        intervening connect) is a structural error per the schema.
        """
        if self._last_websocket_state == "disconnected":
            msg = (
                "websocket_disconnected emitted twice without an intervening "
                "websocket_connected (schema § Notes on cross-field "
                "invariants requires the events to alternate)"
            )
            raise RuntimeError(msg)
        event = WebsocketDisconnectedEvent(timestamp=timestamp or _utcnow(), reason=reason)
        # State update precedes fanout (F7) — see emit_websocket_connected.
        self._last_websocket_state = "disconnected"
        self._fanout("websocket_disconnected", event)

    def emit_fill_received(
        self,
        *,
        order_id: str,
        position_id: str,
        fill_price: float,
        fill_qty: float,
    ) -> None:
        """The monitor wrote a fill to the buffer (after Alpaca's ``trade_updates`` event)."""
        event = FillReceivedEvent(
            order_id=order_id,
            position_id=position_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
        )
        self._fanout("fill_received", event)

    def emit_breach_detected(
        self,
        *,
        rule: str,
        current_value: float,
        limit: float,
        response_classification: BreachResponseClassification,
    ) -> None:
        """The breach detector evaluated a rule as breached against live state."""
        event = BreachDetectedEvent(
            rule=rule,
            current_value=current_value,
            limit=limit,
            response_classification=response_classification,
        )
        self._fanout("breach_detected", event)

    def emit_emergency_invocation_triggered(self, *, reason: str) -> None:
        """The monitor fired ``POST /control/trigger_emergency_invocation`` on the pipeline."""
        event = EmergencyInvocationTriggeredEvent(reason=reason)
        self._fanout("emergency_invocation_triggered", event)

    def emit_greeks_refreshed(self, *, underlying: str, refreshed_at: datetime) -> None:
        """A scheduled or move-based greeks refresh completed for one underlying."""
        event = GreeksRefreshedEvent(underlying=underlying, refreshed_at=refreshed_at)
        self._fanout("greeks_refreshed", event)

    def emit_heartbeat(self, *, timestamp: datetime | None = None) -> None:
        """Idle-cadence heartbeat. The route handler drives the 15 s cadence."""
        event = HeartbeatEvent(timestamp=timestamp or _utcnow())
        self._fanout("heartbeat", event)

    def emit_breach_loop_degraded(self, *, consecutive_failures: int, last_error: str) -> None:
        """The breach loop crossed its consecutive-failure threshold (ALP-732).

        Sets the degraded health flag BEFORE fanning out (mirrors the
        websocket-state F7 ordering) so the flag is coherent even if fanout
        partially fails.
        """
        event = BreachLoopDegradedEvent(
            consecutive_failures=consecutive_failures, last_error=last_error
        )
        self._breach_loop_degraded = True
        self._fanout("breach_loop_degraded", event)

    def emit_breach_loop_recovered(self, *, consecutive_failures: int) -> None:
        """The breach loop's first successful tick after a degraded run (ALP-732)."""
        event = BreachLoopRecoveredEvent(consecutive_failures=consecutive_failures)
        self._breach_loop_degraded = False
        self._fanout("breach_loop_recovered", event)

    # ------------------------------------------------------------------
    # Internal — fan-out
    # ------------------------------------------------------------------

    def _fanout(self, name: str, payload_model: Any) -> None:
        """Push *payload_model* onto every subscriber queue.

        Serializes the Pydantic model to a JSON-friendly dict at the
        emitter boundary so the route handler writes plain values into the
        SSE frame without re-validating downstream.
        """
        payload = payload_model.model_dump(mode="json")
        event = EmittedEvent(name=name, payload=payload)
        if not self._subscribers:
            # No-op-safe; a degraded monitor still functions.
            return
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer's queue saturated — log and drop the
                # event for that subscriber rather than blocking the
                # producer. Live state is transient screen state, not
                # history, per ALP-128 pre-resolved decision (G).
                log.warning("SSE subscriber queue full; dropping event name=%s", name)


def _utcnow() -> datetime:
    """Single now() seam so tests can patch through dependency injection."""
    return datetime.now(UTC)


__all__ = [
    "EmittedEvent",
    "SSEEventEmitter",
]
