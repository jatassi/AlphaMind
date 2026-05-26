"""Frozen-dataclass event vocabulary for the SSE multiplexer (story 02 / ALP-666).

This story ships the type shapes; the parser + multiplexer arrive in 04b
(SSE multiplexer) — at that point the multiplexer reads upstream SSE
records, parses the JSON ``data:`` line, and constructs one
:class:`PipelineEvent` / :class:`MonitorEvent` instance per record before
re-emitting downstream.

Two event-type StrEnums pin the wire vocabulary to the schema docs:

* :class:`PipelineEventType` — 9 members per
  :doc:`docs/design/pipeline-control-and-events-schema.md` § Event schema.
* :class:`MonitorEventType` — 7 members per
  :doc:`docs/design/monitor-control-and-events-schema.md` § Event schema.

The per-event payload is carried as an immutable :class:`MappingProxyType`
wrapping the parsed dict. The multiplexer's parser already validates the
payload against the schema's ``$defs/<name>_event`` definition before
construction, so the event dataclass does not re-validate. ``frozen=True``
alone would not prevent in-place mutation of a dict payload — story 04b
will multiplex these to many subscribers and any subscriber mutating
the payload would corrupt every other subscriber's view; the
``MappingProxyType`` wrapper makes that bug class structurally
impossible (F9, F10).

A per-event-type payload schema (one dataclass per
:class:`PipelineEventType` / :class:`MonitorEventType` member, replacing
the generic ``Mapping``) is the right end-state — story 04b will need
it. Tracked as Linear follow-up; this dispatch ships the immutability
fix only so the type-shape boundary doesn't churn twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

__all__ = [
    "MonitorEvent",
    "MonitorEventType",
    "PipelineEvent",
    "PipelineEventType",
]


def _freeze_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only view onto *payload*.

    ``MappingProxyType`` proxies reads through to the underlying dict
    but raises ``TypeError`` on every write — the dataclass's
    ``frozen=True`` only blocks attribute reassignment, not in-place
    dict mutation (F9, F10).

    If *payload* is already a ``MappingProxyType`` we pass it through
    unchanged so a re-wrap doesn't create a tower of proxies.
    """
    if isinstance(payload, MappingProxyType):
        return payload
    # Copy into a plain dict first so the caller can't retain a mutable
    # reference to the underlying storage (constructing the proxy
    # against the caller's own dict would let them mutate it from the
    # outside).
    return MappingProxyType(dict(payload))


class PipelineEventType(StrEnum):
    """Pipeline SSE event vocabulary — 9 members.

    Per :doc:`docs/design/pipeline-control-and-events-schema.md` § Event
    schema. The StrEnum value is the SSE ``event:`` field literal the
    upstream pipeline emits.
    """

    INVOCATION_STARTED = "invocation_started"
    PHASE_TRANSITION = "phase_transition"
    AGENT_STARTED = "agent_started"
    AGENT_SUCCEEDED = "agent_succeeded"
    AGENT_RETRYING = "agent_retrying"
    AGENT_FAILED = "agent_failed"
    INVOCATION_ENDED = "invocation_ended"
    NEXT_TRIGGER_CHANGED = "next_trigger_changed"
    HEARTBEAT = "heartbeat"


class MonitorEventType(StrEnum):
    """Continuous-monitor SSE event vocabulary — 7 members.

    Per :doc:`docs/design/monitor-control-and-events-schema.md` § Event
    schema. The StrEnum value is the SSE ``event:`` field literal the
    upstream monitor emits.
    """

    WEBSOCKET_CONNECTED = "websocket_connected"
    WEBSOCKET_DISCONNECTED = "websocket_disconnected"
    FILL_RECEIVED = "fill_received"
    BREACH_DETECTED = "breach_detected"
    EMERGENCY_INVOCATION_TRIGGERED = "emergency_invocation_triggered"
    GREEKS_REFRESHED = "greeks_refreshed"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """Decoded pipeline SSE record.

    Constructed by the multiplexer (story 04b) from one upstream SSE
    record: the ``event:`` line names the :class:`PipelineEventType`
    member, the ``data:`` line is parsed into the payload mapping. The
    payload is normalized to an immutable :class:`MappingProxyType`
    view (F9, F10) so subscribers cannot mutate each other's view.
    """

    event_type: PipelineEventType
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks reassignment but allows in-place dict
        # mutation; wrap the payload in a read-only view so mutation
        # raises at the boundary. Using ``object.__setattr__`` because
        # the frozen dataclass blocks normal assignment.
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    """Decoded monitor SSE record.

    Constructed by the multiplexer (story 04b) from one upstream SSE
    record: the ``event:`` line names the :class:`MonitorEventType`
    member, the ``data:`` line is parsed into the payload mapping. The
    payload is normalized to an immutable :class:`MappingProxyType`
    view (F9, F10) so subscribers cannot mutate each other's view.
    """

    event_type: MonitorEventType
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_payload(self.payload))
