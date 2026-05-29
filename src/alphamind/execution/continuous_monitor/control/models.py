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

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# Event-payload + response-envelope datetime fields use AwareDatetime so a
# naive datetime is rejected at validation rather than silently emitted to
# the SSE wire / response body without timezone information (F8).

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
    applied_at: AwareDatetime


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

    timestamp: AwareDatetime


class WebsocketDisconnectedEvent(BaseModel):
    """``event: websocket_disconnected`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: AwareDatetime
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
    refreshed_at: AwareDatetime


class HeartbeatEvent(BaseModel):
    """``event: heartbeat`` payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: AwareDatetime


class BreachLoopDegradedEvent(BaseModel):
    """``event: breach_loop_degraded`` payload (ALP-732).

    Emitted once when the breach loop's consecutive-failure count crosses
    ``breach_loop_consecutive_failure_alert_threshold`` — risk supervision is
    silently down until a tick succeeds. ``last_error`` is the ``repr`` of the
    most recent failing tick's exception so the operator has a first lead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    consecutive_failures: int = Field(ge=1)
    last_error: str = Field(min_length=1)


class BreachLoopRecoveredEvent(BaseModel):
    """``event: breach_loop_recovered`` payload (ALP-732).

    Emitted on the first successful tick after a degraded run, clearing the
    degraded health state. ``consecutive_failures`` is the length of the
    failure run that just ended.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    consecutive_failures: int = Field(ge=1)


__all__ = [
    "BreachDetectedEvent",
    "BreachLoopDegradedEvent",
    "BreachLoopRecoveredEvent",
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
