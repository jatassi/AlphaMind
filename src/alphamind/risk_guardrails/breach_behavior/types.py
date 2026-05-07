"""Canonical typed records and enums for the breach-behavior package.

Single source of truth for the value objects every primitive in this work tree
consumes: zone classification, forced-reduction selection, drawdown halt mode,
emergency invocation, hard rejection, engine envelopes. Pure value objects —
either ``StrEnum`` or frozen Pydantic v2 models — with field/post validators
limited to invariants documented in
``docs/design/06-risk-guardrails/breach-behavior.md`` and
``docs/design/05-execution-layer/engine-envelope-schema.md``.

Upstream enums and records are re-exported (not redefined) so downstream
consumers have a single import surface
(``from alphamind.risk_guardrails.breach_behavior import RiskZone``).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from alphamind.config.models.guardrails import (
    BreachResponse,
    EnforcementTier,
    EscalationZones,
    ProgressiveTier,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionRecord,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeLabel,
    RegimeTransitionState,
)

# ---------------------------------------------------------------------------
# Enums introduced by this package
# ---------------------------------------------------------------------------


class DrawdownTier(StrEnum):
    """Cumulative drawdown progressive response tier."""

    CONSTRAINED = "CONSTRAINED"
    HEAVILY_CONSTRAINED = "HEAVILY_CONSTRAINED"
    FULL_HALT = "FULL_HALT"


class EmergencyTrigger(StrEnum):
    """Emergency invocation trigger taxonomy.

    Mirrors ``docs/design/06-risk-guardrails/breach-behavior.md`` §
    *Emergency invocation trigger*. The continuous monitor's emergency-trigger
    evaluator selects exactly one of these four when it interrupts the
    scheduled cadence.
    """

    REGIME_JUMP = "regime_jump"
    MULTI_RULE_BREACH = "multi_rule_breach"
    DAILY_DRAWDOWN_VELOCITY = "daily_drawdown_velocity"
    MARGIN_CALL = "margin_call"


class SecondaryBreachOutcome(StrEnum):
    """Outcome of a secondary-breach check on a proposed protective close.

    Mirrors the engine-envelope JSON Schema's
    ``secondary_breach_check_result.result`` enum verbatim.
    """

    NO_SECONDARY_BREACH = "no_secondary_breach"
    SECONDARY_BREACH_AVOIDED = "secondary_breach_avoided"
    DEFERRED_TO_PM = "deferred_to_pm"


class CloseRationaleType(StrEnum):
    """Close rationale type the engine envelope's embedded close command carries.

    Engine-originated envelopes are always ``risk_management`` per
    ``engine-envelope-schema.md``; PM-originated rationales (``thesis_invalidated``,
    ``target_reached``, ``conviction_reduced``) belong to the PM-envelope work
    tree and are not re-declared here.
    """

    RISK_MANAGEMENT = "risk_management"


class RiskManagementSubtype(StrEnum):
    """Sub-classification within the ``risk_management`` close rationale.

    Engine-originated envelopes always carry ``engine_guardrail``; the PM-side
    ``pm_directed`` value belongs to the PM-envelope work tree and is not
    re-declared here.
    """

    ENGINE_GUARDRAIL = "engine_guardrail"


# ---------------------------------------------------------------------------
# Halt-state typed record
# ---------------------------------------------------------------------------


class HaltState(BaseModel):
    """Active drawdown-halt state surfaced to state-delivery and the continuous monitor.

    Constructed only when at least one halt is active. Per
    ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Drawdown halt mode*,
    daily-halt fires when daily drawdown reaches 100% of the daily limit;
    cumulative full halt fires when cumulative drawdown reaches the tier-3
    threshold (``DrawdownTier.FULL_HALT``). Tier 1 and tier 2 cumulative
    drawdown responses apply progressive parameter overrides without blocking
    new positions and are *not* halts; their state is carried elsewhere.
    """

    model_config = ConfigDict(frozen=True)

    daily_halt_active: bool
    cumulative_full_halt_active: bool
    daily_drawdown_pct: float
    daily_drawdown_limit_pct: float

    @field_validator("daily_drawdown_pct", "daily_drawdown_limit_pct")
    @classmethod
    def _require_non_negative(cls, v: float) -> float:
        if v < 0:
            msg = f"value must be >= 0; got {v}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_at_least_one_active(self) -> HaltState:
        if not (self.daily_halt_active or self.cumulative_full_halt_active):
            msg = "HaltState should not be constructed unless at least one halt is active"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Emergency invocation context
# ---------------------------------------------------------------------------


class EmergencyContext(BaseModel):
    """Emergency-invocation trigger context surfaced to state-delivery and the scheduler.

    Constructed only when the continuous monitor's emergency-trigger evaluator
    fires. Per ``docs/design/06-risk-guardrails/breach-behavior.md`` §
    *Emergency invocation trigger*, ``trigger_detail`` is a human-readable
    string the strategist/PM consume in the modified guardrail state header.
    """

    model_config = ConfigDict(frozen=True)

    trigger: EmergencyTrigger
    trigger_detail: str
    minutes_since_last_invocation: float
    normal_cadence_minutes: float

    @field_validator("minutes_since_last_invocation", "normal_cadence_minutes")
    @classmethod
    def _require_positive(cls, v: float) -> float:
        if v <= 0:
            msg = f"value must be positive; got {v}"
            raise ValueError(msg)
        return v


# ---------------------------------------------------------------------------
# Engine envelope sub-records
# ---------------------------------------------------------------------------


def _require_tz_aware_datetime(v: datetime, *, field_name: str) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)
    return v


class BreachDetails(BaseModel):
    """Quantitative detail for one breach.

    Mirrors the engine-envelope JSON Schema's ``breach_details`` $def. The
    ``overage`` is signed: positive for overages on upper-bound rules and
    negative for deficit-direction breaches where the sign is more readable
    that way (e.g., per-position max loss).
    """

    model_config = ConfigDict(frozen=True)

    current_value: float
    limit_value: float
    overage: float
    unit: str | None = None
    regime_at_breach: RegimeLabel | None = None


class SecondaryBreachCheckResult(BaseModel):
    """Outcome (and optional notes) of a secondary-breach check.

    Mirrors the engine-envelope JSON Schema's ``secondary_breach_check_result``
    $def. Populated by the continuous monitor during position selection.
    """

    model_config = ConfigDict(frozen=True)

    result: SecondaryBreachOutcome
    notes: str | None = None


class EngineGuardrailTriggerRecord(BaseModel):
    """The breach context that motivated a protective CLOSE.

    Mirrors the engine-envelope JSON Schema's ``guardrail_trigger_record``
    $def. ``rule_breached`` carries the canonical rule ID from
    ``guardrails.yaml``; ``position_selection_rationale`` names the
    deterministic selection rule from ``breach-behavior.md``.
    """

    model_config = ConfigDict(frozen=True)

    rule_breached: str
    trigger_timestamp: datetime
    breach_details: BreachDetails
    position_selection_rationale: str
    cascade_id: str | None = None
    secondary_breach_check_result: SecondaryBreachCheckResult | None = None

    @field_validator("trigger_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        return _require_tz_aware_datetime(v, field_name="trigger_timestamp")


# ---------------------------------------------------------------------------
# EngineCloseCommand — embedded protective close
# ---------------------------------------------------------------------------


class EngineCloseCommand(BaseModel):
    """The protective CLOSE command embedded in an engine envelope.

    Carries only the fields the engine-envelope JSON Schema's allOf constraint
    enforces over the OMS close-command schema. The OMS-side full schema
    (``docs/design/05-execution-layer/oms-command-schema.md``) lands in the
    execution-layer work tree; this story exposes the breach-behavior-relevant
    subset so wave-4 stories can compose envelopes without waiting on the
    OMS work tree.
    """

    model_config = ConfigDict(frozen=True)

    command_id: str
    command_type: Literal["close"] = "close"
    close_rationale_type: CloseRationaleType = CloseRationaleType.RISK_MANAGEMENT
    risk_management_subtype: RiskManagementSubtype = RiskManagementSubtype.ENGINE_GUARDRAIL

    position_id: str
    quantity_or_all: Literal["all"] | float
    execution_method: Literal["market", "limit"] = "market"
    limit_price: float | None = None

    @model_validator(mode="after")
    def _validate_quantity(self) -> EngineCloseCommand:
        if isinstance(self.quantity_or_all, float) and self.quantity_or_all <= 0:
            msg = f"quantity_or_all must be positive when not 'all'; got {self.quantity_or_all}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_limit_price(self) -> EngineCloseCommand:
        if self.execution_method == "limit" and self.limit_price is None:
            msg = "limit_price required when execution_method is 'limit'"
            raise ValueError(msg)
        if self.execution_method == "market" and self.limit_price is not None:
            msg = "limit_price must be None when execution_method is 'market'"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# EngineEnvelope
# ---------------------------------------------------------------------------


_ENVELOPE_ID_PATTERN = re.compile(r"^MON\.[^.]+\.[0-9]+$")
"""Engine-originated envelope ID pattern: ``MON.{monitor_session_id}.{trigger_id}``.

Mirrors ``docs/design/05-execution-layer/engine-envelope-schema.md``'s
``envelope_id`` JSON Schema pattern verbatim. ``trigger_id`` is digits only;
``monitor_session_id`` carries any non-dot characters."""


class EngineEnvelope(BaseModel):
    """Engine-originated command envelope wrapping one protective CLOSE.

    Mirrors the engine-envelope JSON Schema. The Python-side ``command`` is a
    scalar field rather than a single-element array; the schema-aligned JSON
    serialization (``commands: [...]``) is the envelope-assembler's
    responsibility (story 06). One envelope per breach trigger; cascades
    produce multiple envelopes linked by ``cascade_id``, not multiple commands
    per envelope.
    """

    model_config = ConfigDict(frozen=True)

    envelope_id: str
    invocation_id: None = None
    trigger_timestamp: datetime
    source_provenance: Literal["engine_guardrail"] = "engine_guardrail"
    guardrail_trigger_record: EngineGuardrailTriggerRecord
    command: EngineCloseCommand

    @field_validator("envelope_id")
    @classmethod
    def _validate_envelope_id_pattern(cls, v: str) -> str:
        if not _ENVELOPE_ID_PATTERN.fullmatch(v):
            msg = f"envelope_id must match pattern {_ENVELOPE_ID_PATTERN.pattern!r}; got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("trigger_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        return _require_tz_aware_datetime(v, field_name="trigger_timestamp")

    @model_validator(mode="after")
    def _validate_trigger_timestamps_match(self) -> EngineEnvelope:
        if self.trigger_timestamp != self.guardrail_trigger_record.trigger_timestamp:
            msg = "trigger_timestamp must match guardrail_trigger_record.trigger_timestamp"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_command_id_envelope_relationship(self) -> EngineEnvelope:
        expected_prefix = f"{self.envelope_id}."
        if not self.command.command_id.startswith(expected_prefix):
            msg = (
                f"command.command_id must begin with {expected_prefix!r}; "
                f"got {self.command.command_id!r}"
            )
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Hard rejection payload
# ---------------------------------------------------------------------------


class RejectionRuleEntry(BaseModel):
    """One rule's entry in the hard rejection payload.

    Mirrors the per-rule projection shape from the guardrail-evaluation layer
    augmented with the PM-actionable fields documented in
    ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Hard rejection
    semantics*.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    current_value: float
    limit_value: float
    projected_after: float
    overage: float
    headroom_remaining: float
    unit: str


class HardRejectionPayload(BaseModel):
    """Synchronous T3 rejection payload returned to the PM.

    Per ``docs/design/06-risk-guardrails/breach-behavior.md`` § *Hard rejection
    semantics*. Carries every breaching rule, the PM-actionable suggested
    modification, and the projected post-compliance headroom.
    """

    model_config = ConfigDict(frozen=True)

    rejected_command_id: str
    breaching_rules: tuple[RejectionRuleEntry, ...]
    suggested_modification: str
    headroom_after_hypothetical_compliance: tuple[RejectionRuleEntry, ...]

    @model_validator(mode="after")
    def _validate_at_least_one_breaching_rule(self) -> HardRejectionPayload:
        if len(self.breaching_rules) == 0:
            msg = "HardRejectionPayload must carry at least one breaching rule"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# PositionSelectionAction / PositionSelectionResult
# ---------------------------------------------------------------------------


class PositionSelectionAction(StrEnum):
    """Action prescribed by a position-selection primitive (story 04d)."""

    FULL_CLOSE = "full_close"
    PARTIAL_TRIM = "partial_trim"


class PositionSelectionResult(BaseModel):
    """Output of a position-selection primitive.

    Carries the selected position, the prescribed action (full close or
    partial trim with target post-trim size), and the human-readable
    rationale that feeds
    ``EngineGuardrailTriggerRecord.position_selection_rationale``.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    action: PositionSelectionAction
    target_post_action_size_pct_of_portfolio: float | None = None
    rationale: str

    @model_validator(mode="after")
    def _validate_action_target_consistency(self) -> PositionSelectionResult:
        if (
            self.action == PositionSelectionAction.PARTIAL_TRIM
            and self.target_post_action_size_pct_of_portfolio is None
        ):
            msg = "target_post_action_size_pct_of_portfolio required for PARTIAL_TRIM"
            raise ValueError(msg)
        if (
            self.action == PositionSelectionAction.FULL_CLOSE
            and self.target_post_action_size_pct_of_portfolio is not None
        ):
            msg = "target_post_action_size_pct_of_portfolio must be None for FULL_CLOSE"
            raise ValueError(msg)
        return self


__all__ = [
    "BreachDetails",
    "BreachResponse",
    "CloseRationaleType",
    "Direction",
    "DrawdownTier",
    "EmergencyContext",
    "EmergencyTrigger",
    "EnforcementTier",
    "EngineCloseCommand",
    "EngineEnvelope",
    "EngineGuardrailTriggerRecord",
    "EscalationZones",
    "HaltState",
    "HardRejectionPayload",
    "InstrumentType",
    "PositionRecord",
    "PositionSelectionAction",
    "PositionSelectionResult",
    "ProgressiveTier",
    "RegimeLabel",
    "RegimeTransitionState",
    "RejectionRuleEntry",
    "RiskManagementSubtype",
    "RiskZone",
    "SecondaryBreachCheckResult",
    "SecondaryBreachOutcome",
]
