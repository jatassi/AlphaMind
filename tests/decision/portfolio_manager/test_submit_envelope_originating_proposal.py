"""Tests for ``lookup_originating_proposal_json`` — ALP-557.

The helper resolves the originating proposal body (analyst Recommendation or
strategist PositionAssessment / PendingOrderAssessment) from the pre-processor
bundle, keyed by ``envelope.source_recommendation_id``. A miss is a typed
``OriginatingProposalLookupError`` — never a silent empty dict — because Layer-3
validation has already guaranteed the id resolves before the envelope reaches
the submission log.
"""

from __future__ import annotations

import pytest

from alphamind.commands.command_models import (
    CloseCommand,
    OpenCommand,
)
from alphamind.commands.pm_envelope import (
    PMAnalystEnvelope,
    PMStrategistEnvelope,
)
from alphamind.decision.analyst.models import Recommendation
from alphamind.decision.portfolio_manager.submit_envelope.process import (
    OriginatingProposalLookupError,
    lookup_originating_proposal_json,
)
from alphamind.decision.proposal_pre_processor.models import (
    AnalystSection,
    AnalystSideAnnotations,
    ProposalPreProcessorBundle,
    StrategistSection,
    StrategistSideAnnotations,
    WrappedPendingOrderAssessment,
    WrappedPositionAssessment,
    WrappedRecommendation,
)
from alphamind.decision.strategist.models import (
    PendingOrderAssessment,
    PositionAssessment,
)

_INV_ID = "inv-2026-05-19T12:00:00Z-aaaa"


def _wrapped_recommendation(recommendation_id: str) -> WrappedRecommendation:
    rec = Recommendation.model_construct(recommendation_id=recommendation_id)
    return WrappedRecommendation.model_construct(
        recommendation=rec,
        pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
    )


def _wrapped_position_assessment(assessment_id: str) -> WrappedPositionAssessment:
    pa = PositionAssessment.model_construct(assessment_id=assessment_id)
    return WrappedPositionAssessment.model_construct(
        assessment=pa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _wrapped_pending_order_assessment(
    pending_order_assessment_id: str,
) -> WrappedPendingOrderAssessment:
    poa = PendingOrderAssessment.model_construct(
        pending_order_assessment_id=pending_order_assessment_id
    )
    return WrappedPendingOrderAssessment.model_construct(
        pending_order_assessment=poa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _bundle(
    *,
    recommendations: tuple[WrappedRecommendation, ...] = (),
    position_assessments: tuple[WrappedPositionAssessment, ...] = (),
    pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...] = (),
) -> ProposalPreProcessorBundle:
    return ProposalPreProcessorBundle.model_construct(
        invocation_id=_INV_ID,
        timestamp=None,
        aggregate_observations=None,
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=position_assessments,
            pending_order_assessments=pending_order_assessments,
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal", recommendations=recommendations, watchlist=None
        ),
    )


def _analyst_envelope(source_recommendation_id: str = "REC-1") -> PMAnalystEnvelope:
    return PMAnalystEnvelope.model_construct(
        envelope_id="ENV-REC-1",
        invocation_id=_INV_ID,
        source_provenance="pm_analyst",
        source_recommendation_id=source_recommendation_id,
        recommendation_type="new_entry",
        position_id=None,
        commands=(OpenCommand.model_construct(),),
    )


def _strategist_envelope(
    *,
    source_recommendation_id: str,
    recommendation_type: str,
) -> PMStrategistEnvelope:
    return PMStrategistEnvelope.model_construct(
        envelope_id="ENV-SA-1",
        invocation_id=_INV_ID,
        source_provenance="pm_strategist",
        source_recommendation_id=source_recommendation_id,
        recommendation_type=recommendation_type,
        position_id="POS-NVDA-001",
        commands=(CloseCommand.model_construct(),),
    )


def test_resolves_analyst_recommendation() -> None:
    bundle = _bundle(
        recommendations=(_wrapped_recommendation("REC-1"), _wrapped_recommendation("REC-2"))
    )
    envelope = _analyst_envelope(source_recommendation_id="REC-2")

    body = lookup_originating_proposal_json(bundle, envelope)

    assert body["recommendation_id"] == "REC-2"


def test_resolves_strategist_position_assessment() -> None:
    bundle = _bundle(
        position_assessments=(
            _wrapped_position_assessment("SA-1"),
            _wrapped_position_assessment("SA-2"),
        )
    )
    envelope = _strategist_envelope(
        source_recommendation_id="SA-2", recommendation_type="position_assessment"
    )

    body = lookup_originating_proposal_json(bundle, envelope)

    assert body["assessment_id"] == "SA-2"


def test_resolves_strategist_pending_order_assessment() -> None:
    bundle = _bundle(
        pending_order_assessments=(
            _wrapped_pending_order_assessment("SA-ORD-1"),
            _wrapped_pending_order_assessment("SA-ORD-2"),
        )
    )
    envelope = _strategist_envelope(
        source_recommendation_id="SA-ORD-2",
        recommendation_type="pending_order_assessment",
    )

    body = lookup_originating_proposal_json(bundle, envelope)

    assert body["pending_order_assessment_id"] == "SA-ORD-2"


def test_missing_analyst_recommendation_raises_typed_error() -> None:
    bundle = _bundle(recommendations=(_wrapped_recommendation("REC-1"),))
    envelope = _analyst_envelope(source_recommendation_id="REC-99")

    with pytest.raises(OriginatingProposalLookupError) as exc:
        lookup_originating_proposal_json(bundle, envelope)

    assert "REC-99" in str(exc.value)
    assert "ENV-REC-1" in str(exc.value)


def test_missing_strategist_assessment_raises_typed_error() -> None:
    bundle = _bundle(position_assessments=(_wrapped_position_assessment("SA-1"),))
    envelope = _strategist_envelope(
        source_recommendation_id="SA-99", recommendation_type="position_assessment"
    )

    with pytest.raises(OriginatingProposalLookupError) as exc:
        lookup_originating_proposal_json(bundle, envelope)

    assert "SA-99" in str(exc.value)


def test_analyst_envelope_against_watchlist_bundle_raises() -> None:
    """A watchlist-mode bundle carries no analyst recommendations; a pm_analyst
    envelope cannot resolve and the helper raises rather than returning {}."""
    bundle = ProposalPreProcessorBundle.model_construct(
        invocation_id=_INV_ID,
        timestamp=None,
        aggregate_observations=None,
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="watchlist", recommendations=None, watchlist=()
        ),
    )
    envelope = _analyst_envelope(source_recommendation_id="REC-1")

    with pytest.raises(OriginatingProposalLookupError):
        lookup_originating_proposal_json(bundle, envelope)
