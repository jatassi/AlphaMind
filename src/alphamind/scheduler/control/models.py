"""Pydantic boundary models for the pipeline ``/control`` + ``/events`` surface.

One class per request body / response envelope / error envelope /
per-event payload defined in
``docs/design/pipeline-control-and-events-schema.md`` § ``$defs``.  These
models are the surface contract — they validate inbound HTTP bodies,
serialize outbound responses, and shape the per-event payloads written
into the SSE stream.

Per the ALP-128 architectural invariants (Pydantic at boundaries only),
nothing in this module crosses into ``verbs.py`` or the event emitter —
route handlers convert Pydantic ↔ frozen-dataclass at the boundary.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# Event-payload + response-envelope datetime fields use AwareDatetime so a
# naive datetime is rejected at validation rather than silently emitted to
# the SSE wire / response body without timezone information (F8).

__all__ = [
    "AgentFailedEvent",
    "AgentRetryingEvent",
    "AgentStartedEvent",
    "AgentSucceededEvent",
    "ControlError",
    "ControlErrorEnvelope",
    "ControlResponseEnvelope",
    "HeartbeatEvent",
    "InvocationEndedEvent",
    "InvocationStartedEvent",
    "NextTriggerChangedEvent",
    "PauseRequest",
    "PhaseTransitionEvent",
    "ResumeRequest",
    "RunUniverseValidationRequest",
    "RunUniverseValidationResponse",
    "SwitchProfileRequest",
    "TokensUsed",
    "TriggerEmergencyInvocationRequest",
    "TriggerEmergencyInvocationResponse",
    "UniverseValidationCriterionRow",
    "UniverseValidationReport",
    "UniverseValidationTickerRow",
]


# Schema enum aliases — keep the wire vocabulary in one place so each
# event model imports its closed set from a single literal.
_RunType = Literal[
    "market_hours_rolling",
    "off_hours_rolling",
    "market_open",
    "pre_close",
    "weekend_saturday",
    "weekend_sunday",
    "emergency",
]
_Phase = Literal["collect", "distill", "analyze", "decide", "execute"]
_AgentName = Literal[
    "domain_researcher_tech_semis",
    "domain_researcher_financials",
    "domain_researcher_energy",
    "qualitative_researcher",
    "adaptive_researcher",
    "synthesizer",
    "analyst",
    "strategist",
    "portfolio_manager",
]
_FailureMode = Literal[
    "timeout",
    "malformed_output",
    "context_overflow",
    "model_api_error",
    "tool_use_error",
]
_InvocationEndedStatus = Literal[
    "completed",
    "failed",
    "partial",
    "skipped_paused",
]
_ErrorCode = Literal[
    "precondition_failed",
    "validation_failed",
    "not_found",
    "cooldown_active",
    "internal_error",
]
_Verdict = Literal["pass", "fail", "unknown"]
_Criterion = Literal["adv", "analyst_coverage", "beta", "market_cap", "options_oi"]


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class PauseRequest(BaseModel):
    """``POST /control/pause`` request body.

    Schema:
    ``required: ["reason"]`` with ``reason`` ``minLength: 1``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    """``POST /control/resume`` request body — empty per schema.

    Schema sets ``additionalProperties: false``; an empty JSON body
    ``{}`` round-trips to this model with no fields.  Any included
    field is rejected as a validation error.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class TriggerEmergencyInvocationRequest(BaseModel):
    """``POST /control/trigger_emergency_invocation`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class SwitchProfileRequest(BaseModel):
    """``POST /control/switch_profile`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_name: str = Field(min_length=1)


class RunUniverseValidationRequest(BaseModel):
    """``POST /control/run_universe_validation`` request body — empty per schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class ControlResponseEnvelope(BaseModel):
    """Successful response envelope returned by verbs without verb-specific data.

    Verbs with verb-specific response data (``trigger_emergency_invocation``,
    ``run_universe_validation``) extend this envelope.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["accepted"]
    applied_at: AwareDatetime


class TriggerEmergencyInvocationResponse(ControlResponseEnvelope):
    """``trigger_emergency_invocation`` response.

    Extends the envelope with the assigned invocation ID — the caller
    correlates downstream ``invocation_started`` / ``invocation_ended``
    events on ``/events`` by this ID.
    """

    invocation_id: str


class UniverseValidationCriterionRow(BaseModel):
    """One criterion row per ticker in the universe-validation report."""

    model_config = ConfigDict(frozen=True)

    criterion: _Criterion
    verdict: _Verdict
    computed_value: float | dict[str, float] | None = None
    threshold: float | dict[str, float] | None = None
    note: str | None = None


class UniverseValidationTickerRow(BaseModel):
    """One ticker row in the universe-validation report.

    Schema:
    ``criteria`` carries exactly 5 entries in the order
    ``[adv, analyst_coverage, beta, market_cap, options_oi]``.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    verdict: _Verdict
    criteria: list[UniverseValidationCriterionRow] = Field(min_length=5, max_length=5)


class UniverseValidationReport(BaseModel):
    """Inline report shape for ``run_universe_validation_response``."""

    model_config = ConfigDict(frozen=True)

    validated_at: AwareDatetime
    tickers: list[UniverseValidationTickerRow] = Field(min_length=1)


class RunUniverseValidationResponse(ControlResponseEnvelope):
    """``run_universe_validation`` response — extends the envelope with the report."""

    report: UniverseValidationReport


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ControlError(BaseModel):
    """Inner error object carried inside :class:`ControlErrorEnvelope`."""

    model_config = ConfigDict(frozen=True)

    code: _ErrorCode
    detail: str
    details: dict[str, Any] | None = None


class ControlErrorEnvelope(BaseModel):
    """Error response envelope returned with HTTP 4xx/5xx.

    The command-center backend renders ``error.code`` as the user-facing
    error class and ``error.detail`` as the human-readable explanation;
    ``error.details`` carries verb-specific structured fields (cooldown
    remaining seconds, running invocation id, etc.).
    """

    model_config = ConfigDict(frozen=True)

    error: ControlError


# ---------------------------------------------------------------------------
# Event payloads — one Pydantic class per `oneOf` member in the events schema.
# ---------------------------------------------------------------------------


class TokensUsed(BaseModel):
    """Token accounting carried on :class:`AgentSucceededEvent`.

    ``input`` and ``output`` are always present; ``cache_read`` and
    ``cache_creation`` are populated only when prompt caching applied
    (Anthropic SDK).  Per the schema's cross-field invariant,
    ``cache_read + cache_creation <= input``.
    """

    model_config = ConfigDict(frozen=True)

    input: int = Field(ge=0)
    output: int = Field(ge=0)
    cache_read: int | None = Field(default=None, ge=0)
    cache_creation: int | None = Field(default=None, ge=0)


class InvocationStartedEvent(BaseModel):
    """Emitted when a pipeline invocation begins (any ``run_type``)."""

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    run_type: _RunType
    started_at: AwareDatetime


class PhaseTransitionEvent(BaseModel):
    """Emitted when the pipeline enters a new phase.

    Phases advance strictly through the enum order
    (``collect → distill → analyze → decide → execute``).
    """

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    phase: _Phase
    phase_started_at: AwareDatetime


class AgentStartedEvent(BaseModel):
    """Emitted when an LLM agent begins."""

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    agent_name: _AgentName
    started_at: AwareDatetime
    latency_budget_seconds: float = Field(gt=0)


class AgentSucceededEvent(BaseModel):
    """Emitted when an LLM agent returns valid output."""

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    agent_name: _AgentName
    duration_seconds: float = Field(ge=0)
    tokens_used: TokensUsed


class AgentRetryingEvent(BaseModel):
    """Emitted when an LLM agent retries.

    ``attempt`` starts at 2 (the first invocation is attempt 1 and does
    NOT emit ``agent_retrying``).  Caps at 2 for malformed-output and
    timeout retries; model-API-error retries follow the Critical-tier
    shape.
    """

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    agent_name: _AgentName
    attempt: int = Field(ge=2)
    reason: _FailureMode


class AgentFailedEvent(BaseModel):
    """Emitted when an LLM agent's retries are exhausted."""

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    agent_name: _AgentName
    failure_mode: _FailureMode


class InvocationEndedEvent(BaseModel):
    """Emitted when a pipeline invocation reaches a terminal state."""

    model_config = ConfigDict(frozen=True)

    invocation_id: str
    status: _InvocationEndedStatus
    commands_issued: int = Field(ge=0)


class NextTriggerChangedEvent(BaseModel):
    """Emitted when APScheduler's next-trigger preview changes.

    Fires on pause, resume, emergency consumption, and schedule edits.
    """

    model_config = ConfigDict(frozen=True)

    next_trigger_at: AwareDatetime
    next_trigger_type: _RunType


class HeartbeatEvent(BaseModel):
    """Emitted every 15 s when no other event has been emitted on a connection.

    The command-center backend's reconnect logic uses idle-timeout > 30s
    as its disconnect signal.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: AwareDatetime
