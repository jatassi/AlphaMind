"""StrategistOutput data model — ALP-303.

Typed in-memory representation of the strategist agent's output document. The
parser (ALP-305) coerces SDK ``structured_output`` payloads to
:class:`StrategistOutput`; the validator (ALP-306) consumes it for cross-field
checks. ``StrategistOutput.model_json_schema()`` is fed to the SDK's
``output_format={"type": "json_schema", ...}`` mode so the API enforces the
shape post-generation.

The schema lives at ``docs/design/04-decision-layer/strategist-output-schema.md``
and is the authoritative contract; this module is the typed code-side
expression of that schema. Cross-field invariants encoded as ``allOf`` blocks
in the schema are mirrored here as ``model_validator(mode="after")`` checks.

The risk-side 4-way sector taxonomy (``tech | semis | financials | energy``)
is used here, not the analysis-side 3-way.

The schema's ``thesis_status`` vocabulary is lowercase-hyphenated (``on-track``,
``at-risk``, ...). The ``ThesisStatus`` enum in
``alphamind.portfolio_state.records.theses`` carries a different vocabulary
(``ON_TRACK``, ``AT_RISK``, ...) for storage; the wire format is independent.
This module defines a ``ThesisStatus`` Literal that matches the schema.

``RuleProjection`` and ``Greeks`` are imported from
``alphamind.risk_guardrails.guardrail_evaluation`` rather than redefined.
Both are frozen dataclasses; :class:`GuardrailValidationResult` carries them
via ``arbitrary_types_allowed=True``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    field_validator,
    model_validator,
)

from alphamind._kernel.ids import (
    InvocationId,
    OrderId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.ids import (
    recommendation_id as _recommendation_id_constructor,
)
from alphamind.portfolio_state.views.thesis_health import ComponentHealthEntry
from alphamind.risk_guardrails.guardrail_evaluation import Greeks, RuleProjection

__all__ = [
    "ActionParameters",
    "AddParameters",
    "AdjustBracketParameters",
    "BracketAdjustNewStopLevel",
    "BracketAdjustNewTargetLevel",
    "CloseParameters",
    "ComponentHealthEntry",
    "DefensivePostureSummary",
    "EntryOrder",
    "ExposureImpact",
    "Greeks",
    "GuardrailValidationResult",
    "ModificationParameters",
    "NewEventInvalidation",
    "PendingOrderAssessment",
    "PortfolioLevelObservations",
    "PositionAssessment",
    "ReduceParameters",
    "ReductionPriorityEntry",
    "RegimeTransitionAddressedBreach",
    "RegimeTransitionSummary",
    "RegimeTransitionUncuredBreach",
    "RuleProjection",
    "Sector",
    "StrategistOutput",
    "ThesisComponentUpdate",
    "ThesisStatus",
]


# ---------------------------------------------------------------------------
# Wire-format enums (lowercase-hyphenated vocabulary, per schema).
# ---------------------------------------------------------------------------

Sector = Literal["tech", "semis", "financials", "energy"]
ThesisStatus = Literal["on-track", "partially-realized", "at-risk", "stale", "invalidated"]

_SA_ID_PATTERN = re.compile(r"^SA-[0-9]+$")


# ---------------------------------------------------------------------------
# ActionParameters — discriminated union on ``action``.
# ---------------------------------------------------------------------------


class CloseParameters(BaseModel):
    """``close`` action parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["close"]
    quantity: float | Literal["all"]
    order_type: Literal["market", "limit"]
    limit_price: float | None = Field(default=None, gt=0)
    close_rationale_type: Literal[
        "thesis_invalidated",
        "target_reached",
        "conviction_reduced",
        "risk_management",
    ]

    @model_validator(mode="after")
    def _validate_invariants(self) -> CloseParameters:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("CloseParameters.order_type=limit requires limit_price")
        if self.close_rationale_type == "conviction_reduced" and self.quantity == "all":
            raise ValueError(
                "CloseParameters.close_rationale_type=conviction_reduced "
                'forbids quantity="all" (partial-close only)'
            )
        if self.quantity != "all" and self.quantity <= 0:
            raise ValueError("CloseParameters.quantity must be > 0 or 'all'")
        return self


class ReduceParameters(BaseModel):
    """``reduce`` action parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["reduce"]
    quantity: float = Field(gt=0)
    order_type: Literal["market", "limit"]
    limit_price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_limit_price(self) -> ReduceParameters:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("ReduceParameters.order_type=limit requires limit_price")
        return self


class BracketAdjustNewStopLevel(BaseModel):
    """``new_stop_level`` block of an adjust-bracket parameter set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_price: float = Field(gt=0)
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    limit_price: float | None = Field(default=None, gt=0)


class BracketAdjustNewTargetLevel(BaseModel):
    """``new_target_level`` block of an adjust-bracket parameter set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: float = Field(gt=0)
    order_type: Literal["market", "limit"]


class NewEventInvalidation(BaseModel):
    """``new_event_invalidation`` block of an adjust-bracket parameter set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_description: str = Field(min_length=1)


class ThesisComponentUpdate(BaseModel):
    """One thesis component update inside ``thesis_component_updates``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_type: Literal["entry_rationale", "target_rationale", "invalidation_rationale"]
    narrative: str = Field(min_length=1)
    key_assumptions: tuple[str, ...] | None = None


class AdjustBracketParameters(BaseModel):
    """``adjust-bracket`` action parameters.

    Schema's ``anyOf`` requires at least one of ``new_stop_level``,
    ``new_target_level``, ``new_time_expiration``, ``new_event_invalidation``,
    or ``thesis_component_updates`` to be present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["adjust-bracket"]
    new_stop_level: BracketAdjustNewStopLevel | None = None
    new_target_level: BracketAdjustNewTargetLevel | None = None
    new_time_expiration: datetime | None = None
    new_event_invalidation: NewEventInvalidation | None = None
    thesis_component_updates: tuple[ThesisComponentUpdate, ...] | None = None

    @model_validator(mode="after")
    def _validate_at_least_one_field(self) -> AdjustBracketParameters:
        any_present = any(
            getattr(self, name) is not None
            for name in (
                "new_stop_level",
                "new_target_level",
                "new_time_expiration",
                "new_event_invalidation",
                "thesis_component_updates",
            )
        )
        if not any_present:
            raise ValueError(
                "AdjustBracketParameters requires at least one of "
                "new_stop_level, new_target_level, new_time_expiration, "
                "new_event_invalidation, thesis_component_updates"
            )
        return self


class EntryOrder(BaseModel):
    """``entry_order`` block of an add parameter set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["market", "limit", "stop_limit"]
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)


class AddParameters(BaseModel):
    """``add`` action parameters.

    ``bracket_adjustment`` mirrors :class:`AdjustBracketParameters` for
    optional bracket revisions when the add changes the position's average
    cost basis.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["add"]
    additional_quantity: float = Field(gt=0)
    additional_dollar_value: float = Field(gt=0)
    entry_order: EntryOrder
    bracket_adjustment: AdjustBracketParameters | None = None


ActionParameters = Annotated[
    CloseParameters | ReduceParameters | AdjustBracketParameters | AddParameters,
    Discriminator("action"),
]


# ---------------------------------------------------------------------------
# Exposure and guardrail validation
# ---------------------------------------------------------------------------


class ExposureImpact(BaseModel):
    """Per-assessment exposure delta.

    ``sector_delta_adjusted_change`` is negative for close/reduce, positive
    for add. ``net_directional_impact`` is computed from current position data
    for close/reduce and populated by the guardrail validation tool for add.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sector_delta_adjusted_change: float
    net_directional_impact: float


class GuardrailValidationResult(BaseModel):
    """Result of the pre-submission guardrail validation tool call.

    Populated by the tool, not by the LLM. Mirrors analyst-side shape; reuses
    the canonical :class:`RuleProjection` dataclass for per-rule entries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    overall: Literal["PASS", "FAIL"]
    per_rule: tuple[RuleProjection, ...]
    delta_adjusted_exposure: float | None = None
    greeks: Greeks | None = None
    cumulative_impact_note: str | None = None
    checked_at: datetime

    @model_validator(mode="after")
    def _checked_at_tz_aware(self) -> GuardrailValidationResult:
        if self.checked_at.tzinfo is None:
            raise ValueError("GuardrailValidationResult.checked_at must be tz-aware")
        return self


# ---------------------------------------------------------------------------
# PositionAssessment
# ---------------------------------------------------------------------------


class PositionAssessment(BaseModel):
    """One per-position assessment in the strategist output."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    assessment_id: RecommendationId = Field(pattern=r"^SA-[0-9]+$")
    position_id: PositionId = Field(min_length=1)
    thesis_id: ThesisId = Field(min_length=1)
    underlying: Symbol = Field(min_length=1)
    sector: Sector
    thesis_status: ThesisStatus
    prior_status: ThesisStatus | None = None
    recommended_action: Literal["hold", "reduce", "close", "adjust-bracket", "add"]
    action_parameters: ActionParameters | None = None
    exposure_impact: ExposureImpact | None = None
    guardrail_validation_result: GuardrailValidationResult | None = None
    remedy_flag: str | None = None
    status_rationale: str = Field(min_length=1)
    action_rationale: str = Field(min_length=1)
    reduce_rationale: str | None = None
    add_conviction_justification: str | None = None
    adjustment_rationale: str | None = None
    remedy_rationale: str | None = None
    cross_position_observations: str | None = None
    # ALP-351: per-invocation supporting-signal re-assessment, one entry per
    # active thesis component. Mirrors the data carried on
    # ``ThesisHealthSnapshot.component_health``; the typed shape is the schema.
    component_health: tuple[ComponentHealthEntry, ...] = ()

    @field_validator("assessment_id", mode="after")
    @classmethod
    def _construct_assessment_id(cls, value: str) -> RecommendationId:
        return _recommendation_id_constructor(value)

    @model_validator(mode="after")
    def _validate_invariants(self) -> PositionAssessment:
        self._check_invalidated_requires_close()
        self._check_action_parameters_match()
        self._check_action_specific_requirements()
        self._check_remedy_pairing()
        return self

    def _check_invalidated_requires_close(self) -> None:
        if self.thesis_status == "invalidated" and self.recommended_action != "close":
            raise ValueError(
                "PositionAssessment thesis_status=invalidated requires recommended_action=close"
            )

    def _check_action_parameters_match(self) -> None:
        if self.recommended_action == "hold":
            if self.action_parameters is not None:
                raise ValueError(
                    "PositionAssessment recommended_action=hold forbids action_parameters"
                )
            return
        if self.action_parameters is None:
            raise ValueError(
                f"PositionAssessment recommended_action={self.recommended_action} "
                "requires action_parameters"
            )
        # Discriminator guarantees the field name match; double-check defensively.
        if self.action_parameters.action != self.recommended_action:
            raise ValueError(
                f"PositionAssessment action_parameters.action="
                f"{self.action_parameters.action!r} must match "
                f"recommended_action={self.recommended_action!r}"
            )

    def _check_action_specific_requirements(self) -> None:
        # Per the schema's allOf blocks: which sibling fields are required per
        # non-hold action. Each tuple is (attribute name on this model).
        required_fields: dict[str, tuple[str, ...]] = {
            "reduce": ("exposure_impact", "reduce_rationale"),
            "close": ("exposure_impact",),
            "adjust-bracket": ("adjustment_rationale",),
            "add": (
                "exposure_impact",
                "guardrail_validation_result",
                "add_conviction_justification",
            ),
        }
        for field in required_fields.get(self.recommended_action, ()):
            if getattr(self, field) is None:
                raise ValueError(
                    f"PositionAssessment recommended_action="
                    f"{self.recommended_action} requires {field}"
                )

    def _check_remedy_pairing(self) -> None:
        if self.remedy_flag is not None and self.remedy_rationale is None:
            raise ValueError("PositionAssessment remedy_flag requires remedy_rationale")


# ---------------------------------------------------------------------------
# PendingOrderAssessment
# ---------------------------------------------------------------------------


class ModificationParameters(BaseModel):
    """``modification_parameters`` block of a pending-order assessment.

    Schema's ``anyOf`` requires at least one of ``new_limit_price``,
    ``new_trigger_price``, ``new_deadline``, ``new_order_type`` to be present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_limit_price: float | None = Field(default=None, gt=0)
    new_trigger_price: float | None = Field(default=None, gt=0)
    new_deadline: datetime | None = None
    new_order_type: Literal["market", "limit", "stop", "stop_limit"] | None = None

    @model_validator(mode="after")
    def _validate_at_least_one(self) -> ModificationParameters:
        any_present = any(
            getattr(self, name) is not None
            for name in (
                "new_limit_price",
                "new_trigger_price",
                "new_deadline",
                "new_order_type",
            )
        )
        if not any_present:
            raise ValueError(
                "ModificationParameters requires at least one of new_limit_price, "
                "new_trigger_price, new_deadline, new_order_type"
            )
        return self


class PendingOrderAssessment(BaseModel):
    """One per-order assessment in the strategist output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending_order_assessment_id: RecommendationId = Field(pattern=r"^SA-ORD-[0-9]+$")
    order_id: OrderId = Field(min_length=1)
    position_id: PositionId = Field(min_length=1)
    order_type: Literal[
        "entry_limit",
        "entry_stop_limit",
        "bracket_target",
        "bracket_price_stop",
        "bracket_time_stop",
        "bracket_event_stop",
    ]
    order_age_hours: float = Field(ge=0)
    current_distance_pct: float | None = None
    fill_probability_assessment: Literal["likely_soon", "plausible", "unlikely"]
    recommended_action: Literal["maintain", "modify", "cancel"]
    modification_parameters: ModificationParameters | None = None
    linked_position_assessment_id: RecommendationId | None = Field(
        default=None, pattern=r"^SA-[0-9]+$"
    )
    drift_rationale: str = Field(min_length=1)
    action_rationale: str = Field(min_length=1)

    @field_validator("pending_order_assessment_id", "linked_position_assessment_id", mode="after")
    @classmethod
    def _construct_assessment_id(cls, value: str | None) -> RecommendationId | None:
        if value is None:
            return None
        return _recommendation_id_constructor(value)

    @model_validator(mode="after")
    def _validate_modify_requires_parameters(self) -> PendingOrderAssessment:
        if self.recommended_action == "modify" and self.modification_parameters is None:
            raise ValueError(
                "PendingOrderAssessment recommended_action=modify requires modification_parameters"
            )
        return self


# ---------------------------------------------------------------------------
# Portfolio-level observations
# ---------------------------------------------------------------------------


class RegimeTransitionAddressedBreach(BaseModel):
    """One addressed breach inside :class:`RegimeTransitionSummary`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    breach_id: str = Field(min_length=1)
    remedy_assessment_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_assessment_id_pattern(self) -> RegimeTransitionAddressedBreach:
        for sa_id in self.remedy_assessment_ids:
            if not _SA_ID_PATTERN.match(sa_id):
                raise ValueError(f"remedy_assessment_ids entry {sa_id!r} must match ^SA-[0-9]+$")
        return self


class RegimeTransitionUncuredBreach(BaseModel):
    """One uncured breach inside :class:`RegimeTransitionSummary`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    breach_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class RegimeTransitionSummary(BaseModel):
    """Cross-reference of which breaches are addressed by which remedies.

    Present when the guardrail state header flagged regime-transition or
    market-movement breaches.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    addressed_breaches: tuple[RegimeTransitionAddressedBreach, ...]
    uncured_breaches: tuple[RegimeTransitionUncuredBreach, ...]


class ReductionPriorityEntry(BaseModel):
    """One position in the defensive-posture reduction priority list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position_id: PositionId = Field(min_length=1)
    priority_rationale: str = Field(min_length=1)


class DefensivePostureSummary(BaseModel):
    """Required when mode is ``defensive_posture``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reduction_priority: tuple[ReductionPriorityEntry, ...]
    capital_preservation_notes: str = Field(min_length=1)


class PortfolioLevelObservations(BaseModel):
    """Book-wide narrative observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aggregate_thesis_health: str = Field(min_length=1)
    sector_balance_shifts: str = Field(min_length=1)
    thesis_dependency_warnings: str = Field(min_length=1)
    capital_allocation_observations: str = Field(min_length=1)
    regime_transition_summary: RegimeTransitionSummary | None = None
    defensive_posture_summary: DefensivePostureSummary | None = None


# ---------------------------------------------------------------------------
# StrategistOutput root
# ---------------------------------------------------------------------------


class StrategistOutput(BaseModel):
    """Single strategist invocation's output document.

    Mode-conditional invariants enforced here:

    * ``mode="defensive_posture"`` requires
      ``portfolio_level_observations.defensive_posture_summary`` to be set
      and restricts each ``position_assessments[*].recommended_action`` to
      ``hold | reduce | close | adjust-bracket`` (no ``add``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    invocation_id: InvocationId = Field(min_length=1)
    timestamp: datetime
    mode: Literal["normal", "defensive_posture"]
    position_assessments: tuple[PositionAssessment, ...]
    pending_order_assessments: tuple[PendingOrderAssessment, ...]
    portfolio_level_observations: PortfolioLevelObservations

    @model_validator(mode="after")
    def _validate_invariants(self) -> StrategistOutput:
        if self.timestamp.tzinfo is None:
            raise ValueError("StrategistOutput.timestamp must be tz-aware")
        if self.mode == "defensive_posture":
            if self.portfolio_level_observations.defensive_posture_summary is None:
                raise ValueError(
                    "StrategistOutput mode=defensive_posture requires "
                    "portfolio_level_observations.defensive_posture_summary"
                )
            for pa in self.position_assessments:
                if pa.recommended_action == "add":
                    raise ValueError(
                        "StrategistOutput mode=defensive_posture forbids "
                        f"recommended_action=add (assessment {pa.assessment_id})"
                    )
        return self
