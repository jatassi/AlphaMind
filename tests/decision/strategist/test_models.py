"""Tests for StrategistOutput data model — ALP-303.

Mirrors the analyst-side test pattern: round-trip the strategist prompt's
``<example_output>`` block, then exercise each conditional invariant declared
in ``docs/design/04-decision-layer/strategist-output-schema.md`` via direct
construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from alphamind._kernel.ids import (
    InvocationId,
    PositionId,
    RecommendationId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.decision.strategist.models import (
    AddParameters,
    AdjustBracketParameters,
    BracketAdjustNewStopLevel,
    BracketAdjustNewTargetLevel,
    CloseParameters,
    DefensivePostureSummary,
    EntryOrder,
    ExposureImpact,
    GuardrailValidationResult,
    ModificationParameters,
    NewEventInvalidation,
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
    ReduceParameters,
    ReductionPriorityEntry,
    RegimeTransitionAddressedBreach,
    RegimeTransitionSummary,
    RegimeTransitionUncuredBreach,
    StrategistOutput,
    ThesisComponentUpdate,
)
from alphamind.risk_guardrails.guardrail_evaluation import RuleProjection, Status

# ---------------------------------------------------------------------------
# Strategist prompt's <example_output> normal-mode payload
# (prompts/decision/strategist.md lines 138-219).
# ---------------------------------------------------------------------------


_EXAMPLE_OUTPUT_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "timestamp": "2026-04-23T14:33:47Z",
    "mode": "normal",
    "position_assessments": [
        {
            "assessment_id": "SA-1",
            "position_id": "POS-NVDA-001",
            "thesis_id": "TH-NVDA-001",
            "underlying": "NVDA",
            "sector": "semis",
            "thesis_status": "at-risk",
            "prior_status": "at-risk",
            "recommended_action": "reduce",
            "action_parameters": {
                "action": "reduce",
                "quantity": 2,
                "order_type": "limit",
                "limit_price": 843.00,
            },
            "exposure_impact": {
                "sector_delta_adjusted_change": -1.35,
                "net_directional_impact": -1.35,
            },
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [
                    {
                        "rule": "sector_concentration",
                        "status": "PASS",
                        "current": 18.3,
                        "limit": 20.0,
                        "projected_after": 16.95,
                        "headroom_remaining": 3.05,
                        "unit": "% of portfolio (delta-adjusted)",
                    },
                    {
                        "rule": "per_position_max_size",
                        "status": "PASS",
                        "current": 4.5,
                        "limit": 3.5,
                        "projected_after": 3.15,
                        "headroom_remaining": 0.35,
                        "unit": "% of portfolio",
                    },
                ],
                "cumulative_impact_note": "Remedy call #1 in this invocation.",
                "checked_at": "2026-04-23T14:33:12Z",
            },
            "remedy_flag": "BREACH-1",
            "status_rationale": (
                "[SA-TECH-2] reiterates the hyperscaler-capex leg; "
                "[CR-3] flags correlation tightening among NVDA/AMD/AVGO; "
                "[QR-5] shows prediction-market probability softening 71%->64%."
            ),
            "action_rationale": (
                "Reduce to 3.15% of portfolio to cure BREACH-1 while preserving the residual case."
            ),
            "reduce_rationale": (
                "Partial rather than full because the core catalyst fires "
                "inside 30 hours and residual legs remain intact."
            ),
            "remedy_rationale": (
                "BREACH-1 overage is 1.0% of portfolio. Reducing by 2 contracts "
                "cures the breach with 0.35% headroom."
            ),
            "cross_position_observations": (
                "Correlation tightening flagged in [CR-3]; see SA-2 for the adjacent-position read."
            ),
        },
        {
            "assessment_id": "SA-2",
            "position_id": "POS-JPM-002",
            "thesis_id": "TH-JPM-002",
            "underlying": "JPM",
            "sector": "financials",
            "thesis_status": "on-track",
            "prior_status": "on-track",
            "recommended_action": "hold",
            "status_rationale": (
                "[SA-FIN-4] confirms the credit-spread-compression leg; "
                "[QR-2] shows no repricing of the JPM-specific catalyst window."
            ),
            "action_rationale": (
                "No signal in this invocation warrants a parameter change. "
                "Hold without modification."
            ),
            "cross_position_observations": ("No shared-catalyst exposure with POS-NVDA-001."),
        },
    ],
    "pending_order_assessments": [
        {
            "pending_order_assessment_id": "SA-ORD-1",
            "order_id": "ORD-LIMIT-4",
            "position_id": "PENDING-AVGO-003",
            "order_type": "entry_limit",
            "order_age_hours": 36,
            "current_distance_pct": 5.53,
            "fill_probability_assessment": "unlikely",
            "recommended_action": "cancel",
            "drift_rationale": (
                "Entry limit at $380 placed when AVGO traded at $385. AVGO is now at $401."
            ),
            "action_rationale": ("Cancel. The thesis that justified the $380 entry is overtaken."),
        }
    ],
    "portfolio_level_observations": {
        "aggregate_thesis_health": (
            "Two open positions; one on-track, one at-risk with a remedy trim."
        ),
        "sector_balance_shifts": ("POS-NVDA-001 trim moves semis from 18.3% to 16.95%."),
        "thesis_dependency_warnings": ("[CR-3] correlation tightening signal bears watching."),
        "capital_allocation_observations": ("Book is modestly capital-efficient at two positions."),
        "regime_transition_summary": {
            "addressed_breaches": [{"breach_id": "BREACH-1", "remedy_assessment_ids": ["SA-1"]}],
            "uncured_breaches": [],
        },
    },
}


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _make_per_rule(**overrides: Any) -> RuleProjection:
    defaults: dict[str, Any] = {
        "rule": "per_position_max_size",
        "status": Status.PASS,
        "current": 0.0,
        "limit": 5.0,
        "projected_after": 3.0,
        "headroom_remaining": 2.0,
        "unit": "% of portfolio",
    }
    return RuleProjection(**(defaults | overrides))


def _make_guardrail_result(**overrides: Any) -> GuardrailValidationResult:
    defaults: dict[str, Any] = {
        "overall": "PASS",
        "per_rule": (_make_per_rule(),),
        "checked_at": _ts("2026-04-23T14:31:10Z"),
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
        "status_rationale": "Test status rationale.",
        "action_rationale": "Test action rationale.",
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
        "drift_rationale": "Test drift rationale.",
        "action_rationale": "Test action rationale.",
    }
    return PendingOrderAssessment(**(defaults | overrides))


def _make_portfolio_observations(**overrides: Any) -> PortfolioLevelObservations:
    defaults: dict[str, Any] = {
        "aggregate_thesis_health": "Test aggregate health.",
        "sector_balance_shifts": "Test sector balance.",
        "thesis_dependency_warnings": "Test warnings.",
        "capital_allocation_observations": "Test capital observations.",
    }
    return PortfolioLevelObservations(**(defaults | overrides))


def _make_output(**overrides: Any) -> StrategistOutput:
    defaults: dict[str, Any] = {
        "invocation_id": "inv-001",
        "timestamp": _ts("2026-04-23T14:33:47Z"),
        "mode": "normal",
        "position_assessments": (),
        "pending_order_assessments": (),
        "portfolio_level_observations": _make_portfolio_observations(),
    }
    return StrategistOutput(**(defaults | overrides))


# ---------------------------------------------------------------------------
# 1. Round-trip the strategist prompt's <example_output> payload
# ---------------------------------------------------------------------------


class TestExampleOutputRoundTrip:
    def test_example_output_round_trips_via_model_validate(self) -> None:
        """The strategist.md prompt's <example_output> coerces without loss."""
        output = StrategistOutput.model_validate(_EXAMPLE_OUTPUT_PAYLOAD)
        assert output.mode == "normal"
        assert len(output.position_assessments) == 2

        sa1 = output.position_assessments[0]
        assert sa1.assessment_id == "SA-1"
        assert sa1.thesis_status == "at-risk"
        assert sa1.recommended_action == "reduce"
        assert isinstance(sa1.action_parameters, ReduceParameters)
        assert sa1.action_parameters.quantity == 2
        assert sa1.guardrail_validation_result is not None
        assert sa1.guardrail_validation_result.overall == "PASS"
        assert len(sa1.guardrail_validation_result.per_rule) == 2
        assert sa1.remedy_flag == "BREACH-1"

        sa2 = output.position_assessments[1]
        assert sa2.recommended_action == "hold"
        assert sa2.action_parameters is None

        assert len(output.pending_order_assessments) == 1
        ord1 = output.pending_order_assessments[0]
        assert ord1.recommended_action == "cancel"

        plo = output.portfolio_level_observations
        assert plo.regime_transition_summary is not None
        assert len(plo.regime_transition_summary.addressed_breaches) == 1
        assert plo.regime_transition_summary.addressed_breaches[0].breach_id == "BREACH-1"


# ---------------------------------------------------------------------------
# 2. Mode-conditional invariants on StrategistOutput
# ---------------------------------------------------------------------------


class TestModeInvariants:
    def test_defensive_posture_requires_summary(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)defensive_posture_summary"):
            _make_output(mode="defensive_posture")

    def test_defensive_posture_with_summary_accepted(self) -> None:
        plo = _make_portfolio_observations(
            defensive_posture_summary=DefensivePostureSummary(
                reduction_priority=(
                    ReductionPriorityEntry(
                        position_id=PositionId("POS-NVDA-001"),
                        priority_rationale="Weakest thesis.",
                    ),
                ),
                capital_preservation_notes="Test notes.",
            ),
        )
        output = _make_output(mode="defensive_posture", portfolio_level_observations=plo)
        assert output.mode == "defensive_posture"
        assert output.portfolio_level_observations.defensive_posture_summary is not None

    def test_defensive_posture_forbids_add_action(self) -> None:
        plo = _make_portfolio_observations(
            defensive_posture_summary=DefensivePostureSummary(
                reduction_priority=(),
                capital_preservation_notes="Notes.",
            ),
        )
        add_assessment = _make_position_assessment(
            recommended_action="add",
            action_parameters=AddParameters(
                action="add",
                additional_quantity=1.0,
                additional_dollar_value=money(100.0),
                entry_order=EntryOrder(type="market"),
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=money(1.0), net_directional_impact=money(1.0)
            ),
            guardrail_validation_result=_make_guardrail_result(),
            add_conviction_justification="Strengthening signal absent at entry.",
            thesis_status="on-track",
        )
        with pytest.raises(ValidationError, match=r"(?i)defensive_posture"):
            _make_output(
                mode="defensive_posture",
                position_assessments=(add_assessment,),
                portfolio_level_observations=plo,
            )

    def test_normal_mode_with_add_accepted(self) -> None:
        add_assessment = _make_position_assessment(
            recommended_action="add",
            action_parameters=AddParameters(
                action="add",
                additional_quantity=1.0,
                additional_dollar_value=money(100.0),
                entry_order=EntryOrder(type="market"),
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=money(1.0), net_directional_impact=money(1.0)
            ),
            guardrail_validation_result=_make_guardrail_result(),
            add_conviction_justification="Strengthening signal absent at entry.",
        )
        output = _make_output(mode="normal", position_assessments=(add_assessment,))
        assert output.position_assessments[0].recommended_action == "add"


# ---------------------------------------------------------------------------
# 3. Timestamp tz-awareness
# ---------------------------------------------------------------------------


class TestTimestampTzAware:
    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)tz|timezone|aware"):
            _make_output(timestamp=datetime(2026, 4, 23, 14, 33, 47))  # noqa: DTZ001

    def test_utc_aware_timestamp_accepted(self) -> None:
        output = _make_output(timestamp=datetime(2026, 4, 23, 14, 33, 47, tzinfo=UTC))
        assert output.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# 4. PositionAssessment invariants
# ---------------------------------------------------------------------------


class TestPositionAssessmentInvariants:
    def test_invalidated_requires_close(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)invalidated"):
            _make_position_assessment(
                thesis_status="invalidated",
                recommended_action="hold",
            )

    def test_invalidated_with_close_accepted(self) -> None:
        pa = _make_position_assessment(
            thesis_status="invalidated",
            recommended_action="close",
            action_parameters=CloseParameters(
                action="close",
                quantity="all",
                order_type="market",
                close_rationale_type="thesis_invalidated",
            ),
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=signed_money(-1.0),
                net_directional_impact=signed_money(-1.0),
            ),
        )
        assert pa.thesis_status == "invalidated"
        assert pa.recommended_action == "close"

    def test_action_parameters_must_match_recommended_action(self) -> None:
        """Discriminator routing rejects mismatched action literal."""
        with pytest.raises(ValidationError):
            _make_position_assessment(
                recommended_action="reduce",
                action_parameters=CloseParameters(
                    action="close",
                    quantity=2.0,
                    order_type="market",
                    close_rationale_type="risk_management",
                ),
                exposure_impact=ExposureImpact(
                    sector_delta_adjusted_change=signed_money(-1.0),
                    net_directional_impact=signed_money(-1.0),
                ),
                reduce_rationale="Test reduce rationale.",
            )

    def test_reduce_requires_action_parameters(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)action_parameters"):
            _make_position_assessment(
                recommended_action="reduce",
                action_parameters=None,
                reduce_rationale="Test reduce rationale.",
                exposure_impact=ExposureImpact(
                    sector_delta_adjusted_change=signed_money(-1.0),
                    net_directional_impact=signed_money(-1.0),
                ),
            )

    def test_reduce_requires_reduce_rationale(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)reduce_rationale"):
            _make_position_assessment(
                recommended_action="reduce",
                action_parameters=ReduceParameters(
                    action="reduce", quantity=2.0, order_type="market"
                ),
                exposure_impact=ExposureImpact(
                    sector_delta_adjusted_change=signed_money(-1.0),
                    net_directional_impact=signed_money(-1.0),
                ),
            )

    def test_reduce_requires_exposure_impact(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)exposure_impact"):
            _make_position_assessment(
                recommended_action="reduce",
                action_parameters=ReduceParameters(
                    action="reduce", quantity=2.0, order_type="market"
                ),
                reduce_rationale="Test reduce rationale.",
            )

    def test_close_requires_exposure_impact(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)exposure_impact"):
            _make_position_assessment(
                recommended_action="close",
                action_parameters=CloseParameters(
                    action="close",
                    quantity="all",
                    order_type="market",
                    close_rationale_type="thesis_invalidated",
                ),
                thesis_status="invalidated",
            )

    def test_adjust_bracket_requires_adjustment_rationale(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)adjustment_rationale"):
            _make_position_assessment(
                recommended_action="adjust-bracket",
                action_parameters=AdjustBracketParameters(
                    action="adjust-bracket",
                    new_target_level=BracketAdjustNewTargetLevel(
                        price=price(900.0), order_type="limit"
                    ),
                ),
            )

    def test_add_requires_add_conviction_justification(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)add_conviction_justification"):
            _make_position_assessment(
                recommended_action="add",
                action_parameters=AddParameters(
                    action="add",
                    additional_quantity=1.0,
                    additional_dollar_value=money(100.0),
                    entry_order=EntryOrder(type="market"),
                ),
                exposure_impact=ExposureImpact(
                    sector_delta_adjusted_change=money(1.0), net_directional_impact=money(1.0)
                ),
                guardrail_validation_result=_make_guardrail_result(),
            )

    def test_add_requires_guardrail_validation_result(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)guardrail_validation_result"):
            _make_position_assessment(
                recommended_action="add",
                action_parameters=AddParameters(
                    action="add",
                    additional_quantity=1.0,
                    additional_dollar_value=money(100.0),
                    entry_order=EntryOrder(type="market"),
                ),
                exposure_impact=ExposureImpact(
                    sector_delta_adjusted_change=money(1.0), net_directional_impact=money(1.0)
                ),
                add_conviction_justification="Strengthening signal.",
            )

    def test_remedy_flag_requires_remedy_rationale(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)remedy_rationale"):
            _make_position_assessment(remedy_flag="BREACH-1", remedy_rationale=None)

    def test_remedy_flag_with_rationale_accepted(self) -> None:
        pa = _make_position_assessment(
            remedy_flag="BREACH-1",
            remedy_rationale="Cures the breach with headroom.",
        )
        assert pa.remedy_flag == "BREACH-1"

    def test_component_health_field_present_default_empty(self) -> None:
        """ALP-351 — PositionAssessment carries the new per-component health snapshot.

        Defaults to empty so existing callers and the schema's ``allOf`` blocks
        keep working; producers populate it when they re-assess cited signals.
        """
        pa = _make_position_assessment()
        assert pa.component_health == ()

    def test_component_health_accepts_per_component_entries(self) -> None:
        """ALP-351 — each entry mirrors ComponentHealthEntry shape."""
        from alphamind.portfolio_state.records.theses import (
            SupportingSignal,
            SupportingSignalStatus,
        )
        from alphamind.portfolio_state.views.thesis_health import ComponentHealthEntry

        entry = ComponentHealthEntry(
            component_id="comp-entry",
            supporting_signals=(
                SupportingSignal(name="capex", status=SupportingSignalStatus.STRENGTHENED),
            ),
        )
        pa = _make_position_assessment(component_health=(entry,))
        assert pa.component_health == (entry,)


# ---------------------------------------------------------------------------
# 5. CloseParameters: conviction_reduced forbids quantity="all"
# ---------------------------------------------------------------------------


class TestCloseParametersInvariants:
    def test_conviction_reduced_forbids_all(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)conviction_reduced|all"):
            CloseParameters(
                action="close",
                quantity="all",
                order_type="market",
                close_rationale_type="conviction_reduced",
            )

    def test_conviction_reduced_with_partial_accepted(self) -> None:
        cp = CloseParameters(
            action="close",
            quantity=2.0,
            order_type="market",
            close_rationale_type="conviction_reduced",
        )
        assert cp.quantity == 2.0

    def test_thesis_invalidated_with_all_accepted(self) -> None:
        cp = CloseParameters(
            action="close",
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
        )
        assert cp.quantity == "all"

    def test_limit_order_requires_limit_price(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)limit_price"):
            CloseParameters(
                action="close",
                quantity=2.0,
                order_type="limit",
                close_rationale_type="risk_management",
            )

    def test_limit_with_price_accepted(self) -> None:
        cp = CloseParameters(
            action="close",
            quantity=2.0,
            order_type="limit",
            limit_price=price(843.0),
            close_rationale_type="risk_management",
        )
        assert cp.limit_price == 843.0


class TestReduceParametersInvariants:
    def test_limit_requires_limit_price(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)limit_price"):
            ReduceParameters(action="reduce", quantity=2.0, order_type="limit")

    def test_market_no_price_required(self) -> None:
        rp = ReduceParameters(action="reduce", quantity=2.0, order_type="market")
        assert rp.limit_price is None


# ---------------------------------------------------------------------------
# 6. AdjustBracketParameters: at least one field required
# ---------------------------------------------------------------------------


class TestAdjustBracketAtLeastOneField:
    def test_no_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)at least one"):
            AdjustBracketParameters(action="adjust-bracket")

    def test_only_new_stop_level_accepted(self) -> None:
        ap = AdjustBracketParameters(
            action="adjust-bracket",
            new_stop_level=BracketAdjustNewStopLevel(
                trigger_price=price(820.0), order_type="market"
            ),
        )
        assert ap.new_stop_level is not None

    def test_only_new_event_invalidation_accepted(self) -> None:
        ap = AdjustBracketParameters(
            action="adjust-bracket",
            new_event_invalidation=NewEventInvalidation(event_description="Earnings miss."),
        )
        assert ap.new_event_invalidation is not None

    def test_only_thesis_component_updates_accepted(self) -> None:
        ap = AdjustBracketParameters(
            action="adjust-bracket",
            thesis_component_updates=(
                ThesisComponentUpdate(
                    component_type="entry_rationale",
                    narrative="Updated entry rationale.",
                ),
            ),
        )
        assert ap.thesis_component_updates is not None


# ---------------------------------------------------------------------------
# 7. Discriminator routing on ActionParameters
# ---------------------------------------------------------------------------


class TestActionParametersDiscriminator:
    def test_close_routes_to_close_parameters(self) -> None:
        pa = _make_position_assessment(
            recommended_action="close",
            action_parameters={
                "action": "close",
                "quantity": "all",
                "order_type": "market",
                "close_rationale_type": "thesis_invalidated",
            },
            thesis_status="invalidated",
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=signed_money(-1.0),
                net_directional_impact=signed_money(-1.0),
            ),
        )
        assert isinstance(pa.action_parameters, CloseParameters)

    def test_reduce_routes_to_reduce_parameters(self) -> None:
        pa = _make_position_assessment(
            recommended_action="reduce",
            action_parameters={
                "action": "reduce",
                "quantity": 2.0,
                "order_type": "market",
            },
            reduce_rationale="Partial reduction rationale.",
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=signed_money(-1.0),
                net_directional_impact=signed_money(-1.0),
            ),
        )
        assert isinstance(pa.action_parameters, ReduceParameters)

    def test_adjust_bracket_routes_to_adjust_bracket_parameters(self) -> None:
        pa = _make_position_assessment(
            recommended_action="adjust-bracket",
            action_parameters={
                "action": "adjust-bracket",
                "new_target_level": {"price": 900.0, "order_type": "limit"},
            },
            adjustment_rationale="Updated target.",
        )
        assert isinstance(pa.action_parameters, AdjustBracketParameters)

    def test_add_routes_to_add_parameters(self) -> None:
        pa = _make_position_assessment(
            recommended_action="add",
            action_parameters={
                "action": "add",
                "additional_quantity": 1.0,
                "additional_dollar_value": 100.0,
                "entry_order": {"type": "market"},
            },
            exposure_impact=ExposureImpact(
                sector_delta_adjusted_change=money(1.0), net_directional_impact=money(1.0)
            ),
            guardrail_validation_result=_make_guardrail_result(),
            add_conviction_justification="Strengthening signal absent at entry.",
        )
        assert isinstance(pa.action_parameters, AddParameters)


# ---------------------------------------------------------------------------
# 8. PendingOrderAssessment: modify requires modification_parameters
# ---------------------------------------------------------------------------


class TestPendingOrderInvariants:
    def test_modify_requires_modification_parameters(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)modification_parameters"):
            _make_pending_order_assessment(
                recommended_action="modify",
                modification_parameters=None,
            )

    def test_modify_with_parameters_accepted(self) -> None:
        poa = _make_pending_order_assessment(
            recommended_action="modify",
            modification_parameters=ModificationParameters(new_limit_price=price(400.0)),
        )
        assert poa.modification_parameters is not None

    def test_maintain_no_parameters_required(self) -> None:
        poa = _make_pending_order_assessment(recommended_action="maintain")
        assert poa.modification_parameters is None

    def test_modification_parameters_at_least_one_field(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)at least one"):
            ModificationParameters()


# ---------------------------------------------------------------------------
# 9. Pattern checks
# ---------------------------------------------------------------------------


class TestPatterns:
    def test_assessment_id_pattern(self) -> None:
        for valid in ("SA-1", "SA-12", "SA-100"):
            pa = _make_position_assessment(assessment_id=valid)
            assert pa.assessment_id == valid
        for invalid in ("sa-1", "SA1", "SA-", "SA-ORD-1"):
            with pytest.raises(ValidationError):
                _make_position_assessment(assessment_id=invalid)

    def test_pending_order_assessment_id_pattern(self) -> None:
        for valid in ("SA-ORD-1", "SA-ORD-99"):
            poa = _make_pending_order_assessment(pending_order_assessment_id=valid)
            assert poa.pending_order_assessment_id == valid
        for invalid in ("SA-1", "sa-ord-1", "SA-ORD"):
            with pytest.raises(ValidationError):
                _make_pending_order_assessment(pending_order_assessment_id=invalid)

    def test_linked_position_assessment_id_pattern(self) -> None:
        poa = _make_pending_order_assessment(linked_position_assessment_id="SA-1")
        assert poa.linked_position_assessment_id == "SA-1"
        with pytest.raises(ValidationError):
            _make_pending_order_assessment(linked_position_assessment_id="SA-ORD-1")

    def test_addressed_breach_assessment_id_pattern(self) -> None:
        good = RegimeTransitionAddressedBreach(
            breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
        )
        assert good.remedy_assessment_ids == ("SA-1",)
        with pytest.raises(ValidationError):
            RegimeTransitionAddressedBreach(breach_id="BREACH-1", remedy_assessment_ids=("sa-1",))


# ---------------------------------------------------------------------------
# 10. Sector and status enum coverage
# ---------------------------------------------------------------------------


class TestEnums:
    def test_all_sectors_accepted(self) -> None:
        for s in ("tech", "semis", "financials", "energy"):
            pa = _make_position_assessment(sector=s)
            assert pa.sector == s

    def test_unknown_sector_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_position_assessment(sector="healthcare")

    def test_all_thesis_statuses_accepted(self) -> None:
        for s in ("on-track", "partially-realized", "at-risk", "stale", "invalidated"):
            if s == "invalidated":
                pa = _make_position_assessment(
                    thesis_status=s,
                    recommended_action="close",
                    action_parameters=CloseParameters(
                        action="close",
                        quantity="all",
                        order_type="market",
                        close_rationale_type="thesis_invalidated",
                    ),
                    exposure_impact=ExposureImpact(
                        sector_delta_adjusted_change=signed_money(-1.0),
                        net_directional_impact=signed_money(-1.0),
                    ),
                )
            else:
                pa = _make_position_assessment(thesis_status=s)
            assert pa.thesis_status == s

    def test_thesis_status_uppercase_rejected(self) -> None:
        """Schema vocabulary is lowercase-hyphenated; uppercase rejected."""
        with pytest.raises(ValidationError):
            _make_position_assessment(thesis_status="ON_TRACK")

    def test_prior_status_nullable(self) -> None:
        pa = _make_position_assessment(prior_status=None)
        assert pa.prior_status is None


# ---------------------------------------------------------------------------
# 11. Frozen-ness / extra=forbid
# ---------------------------------------------------------------------------


class TestFrozenForbid:
    def test_models_frozen(self) -> None:
        pa = _make_position_assessment()
        with pytest.raises(ValidationError):
            pa.assessment_id = RecommendationId("SA-99")

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError, match=r"(?i)extra"):
            StrategistOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=_ts("2026-04-23T14:33:47Z"),
                mode="normal",
                position_assessments=(),
                pending_order_assessments=(),
                portfolio_level_observations=_make_portfolio_observations(),
                unknown_field="oops",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# 12. JSON Schema export
# ---------------------------------------------------------------------------


class TestModelJsonSchema:
    def test_returns_dict(self) -> None:
        schema = StrategistOutput.model_json_schema()
        assert isinstance(schema, dict)

    def test_top_level_is_object(self) -> None:
        """Anthropic JSON-Schema mode rejects top-level oneOf/anyOf/allOf."""
        schema = StrategistOutput.model_json_schema()
        assert "oneOf" not in schema
        assert "anyOf" not in schema
        assert schema.get("type") == "object"

    def test_required_top_level_fields_present(self) -> None:
        schema = StrategistOutput.model_json_schema()
        required = set(schema.get("required", ()))
        assert {
            "invocation_id",
            "timestamp",
            "mode",
            "position_assessments",
            "pending_order_assessments",
            "portfolio_level_observations",
        }.issubset(required)


# ---------------------------------------------------------------------------
# 13. Public-surface re-exports
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_models_module_all_contains_every_name(self) -> None:
        import alphamind.decision.strategist.models as m

        expected = {
            "StrategistOutput",
            "PositionAssessment",
            "PendingOrderAssessment",
            "PortfolioLevelObservations",
            "ActionParameters",
            "CloseParameters",
            "ReduceParameters",
            "AdjustBracketParameters",
            "AddParameters",
            "ExposureImpact",
            "GuardrailValidationResult",
            "RuleProjection",
            "RegimeTransitionSummary",
            "DefensivePostureSummary",
            "ThesisStatus",
        }
        assert expected.issubset(set(m.__all__))

    def test_package_init_re_exports_models(self) -> None:
        import alphamind.decision.strategist as pkg

        for name in (
            "StrategistOutput",
            "PositionAssessment",
            "PendingOrderAssessment",
            "PortfolioLevelObservations",
            "ActionParameters",
            "ExposureImpact",
            "GuardrailValidationResult",
            "RegimeTransitionSummary",
            "DefensivePostureSummary",
            "ThesisStatus",
        ):
            assert hasattr(pkg, name), f"{name} not re-exported"


# ---------------------------------------------------------------------------
# 14. Greeks reuse
# ---------------------------------------------------------------------------


class TestCanonicalTypeReuse:
    def test_greeks_imported_from_canonical(self) -> None:
        from alphamind.decision.strategist.models import Greeks as ModelsG
        from alphamind.risk_guardrails.guardrail_evaluation import Greeks as CanonG

        assert ModelsG is CanonG

    def test_rule_projection_imported_from_canonical(self) -> None:
        from alphamind.decision.strategist.models import RuleProjection as ModelsRP
        from alphamind.risk_guardrails.guardrail_evaluation import (
            RuleProjection as CanonRP,
        )

        assert ModelsRP is CanonRP


# ---------------------------------------------------------------------------
# 15. RegimeTransitionSummary structure
# ---------------------------------------------------------------------------


class TestRegimeTransitionSummary:
    def test_uncured_breach_requires_non_empty_rationale(self) -> None:
        with pytest.raises(ValidationError):
            RegimeTransitionUncuredBreach(breach_id="BREACH-2", rationale="")

    def test_summary_construction(self) -> None:
        summary = RegimeTransitionSummary(
            addressed_breaches=(
                RegimeTransitionAddressedBreach(
                    breach_id="BREACH-1", remedy_assessment_ids=("SA-1",)
                ),
            ),
            uncured_breaches=(
                RegimeTransitionUncuredBreach(
                    breach_id="BREACH-2",
                    rationale="Hold-with-rationale on near-target position.",
                ),
            ),
        )
        assert len(summary.addressed_breaches) == 1
        assert len(summary.uncured_breaches) == 1
