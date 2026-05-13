"""Tests for StrategistOutput Layer-2/3 validator — ALP-306.

Layer-2 (cross-field structural invariants Pydantic can't see across siblings)
and Layer-3 (referential integrity against the per-invocation retrieval store).
Mirrors the sibling shape of ``alphamind.decision.analyst.validation`` while
following the strategist-specific contract documented in
``docs/design/04-decision-layer/strategist-output-schema.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind._kernel.ids import (
    PositionId,
)
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    BracketAdjustNewStopLevel,
    DefensivePostureSummary,
    EntryOrder,
    ExposureImpact,
    GuardrailValidationResult,
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
    ReduceParameters,
    ReductionPriorityEntry,
    RegimeTransitionAddressedBreach,
    RegimeTransitionSummary,
    RegimeTransitionUncuredBreach,
    StrategistOutput,
)
from alphamind.decision.strategist.validation import validate_strategist_output
from alphamind.risk_guardrails.guardrail_evaluation import RuleProjection, Status

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


_DEFAULT_ACTIVE_SECTORS = frozenset({"tech", "semis", "financials", "energy"})


def _retrieval_store(*ref_ids: str) -> RetrievalStore:
    """Build a RetrievalStore with the given prefixed reference IDs."""
    entries = {ref_id: f"section text for {ref_id}" for ref_id in ref_ids}
    freshness = {BriefSource.SA_TECH: datetime(2026, 4, 23, 14, 30, tzinfo=UTC)}
    return RetrievalStore(entries=entries, freshness_by_source=freshness)


def _make_guardrail_result(**overrides: Any) -> GuardrailValidationResult:
    defaults: dict[str, Any] = {
        "overall": "PASS",
        "per_rule": (
            RuleProjection(
                rule="per_position_max_size",
                status=Status.PASS,
                current=4.5,
                limit=3.5,
                projected_after=3.15,
                headroom_remaining=0.35,
                unit="% of portfolio",
            ),
        ),
        "checked_at": _ts("2026-04-23T14:33:12Z"),
    }
    return GuardrailValidationResult(**(defaults | overrides))


def _make_position_assessment(**overrides: Any) -> PositionAssessment:
    defaults: dict[str, Any] = {
        "assessment_id": "SA-1",
        "position_id": "POS-NVDA-001",
        "thesis_id": "TH-NVDA-001",
        "underlying": "NVDA",
        "sector": "semis",
        "thesis_status": "on-track",
        "prior_status": "on-track",
        "recommended_action": "hold",
        "status_rationale": "Thesis remains supported by [SA-TECH-2].",
        "action_rationale": "No signal to change; hold.",
    }
    return PositionAssessment(**(defaults | overrides))


def _make_pending_order_assessment(**overrides: Any) -> PendingOrderAssessment:
    defaults: dict[str, Any] = {
        "pending_order_assessment_id": "SA-ORD-1",
        "order_id": "ORD-LIMIT-4",
        "position_id": "PENDING-AVGO-003",
        "order_type": "entry_limit",
        "order_age_hours": 36.0,
        "fill_probability_assessment": "unlikely",
        "recommended_action": "maintain",
        "drift_rationale": "Limit at $380 placed when AVGO traded at $385.",
        "action_rationale": "Maintain pending order; thesis intact.",
    }
    return PendingOrderAssessment(**(defaults | overrides))


def _make_portfolio_observations(**overrides: Any) -> PortfolioLevelObservations:
    defaults: dict[str, Any] = {
        "aggregate_thesis_health": "Two open positions; one on-track, one at-risk.",
        "sector_balance_shifts": "Semis trim moves sector exposure down.",
        "thesis_dependency_warnings": "No correlated breakdowns to flag.",
        "capital_allocation_observations": "Capital efficient at current sizing.",
    }
    return PortfolioLevelObservations(**(defaults | overrides))


def _make_output(**overrides: Any) -> StrategistOutput:
    defaults: dict[str, Any] = {
        "invocation_id": "inv-001",
        "timestamp": _ts("2026-04-23T14:33:47Z"),
        "mode": "normal",
        "position_assessments": (_make_position_assessment(),),
        "pending_order_assessments": (),
        "portfolio_level_observations": _make_portfolio_observations(),
    }
    return StrategistOutput(**(defaults | overrides))


def _store_with_baseline_refs() -> RetrievalStore:
    """The retrieval store every baseline narrative cites by default."""
    return _retrieval_store("SA-TECH-2")


# ---------------------------------------------------------------------------
# Tracer — baseline strategist output passes
# ---------------------------------------------------------------------------


class TestTracerBaseline:
    def test_baseline_output_passes(self) -> None:
        result = validate_strategist_output(
            _make_output(),
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "PASS"
        assert result.failures == ()
        assert result.warnings == ()


# ---------------------------------------------------------------------------
# Layer-2 — action_parameters.action matches recommended_action
# ---------------------------------------------------------------------------


class TestActionParametersMatch:
    def test_mismatched_action_parameters_is_failure(self) -> None:
        """Pydantic's discriminator catches this on parse, but the validator
        provides a defensive double-check by walking the constructed model.

        We construct a valid PositionAssessment with recommended_action=reduce
        and reduce parameters, then mutate the recommended_action via
        ``model_copy`` so the parse-time discriminator no longer sees them
        agree. ``StrategistOutput.model_construct`` skips re-validation so the
        mismatch survives long enough for the validator to catch it.
        """
        reduce_assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(
                action="reduce",
                quantity=2.0,
                order_type="market",
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim to cure breach.",
        )
        mutated = reduce_assessment.model_copy(update={"recommended_action": "close"})
        baseline = _make_output()
        output = StrategistOutput.model_construct(
            invocation_id=baseline.invocation_id,
            timestamp=baseline.timestamp,
            mode=baseline.mode,
            position_assessments=(mutated,),
            pending_order_assessments=baseline.pending_order_assessments,
            portfolio_level_observations=baseline.portfolio_level_observations,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "action_parameters_match" in rules

    def test_matching_action_parameters_passes(self) -> None:
        reduce_assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(
                action="reduce",
                quantity=2.0,
                order_type="market",
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim to cure breach.",
        )
        output = _make_output(position_assessments=(reduce_assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "action_parameters_match" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — assessment_id uniqueness within position_assessments[]
# ---------------------------------------------------------------------------


class TestAssessmentIdUnique:
    def test_duplicate_assessment_id_is_failure(self) -> None:
        a1 = _make_position_assessment(assessment_id="SA-1", position_id="POS-A")
        a2 = _make_position_assessment(assessment_id="SA-1", position_id="POS-B")
        output = _make_output(position_assessments=(a1, a2))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "assessment_id_unique" in rules

    def test_distinct_assessment_ids_pass(self) -> None:
        a1 = _make_position_assessment(assessment_id="SA-1", position_id="POS-A")
        a2 = _make_position_assessment(assessment_id="SA-2", position_id="POS-B")
        output = _make_output(position_assessments=(a1, a2))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "assessment_id_unique" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — pending_order_assessment_id uniqueness
# ---------------------------------------------------------------------------


class TestPendingOrderAssessmentIdUnique:
    def test_duplicate_pending_order_assessment_id_is_failure(self) -> None:
        o1 = _make_pending_order_assessment(
            pending_order_assessment_id="SA-ORD-1", order_id="ORD-A"
        )
        o2 = _make_pending_order_assessment(
            pending_order_assessment_id="SA-ORD-1", order_id="ORD-B"
        )
        output = _make_output(pending_order_assessments=(o1, o2))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "pending_order_assessment_id_unique" in rules

    def test_distinct_pending_order_assessment_ids_pass(self) -> None:
        o1 = _make_pending_order_assessment(
            pending_order_assessment_id="SA-ORD-1", order_id="ORD-A"
        )
        o2 = _make_pending_order_assessment(
            pending_order_assessment_id="SA-ORD-2", order_id="ORD-B"
        )
        output = _make_output(pending_order_assessments=(o1, o2))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "pending_order_assessment_id_unique" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — linked_position_assessment_id referential integrity
# ---------------------------------------------------------------------------


class TestLinkedPositionAssessmentId:
    def test_linked_position_assessment_id_missing_target_is_failure(self) -> None:
        position = _make_position_assessment(assessment_id="SA-1")
        order = _make_pending_order_assessment(linked_position_assessment_id="SA-99")
        output = _make_output(
            position_assessments=(position,),
            pending_order_assessments=(order,),
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "linked_position_assessment_id_resolves" in rules

    def test_linked_position_assessment_id_resolves_passes(self) -> None:
        position = _make_position_assessment(assessment_id="SA-1")
        order = _make_pending_order_assessment(linked_position_assessment_id="SA-1")
        output = _make_output(
            position_assessments=(position,),
            pending_order_assessments=(order,),
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "linked_position_assessment_id_resolves" not in rules

    def test_no_linked_position_assessment_id_no_failure(self) -> None:
        """An order without a link is not subject to this check."""
        position = _make_position_assessment(assessment_id="SA-1")
        order = _make_pending_order_assessment()
        output = _make_output(
            position_assessments=(position,),
            pending_order_assessments=(order,),
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "linked_position_assessment_id_resolves" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — remedy_flag ↔ addressed_breaches[].breach_id pairing
# ---------------------------------------------------------------------------


def _plo_with_breaches(
    *,
    addressed: tuple[RegimeTransitionAddressedBreach, ...] = (),
    uncured: tuple[RegimeTransitionUncuredBreach, ...] = (),
) -> PortfolioLevelObservations:
    return _make_portfolio_observations(
        regime_transition_summary=RegimeTransitionSummary(
            addressed_breaches=addressed,
            uncured_breaches=uncured,
        ),
    )


class TestRemedyFlagBreachPairing:
    def test_remedy_flag_not_in_addressed_breaches_is_failure(self) -> None:
        """A remedy_flag with no matching addressed_breach.breach_id → FAIL."""
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-2",
            remedy_rationale="Cures BREACH-2.",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "remedy_flag_pairing" in rules

    def test_addressed_breach_without_remedy_flag_is_failure(self) -> None:
        """An addressed_breach.breach_id with no matching remedy_flag → FAIL."""
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1.",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-2", remedy_assessment_ids=("SA-1",)
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "remedy_flag_pairing" in rules

    def test_balanced_remedy_flag_and_addressed_breach_pass(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1.",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "remedy_flag_pairing" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — addressed and uncured breach IDs are disjoint
# ---------------------------------------------------------------------------


class TestAddressedUncuredDisjoint:
    def test_breach_in_both_addressed_and_uncured_is_failure(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1.",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
            uncured=(
                RegimeTransitionUncuredBreach(
                    breach_id="BREACH-1", rationale="Also listed uncured."
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "addressed_uncured_disjoint" in rules

    def test_disjoint_addressed_and_uncured_passes(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1.",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
            uncured=(
                RegimeTransitionUncuredBreach(
                    breach_id="BREACH-2", rationale="Cannot remedy this cycle."
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "addressed_uncured_disjoint" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — sector ∈ active_sectors
# ---------------------------------------------------------------------------


class TestSectorInActiveSectors:
    def test_sector_outside_active_set_is_failure(self) -> None:
        """A `tech` assessment when active_sectors omits `tech` → FAIL."""
        assessment = _make_position_assessment(sector="tech")
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=frozenset({"semis", "financials", "energy"}),
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "sector_not_active" in rules

    def test_sector_inside_active_set_passes(self) -> None:
        assessment = _make_position_assessment(sector="semis")
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=frozenset({"semis"}),
        )
        rules = [f.rule for f in result.failures]
        assert "sector_not_active" not in rules


# ---------------------------------------------------------------------------
# Layer-2 — defensive_posture_summary presence (defensive double-check)
# ---------------------------------------------------------------------------


class TestDefensivePostureSummaryPresence:
    def test_defensive_posture_with_summary_passes(self) -> None:
        plo = _make_portfolio_observations(
            defensive_posture_summary=DefensivePostureSummary(
                reduction_priority=(
                    ReductionPriorityEntry(
                        position_id=PositionId("POS-NVDA-001"), priority_rationale="Weakest thesis."
                    ),
                ),
                capital_preservation_notes="Capital preservation notes.",
            ),
        )
        output = _make_output(
            mode="defensive_posture",
            position_assessments=(_make_position_assessment(),),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "defensive_posture_summary_present" not in rules

    def test_defensive_posture_without_summary_is_failure(self) -> None:
        """Pydantic catches this on parse; the validator double-checks via
        ``model_construct`` so a forged StrategistOutput cannot slip through.
        """
        plo = _make_portfolio_observations()  # no defensive_posture_summary
        baseline = _make_output()
        output = StrategistOutput.model_construct(
            invocation_id=baseline.invocation_id,
            timestamp=baseline.timestamp,
            mode="defensive_posture",
            position_assessments=baseline.position_assessments,
            pending_order_assessments=baseline.pending_order_assessments,
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        rules = [f.rule for f in result.failures]
        assert "defensive_posture_summary_present" in rules


# ---------------------------------------------------------------------------
# Layer-3 — referential integrity in narrative fields
# ---------------------------------------------------------------------------


class TestLayer3PositionAssessmentReferences:
    def test_unknown_reference_in_status_rationale_is_failure(self) -> None:
        assessment = _make_position_assessment(
            status_rationale="Cited [SA-TECH-99] which is missing.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-TECH-99" in f.message for f in unknown_failures)
        assert any("status_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_action_rationale_is_failure(self) -> None:
        assessment = _make_position_assessment(
            action_rationale="Per [QR-99] no action.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("QR-99" in f.message for f in unknown_failures)
        assert any("action_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_reduce_rationale_is_failure(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim per [SA-FIN-99].",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-FIN-99" in f.message for f in unknown_failures)
        assert any("reduce_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_add_conviction_justification_is_failure(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="add",
            action_parameters=AddParameters(
                action="add",
                additional_quantity=1.0,
                additional_dollar_value=100.0,
                entry_order=EntryOrder(type="market"),
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=1.0, net_directional_impact=1.0
            ),
            guardrail_validation_result=_make_guardrail_result(),
            add_conviction_justification="Strengthening absent at entry per [AR-99].",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("AR-99" in f.message for f in unknown_failures)
        assert any("add_conviction_justification" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_adjustment_rationale_is_failure(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="adjust-bracket",
            action_parameters=AdjustBracketParameters(
                action="adjust-bracket",
                new_stop_level=BracketAdjustNewStopLevel(trigger_price=820.0, order_type="market"),
            ),
            adjustment_rationale="Tighten stop per [CR-99].",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("CR-99" in f.message for f in unknown_failures)
        assert any("adjustment_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_remedy_rationale_is_failure(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1; see [QR-CW-99].",
        )
        plo = _plo_with_breaches(
            addressed=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
        )
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("QR-CW-99" in f.message for f in unknown_failures)
        assert any("remedy_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_cross_position_observations_is_failure(self) -> None:
        assessment = _make_position_assessment(
            cross_position_observations="Cited [SA-ENERGY-99] across positions.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-ENERGY-99" in f.message for f in unknown_failures)
        assert any("cross_position_observations" in f.field_path for f in unknown_failures)

    def test_known_reference_passes(self) -> None:
        assessment = _make_position_assessment(
            status_rationale="Per [SA-TECH-2] thesis is intact.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "unknown_reference" not in rules

    def test_non_canonical_prefix_skipped(self) -> None:
        """Non-canonical prefixes (e.g., remedy/breach IDs) are not citations."""
        assessment = _make_position_assessment(
            status_rationale="Triggered [BREACH-1] internally; not a citation.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        rules = [f.rule for f in result.failures]
        assert "unknown_reference" not in rules


class TestLayer3PendingOrderReferences:
    def test_unknown_reference_in_drift_rationale_is_failure(self) -> None:
        order = _make_pending_order_assessment(
            drift_rationale="Per [SA-TECH-99] limit is too far.",
        )
        output = _make_output(pending_order_assessments=(order,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-TECH-99" in f.message for f in unknown_failures)
        assert any("drift_rationale" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_pending_order_action_rationale_is_failure(self) -> None:
        order = _make_pending_order_assessment(
            action_rationale="Cancel per [QR-99].",
        )
        output = _make_output(pending_order_assessments=(order,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("QR-99" in f.message for f in unknown_failures)
        # Field path should reference pending_order_assessments
        assert any(
            "pending_order_assessments" in f.field_path and "action_rationale" in f.field_path
            for f in unknown_failures
        )


class TestLayer3PortfolioLevelReferences:
    def test_unknown_reference_in_aggregate_thesis_health_is_failure(self) -> None:
        plo = _make_portfolio_observations(
            aggregate_thesis_health="Citing [SA-TECH-99] as a missing ref.",
        )
        output = _make_output(portfolio_level_observations=plo)
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-TECH-99" in f.message for f in unknown_failures)
        assert any("aggregate_thesis_health" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_sector_balance_shifts_is_failure(self) -> None:
        plo = _make_portfolio_observations(
            sector_balance_shifts="Per [SA-FIN-99] sector tilt shifted.",
        )
        output = _make_output(portfolio_level_observations=plo)
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("SA-FIN-99" in f.message for f in unknown_failures)
        assert any("sector_balance_shifts" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_thesis_dependency_warnings_is_failure(self) -> None:
        plo = _make_portfolio_observations(
            thesis_dependency_warnings="Per [CR-99] correlation tightening.",
        )
        output = _make_output(portfolio_level_observations=plo)
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("CR-99" in f.message for f in unknown_failures)
        assert any("thesis_dependency_warnings" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_capital_allocation_observations_is_failure(self) -> None:
        plo = _make_portfolio_observations(
            capital_allocation_observations="Per [AR-99] capital is tight.",
        )
        output = _make_output(portfolio_level_observations=plo)
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("AR-99" in f.message for f in unknown_failures)
        assert any("capital_allocation_observations" in f.field_path for f in unknown_failures)

    def test_unknown_reference_in_capital_preservation_notes_is_failure(self) -> None:
        plo = _make_portfolio_observations(
            defensive_posture_summary=DefensivePostureSummary(
                reduction_priority=(
                    ReductionPriorityEntry(
                        position_id=PositionId("POS-NVDA-001"),
                        priority_rationale="Weakest thesis.",
                    ),
                ),
                capital_preservation_notes="Per [QR-CW-99] preserve cash.",
            ),
        )
        output = _make_output(
            mode="defensive_posture",
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any("QR-CW-99" in f.message for f in unknown_failures)
        assert any("capital_preservation_notes" in f.field_path for f in unknown_failures)


# ---------------------------------------------------------------------------
# Acceptance-criteria scenarios — schema-valid output with all refs resolved
# passes; each named synthetic FAIL produces a clear field path.
# ---------------------------------------------------------------------------


class TestSchemaValidHappyPath:
    def test_schema_valid_output_with_all_refs_passes_clean(self) -> None:
        """A complete schema-valid output with every cited ref in the store
        and every assessed sector active validates without failures or
        warnings.
        """
        reduce_assessment = _make_position_assessment(
            assessment_id="SA-1",
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=2.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.35, net_directional_impact=-1.35
            ),
            reduce_rationale="Trim per [SA-TECH-2].",
            remedy_flag="BREACH-1",
            remedy_rationale="Cures BREACH-1.",
            cross_position_observations="See SA-2 for adjacent read.",
        )
        hold_assessment = _make_position_assessment(
            assessment_id="SA-2",
            position_id="POS-JPM-002",
            sector="financials",
            status_rationale="Per [SA-FIN-4] thesis intact.",
        )
        order = _make_pending_order_assessment(
            pending_order_assessment_id="SA-ORD-1",
            linked_position_assessment_id="SA-1",
            drift_rationale="Per [QR-2] entry is far from current price.",
            action_rationale="Maintain for now.",
        )
        plo = _make_portfolio_observations(
            aggregate_thesis_health="Both positions tracked per [CR-3].",
            regime_transition_summary=RegimeTransitionSummary(
                addressed_breaches=(
                    RegimeTransitionAddressedBreach(
                        breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                    ),
                ),
                uncured_breaches=(),
            ),
        )
        output = _make_output(
            position_assessments=(reduce_assessment, hold_assessment),
            pending_order_assessments=(order,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store("SA-TECH-2", "SA-FIN-4", "QR-2", "CR-3"),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "PASS"
        assert result.failures == ()
        assert result.warnings == ()


class TestSyntheticFailScenarios:
    """The four synthetic FAIL fixtures the story acceptance criteria name.

    Each produces FAIL with a clear field path identifying the failing field.
    """

    def test_missing_linked_position_assessment_id_target(self) -> None:
        position = _make_position_assessment(assessment_id="SA-1")
        order = _make_pending_order_assessment(linked_position_assessment_id="SA-99")
        output = _make_output(
            position_assessments=(position,),
            pending_order_assessments=(order,),
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        link_failures = [
            f for f in result.failures if f.rule == "linked_position_assessment_id_resolves"
        ]
        assert any("linked_position_assessment_id" in f.field_path for f in link_failures)

    def test_remedy_flag_not_in_addressed_breaches(self) -> None:
        assessment = _make_position_assessment(
            recommended_action="reduce",
            action_parameters=ReduceParameters(action="reduce", quantity=1.0, order_type="market"),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=-1.0, net_directional_impact=-1.0
            ),
            reduce_rationale="Trim.",
            remedy_flag="BREACH-99",
            remedy_rationale="Cures BREACH-99.",
        )
        plo = _plo_with_breaches()  # no addressed breaches
        output = _make_output(
            position_assessments=(assessment,),
            portfolio_level_observations=plo,
        )
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        remedy_failures = [f for f in result.failures if f.rule == "remedy_flag_pairing"]
        assert any("remedy_flag" in f.field_path for f in remedy_failures)

    def test_unresolvable_qr_99_reference(self) -> None:
        assessment = _make_position_assessment(
            status_rationale="Cited [QR-99] which is missing.",
        )
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
        )
        assert result.overall == "FAIL"
        unknown_failures = [f for f in result.failures if f.rule == "unknown_reference"]
        assert any(
            "QR-99" in f.message and "status_rationale" in f.field_path for f in unknown_failures
        )

    def test_sector_outside_active_sectors(self) -> None:
        assessment = _make_position_assessment(sector="energy")
        output = _make_output(position_assessments=(assessment,))
        result = validate_strategist_output(
            output,
            retrieval_store=_store_with_baseline_refs(),
            active_sectors=frozenset({"semis", "financials"}),
        )
        assert result.overall == "FAIL"
        sector_failures = [f for f in result.failures if f.rule == "sector_not_active"]
        assert any("sector" in f.field_path for f in sector_failures)


class TestAggregation:
    def test_multiple_failures_collected_not_short_circuited(self) -> None:
        """Validator accumulates failures across invariants and references."""
        a1 = _make_position_assessment(
            assessment_id="SA-1",
            sector="energy",  # outside active_sectors below
            status_rationale="Cited [SA-FIN-99] which is missing.",
        )
        a2 = _make_position_assessment(
            assessment_id="SA-1",  # duplicate id
            position_id="POS-OTHER",
            sector="semis",
        )
        output = _make_output(position_assessments=(a1, a2))
        result = validate_strategist_output(
            output,
            retrieval_store=_retrieval_store(),
            active_sectors=frozenset({"semis", "financials"}),
        )
        assert result.overall == "FAIL"
        rules = {f.rule for f in result.failures}
        assert "sector_not_active" in rules
        assert "unknown_reference" in rules
        assert "assessment_id_unique" in rules
