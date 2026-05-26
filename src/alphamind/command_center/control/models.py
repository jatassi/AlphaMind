"""Pydantic boundary models for ``/api/control/*`` (story 04a / ALP-668).

One class per request body, response envelope, and error envelope the
proxy speaks at the browser boundary. The wire shapes match the
upstream pipeline + monitor schemas exactly — the proxy is a faithful
relay of the upstream contract, augmented only by the
``OPERATOR_CONSOLE`` audit row and the session / CSRF gate.

Per the parent-issue invariant (Pydantic at boundaries only), nothing
in this module crosses into :mod:`alphamind.command_center.control.proxy`
or the client modules — those operate on the frozen-dataclass mirrors
in :mod:`alphamind.command_center._kernel.control`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = [
    "CancelOrderRequest",
    "ControlError",
    "ControlErrorEnvelope",
    "ControlResponseEnvelope",
    "ForceClosePositionRequest",
    "ForceClosePositionResponse",
    "PauseRequest",
    "ResumeRequest",
    "RunUniverseValidationRequest",
    "RunUniverseValidationResponse",
    "SetHaltModeRequest",
    "SwitchProfileRequest",
    "TriggerEmergencyInvocationRequest",
    "TriggerEmergencyInvocationResponse",
    "UniverseValidationCriterionRow",
    "UniverseValidationReport",
    "UniverseValidationTickerRow",
]


# ---------------------------------------------------------------------------
# Closed-set vocabularies — mirrors of the upstream schemas' enums.
# ---------------------------------------------------------------------------

_ErrorCode = Literal[
    "validation_failed",
    "not_found",
    "precondition_failed",
    "cooldown_active",
    "broker_error",
    "internal_error",
]
"""Union of the pipeline + monitor schemas' error codes.

The pipeline schema lists ``validation_failed / not_found /
precondition_failed / cooldown_active / internal_error``; the monitor
schema additionally lists ``broker_error``. The proxy emits the union so
the browser side has one closed vocabulary to render.
"""

_Verdict = Literal["pass", "fail", "unknown"]
_Criterion = Literal["adv", "analyst_coverage", "beta", "market_cap", "options_oi"]


# ---------------------------------------------------------------------------
# Request bodies — one per verb. ``extra='forbid'`` rejects unknown keys
# so a frontend typo fails loud at the boundary instead of silently
# becoming a no-op.
# ---------------------------------------------------------------------------


class PauseRequest(BaseModel):
    """``POST /api/control/pause`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class ResumeRequest(BaseModel):
    """``POST /api/control/resume`` request body — empty per upstream schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TriggerEmergencyInvocationRequest(BaseModel):
    """``POST /api/control/trigger_emergency_invocation`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)


class SwitchProfileRequest(BaseModel):
    """``POST /api/control/switch_profile`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_name: str = Field(min_length=1)


class RunUniverseValidationRequest(BaseModel):
    """``POST /api/control/run_universe_validation`` request body — empty per schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CancelOrderRequest(BaseModel):
    """``POST /api/control/cancel_order`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str = Field(min_length=1)


class ForceClosePositionRequest(BaseModel):
    """``POST /api/control/force_close_position`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SetHaltModeRequest(BaseModel):
    """``POST /api/control/set_halt_mode`` request body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class ControlResponseEnvelope(BaseModel):
    """Successful response envelope for verbs without verb-specific data.

    Mirrors the upstream pipeline + monitor schemas' shared shape:
    ``status: "accepted"`` + ``applied_at`` ISO-8601 timestamp. Verbs
    with verb-specific success payloads extend this envelope.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["accepted"]
    applied_at: AwareDatetime


class TriggerEmergencyInvocationResponse(ControlResponseEnvelope):
    """``trigger_emergency_invocation`` response — envelope + invocation id."""

    invocation_id: str = Field(min_length=1)


class UniverseValidationCriterionRow(BaseModel):
    """One criterion row per ticker in the universe-validation report.

    Mirrors :class:`alphamind.scheduler.control.models.UniverseValidationCriterionRow`.
    """

    model_config = ConfigDict(frozen=True)

    criterion: _Criterion
    verdict: _Verdict
    computed_value: float | dict[str, float] | None = None
    threshold: float | dict[str, float] | None = None
    note: str | None = None


class UniverseValidationTickerRow(BaseModel):
    """One ticker row in the universe-validation report.

    The upstream schema pins ``criteria`` at exactly 5 entries in the
    canonical order ``[adv, analyst_coverage, beta, market_cap,
    options_oi]``.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    verdict: _Verdict
    criteria: list[UniverseValidationCriterionRow] = Field(min_length=5, max_length=5)


class UniverseValidationReport(BaseModel):
    """Inline report shape carried on the ``run_universe_validation`` response."""

    model_config = ConfigDict(frozen=True)

    validated_at: AwareDatetime
    tickers: list[UniverseValidationTickerRow] = Field(min_length=1)


class RunUniverseValidationResponse(ControlResponseEnvelope):
    """``run_universe_validation`` response — extends the envelope with the report."""

    report: UniverseValidationReport


class ForceClosePositionResponse(ControlResponseEnvelope):
    """``force_close_position`` response — extends the envelope with the engine envelope id.

    The upstream monitor schema pins ``envelope_id`` to the format
    ``MON.{monitor_session_id}.{trigger_id}`` (see
    ``docs/design/oms-command-ids.md`` § Engine-originated command IDs).
    """

    envelope_id: str = Field(pattern=r"^MON\.[^.]+\.[0-9]+$")


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ControlError(BaseModel):
    """Inner ``error`` object carried inside :class:`ControlErrorEnvelope`."""

    model_config = ConfigDict(frozen=True)

    code: _ErrorCode
    detail: str = Field(min_length=1)
    details: dict[str, Any] | None = None


class ControlErrorEnvelope(BaseModel):
    """Error response envelope returned with HTTP 4xx / 5xx statuses.

    Mirrors the upstream shape — one ``error`` object with ``code`` /
    ``detail`` / optional ``details`` payload. The browser uses
    ``error.code`` as the user-facing error class and ``error.detail``
    as the human-readable explanation; ``error.details`` carries
    verb-specific structured fields (cooldown remaining seconds,
    running invocation id, broker message, etc.).
    """

    model_config = ConfigDict(frozen=True)

    error: ControlError
