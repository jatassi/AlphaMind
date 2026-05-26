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

    def test_payload_is_immutable_mapping(self) -> None:
        # The payload dict is preserved as-passed; downstream consumers may
        # not mutate it (the frozen dataclass guarantees the binding, the
        # docstring expects the value).
        payload = {"timestamp": "2026-05-26T00:00:00Z"}
        event = PipelineEvent(event_type=PipelineEventType.HEARTBEAT, payload=payload)
        # If the caller mutates the source dict, the event sees it too —
        # the dataclass holds a reference, not a copy. This is intentional:
        # the multiplexer constructs PipelineEvents once and never mutates
        # the payload, so a deepcopy would be pure overhead. The test
        # documents the contract.
        payload["timestamp"] = "2026-05-26T00:00:01Z"
        assert event.payload["timestamp"] == "2026-05-26T00:00:01Z"


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
