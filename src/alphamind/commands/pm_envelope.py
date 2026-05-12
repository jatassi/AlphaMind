"""PMEnvelope wire-format types — the LLM↔engine envelope contract.

Typed Pydantic translation of
``docs/design/04-decision-layer/pm-envelope-schema.md``. The
:func:`envelope_schema` accessor returns the JSON-Schema shape; per parent
decision (D), the PM harness's ``output_format = json_schema`` mode targets
the :class:`PMCompletionRecord` sentinel — envelopes flow through the
``submit_envelope`` MCP tool, not the structured-output payload.

The PM envelope is a discriminated union over ``source_provenance``
(``pm_analyst`` / ``pm_strategist``); each variant carries verdict-conditional
invariants on its ``commands`` / ``modifications`` / ``concerns`` fields,
implemented via :func:`pydantic.model_validator`.

Hoisted from :mod:`alphamind.decision.portfolio_manager.models` to
:mod:`alphamind.commands` by ALP-458 to break the decision↔execution import
cycle. ``commands/`` modules import zero first-party ``alphamind.*`` modules
outside ``alphamind._kernel.*`` and ``alphamind.commands.*``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    TypeAdapter,
    model_validator,
)

from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
)

__all__ = [
    "AddCommand",
    "AdjustCommand",
    "AdjustmentCategory",
    "AntiPattern",
    "CancelCommand",
    "CloseCommand",
    "ConcernRecord",
    "CriterionAssessment",
    "ModificationRecord",
    "OMSCommand",
    "OpenCommand",
    "PMAnalystEnvelope",
    "PMCompletionRecord",
    "PMEnvelope",
    "PMStrategistEnvelope",
    "PositionActionEvaluation",
    "RecommendationType",
    "SourceProvenance",
    "ThesisQualityEvaluation",
    "Verdict",
    "VerdictSummary",
    "completion_record_schema",
    "envelope_schema",
]


# ---------------------------------------------------------------------------
# Vocabulary aliases — wire-format Literal types per pm-envelope-schema.md.
# ---------------------------------------------------------------------------


SourceProvenance = Literal["pm_analyst", "pm_strategist"]
RecommendationType = Literal["new_entry", "position_assessment", "pending_order_assessment"]
Verdict = Literal["approve", "approve_with_modification", "reject"]
AdjustmentCategory = Literal[
    "risk_reduction",
    "conviction_disagreement",
    "capital_constraint",
    "portfolio_balance",
    "guardrail_rejection_response",
]
AntiPattern = Literal[
    "conviction_inflation",
    "sunk_cost_persistence",
    "rationalized_continuation",
    "thesis_contradiction_suppression",
    "engine_originated_closure_signal",
]


# ---------------------------------------------------------------------------
# CriterionAssessment + evaluation containers
# ---------------------------------------------------------------------------


class CriterionAssessment(BaseModel):
    """Per-criterion pass/fail assessment with optional note."""

    model_config = ConfigDict(frozen=True)

    status: Literal["pass", "fail"]
    note: str | None = None


class ThesisQualityEvaluation(BaseModel):
    """Five-criterion evaluation for ``pm_analyst`` envelopes (new-entry proposals)."""

    model_config = ConfigDict(frozen=True)

    falsifiability: CriterionAssessment
    sizing_proportionality: CriterionAssessment
    portfolio_coherence: CriterionAssessment
    timing_plausibility: CriterionAssessment
    counterargument_consideration: CriterionAssessment


class PositionActionEvaluation(BaseModel):
    """Four-criterion evaluation for ``pm_strategist`` envelopes (position/order assessments)."""

    model_config = ConfigDict(frozen=True)

    status_classification_warrant: CriterionAssessment
    action_status_alignment: CriterionAssessment
    action_specific_justification: CriterionAssessment
    portfolio_coherence: CriterionAssessment


# ---------------------------------------------------------------------------
# ModificationRecord + ConcernRecord
# ---------------------------------------------------------------------------


class ModificationRecord(BaseModel):
    """One structured PM modification of an upstream proposal.

    Cross-field invariant: ``adjustment_category == "guardrail_rejection_response"``
    iff ``phase == "post_rejection"`` and ``triggering_rule`` is populated;
    every other ``adjustment_category`` requires ``phase == "pre_submission"``.
    """

    model_config = ConfigDict(frozen=True)

    phase: Literal["pre_submission", "post_rejection"]
    field_changed: str = Field(min_length=1)
    original_value: Any
    approved_value: Any
    adjustment_category: AdjustmentCategory
    rationale: str = Field(min_length=1)
    triggering_rule: str | None = None

    @model_validator(mode="after")
    def _validate_category_phase_pairing(self) -> ModificationRecord:
        if self.adjustment_category == "guardrail_rejection_response":
            if self.phase != "post_rejection":
                raise ValueError(
                    "ModificationRecord adjustment_category=guardrail_rejection_response "
                    "requires phase=post_rejection"
                )
            if self.triggering_rule is None:
                raise ValueError(
                    "ModificationRecord adjustment_category=guardrail_rejection_response "
                    "requires triggering_rule"
                )
        elif self.phase != "pre_submission":
            raise ValueError(
                f"ModificationRecord adjustment_category={self.adjustment_category} "
                "requires phase=pre_submission"
            )
        return self


class ConcernRecord(BaseModel):
    """One structured PM concern attached to an envelope.

    ``source`` matches an evaluation criterion key (e.g., ``falsifiability``)
    or ``"other"`` for concerns not captured by a named criterion.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1)
    summary: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Envelope variants
# ---------------------------------------------------------------------------


def _validate_verdict_invariants(
    verdict: Verdict,
    commands: tuple[Any, ...],
    modifications: tuple[ModificationRecord, ...],
    concerns: tuple[ConcernRecord, ...],
) -> None:
    """Shared verdict-conditional invariant check for both envelope variants."""
    if verdict == "reject":
        if len(commands) != 0:
            raise ValueError("verdict=reject requires empty commands")
        if len(modifications) != 0:
            raise ValueError("verdict=reject requires empty modifications")
        if len(concerns) < 1:
            raise ValueError("verdict=reject requires at least one concern")
    elif verdict == "approve":
        if len(modifications) != 0:
            raise ValueError("verdict=approve requires empty modifications")
    elif verdict == "approve_with_modification":
        if len(modifications) < 1:
            raise ValueError("verdict=approve_with_modification requires at least one modification")
        if len(commands) < 1:
            raise ValueError("verdict=approve_with_modification requires at least one command")


class PMAnalystEnvelope(BaseModel):
    """PM envelope wrapping an analyst new-entry proposal."""

    model_config = ConfigDict(frozen=True)

    envelope_id: str = Field(pattern=r"^ENV-REC-[0-9]+$")
    invocation_id: str = Field(min_length=1)
    source_provenance: Literal["pm_analyst"]
    source_recommendation_id: str = Field(pattern=r"^REC-[0-9]+$")
    recommendation_type: Literal["new_entry"]
    position_id: None = None
    verdict: Verdict
    evaluation: ThesisQualityEvaluation
    modifications: tuple[ModificationRecord, ...]
    concerns: tuple[ConcernRecord, ...]
    rationale_narrative: str = Field(min_length=1)
    anti_patterns_identified: tuple[AntiPattern, ...] | None = None
    commands: tuple[OMSCommand, ...]

    @model_validator(mode="after")
    def _validate_invariants(self) -> PMAnalystEnvelope:
        _validate_verdict_invariants(self.verdict, self.commands, self.modifications, self.concerns)
        return self


class PMStrategistEnvelope(BaseModel):
    """PM envelope wrapping a strategist position or pending-order assessment."""

    model_config = ConfigDict(frozen=True)

    envelope_id: str = Field(pattern=r"^ENV-(SA|SA-ORD)-[0-9]+$")
    invocation_id: str = Field(min_length=1)
    source_provenance: Literal["pm_strategist"]
    source_recommendation_id: str = Field(pattern=r"^SA(-ORD)?-[0-9]+$")
    recommendation_type: Literal["position_assessment", "pending_order_assessment"]
    position_id: str = Field(min_length=1)
    verdict: Verdict
    evaluation: PositionActionEvaluation
    modifications: tuple[ModificationRecord, ...]
    concerns: tuple[ConcernRecord, ...]
    rationale_narrative: str = Field(min_length=1)
    anti_patterns_identified: tuple[AntiPattern, ...] | None = None
    commands: tuple[OMSCommand, ...]

    @model_validator(mode="after")
    def _validate_invariants(self) -> PMStrategistEnvelope:
        _validate_verdict_invariants(self.verdict, self.commands, self.modifications, self.concerns)
        return self


PMEnvelope = Annotated[
    PMAnalystEnvelope | PMStrategistEnvelope,
    Discriminator("source_provenance"),
]


# ---------------------------------------------------------------------------
# PMCompletionRecord + VerdictSummary (sentinel target for json_schema mode)
# ---------------------------------------------------------------------------


class VerdictSummary(BaseModel):
    """Per-verdict counts emitted in the completion sentinel."""

    model_config = ConfigDict(frozen=True)

    approve: int = Field(ge=0)
    approve_with_modification: int = Field(ge=0)
    reject: int = Field(ge=0)


class PMCompletionRecord(BaseModel):
    """Thin completion sentinel — the JSON-Schema-mode target per parent decision (D).

    Envelopes flow through ``submit_envelope`` tool calls, not the structured
    output payload. The sentinel asserts only that the per-verdict counts sum
    to the reported envelope total.
    """

    model_config = ConfigDict(frozen=True)

    invocation_id: str = Field(min_length=1)
    timestamp: datetime
    envelopes_submitted: int = Field(ge=0)
    verdict_summary: VerdictSummary

    @model_validator(mode="after")
    def _validate_sum(self) -> PMCompletionRecord:
        total = (
            self.verdict_summary.approve
            + self.verdict_summary.approve_with_modification
            + self.verdict_summary.reject
        )
        if total != self.envelopes_submitted:
            raise ValueError(
                f"PMCompletionRecord verdict_summary sum ({total}) must equal "
                f"envelopes_submitted ({self.envelopes_submitted})"
            )
        return self


# ---------------------------------------------------------------------------
# Schema accessors
# ---------------------------------------------------------------------------


_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


def envelope_schema() -> dict[str, Any]:
    """Return the JSON Schema for a single PM envelope (discriminated union)."""
    schema = dict(_ENVELOPE_ADAPTER.json_schema())
    schema["title"] = "PMEnvelope"
    return schema


def completion_record_schema() -> dict[str, Any]:
    """Return the JSON Schema for the PM completion-sentinel record."""
    return PMCompletionRecord.model_json_schema()
