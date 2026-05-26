"""Tests for ``command_center._kernel.events`` (story 02 / ALP-666).

Covers the frozen-dataclass event vocabulary that the SSE multiplexer
(story 04b) will produce from upstream SSE records. This story 02 ships
the type shapes; the parser + multiplexer arrive in 04b.

Event-type StrEnums are pinned to the schema docs:

* ``PipelineEventType``: 9 members per
  ``pipeline-control-and-events-schema.md`` § Event schema.
* ``MonitorEventType``: 7 members per
  ``monitor-control-and-events-schema.md`` § Event schema.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)


class TestPipelineEventType:
    def test_has_all_nine_documented_members(self) -> None:
        # Per docs/design/pipeline-control-and-events-schema.md § Event schema.
        members = {m.value for m in PipelineEventType}
        assert members == {
            "invocation_started",
            "phase_transition",
            "agent_started",
            "agent_succeeded",
            "agent_retrying",
            "agent_failed",
            "invocation_ended",
            "next_trigger_changed",
            "heartbeat",
        }


class TestMonitorEventType:
    def test_has_all_seven_documented_members(self) -> None:
        # Per docs/design/monitor-control-and-events-schema.md § Event schema.
        members = {m.value for m in MonitorEventType}
        assert members == {
            "websocket_connected",
            "websocket_disconnected",
            "fill_received",
            "breach_detected",
            "emergency_invocation_triggered",
            "greeks_refreshed",
            "heartbeat",
        }


class TestPipelineEvent:
    def test_constructs_with_event_type_and_payload(self) -> None:
        # PipelineEvent wraps a typed event with its decoded JSON payload.
        # 04b will parse the SSE ``data:`` line into this shape before
        # re-emitting downstream.
        event = PipelineEvent(
            event_type=PipelineEventType.HEARTBEAT,
            payload={"timestamp": "2026-05-26T00:00:00Z"},
        )
        assert event.event_type is PipelineEventType.HEARTBEAT
        assert event.payload == {"timestamp": "2026-05-26T00:00:00Z"}

    def test_is_frozen(self) -> None:
        event = PipelineEvent(
            event_type=PipelineEventType.HEARTBEAT,
            payload={"timestamp": "2026-05-26T00:00:00Z"},
        )
        with pytest.raises(FrozenInstanceError):
            event.event_type = PipelineEventType.INVOCATION_STARTED  # type: ignore[misc]

    def test_payload_is_read_only_mapping(self) -> None:
        # The payload is wrapped in a MappingProxyType so subscribers
        # cannot mutate each other's view (F9, F10). The wrap also copies
        # the input dict so an external mutation of the source does not
        # leak through.
        payload = {"timestamp": "2026-05-26T00:00:00Z"}
        event = PipelineEvent(event_type=PipelineEventType.HEARTBEAT, payload=payload)
        # External mutation of the source dict does NOT propagate.
        payload["timestamp"] = "2026-05-26T00:00:01Z"
        assert event.payload["timestamp"] == "2026-05-26T00:00:00Z"
        # In-place mutation of the event's payload raises TypeError.
        with pytest.raises(TypeError):
            event.payload["timestamp"] = "tampered"  # type: ignore[index]

    def test_default_empty_payload(self) -> None:
        # PipelineEvent now defaults the payload to an empty mapping so
        # tests + producers that emit a meta-only event (e.g., a sentinel
        # heartbeat) don't have to spell out ``payload={}``.
        event = PipelineEvent(event_type=PipelineEventType.HEARTBEAT)
        assert dict(event.payload) == {}


class TestMonitorEvent:
    def test_constructs_with_event_type_and_payload(self) -> None:
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={"timestamp": "2026-05-26T00:00:00Z"},
        )
        assert event.event_type is MonitorEventType.WEBSOCKET_CONNECTED
        assert event.payload == {"timestamp": "2026-05-26T00:00:00Z"}

    def test_is_frozen(self) -> None:
        event = MonitorEvent(
            event_type=MonitorEventType.HEARTBEAT,
            payload={"timestamp": "2026-05-26T00:00:00Z"},
        )
        with pytest.raises(FrozenInstanceError):
            event.event_type = MonitorEventType.FILL_RECEIVED  # type: ignore[misc]

    def test_payload_is_read_only_mapping(self) -> None:
        payload = {"timestamp": "2026-05-26T00:00:00Z"}
        event = MonitorEvent(event_type=MonitorEventType.HEARTBEAT, payload=payload)
        payload["timestamp"] = "2026-05-26T00:00:01Z"
        assert event.payload["timestamp"] == "2026-05-26T00:00:00Z"
        with pytest.raises(TypeError):
            event.payload["timestamp"] = "tampered"  # type: ignore[index]
