"""Pydantic boundary models for the monitor control + events HTTP surface (ALP-665).

Translates every ``$defs`` entry in
``docs/design/monitor-control-and-events-schema.md`` into a typed Pydantic
shape with the documented ``extra="forbid"`` discipline.

Per the parent issue's architectural invariants, Pydantic lives only at the
HTTP boundary. The verb implementations (``verbs.py``) and the SSE event
emitter (``events.py``) operate on plain Python values; the route handlers
(``routes.py``) convert at the seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CancelOrderRequest(BaseModel):
    """``POST /control/cancel_order`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str = Field(min_length=1)


class ForceClosePositionRequest(BaseModel):
    """``POST /control/force_close_position`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SetHaltModeRequest(BaseModel):
    """``POST /control/set_halt_mode`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


ControlStatus = Literal["accepted"]
ControlErrorCode = Literal[
    "precondition_failed",
    "validation_failed",
    "not_found",
    "broker_error",
    "internal_error",
]


class ControlResponseEnvelope(BaseModel):
    """Successful response envelope for ``POST /control/*`` verbs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ControlStatus
    applied_at: datetime


class ForceClosePositionResponse(ControlResponseEnvelope):
    """``POST /control/force_close_position`` response — extends the envelope.

    Carries the synthesized engine-originated envelope's ID per
    ``docs/design/oms-command-ids.md`` § Engine-originated command IDs:
    ``MON.{monitor_session_id}.{trigger_id}``.
    """

    envelope_id: str = Field(pattern=r"^MON\.[^.]+\.[0-9]+$")


class ErrorBody(BaseModel):
    """Inner ``error`` object on ``ControlErrorEnvelope``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ControlErrorCode
    detail: str = Field(min_length=1)
    details: dict[str, Any] | None = None


class ControlErrorEnvelope(BaseModel):
    """Error response envelope for ``POST /control/*`` verbs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorBody


# ---------------------------------------------------------------------------
# SSE event payloads
# ---------------------------------------------------------------------------


BreachResponseClassification = Literal["immediate", "deferred"]


class WebsocketConnectedEvent(BaseModel):
    """``event: websocket_connected`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime


class WebsocketDisconnectedEvent(BaseModel):
    """``event: websocket_disconnected`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    reason: str = Field(min_length=1)


class FillReceivedEvent(BaseModel):
    """``event: fill_received`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str = Field(min_length=1)
    position_id: str = Field(min_length=1)
    fill_price: float = Field(gt=0)
    fill_qty: float


class BreachDetectedEvent(BaseModel):
    """``event: breach_detected`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str = Field(min_length=1)
    current_value: float
    limit: float
    response_classification: BreachResponseClassification


class EmergencyInvocationTriggeredEvent(BaseModel):
    """``event: emergency_invocation_triggered`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class GreeksRefreshedEvent(BaseModel):
    """``event: greeks_refreshed`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    underlying: str = Field(min_length=1)
    refreshed_at: datetime


class HeartbeatEvent(BaseModel):
    """``event: heartbeat`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime


__all__ = [
    "BreachDetectedEvent",
    "BreachResponseClassification",
    "CancelOrderRequest",
    "ControlErrorCode",
    "ControlErrorEnvelope",
    "ControlResponseEnvelope",
    "ControlStatus",
    "EmergencyInvocationTriggeredEvent",
    "ErrorBody",
    "FillReceivedEvent",
    "ForceClosePositionRequest",
    "ForceClosePositionResponse",
    "GreeksRefreshedEvent",
    "HeartbeatEvent",
    "SetHaltModeRequest",
    "WebsocketConnectedEvent",
    "WebsocketDisconnectedEvent",
]
