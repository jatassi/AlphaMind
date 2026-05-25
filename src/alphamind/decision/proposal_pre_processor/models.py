"""ProposalPreProcessorBundle data model — ALP-313.

Typed in-memory representation of the proposal pre-processor's output bundle.
The schema lives at
``docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md`` and
is the authoritative contract; this module is the typed code-side expression
of that schema.

Inner records (``Recommendation``, ``WatchlistEntry``, ``PositionAssessment``,
``PendingOrderAssessment``, ``PortfolioLevelObservations``) are imported from
:mod:`alphamind.decision.analyst` and :mod:`alphamind.decision.strategist` and
held verbatim inside wrapper envelopes — never redefined here.

``BUNDLE_OUTPUT_SCHEMA`` is the JSON Schema (Draft 2020-12) produced by
calling ``ProposalPreProcessorBundle.model_json_schema()``.  ``bundle_schema``
is the callable form, consistent with the pattern used by downstream tools.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind.decision.analyst.models import Recommendation, WatchlistEntry
from alphamind.decision.strategist.models import (
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
)

_REC_ID_PATTERN = re.compile(r"^REC-[0-9]+$")
_SA_ID_PATTERN = re.compile(r"^SA-[0-9]+$")


__all__ = [
    "BUNDLE_OUTPUT_SCHEMA",
    "AggregateObservations",
    "AnalystSection",
    "AnalystSideAnnotations",
    "AnalystSideConflict",
    "BasisSection",
    "BookHealthSummary",
    "BreachEntry",
    "ByRecommendedAction",
    "ByThesisStatus",
    "CombinedSetImpact",
    "ConflictType",
    "ContributorEntry",
    "ConvictionDistribution",
    "ConvictionHistogram",
    "PerRuleEntry",
    "ProposalPreProcessorBundle",
    "StrategistSection",
    "StrategistSideAnnotations",
    "StrategistSideConflict",
    "WrappedPendingOrderAssessment",
    "WrappedPositionAssessment",
    "WrappedRecommendation",
    "bundle_schema",
]


# ---------------------------------------------------------------------------
# ConflictType
# ---------------------------------------------------------------------------


class ConflictType(StrEnum):
    """Classification of the same-underlying interaction.

    See ``proposal-pre-processor.md`` § Wrap pattern and conflicts annotation.
    Member names equal their value strings.
    """

    entry_vs_close = "entry_vs_close"
    entry_vs_add = "entry_vs_add"
    entry_vs_hold = "entry_vs_hold"
    entry_direction_conflict = "entry_direction_conflict"
    entry_vs_pending_maintain = "entry_vs_pending_maintain"
    entry_vs_pending_modify = "entry_vs_pending_modify"
    entry_vs_pending_cancel = "entry_vs_pending_cancel"


# ---------------------------------------------------------------------------
# §1. BasisSection and CombinedSetImpact
# ---------------------------------------------------------------------------


class BasisSection(BaseModel):
    """Basis block inside ``combined_set_impact``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_proposal_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Analyst recommendations included in the projection. "
            "Empty when mode is watchlist or no recommendations were produced."
        ),
    )
    strategist_action_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Strategist position assessments whose recommended_action is non-hold. "
            "Holds do not affect exposure and are excluded from the projection."
        ),
    )
    strategist_holds_excluded_count: int = Field(
        ge=0,
        description=(
            "Number of strategist position assessments excluded from the projection "
            "because their recommended_action is hold."
        ),
    )
    snapshot_timestamp: datetime = Field(
        description="Phase 1 portfolio state snapshot used as the projection baseline."
    )

    @model_validator(mode="after")
    def _validate_id_patterns(self) -> BasisSection:
        for pid in self.analyst_proposal_ids:
            if not _REC_ID_PATTERN.match(pid):
                raise ValueError(f"analyst_proposal_ids entry {pid!r} must match ^REC-[0-9]+$")
        for sid in self.strategist_action_ids:
            if not _SA_ID_PATTERN.match(sid):
                raise ValueError(f"strategist_action_ids entry {sid!r} must match ^SA-[0-9]+$")
        return self


class ContributorEntry(BaseModel):
    """Per-proposal attribution inside a breach entry.

    Contribution semantics vary by rule type:

    * **Sum-of-contributions rules** (the default — e.g., ``net_long_pct``,
      ``sector_concentration_*``): signed contribution to ``projected_after``.
      Positive when the proposal pushes the rule toward breach, negative
      when it pulls the rule away. Summing contributions equals the rule's
      delta from ``current``.
    * **Holistic rules** (rules with a ``RuleSpec.project_after_batch``
      projector — e.g., ``position_max_size_pct``): contribution is the
      post-batch shape the proposal helped produce, not a signed delta.
      For ``position_max_size_pct`` each proposal that shaped a position
      tied at the post-batch maximum emits the same ``contribution =
      post-batch max as % of portfolio``. Contributions for holistic
      rules don't sum to ``projected_after`` and the sign does not encode
      toward-vs-away-from-breach (ALP-636).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(
        pattern=r"^(REC|SA)-[0-9]+$",
        description="Analyst recommendation ID (REC-N) or strategist assessment ID (SA-N).",
    )
    contribution: float = Field(
        description=(
            "Per-proposal attribution. For sum-of-contributions rules: signed "
            "contribution to projected_after, positive toward breach. For "
            "holistic rules (e.g., position_max_size_pct): the post-batch "
            "shape the proposal helped produce — see the class docstring."
        )
    )


class PerRuleEntry(BaseModel):
    """One entry in the per-rule impact table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(description="Canonical rule identifier from rules-and-limits.md.")
    status: Literal["PASS", "WARNING", "FAIL"] = Field(
        description="Per-rule decision after applying the combined set."
    )
    current: float = Field(description="Rule value before applying the combined set.")
    limit: float = Field(description="Rule limit under the active regime.")
    projected_after: float = Field(description="Rule value after applying the combined set.")
    headroom_remaining: float = Field(
        description=(
            "Direction-aware signed slack carried verbatim from the upstream "
            "``RuleProjection.headroom_remaining``. Positive means compliant in "
            "either direction; the magnitude is slack on PASS/WARNING or the "
            "shortfall/overage on FAIL. For cap-style rules it is "
            "``limit - measured`` (with ``measured = |projected_after|`` for "
            "magnitude rules); for inverse (floor) rules it is "
            "``projected_after - limit``. Downstream consumers must read this "
            "field rather than recomputing from ``projected_after`` vs "
            "``limit`` — direction- and escalation-blind arithmetic produces "
            "wrong verdicts on inverse rules and hard-block-zone FAILs (see "
            "ALP-622)."
        ),
    )
    unit: str


class BreachEntry(BaseModel):
    """A single rule breach augmented with contributor attribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(
        description="Matches the rule field of a per_rule entry whose status is FAIL."
    )
    overage: float = Field(description="projected_after - limit. Always positive for a breach.")
    unit: str
    contributors: tuple[ContributorEntry, ...] = Field(
        description="Signed per-proposal attribution."
    )


class CombinedSetImpact(BaseModel):
    """Combined guardrail impact across all analyst and strategist proposals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: BasisSection
    per_rule: tuple[PerRuleEntry, ...]
    breaches: tuple[BreachEntry, ...]


# ---------------------------------------------------------------------------
# §2. Histogram models
# ---------------------------------------------------------------------------


class ConvictionHistogram(BaseModel):
    """Histogram of analyst recommendations by conviction level (1-5).

    Non-Python-identifier keys require ``Field(alias=...)``.
    ``populate_by_name=True`` allows setting by both name and alias.
    Serialization uses ``by_alias=True`` to produce the schema-conformant keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    level_1: int = Field(alias="1", ge=0)
    level_2: int = Field(alias="2", ge=0)
    level_3: int = Field(alias="3", ge=0)
    level_4: int = Field(alias="4", ge=0)
    level_5: int = Field(alias="5", ge=0)


class ConvictionDistribution(BaseModel):
    """Distribution of analyst recommendations by conviction level."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_level: ConvictionHistogram
    total: int = Field(ge=0)


class ByThesisStatus(BaseModel):
    """Position count keyed by thesis status.

    Uses hyphenated aliases matching the wire schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    on_track: int = Field(alias="on-track", ge=0)
    partially_realized: int = Field(alias="partially-realized", ge=0)
    at_risk: int = Field(alias="at-risk", ge=0)
    stale: int = Field(alias="stale", ge=0)
    invalidated: int = Field(alias="invalidated", ge=0)


class ByRecommendedAction(BaseModel):
    """Position count keyed by recommended action.

    Uses hyphenated aliases for ``adjust-bracket`` to match the wire schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    hold: int = Field(alias="hold", ge=0)
    reduce: int = Field(alias="reduce", ge=0)
    close: int = Field(alias="close", ge=0)
    adjust_bracket: int = Field(alias="adjust-bracket", ge=0)
    add: int = Field(alias="add", ge=0)


class BookHealthSummary(BaseModel):
    """Book-wide health summary histogram."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_thesis_status: ByThesisStatus
    by_recommended_action: ByRecommendedAction
    remedy_flagged_count: int = Field(ge=0)
    total: int = Field(ge=0)


class AggregateObservations(BaseModel):
    """Aggregate cross-set guardrail observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    combined_set_impact: CombinedSetImpact
    conviction_distribution: ConvictionDistribution
    book_health_summary: BookHealthSummary


# ---------------------------------------------------------------------------
# §3. Strategist section + wrappers
# ---------------------------------------------------------------------------


class StrategistSideConflict(BaseModel):
    """A single strategist-side conflict with an analyst recommendation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    with_recommendation_id: str = Field(
        pattern=r"^REC-[0-9]+$",
        description="Cross-reference to the analyst recommendation.",
    )
    underlying: str
    conflict_type: ConflictType


class StrategistSideAnnotations(BaseModel):
    """Pre-processor annotations attached to a strategist assessment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflicts: tuple[StrategistSideConflict, ...]


class WrappedPositionAssessment(BaseModel):
    """Strategist position assessment wrapped with pre-processor annotations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessment: PositionAssessment
    pre_processor_annotations: StrategistSideAnnotations


class WrappedPendingOrderAssessment(BaseModel):
    """Strategist pending-order assessment wrapped with pre-processor annotations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending_order_assessment: PendingOrderAssessment
    pre_processor_annotations: StrategistSideAnnotations


class StrategistSection(BaseModel):
    """Strategist section of the bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["normal", "defensive_posture"]
    position_assessments: tuple[WrappedPositionAssessment, ...]
    pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...]
    portfolio_level_observations: PortfolioLevelObservations


# ---------------------------------------------------------------------------
# §4. Analyst section + mode-dispatch + analyst-side conflicts
# ---------------------------------------------------------------------------


class AnalystSideConflict(BaseModel):
    """A single analyst-side conflict with a strategist assessment.

    Exactly one of ``with_assessment_id`` or ``with_pending_order_assessment_id``
    must be populated (the JSON Schema's ``oneOf``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    with_assessment_id: str | None = Field(
        default=None,
        pattern=r"^SA-[0-9]+$",
        description="Cross-reference to a strategist position assessment.",
    )
    with_pending_order_assessment_id: str | None = Field(
        default=None,
        pattern=r"^SA-ORD-[0-9]+$",
        description="Cross-reference to a strategist pending-order assessment.",
    )
    underlying: str
    conflict_type: ConflictType

    @model_validator(mode="after")
    def _validate_exactly_one_cross_reference(self) -> AnalystSideConflict:
        has_assessment = self.with_assessment_id is not None
        has_pending = self.with_pending_order_assessment_id is not None
        if has_assessment == has_pending:
            raise ValueError(
                "AnalystSideConflict requires exactly one of with_assessment_id or "
                "with_pending_order_assessment_id to be populated"
            )
        return self


class AnalystSideAnnotations(BaseModel):
    """Pre-processor annotations attached to an analyst recommendation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflicts: tuple[AnalystSideConflict, ...]


class WrappedRecommendation(BaseModel):
    """Analyst recommendation wrapped with pre-processor annotations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendation: Recommendation
    pre_processor_annotations: AnalystSideAnnotations


class AnalystSection(BaseModel):
    """Analyst section of the bundle.

    Mode-conditional invariant:
    - ``mode="normal"`` ⇒ ``recommendations`` is not None, ``watchlist`` is None.
    - ``mode="watchlist"`` ⇒ ``watchlist`` is not None, ``recommendations`` is None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["normal", "watchlist"]
    recommendations: tuple[WrappedRecommendation, ...] | None = None
    watchlist: tuple[WatchlistEntry, ...] | None = None

    @model_validator(mode="after")
    def _validate_mode_conditional(self) -> AnalystSection:
        if self.mode == "normal":
            if self.recommendations is None:
                raise ValueError("AnalystSection mode=normal requires recommendations")
            if self.watchlist is not None:
                raise ValueError("AnalystSection mode=normal forbids watchlist")
        elif self.mode == "watchlist":
            if self.watchlist is None:
                raise ValueError("AnalystSection mode=watchlist requires watchlist")
            if self.recommendations is not None:
                raise ValueError("AnalystSection mode=watchlist forbids recommendations")
        return self


# ---------------------------------------------------------------------------
# Top-level bundle
# ---------------------------------------------------------------------------


class ProposalPreProcessorBundle(BaseModel):
    """One pre-processor invocation's output bundle.

    The bundle wraps the analyst and strategist outputs with aggregate
    guardrail observations and pre-processor conflict annotations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str = Field(
        description="Pipeline invocation ID. Matches the analyst's and strategist's invocation_id."
    )
    timestamp: datetime = Field(description="When the pre-processor finalized the bundle.")
    aggregate_observations: AggregateObservations
    strategist_section: StrategistSection
    analyst_section: AnalystSection


# ---------------------------------------------------------------------------
# §5. Schema export
# ---------------------------------------------------------------------------

BUNDLE_OUTPUT_SCHEMA: dict[str, Any] = ProposalPreProcessorBundle.model_json_schema()


def bundle_schema() -> dict[str, Any]:
    """Return a fresh copy of the JSON Schema (Draft 2020-12) for ProposalPreProcessorBundle.

    Returns a deep copy so callers that mutate the result cannot affect the
    cached :data:`BUNDLE_OUTPUT_SCHEMA` constant or other callers.
    """
    return copy.deepcopy(BUNDLE_OUTPUT_SCHEMA)
