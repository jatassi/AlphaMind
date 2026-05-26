"""Pydantic boundary-model tests for the monitor control + events HTTP surface.

Verifies that every ``$defs`` entry in
``docs/design/monitor-control-and-events-schema.md`` has a Pydantic counterpart
with matching field shapes, the documented ``extra="forbid"`` discipline, and
the enum values from the schema.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.execution.continuous_monitor.control.models import (
    BreachDetectedEvent,
    CancelOrderRequest,
    ControlErrorEnvelope,
    ControlResponseEnvelope,
    EmergencyInvocationTriggeredEvent,
    ErrorBody,
    FillReceivedEvent,
    ForceClosePositionRequest,
    ForceClosePositionResponse,
    GreeksRefreshedEvent,
    HeartbeatEvent,
    SetHaltModeRequest,
    WebsocketConnectedEvent,
    WebsocketDisconnectedEvent,
)

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class TestCancelOrderRequest:
    def test_accepts_order_id(self) -> None:
        request = CancelOrderRequest(order_id="ord-1")
        assert request.order_id == "ord-1"

    def test_rejects_missing_order_id(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrderRequest()  # type: ignore[call-arg]

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrderRequest.model_validate({"order_id": "ord-1", "extra": "x"})


class TestForceClosePositionRequest:
    def test_accepts_position_id_and_rationale(self) -> None:
        request = ForceClosePositionRequest(position_id="pos-1", rationale="too much vega")
        assert request.position_id == "pos-1"
        assert request.rationale == "too much vega"

    def test_rejects_empty_rationale(self) -> None:
        with pytest.raises(ValidationError):
            ForceClosePositionRequest(position_id="pos-1", rationale="")

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ForceClosePositionRequest.model_validate(
                {"position_id": "pos-1", "rationale": "x", "extra": "x"}
            )


class TestSetHaltModeRequest:
    def test_accepts_enabled_and_reason(self) -> None:
        request = SetHaltModeRequest(enabled=True, reason="circuit breaker")
        assert request.enabled is True
        assert request.reason == "circuit breaker"

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            SetHaltModeRequest(enabled=True, reason="")

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            SetHaltModeRequest.model_validate({"enabled": False, "reason": "x", "extra": "x"})


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class TestControlResponseEnvelope:
    def test_status_pinned_to_accepted(self) -> None:
        envelope = ControlResponseEnvelope(status="accepted", applied_at=datetime.now(UTC))
        assert envelope.status == "accepted"

    def test_rejects_any_other_status(self) -> None:
        with pytest.raises(ValidationError):
            ControlResponseEnvelope.model_validate(
                {"status": "rejected", "applied_at": "2026-05-26T00:00:00Z"}
            )

    def test_applied_at_is_datetime(self) -> None:
        envelope = ControlResponseEnvelope.model_validate(
            {"status": "accepted", "applied_at": "2026-05-26T00:00:00Z"}
        )
        assert envelope.applied_at.tzinfo is not None


class TestForceClosePositionResponse:
    def test_extends_envelope_with_envelope_id(self) -> None:
        response = ForceClosePositionResponse(
            status="accepted",
            applied_at=datetime.now(UTC),
            envelope_id="MON.session-1.42",
        )
        assert response.envelope_id == "MON.session-1.42"

    def test_envelope_id_matches_mon_pattern(self) -> None:
        with pytest.raises(ValidationError):
            ForceClosePositionResponse.model_validate(
                {
                    "status": "accepted",
                    "applied_at": "2026-05-26T00:00:00Z",
                    "envelope_id": "not-a-MON-id",
                }
            )


class TestControlErrorEnvelope:
    def test_holds_error_code_and_detail(self) -> None:
        envelope = ControlErrorEnvelope(error=ErrorBody(code="not_found", detail="missing"))
        assert envelope.error.code == "not_found"
        assert envelope.error.detail == "missing"
        assert envelope.error.details is None

    def test_rejects_unknown_error_code(self) -> None:
        with pytest.raises(ValidationError):
            ControlErrorEnvelope.model_validate({"error": {"code": "teapot", "detail": "x"}})

    def test_carries_structured_details_object(self) -> None:
        envelope = ControlErrorEnvelope(
            error=ErrorBody(
                code="precondition_failed",
                detail="already filled",
                details={"current_status": "filled"},
            )
        )
        assert envelope.error.details == {"current_status": "filled"}


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------


class TestWebsocketEvents:
    def test_websocket_connected_requires_timestamp(self) -> None:
        event = WebsocketConnectedEvent(timestamp=datetime.now(UTC))
        assert event.timestamp is not None

    def test_websocket_disconnected_requires_reason(self) -> None:
        event = WebsocketDisconnectedEvent(timestamp=datetime.now(UTC), reason="network_error")
        assert event.reason == "network_error"

    def test_websocket_disconnected_rejects_missing_reason(self) -> None:
        with pytest.raises(ValidationError):
            WebsocketDisconnectedEvent.model_validate({"timestamp": "2026-05-26T00:00:00Z"})


class TestFillReceivedEvent:
    def test_carries_order_position_price_qty(self) -> None:
        event = FillReceivedEvent(
            order_id="ord-1",
            position_id="pos-1",
            fill_price=100.5,
            fill_qty=10.0,
        )
        assert event.order_id == "ord-1"
        assert event.fill_price == 100.5

    def test_rejects_zero_fill_price(self) -> None:
        with pytest.raises(ValidationError):
            FillReceivedEvent(order_id="ord-1", position_id="pos-1", fill_price=0.0, fill_qty=10.0)


class TestBreachDetectedEvent:
    def test_immediate_classification(self) -> None:
        event = BreachDetectedEvent(
            rule="per_position_max_size",
            current_value=0.07,
            limit=0.05,
            response_classification="immediate",
        )
        assert event.response_classification == "immediate"

    def test_rejects_unknown_classification(self) -> None:
        with pytest.raises(ValidationError):
            BreachDetectedEvent.model_validate(
                {
                    "rule": "x",
                    "current_value": 1.0,
                    "limit": 0.5,
                    "response_classification": "weird",
                }
            )


class TestEmergencyInvocationTriggeredEvent:
    def test_carries_reason(self) -> None:
        event = EmergencyInvocationTriggeredEvent(reason="regime_jump")
        assert event.reason == "regime_jump"


class TestGreeksRefreshedEvent:
    def test_carries_underlying_and_refreshed_at(self) -> None:
        ts = datetime.now(UTC)
        event = GreeksRefreshedEvent(underlying="AAPL", refreshed_at=ts)
        assert event.underlying == "AAPL"
        assert event.refreshed_at == ts


class TestHeartbeatEvent:
    def test_carries_timestamp(self) -> None:
        event = HeartbeatEvent(timestamp=datetime.now(UTC))
        assert event.timestamp is not None
