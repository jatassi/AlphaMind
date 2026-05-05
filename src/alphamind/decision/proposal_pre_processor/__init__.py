"""Proposal pre-processor decision-layer public surface.

The bundle model (story 01 / ALP-313) is re-exported so downstream callers
can import from :mod:`alphamind.decision.proposal_pre_processor` without
reaching into submodules. Later stories will append their additions to this
file.
"""

from alphamind.decision.proposal_pre_processor.conflicts import (
    ConflictDetectionResult,
    detect_conflicts,
)
from alphamind.decision.proposal_pre_processor.models import (
    BUNDLE_OUTPUT_SCHEMA,
    AggregateObservations,
    AnalystSection,
    AnalystSideAnnotations,
    AnalystSideConflict,
    BasisSection,
    BookHealthSummary,
    BreachEntry,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ConflictType,
    ContributorEntry,
    ConvictionDistribution,
    ConvictionHistogram,
    PerRuleEntry,
    ProposalPreProcessorBundle,
    StrategistSection,
    StrategistSideAnnotations,
    StrategistSideConflict,
    WrappedPendingOrderAssessment,
    WrappedPositionAssessment,
    WrappedRecommendation,
    bundle_schema,
)
from alphamind.decision.proposal_pre_processor.observations import (
    compute_book_health_summary,
    compute_conviction_distribution,
)
from alphamind.decision.proposal_pre_processor.translator import (
    TranslatorError,
    translate_position_assessment_to_proposed_delta,
    translate_recommendation_to_proposed_delta,
)

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
    "ConflictDetectionResult",
    "ConflictType",
    "ContributorEntry",
    "ConvictionDistribution",
    "ConvictionHistogram",
    "PerRuleEntry",
    "ProposalPreProcessorBundle",
    "StrategistSection",
    "StrategistSideAnnotations",
    "StrategistSideConflict",
    "TranslatorError",
    "WrappedPendingOrderAssessment",
    "WrappedPositionAssessment",
    "WrappedRecommendation",
    "bundle_schema",
    "compute_book_health_summary",
    "compute_conviction_distribution",
    "detect_conflicts",
    "translate_position_assessment_to_proposed_delta",
    "translate_recommendation_to_proposed_delta",
]
