"""Tests for PMEnvelope Layer-2/3 validator — ALP-327.

Mirrors the analyst-side test pattern (tests/decision/analyst/test_validation.py)
extended with PM-specific cross-record checks and embedded-OMS-command checks.
Covers each acceptance criterion of ALP-327 with at least one positive and one
negative test:

* Layer-2 invariants (a)-(h): envelope_id ↔ source_recommendation_id integer
  bijection, verdict-conditional invariants, evaluation criterion-set ↔
  source_provenance, modification adjustment_category ↔ phase, embedded close
  command risk_management_subtype, anti_patterns_identified canonical enum,
  halt-mode no-OPEN/ADD, embedded OPEN/ADD sector ∈ active_sectors.
* Layer-3 referential checks (i)-(k): narrative reference resolution,
  source_recommendation_id ∈ pre-processor bundle, position_id ∈ pm_view.
* Inventory completeness (returns all errors, not first-error-only).
* Warnings vs errors (warnings do not invalidate).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
    from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
    from alphamind.portfolio_state.records.positions import PositionRecord

import pytest

from alphamind._kernel.ids import (
    EnvelopeId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.command_models import (
    BracketOrderParameters,
    EntryOrder,
    EquityInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.decision.portfolio_manager.models import (
    AddCommand,
    CloseCommand,
    ConcernRecord,
    CriterionAssessment,
    ModificationRecord,
    OpenCommand,
    PMAnalystEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
from alphamind.decision.portfolio_manager.validation import validate_pm_envelope
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    AnalystSection,
    AnalystSideAnnotations,
    BasisSection,
    BookHealthSummary,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ConvictionDistribution,
    ConvictionHistogram,
    ProposalPreProcessorBundle,
    StrategistSection,
    StrategistSideAnnotations,
    WrappedPendingOrderAssessment,
    WrappedPositionAssessment,
    WrappedRecommendation,
)
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})


# Default fixture sector resolver — maps a small test universe of tickers to
# the risk-side 4-way sector taxonomy. Tests that need to exercise inactive
# sectors override the resolver via :func:`_validate(sector_resolver=...)`.
_DEFAULT_TICKER_TO_SECTOR: dict[str, str] = {
    "NVDA": "semis",
    "JPM": "financials",
    "XOM": "energy",
    "AAPL": "tech",
    "MSFT": "tech",
    "GOOGL": "tech",
}


# Bypass-init helpers — replace Pydantic ``model_construct``. The dataclass __init__
# enforces all fields; these helpers skip validation so tests can inject sparse fixtures.


def _bypass_init_PortfolioManagerView(**kwargs: object) -> PortfolioManagerView:  # noqa: N802
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

    obj = object.__new__(PortfolioManagerView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_StrategistPositionView(**kwargs: object) -> StrategistPositionView:  # noqa: N802
    from alphamind.portfolio_state.consumers.strategist import StrategistPositionView

    obj = object.__new__(StrategistPositionView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _bypass_init_PositionRecord(**kwargs: object) -> PositionRecord:  # noqa: N802
    from alphamind.portfolio_state.records.positions import PositionRecord

    obj = object.__new__(PositionRecord)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


def _default_sector_resolver(ticker: str) -> str:
    return _DEFAULT_TICKER_TO_SECTOR.get(ticker, "tech")


def _retrieval_store(*ref_ids: str) -> RetrievalStore:
    """Build a RetrievalStore with the given prefixed reference IDs."""
    entries = {ref_id: f"section text for {ref_id}" for ref_id in ref_ids}
    freshness = {BriefSource.SA_TECH: _NOW}
    return RetrievalStore(entries=entries, freshness_by_source=freshness)


def _pass(note: str | None = None) -> CriterionAssessment:
    return CriterionAssessment(status="pass", note=note)


def _thesis_eval_all_pass() -> ThesisQualityEvaluation:
    return ThesisQualityEvaluation(
        falsifiability=_pass(),
        sizing_proportionality=_pass(),
        portfolio_coherence=_pass(),
        timing_plausibility=_pass(),
        counterargument_consideration=_pass(),
    )


def _position_eval_all_pass() -> PositionActionEvaluation:
    return PositionActionEvaluation(
        status_classification_warrant=_pass(),
        action_status_alignment=_pass(),
        action_specific_justification=_pass(),
        portfolio_coherence=_pass(),
    )


def _full_thesis() -> Thesis:
    """Minimal canonical thesis with one entry-rationale component."""
    return Thesis(
        summary="Long NVDA on continued AI capex tailwind.",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference="NVDA",
                narrative="AI demand sustains topline growth.",
                key_assumptions=("Hyperscaler capex remains elevated.",),
            ),
        ),
    )


def _hard_price_invalidation_leg() -> PriceLeg:
    """Minimal hard price-trigger leg satisfying OpenCommand's hard-backstop invariant."""
    return PriceLeg(
        type="price",
        is_hard=True,
        condition=PriceCondition(
            underlying_trigger="NVDA",
            comparator="<=",
            trigger_price=price(750.0),
        ),
        order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
    )


def _open_command(underlying: str = "NVDA") -> OpenCommand:
    """Construct a canonical :class:`OpenCommand` with the full required shape.

    Sector is no longer a wire-format field per parent decision (B); the
    validator derives it via ``sector_resolver(instrument.ticker)``.
    """
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=underlying, direction="long"),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_price_invalidation_leg(),),
        thesis=_full_thesis(),
    )


def _add_command(position_id: str = "POS-NVDA-001") -> AddCommand:
    """Construct a canonical :class:`AddCommand`.

    Canonical ADD references an existing position by id; it carries no
    embedded ``instrument`` (per oms-command-schema.md). The validator's
    sector check therefore applies only to OPEN, not ADD.
    """
    return AddCommand(
        command_type="add",
        position_id=PositionId(position_id),
        additional_quantity=5.0,
        additional_dollar_value=money(5_000.0),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        thesis_addition_component=ThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference="NVDA",
            narrative="Add to NVDA on continued strength.",
            key_assumptions=("Setup intact.",),
        ),
        bracket_adjustment=None,
    )


def _close_command(
    *,
    rationale_type: str = "thesis_invalidated",
    risk_management_subtype: str | None = None,
) -> CloseCommand:
    """Construct a canonical :class:`CloseCommand` with the full required shape."""
    return CloseCommand.model_validate(
        {
            "command_type": "close",
            "position_id": "POS-NVDA-001",
            "quantity": "all",
            "order_type": "market",
            "limit_price": None,
            "close_rationale_type": rationale_type,
            "invalidation_reason": (
                "Thesis broken." if rationale_type == "thesis_invalidated" else None
            ),
            "risk_management_subtype": risk_management_subtype,
        }
    )


def _make_analyst_envelope(**overrides: Any) -> PMAnalystEnvelope:
    defaults: dict[str, Any] = {
        "envelope_id": "ENV-REC-1",
        "invocation_id": "inv-2026-05-05",
        "source_provenance": "pm_analyst",
        "source_recommendation_id": "REC-1",
        "recommendation_type": "new_entry",
        "verdict": "approve",
        "evaluation": _thesis_eval_all_pass(),
        "modifications": (),
        "concerns": (),
        "rationale_narrative": "Analyst proposal aligns with the book.",
        "anti_patterns_identified": None,
        "commands": (_open_command(),),
    }
    return PMAnalystEnvelope(**(defaults | overrides))


def _make_strategist_envelope(**overrides: Any) -> PMStrategistEnvelope:
    defaults: dict[str, Any] = {
        "envelope_id": "ENV-SA-1",
        "invocation_id": "inv-2026-05-05",
        "source_provenance": "pm_strategist",
        "source_recommendation_id": "SA-1",
        "recommendation_type": "position_assessment",
        "position_id": "POS-NVDA-001",
        "verdict": "approve",
        "evaluation": _position_eval_all_pass(),
        "modifications": (),
        "concerns": (),
        "rationale_narrative": "Strategist position assessment is well-grounded.",
        "anti_patterns_identified": None,
        "commands": (_close_command(),),
    }
    return PMStrategistEnvelope(**(defaults | overrides))


def _empty_combined_set_impact() -> CombinedSetImpact:
    return CombinedSetImpact(
        basis=BasisSection(
            analyst_proposal_ids=(),
            strategist_action_ids=(),
            strategist_holds_excluded_count=0,
            snapshot_timestamp=_NOW,
        ),
        per_rule=(),
        breaches=(),
    )


def _empty_aggregate_observations() -> AggregateObservations:
    histogram = ConvictionHistogram.model_validate({"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    by_thesis = ByThesisStatus.model_validate(
        {
            "on-track": 0,
            "partially-realized": 0,
            "at-risk": 0,
            "stale": 0,
            "invalidated": 0,
        }
    )
    by_action = ByRecommendedAction.model_validate(
        {"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
    )
    return AggregateObservations(
        combined_set_impact=_empty_combined_set_impact(),
        conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
        book_health_summary=BookHealthSummary(
            by_thesis_status=by_thesis,
            by_recommended_action=by_action,
            remedy_flagged_count=0,
            total=0,
        ),
    )


def _recommendation_stub(recommendation_id: str) -> WrappedRecommendation:
    """Build a wrapped Recommendation stub carrying only the ID the validator reads."""
    from alphamind.decision.analyst.models import Recommendation

    rec = Recommendation.model_construct(recommendation_id=recommendation_id)
    return WrappedRecommendation.model_construct(
        recommendation=rec,
        pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
    )


def _position_assessment_stub(assessment_id: str) -> WrappedPositionAssessment:
    """Build a wrapped PositionAssessment stub carrying only the ID the validator reads."""
    from alphamind.decision.strategist.models import PositionAssessment

    pa = PositionAssessment.model_construct(assessment_id=assessment_id)
    return WrappedPositionAssessment.model_construct(
        assessment=pa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _pending_order_assessment_stub(
    pending_order_assessment_id: str,
) -> WrappedPendingOrderAssessment:
    """Build a wrapped PendingOrderAssessment stub carrying only the ID the validator reads."""
    from alphamind.decision.strategist.models import PendingOrderAssessment

    poa = PendingOrderAssessment.model_construct(
        pending_order_assessment_id=pending_order_assessment_id
    )
    return WrappedPendingOrderAssessment.model_construct(
        pending_order_assessment=poa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )


def _make_bundle(
    *,
    recommendations: tuple[WrappedRecommendation, ...] = (),
    position_assessments: tuple[WrappedPositionAssessment, ...] = (),
    pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...] = (),
    analyst_mode: str = "normal",
) -> ProposalPreProcessorBundle:
    """Build a minimal pre-processor bundle for validator tests.

    Uses ``model_construct`` to skip the model validators on inner sections —
    the validator under test reads only ``analyst_section.recommendations[].recommendation``,
    ``strategist_section.position_assessments[].assessment``, and
    ``strategist_section.pending_order_assessments[].pending_order_assessment``,
    so the rest of the fields can be elided safely.
    """
    if analyst_mode == "normal":
        analyst = AnalystSection.model_construct(
            mode="normal", recommendations=recommendations, watchlist=None
        )
    else:
        analyst = AnalystSection.model_construct(
            mode="watchlist", recommendations=None, watchlist=()
        )
    return ProposalPreProcessorBundle.model_construct(
        invocation_id="inv-2026-05-05",
        timestamp=_NOW,
        aggregate_observations=_empty_aggregate_observations(),
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=position_assessments,
            pending_order_assessments=pending_order_assessments,
            portfolio_level_observations=None,
        ),
        analyst_section=analyst,
    )


def _make_pm_view(positions: tuple[Any, ...] = ()) -> PortfolioManagerView:
    """Build a minimal PortfolioManagerView for validator tests.

    Constructs only the fields the validator inspects via ``positions``;
    the remaining required fields are filled with empty/default sentinel
    values via ``model_construct`` to avoid full snapshot wiring noise.
    """
    return _bypass_init_PortfolioManagerView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=None,
        drawdown=None,
        sector_exposure=(),
        directional_exposure=None,
        risk_budget=None,
        active_risk_parameters=None,
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=None,
        position_modification_trail={},
    )


def _position_view(position_id: str) -> Any:
    """Build a StrategistPositionView with only the position_id-bearing field set."""

    return _bypass_init_StrategistPositionView(
        position=_bypass_init_PositionRecord(position_id=position_id),
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )


# ---------------------------------------------------------------------------
# Tracer bullet: a fully valid analyst envelope passes
# ---------------------------------------------------------------------------


def _validate(
    envelope: PMAnalystEnvelope | PMStrategistEnvelope,
    *,
    bundle: ProposalPreProcessorBundle | None = None,
    pm_view: PortfolioManagerView | None = None,
    retrieval_store: RetrievalStore | None = None,
    active_sectors: frozenset[str] = _DEFAULT_ACTIVE_SECTORS,
    halt_mode: bool = False,
    sector_resolver: Any = None,
) -> Any:
    """Helper that fills sensible defaults and invokes the validator."""
    if bundle is None:
        if envelope.source_provenance == "pm_analyst":
            bundle = _make_bundle(
                recommendations=(_recommendation_stub(envelope.source_recommendation_id),),
            )
        elif envelope.recommendation_type == "position_assessment":
            bundle = _make_bundle(
                position_assessments=(
                    _position_assessment_stub(envelope.source_recommendation_id),
                ),
            )
        else:
            bundle = _make_bundle(
                pending_order_assessments=(
                    _pending_order_assessment_stub(envelope.source_recommendation_id),
                ),
            )
    if pm_view is None:
        if envelope.source_provenance == "pm_strategist":
            pm_view = _make_pm_view(positions=(_position_view(envelope.position_id),))
        else:
            pm_view = _make_pm_view()
    if retrieval_store is None:
        retrieval_store = _retrieval_store()
    if sector_resolver is None:
        sector_resolver = _default_sector_resolver
    return validate_pm_envelope(
        envelope,
        retrieval_store=retrieval_store,
        pre_processor_bundle=bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
    )


# ---------------------------------------------------------------------------
# Tracer bullet: a fully valid analyst envelope passes
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_valid_analyst_envelope_passes(self) -> None:
        envelope = _make_analyst_envelope()
        result = _validate(envelope)
        assert result.is_valid
        assert result.errors == ()

    def test_valid_strategist_envelope_passes(self) -> None:
        envelope = _make_strategist_envelope()
        result = _validate(envelope)
        assert result.is_valid
        assert result.errors == ()


# ---------------------------------------------------------------------------
# Layer-2 (a): envelope_id ↔ source_recommendation_id integer bijection
# ---------------------------------------------------------------------------


class TestEnvelopeIdSourceProvenanceBijection:
    def test_analyst_envelope_id_integer_matches_rec_integer_passes(self) -> None:
        envelope = _make_analyst_envelope(
            envelope_id=EnvelopeId("ENV-REC-3"), source_recommendation_id="REC-3"
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-3"),))
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_analyst_envelope_id_integer_mismatch_fails(self) -> None:
        envelope = _make_analyst_envelope(
            envelope_id=EnvelopeId("ENV-REC-3"), source_recommendation_id="REC-5"
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-5"),))
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("envelope_id" in err.field_path for err in result.errors)
        assert any(
            "integer" in err.message.lower() or "mismatch" in err.message.lower()
            for err in result.errors
        )

    def test_strategist_position_assessment_envelope_id_match_passes(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-7"),
            source_recommendation_id="SA-7",
            recommendation_type="position_assessment",
        )
        bundle = _make_bundle(position_assessments=(_position_assessment_stub("SA-7"),))
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_strategist_pending_order_envelope_id_match_passes(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-ORD-2"),
            source_recommendation_id="SA-ORD-2",
            recommendation_type="pending_order_assessment",
        )
        bundle = _make_bundle(
            pending_order_assessments=(_pending_order_assessment_stub("SA-ORD-2"),)
        )
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_strategist_envelope_id_integer_mismatch_fails(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-7"),
            source_recommendation_id="SA-9",
        )
        bundle = _make_bundle(position_assessments=(_position_assessment_stub("SA-9"),))
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("envelope_id" in err.field_path for err in result.errors)


# ---------------------------------------------------------------------------
# Layer-2 (b): verdict-conditional invariants (defense-in-depth)
# ---------------------------------------------------------------------------


class TestVerdictConditionalInvariants:
    def test_reject_with_empty_concerns_fails(self) -> None:
        # Bypass Pydantic's model validator via model_construct.
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="reject",
            evaluation=_thesis_eval_all_pass(),
            modifications=(),
            concerns=(),
            rationale_narrative="Rejected.",
            anti_patterns_identified=None,
            commands=(),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("concerns" in err.field_path for err in result.errors)

    def test_reject_with_commands_fails(self) -> None:
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="reject",
            evaluation=_thesis_eval_all_pass(),
            modifications=(),
            concerns=(ConcernRecord(source="other", summary="No."),),
            rationale_narrative="Rejected.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("commands" in err.field_path for err in result.errors)

    def test_approve_with_modifications_fails(self) -> None:
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve",
            evaluation=_thesis_eval_all_pass(),
            modifications=(
                ModificationRecord(
                    phase="pre_submission",
                    field_changed="position_size",
                    original_value=4,
                    approved_value=3,
                    adjustment_category="risk_reduction",
                    rationale="Trim.",
                ),
            ),
            concerns=(),
            rationale_narrative="Approved.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("modifications" in err.field_path for err in result.errors)

    def test_approve_with_modification_no_modifications_fails(self) -> None:
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve_with_modification",
            evaluation=_thesis_eval_all_pass(),
            modifications=(),
            concerns=(),
            rationale_narrative="Approved.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("modifications" in err.field_path for err in result.errors)

    def test_reject_envelope_with_concern_passes(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="reject",
            modifications=(),
            commands=(),
            concerns=(ConcernRecord(source="falsifiability", summary="Not falsifiable."),),
        )
        result = _validate(envelope)
        assert result.is_valid


# ---------------------------------------------------------------------------
# Layer-2 (c): evaluation criterion-set ↔ source_provenance
# ---------------------------------------------------------------------------


class TestEvaluationCriterionSetMatchesSourceProvenance:
    def test_pm_analyst_with_thesis_quality_evaluation_passes(self) -> None:
        envelope = _make_analyst_envelope()
        result = _validate(envelope)
        assert result.is_valid

    def test_pm_analyst_with_position_action_evaluation_fails(self) -> None:
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve",
            evaluation=_position_eval_all_pass(),
            modifications=(),
            concerns=(),
            rationale_narrative="Mismatched eval shape.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("evaluation" in err.field_path for err in result.errors)

    def test_pm_strategist_with_thesis_quality_evaluation_fails(self) -> None:
        envelope = PMStrategistEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-SA-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_strategist",
            source_recommendation_id="SA-1",
            recommendation_type="position_assessment",
            position_id=PositionId("POS-NVDA-001"),
            verdict="approve",
            evaluation=_thesis_eval_all_pass(),
            modifications=(),
            concerns=(),
            rationale_narrative="Mismatched eval shape.",
            anti_patterns_identified=None,
            commands=(_close_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("evaluation" in err.field_path for err in result.errors)


# ---------------------------------------------------------------------------
# Layer-2 (d): modification adjustment_category ↔ phase ↔ triggering_rule
# ---------------------------------------------------------------------------


class TestModificationAdjustmentCategoryPhase:
    def test_pre_submission_risk_reduction_passes(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="approve_with_modification",
            modifications=(
                ModificationRecord(
                    phase="pre_submission",
                    field_changed="position_size",
                    original_value=4,
                    approved_value=3,
                    adjustment_category="risk_reduction",
                    rationale="Trim.",
                ),
            ),
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert result.is_valid

    def test_guardrail_rejection_with_post_rejection_phase_passes(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="approve_with_modification",
            modifications=(
                ModificationRecord(
                    phase="post_rejection",
                    field_changed="position_size",
                    original_value=4,
                    approved_value=3,
                    adjustment_category="guardrail_rejection_response",
                    rationale="Trim per breach.",
                    triggering_rule="per_position_max_size",
                ),
            ),
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert result.is_valid

    def test_guardrail_rejection_with_pre_submission_phase_fails(self) -> None:
        # Bypass model validator via model_construct.
        bad_mod = ModificationRecord.model_construct(
            phase="pre_submission",
            field_changed="position_size",
            original_value=4,
            approved_value=3,
            adjustment_category="guardrail_rejection_response",
            rationale="Wrong phase.",
            triggering_rule="per_position_max_size",
        )
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve_with_modification",
            evaluation=_thesis_eval_all_pass(),
            modifications=(bad_mod,),
            concerns=(),
            rationale_narrative="Bad mod.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("modifications[0]" in err.field_path for err in result.errors)

    def test_risk_reduction_with_post_rejection_phase_fails(self) -> None:
        bad_mod = ModificationRecord.model_construct(
            phase="post_rejection",
            field_changed="position_size",
            original_value=4,
            approved_value=3,
            adjustment_category="risk_reduction",
            rationale="Wrong phase.",
            triggering_rule=None,
        )
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve_with_modification",
            evaluation=_thesis_eval_all_pass(),
            modifications=(bad_mod,),
            concerns=(),
            rationale_narrative="Bad mod.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("modifications[0]" in err.field_path for err in result.errors)

    def test_guardrail_rejection_missing_triggering_rule_fails(self) -> None:
        bad_mod = ModificationRecord.model_construct(
            phase="post_rejection",
            field_changed="position_size",
            original_value=4,
            approved_value=3,
            adjustment_category="guardrail_rejection_response",
            rationale="Missing rule.",
            triggering_rule=None,
        )
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve_with_modification",
            evaluation=_thesis_eval_all_pass(),
            modifications=(bad_mod,),
            concerns=(),
            rationale_narrative="Missing rule.",
            anti_patterns_identified=None,
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("triggering_rule" in err.message for err in result.errors)


# ---------------------------------------------------------------------------
# Layer-2 (e): embedded close command risk_management_subtype = pm_directed
# ---------------------------------------------------------------------------


class TestCloseCommandRiskManagementSubtype:
    def test_pm_directed_close_passes(self) -> None:
        envelope = _make_strategist_envelope(
            commands=(
                _close_command(
                    rationale_type="risk_management",
                    risk_management_subtype="pm_directed",
                ),
            ),
        )
        result = _validate(envelope)
        assert result.is_valid

    def test_engine_guardrail_close_fails(self) -> None:
        # Engine-originated provenance leaking into a PM envelope is the bug.
        # Use the canonical full shape so the parent envelope construction's
        # discriminated-union validation succeeds.
        bad_close = CloseCommand(
            command_type="close",
            position_id=PositionId("POS-NVDA-001"),
            quantity="all",
            order_type="market",
            limit_price=None,
            close_rationale_type="risk_management",
            invalidation_reason=None,
            risk_management_subtype="engine_guardrail",
        )
        envelope = _make_strategist_envelope(commands=(bad_close,))
        result = _validate(envelope)
        assert not result.is_valid
        assert any(
            "risk_management_subtype" in err.field_path or "risk_management_subtype" in err.message
            for err in result.errors
        )

    def test_thesis_invalidated_close_passes(self) -> None:
        # Non-risk-management closes don't trigger the subtype check at all.
        envelope = _make_strategist_envelope(
            commands=(_close_command(rationale_type="thesis_invalidated"),),
        )
        result = _validate(envelope)
        assert result.is_valid


# ---------------------------------------------------------------------------
# Layer-2 (f): anti_patterns_identified ∈ canonical enum
# ---------------------------------------------------------------------------


class TestAntiPatternsCanonicalEnum:
    def test_canonical_anti_pattern_passes(self) -> None:
        envelope = _make_analyst_envelope(
            anti_patterns_identified=("conviction_inflation", "sunk_cost_persistence"),
        )
        result = _validate(envelope)
        assert result.is_valid

    def test_unknown_anti_pattern_fails(self) -> None:
        envelope = PMAnalystEnvelope.model_construct(
            envelope_id=EnvelopeId("ENV-REC-1"),
            invocation_id="inv-2026-05-05",
            source_provenance="pm_analyst",
            source_recommendation_id="REC-1",
            recommendation_type="new_entry",
            verdict="approve",
            evaluation=_thesis_eval_all_pass(),
            modifications=(),
            concerns=(),
            rationale_narrative="Bad enum.",
            anti_patterns_identified=("not_a_canonical_string",),
            commands=(_open_command(),),
        )
        result = _validate(envelope)
        assert not result.is_valid
        assert any("anti_patterns" in err.field_path for err in result.errors)


# ---------------------------------------------------------------------------
# Layer-2 (g): halt mode → no embedded OPEN or ADD commands
# ---------------------------------------------------------------------------


class TestHaltModeNoConstructiveCommands:
    def test_halt_mode_with_close_command_passes(self) -> None:
        envelope = _make_strategist_envelope(commands=(_close_command(),))
        result = _validate(envelope, halt_mode=True)
        assert result.is_valid

    def test_halt_mode_with_open_command_fails(self) -> None:
        envelope = _make_analyst_envelope(commands=(_open_command(),))
        result = _validate(envelope, halt_mode=True)
        assert not result.is_valid
        assert any("commands" in err.field_path for err in result.errors)
        assert any("halt" in err.message.lower() for err in result.errors)

    def test_halt_mode_with_add_command_fails(self) -> None:
        envelope = _make_strategist_envelope(
            verdict="approve",
            commands=(_add_command(),),
        )
        result = _validate(envelope, halt_mode=True)
        assert not result.is_valid
        assert any("commands" in err.field_path for err in result.errors)

    def test_normal_mode_with_open_command_passes(self) -> None:
        envelope = _make_analyst_envelope(commands=(_open_command(),))
        result = _validate(envelope, halt_mode=False)
        assert result.is_valid


# ---------------------------------------------------------------------------
# Layer-2 (h): embedded OPEN/ADD commands' sector ∈ active_sectors
# ---------------------------------------------------------------------------


class TestEmbeddedCommandSectorActive:
    def test_open_command_with_active_sector_passes(self) -> None:
        # NVDA → "semis" via the default fixture resolver; semis ∈ active_sectors.
        envelope = _make_analyst_envelope(commands=(_open_command(underlying=Symbol("NVDA")),))
        result = _validate(envelope, active_sectors=frozenset({"semis", "tech"}))
        assert result.is_valid

    def test_open_command_with_inactive_sector_fails(self) -> None:
        # XOM → "energy" via the default fixture resolver; energy ∉ {tech, semis}.
        envelope = _make_analyst_envelope(commands=(_open_command(underlying=Symbol("XOM")),))
        result = _validate(envelope, active_sectors=frozenset({"tech", "semis"}))
        assert not result.is_valid
        assert any("commands[0]" in err.field_path for err in result.errors)
        assert any("instrument" in err.field_path for err in result.errors)

    def test_open_command_sector_derived_via_resolver(self) -> None:
        # Custom resolver maps NVDA → "energy" — overrides the default and
        # exercises the resolver-driven dispatch path explicitly.
        envelope = _make_analyst_envelope(commands=(_open_command(underlying=Symbol("NVDA")),))
        result = _validate(
            envelope,
            active_sectors=frozenset({"tech", "semis"}),
            sector_resolver=lambda t: "energy" if t == "NVDA" else "tech",
        )
        assert not result.is_valid
        assert any(err.criterion == "embedded_command_sector_active" for err in result.errors)

    def test_add_command_skipped_no_embedded_instrument(self) -> None:
        # Canonical AddCommand carries no embedded instrument (it references an
        # existing position by id), so the sector check is a no-op for ADD.
        envelope = _make_strategist_envelope(
            verdict="approve",
            commands=(_add_command(),),
        )
        result = _validate(envelope, active_sectors=frozenset({"tech", "semis"}))
        assert result.is_valid

    def test_close_command_sector_check_skipped(self) -> None:
        # CloseCommand carries no instrument field; the check applies only to OPEN.
        envelope = _make_strategist_envelope(commands=(_close_command(),))
        result = _validate(envelope, active_sectors=frozenset({"tech"}))
        assert result.is_valid


# ---------------------------------------------------------------------------
# Layer-3 (i): narrative [XX-N] references resolve in the retrieval store
# ---------------------------------------------------------------------------


class TestNarrativeReferenceResolution:
    def test_resolved_reference_in_rationale_passes(self) -> None:
        envelope = _make_analyst_envelope(
            rationale_narrative="Anchored on [SA-TECH-3] reasoning.",
        )
        result = _validate(envelope, retrieval_store=_retrieval_store("SA-TECH-3"))
        assert result.is_valid

    def test_unresolved_reference_in_rationale_fails(self) -> None:
        envelope = _make_analyst_envelope(
            rationale_narrative="Anchored on [SA-TECH-99] reasoning that does not exist.",
        )
        result = _validate(envelope, retrieval_store=_retrieval_store("SA-TECH-3"))
        assert not result.is_valid
        assert any("rationale_narrative" in err.field_path for err in result.errors)
        assert any("SA-TECH-99" in err.message for err in result.errors)

    def test_unresolved_reference_in_modification_rationale_fails(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="approve_with_modification",
            modifications=(
                ModificationRecord(
                    phase="pre_submission",
                    field_changed="position_size",
                    original_value=4,
                    approved_value=3,
                    adjustment_category="risk_reduction",
                    rationale="Trim per [QR-CW-7] which is missing.",
                ),
            ),
            commands=(_open_command(),),
        )
        result = _validate(envelope, retrieval_store=_retrieval_store())
        assert not result.is_valid
        assert any("modifications[0].rationale" in err.field_path for err in result.errors)

    def test_non_canonical_reference_skipped(self) -> None:
        # Non-canonical prefixes (e.g., [BREACH-1], [INV-1]) live outside the
        # synthesizer reference taxonomy and aren't resolved against the store.
        envelope = _make_analyst_envelope(
            rationale_narrative="Triggered by [BREACH-1] and [INV-2].",
        )
        result = _validate(envelope, retrieval_store=_retrieval_store())
        assert result.is_valid

    def test_resolved_modification_rationale_reference_passes(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="approve_with_modification",
            modifications=(
                ModificationRecord(
                    phase="pre_submission",
                    field_changed="position_size",
                    original_value=4,
                    approved_value=3,
                    adjustment_category="risk_reduction",
                    rationale="Trim per [SA-FIN-2] which exists.",
                ),
            ),
            commands=(_open_command(),),
        )
        result = _validate(envelope, retrieval_store=_retrieval_store("SA-FIN-2"))
        assert result.is_valid


# ---------------------------------------------------------------------------
# Layer-3 (j): source_recommendation_id ∈ pre-processor bundle
# ---------------------------------------------------------------------------


class TestSourceRecommendationIdResolves:
    def test_analyst_rec_in_bundle_passes(self) -> None:
        envelope = _make_analyst_envelope(
            envelope_id=EnvelopeId("ENV-REC-2"), source_recommendation_id="REC-2"
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-2"),))
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_analyst_rec_missing_from_bundle_fails(self) -> None:
        envelope = _make_analyst_envelope(
            envelope_id=EnvelopeId("ENV-REC-2"), source_recommendation_id="REC-2"
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-99"),))
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("source_recommendation_id" in err.field_path for err in result.errors)

    def test_strategist_position_assessment_in_bundle_passes(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-3"),
            source_recommendation_id="SA-3",
            recommendation_type="position_assessment",
        )
        bundle = _make_bundle(position_assessments=(_position_assessment_stub("SA-3"),))
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_strategist_position_assessment_missing_fails(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-3"),
            source_recommendation_id="SA-3",
            recommendation_type="position_assessment",
        )
        bundle = _make_bundle(position_assessments=(_position_assessment_stub("SA-99"),))
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("source_recommendation_id" in err.field_path for err in result.errors)

    def test_strategist_pending_order_in_bundle_passes(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-ORD-4"),
            source_recommendation_id="SA-ORD-4",
            recommendation_type="pending_order_assessment",
        )
        bundle = _make_bundle(
            pending_order_assessments=(_pending_order_assessment_stub("SA-ORD-4"),)
        )
        result = _validate(envelope, bundle=bundle)
        assert result.is_valid

    def test_strategist_pending_order_missing_fails(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-ORD-4"),
            source_recommendation_id="SA-ORD-4",
            recommendation_type="pending_order_assessment",
        )
        bundle = _make_bundle(
            pending_order_assessments=(_pending_order_assessment_stub("SA-ORD-99"),)
        )
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("source_recommendation_id" in err.field_path for err in result.errors)

    def test_analyst_envelope_in_watchlist_mode_fails(self) -> None:
        # Watchlist mode means analyst proposals are absent — a pm_analyst envelope
        # in this mode is a Layer-2 invariant violation.
        envelope = _make_analyst_envelope()
        bundle = _make_bundle(analyst_mode="watchlist")
        result = _validate(envelope, bundle=bundle)
        assert not result.is_valid
        assert any("source_recommendation_id" in err.field_path for err in result.errors)


# ---------------------------------------------------------------------------
# Layer-3 (k): position_id ∈ pm_view.positions
# ---------------------------------------------------------------------------


class TestPositionIdResolves:
    def test_strategist_envelope_with_known_position_passes(self) -> None:
        envelope = _make_strategist_envelope(position_id=PositionId("POS-NVDA-001"))
        pm_view = _make_pm_view(positions=(_position_view("POS-NVDA-001"),))
        result = _validate(envelope, pm_view=pm_view)
        assert result.is_valid

    def test_strategist_envelope_with_unknown_position_fails(self) -> None:
        envelope = _make_strategist_envelope(position_id=PositionId("POS-NVDA-999"))
        pm_view = _make_pm_view(positions=(_position_view("POS-NVDA-001"),))
        result = _validate(envelope, pm_view=pm_view)
        assert not result.is_valid
        assert any("position_id" in err.field_path for err in result.errors)

    def test_analyst_envelope_with_no_position_id_passes(self) -> None:
        # pm_analyst envelopes carry position_id=None — the check is skipped.
        envelope = _make_analyst_envelope()
        pm_view = _make_pm_view()
        result = _validate(envelope, pm_view=pm_view)
        assert result.is_valid


# ---------------------------------------------------------------------------
# Inventory completeness — returns full error inventory, not first-error-only
# ---------------------------------------------------------------------------


class TestErrorInventoryCompleteness:
    def test_returns_full_inventory_not_first_error(self) -> None:
        # Construct an envelope with two distinct invariant violations:
        # (1) envelope_id integer mismatch with source_recommendation_id;
        # (2) embedded OPEN command sector outside active_sectors (XOM → "energy").
        envelope = _make_analyst_envelope(
            envelope_id=EnvelopeId("ENV-REC-2"),
            source_recommendation_id="REC-7",
            commands=(_open_command(underlying=Symbol("XOM")),),
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-7"),))
        result = _validate(
            envelope,
            bundle=bundle,
            active_sectors=frozenset({"tech", "semis"}),
        )
        assert not result.is_valid
        assert len(result.errors) >= 2
        criteria = {err.criterion for err in result.errors}
        assert "envelope_id_source_recommendation_id_bijection" in criteria
        assert "embedded_command_sector_active" in criteria

    def test_warnings_do_not_invalidate(self) -> None:
        # The PM validator does not currently surface any warning paths; this
        # test pins the contract that ``is_valid`` ignores warnings even when
        # warnings are emitted in future. Constructed via ``model_construct``
        # so the contract is exercised regardless of which check (if any) the
        # validator currently emits as a warning.
        from alphamind.decision.portfolio_manager.validation import (
            ValidationResult,
            ValidationWarning,
        )

        result = ValidationResult(
            envelope_id=EnvelopeId("ENV-REC-1"),
            errors=(),
            warnings=(
                ValidationWarning(
                    field_path="rationale_narrative",
                    message="soft warning",
                    criterion=None,
                ),
            ),
        )
        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants (ALP-475: 10b conversion)
# ---------------------------------------------------------------------------


class TestPMValidationTypesAreFrozenDataclasses:
    """Per ALP-475, PM validation public types (hoisted to
    :mod:`alphamind.commands.validation_results` per ALP-458) are
    ``@dataclass(frozen=True, slots=True)``.
    """

    def test_validation_error_is_frozen_dataclass(self) -> None:
        from alphamind.decision.portfolio_manager.validation import ValidationError

        err = ValidationError(field_path="commands[0]", message="m1", criterion="c1")
        assert dataclasses.is_dataclass(ValidationError)
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.message = "mutated"  # type: ignore[misc]

    def test_validation_warning_is_frozen_dataclass(self) -> None:
        from alphamind.decision.portfolio_manager.validation import ValidationWarning

        warn = ValidationWarning(field_path="x.y", message="m1")
        assert dataclasses.is_dataclass(ValidationWarning)
        with pytest.raises(dataclasses.FrozenInstanceError):
            warn.criterion = "mutated"  # type: ignore[misc]

    def test_validation_result_is_frozen_dataclass(self) -> None:
        from alphamind.decision.portfolio_manager.validation import ValidationResult

        result = ValidationResult(envelope_id=EnvelopeId("env-z"), errors=(), warnings=())
        assert dataclasses.is_dataclass(ValidationResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.errors = ()  # type: ignore[misc]

    def test_validation_result_is_valid_property_works_with_dataclass(self) -> None:
        from alphamind.decision.portfolio_manager.validation import (
            ValidationError,
            ValidationResult,
        )

        empty = ValidationResult(envelope_id=EnvelopeId("env-x"), errors=(), warnings=())
        assert empty.is_valid is True
        with_err = ValidationResult(
            envelope_id=EnvelopeId("env-y"),
            errors=(ValidationError(field_path="x", message="m"),),
            warnings=(),
        )
        assert with_err.is_valid is False
