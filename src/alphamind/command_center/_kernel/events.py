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

The per-event payload is carried as a plain ``Mapping[str, Any]`` — the
multiplexer's parser already validates the payload against the schema's
``$defs/<name>_event`` definition before construction, so the event
dataclass does not re-validate. Pydantic per-event-type response models
will live alongside the FastAPI routes that re-emit downstream (story
04b's boundary).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "MonitorEvent",
    "MonitorEventType",
    "PipelineEvent",
    "PipelineEventType",
]


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
    member, the ``data:`` line is parsed into the payload mapping.
    """

    event_type: PipelineEventType
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    """Decoded monitor SSE record.

    Constructed by the multiplexer (story 04b) from one upstream SSE
    record: the ``event:`` line names the :class:`MonitorEventType`
    member, the ``data:`` line is parsed into the payload mapping.
    """

    event_type: MonitorEventType
    payload: Mapping[str, Any]
