"""Tests for PMEnvelope + PMCompletionRecord + minimal OMS command models — ALP-323.

Mirrors the strategist-side test pattern (tests/decision/strategist/test_models.py).
Covers each acceptance criterion of ALP-323 with at least one positive and one
negative test:

* Envelope-shape construction per source_provenance / verdict.
* Verdict-conditional invariants on commands/modifications/concerns.
* Modification-record invariants (adjustment_category ↔ phase + triggering_rule).
* envelope_id ↔ source_provenance pattern.
* PMCompletionRecord verdict-summary sum invariant.
* Schema parity against ``pm-envelope-schema.md``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter

from alphamind._kernel.ids import (
    EnvelopeId,
    InvocationId,
    PositionId,
    RecommendationId,
    Symbol,
)
from alphamind._kernel.money import money, price
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
    AdjustCommand,
    AntiPattern,
    CancelCommand,
    CloseCommand,
    ConcernRecord,
    CriterionAssessment,
    ModificationRecord,
    OMSCommand,
    OpenCommand,
    PMAnalystEnvelope,
    PMCompletionRecord,
    PMEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
    VerdictSummary,
    completion_record_schema,
    envelope_schema,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


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


def _close_command_basic() -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId("POS-NVDA-001"),
        quantity="all",
        order_type="market",
        limit_price=None,
        close_rationale_type="thesis_invalidated",
        invalidation_reason="Thesis broken.",
        risk_management_subtype=None,
    )


def _full_thesis_for_open() -> Thesis:
    return Thesis(
        summary="Long NVDA on AI capex tailwind.",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference="NVDA",
                narrative="Hyperscaler capex sustains topline growth.",
                key_assumptions=("Capex run-rate intact.",),
            ),
        ),
    )


def _hard_price_leg() -> PriceLeg:
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


def _open_command_basic() -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=Symbol("NVDA"), direction="long"),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(_hard_price_leg(),),
        thesis=_full_thesis_for_open(),
    )


# Canonical OPEN command JSON payload (used in TypeAdapter.validate_python tests).
# Mirrors the canonical schema in oms-command-schema.md (no transitional fields).
_OPEN_COMMAND_PAYLOAD: dict[str, Any] = {
    "command_type": "open",
    "instrument": {"asset_type": "equity", "ticker": "NVDA", "direction": "long"},
    "entry_order": {"type": "market"},
    "position_size": {"quantity": 10.0, "dollar_value": 10_000.0},
    "target": {
        "target_type": "absolute_price",
        "price": 950.0,
        "order_type": "limit",
    },
    "invalidation_legs": [
        {
            "type": "price",
            "is_hard": True,
            "condition": {
                "underlying_trigger": "NVDA",
                "comparator": "<=",
                "trigger_price": 750.0,
            },
            "order_parameters": {"order_type": "market"},
        }
    ],
    "thesis": {
        "summary": "Long NVDA.",
        "components": [
            {
                "component_type": "entry_rationale",
                "linked_leg": "entry",
                "instrument_reference": "NVDA",
                "narrative": "Capex tailwind.",
                "key_assumptions": ["Capex stays elevated."],
            }
        ],
    },
}


_CLOSE_COMMAND_PAYLOAD: dict[str, Any] = {
    "command_type": "close",
    "position_id": "POS-NVDA-001",
    "quantity": "all",
    "order_type": "market",
    "close_rationale_type": "thesis_invalidated",
    "invalidation_reason": "Thesis broken.",
}


_ADD_COMMAND_PAYLOAD: dict[str, Any] = {
    "command_type": "add",
    "position_id": "POS-1",
    "additional_quantity": 5.0,
    "additional_dollar_value": 5_000.0,
    "entry_order": {"type": "market"},
    "thesis_addition_component": {
        "component_type": "entry_rationale",
        "linked_leg": "add",
        "instrument_reference": "NVDA",
        "narrative": "Add.",
        "key_assumptions": ["Setup intact."],
    },
}


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
        "rationale_narrative": "Analyst proposal aligns with the book; no concerns.",
        "anti_patterns_identified": None,
        "commands": (_open_command_basic(),),
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
        "commands": (_close_command_basic(),),
    }
    return PMStrategistEnvelope(**(defaults | overrides))


def _load_design_doc_schema() -> dict[str, Any]:
    """Parse the JSON schema block from ``pm-envelope-schema.md``."""
    doc_path = (
        Path(__file__).parents[3]
        / "docs"
        / "design"
        / "04-decision-layer"
        / "pm-envelope-schema.md"
    )
    text = doc_path.read_text()
    match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    assert match, "Could not find JSON code block in pm-envelope-schema.md"
    return dict(json.loads(match.group(1)))


# ---------------------------------------------------------------------------
# 1. Tracer: PMAnalystEnvelope basic construction (approve)
# ---------------------------------------------------------------------------


class TestEnvelopeConstruction:
    def test_pm_analyst_envelope_approve_constructs(self) -> None:
        envelope = _make_analyst_envelope()
        assert envelope.envelope_id == "ENV-REC-1"
        assert envelope.source_provenance == "pm_analyst"
        assert envelope.verdict == "approve"
        assert envelope.recommendation_type == "new_entry"
        assert len(envelope.commands) == 1

    def test_pm_strategist_envelope_approve_constructs(self) -> None:
        envelope = _make_strategist_envelope()
        assert envelope.envelope_id == "ENV-SA-1"
        assert envelope.source_provenance == "pm_strategist"
        assert envelope.position_id == "POS-NVDA-001"
        assert envelope.recommendation_type == "position_assessment"


# ---------------------------------------------------------------------------
# 2. Verdict-conditional invariants
# ---------------------------------------------------------------------------


class TestVerdictInvariants:
    def test_approve_with_zero_modifications_accepted(self) -> None:
        envelope = _make_analyst_envelope(verdict="approve", modifications=())
        assert envelope.verdict == "approve"

    def test_approve_with_modifications_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)approve.*modifications"):
            _make_analyst_envelope(
                verdict="approve",
                modifications=(
                    ModificationRecord(
                        phase="pre_submission",
                        field_changed="position_size.quantity",
                        original_value=10,
                        approved_value=8,
                        adjustment_category="risk_reduction",
                        rationale="Trim sizing.",
                    ),
                ),
            )

    def test_approve_with_modification_constructs(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="approve_with_modification",
            modifications=(
                ModificationRecord(
                    phase="pre_submission",
                    field_changed="position_size.quantity",
                    original_value=10,
                    approved_value=8,
                    adjustment_category="risk_reduction",
                    rationale="Trim sizing per coherence concern.",
                ),
            ),
            commands=(_open_command_basic(),),
        )
        assert len(envelope.modifications) == 1
        assert len(envelope.commands) == 1

    def test_approve_with_modification_requires_modifications(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)modification"):
            _make_analyst_envelope(
                verdict="approve_with_modification",
                modifications=(),
                commands=(_open_command_basic(),),
            )

    def test_approve_with_modification_requires_commands(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)command"):
            _make_analyst_envelope(
                verdict="approve_with_modification",
                modifications=(
                    ModificationRecord(
                        phase="pre_submission",
                        field_changed="position_size.quantity",
                        original_value=10,
                        approved_value=8,
                        adjustment_category="risk_reduction",
                        rationale="Trim sizing.",
                    ),
                ),
                commands=(),
            )

    def test_reject_constructs(self) -> None:
        envelope = _make_analyst_envelope(
            verdict="reject",
            modifications=(),
            commands=(),
            concerns=(ConcernRecord(source="falsifiability", summary="Thesis untestable."),),
        )
        assert envelope.verdict == "reject"
        assert envelope.commands == ()

    def test_reject_with_commands_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)reject.*command"):
            _make_analyst_envelope(
                verdict="reject",
                modifications=(),
                commands=(_open_command_basic(),),
                concerns=(ConcernRecord(source="other", summary="Some concern."),),
            )

    def test_reject_with_modifications_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)reject.*modification"):
            _make_analyst_envelope(
                verdict="reject",
                modifications=(
                    ModificationRecord(
                        phase="pre_submission",
                        field_changed="position_size.quantity",
                        original_value=10,
                        approved_value=8,
                        adjustment_category="risk_reduction",
                        rationale="Trim.",
                    ),
                ),
                commands=(),
                concerns=(ConcernRecord(source="other", summary="x"),),
            )

    def test_reject_without_concerns_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)reject.*concern"):
            _make_analyst_envelope(verdict="reject", modifications=(), commands=(), concerns=())


# ---------------------------------------------------------------------------
# 2b. override_with_corrective_action verdict-conditional invariants (ALP-626)
# ---------------------------------------------------------------------------


def _adjust_command_basic() -> AdjustCommand:
    from alphamind.commands.command_models import NewStopLevel

    return AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Tighten stop ahead of catalyst.",
        new_stop_level=NewStopLevel(trigger_price=price(100.0), order_type="market"),
    )


def _cancel_command_basic() -> CancelCommand:
    from alphamind._kernel.ids import OrderId

    return CancelCommand(
        command_type="cancel",
        order_id=OrderId("ORD-1"),
        cancel_reason="stale",
    )


class TestOverrideWithCorrectiveActionVerdictInvariants:
    """Parse-time invariants for the override_with_corrective_action verdict — ALP-626.

    The verdict represents a PM-authored corrective package overriding a
    strategist HOLD-with-disagreement; carries both the disagreement
    (concerns + optional anti-pattern) and the corrective commands
    (CLOSE / ADJUST / CANCEL — never OPEN / ADD).
    """

    def test_override_with_close_command_constructs(self) -> None:
        envelope = _make_strategist_envelope(
            verdict="override_with_corrective_action",
            modifications=(),
            commands=(_close_command_basic(),),
            concerns=(
                ConcernRecord(
                    source="action_status_alignment",
                    summary="Hold inappropriate given size breach.",
                ),
            ),
        )
        assert envelope.verdict == "override_with_corrective_action"
        assert len(envelope.commands) == 1
        assert len(envelope.concerns) == 1
        assert envelope.modifications == ()

    def test_override_with_adjust_command_constructs(self) -> None:
        envelope = _make_strategist_envelope(
            verdict="override_with_corrective_action",
            modifications=(),
            commands=(_adjust_command_basic(),),
            concerns=(ConcernRecord(source="other", summary="Override needed."),),
        )
        assert envelope.verdict == "override_with_corrective_action"

    def test_override_with_cancel_command_constructs(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id="ENV-SA-ORD-1",
            source_recommendation_id="SA-ORD-1",
            recommendation_type="pending_order_assessment",
            verdict="override_with_corrective_action",
            modifications=(),
            commands=(_cancel_command_basic(),),
            concerns=(ConcernRecord(source="other", summary="Cancel stale order."),),
        )
        assert envelope.verdict == "override_with_corrective_action"

    def test_override_without_commands_rejected(self) -> None:
        with pytest.raises(
            (ValueError, TypeError),
            match=r"(?i)override_with_corrective_action.*command",
        ):
            _make_strategist_envelope(
                verdict="override_with_corrective_action",
                modifications=(),
                commands=(),
                concerns=(ConcernRecord(source="other", summary="x"),),
            )

    def test_override_without_concerns_rejected(self) -> None:
        with pytest.raises(
            (ValueError, TypeError),
            match=r"(?i)override_with_corrective_action.*concern",
        ):
            _make_strategist_envelope(
                verdict="override_with_corrective_action",
                modifications=(),
                commands=(_close_command_basic(),),
                concerns=(),
            )

    def test_override_with_modifications_rejected(self) -> None:
        with pytest.raises(
            (ValueError, TypeError),
            match=r"(?i)override_with_corrective_action.*modification",
        ):
            _make_strategist_envelope(
                verdict="override_with_corrective_action",
                modifications=(
                    ModificationRecord(
                        phase="pre_submission",
                        field_changed="position_size.quantity",
                        original_value=10,
                        approved_value=8,
                        adjustment_category="risk_reduction",
                        rationale="Trim.",
                    ),
                ),
                commands=(_close_command_basic(),),
                concerns=(ConcernRecord(source="other", summary="x"),),
            )

    def test_override_with_open_command_rejected(self) -> None:
        with pytest.raises(
            (ValueError, TypeError),
            match=r"(?i)override_with_corrective_action.*(open|corrective)",
        ):
            _make_strategist_envelope(
                verdict="override_with_corrective_action",
                modifications=(),
                commands=(_open_command_basic(),),
                concerns=(ConcernRecord(source="other", summary="x"),),
            )

    def test_override_with_add_command_rejected(self) -> None:
        add_cmd = AddCommand(
            command_type="add",
            position_id=PositionId("POS-NVDA-001"),
            additional_quantity=5.0,
            additional_dollar_value=money(5_000.0),
            entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
            thesis_addition_component=ThesisComponent(
                component_type="entry_rationale",
                linked_leg="add",
                instrument_reference="NVDA",
                narrative="Add.",
                key_assumptions=("Setup intact.",),
            ),
            bracket_adjustment=None,
        )
        with pytest.raises(
            (ValueError, TypeError),
            match=r"(?i)override_with_corrective_action.*(add|corrective)",
        ):
            _make_strategist_envelope(
                verdict="override_with_corrective_action",
                modifications=(),
                commands=(add_cmd,),
                concerns=(ConcernRecord(source="other", summary="x"),),
            )

    def test_verdict_literal_includes_override(self) -> None:
        """The shared Verdict literal includes override_with_corrective_action."""
        from alphamind.commands.pm_envelope import Verdict

        assert "override_with_corrective_action" in get_args(Verdict)


# ---------------------------------------------------------------------------
# 3. Modification-record invariants (adjustment_category ↔ phase)
# ---------------------------------------------------------------------------


class TestModificationInvariants:
    def test_pre_submission_with_risk_reduction_accepted(self) -> None:
        rec = ModificationRecord(
            phase="pre_submission",
            field_changed="position_size.quantity",
            original_value=10,
            approved_value=8,
            adjustment_category="risk_reduction",
            rationale="Trim sizing.",
        )
        assert rec.phase == "pre_submission"
        assert rec.adjustment_category == "risk_reduction"

    def test_post_rejection_with_guardrail_response_accepted(self) -> None:
        rec = ModificationRecord(
            phase="post_rejection",
            field_changed="position_size.quantity",
            original_value=8,
            approved_value=5,
            adjustment_category="guardrail_rejection_response",
            rationale="Cure sector_concentration breach.",
            triggering_rule="sector_concentration",
        )
        assert rec.triggering_rule == "sector_concentration"

    def test_pre_submission_with_guardrail_response_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)post_rejection"):
            ModificationRecord(
                phase="pre_submission",
                field_changed="position_size.quantity",
                original_value=8,
                approved_value=5,
                adjustment_category="guardrail_rejection_response",
                rationale="Cure breach.",
                triggering_rule="sector_concentration",
            )

    def test_post_rejection_with_risk_reduction_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)pre_submission"):
            ModificationRecord(
                phase="post_rejection",
                field_changed="position_size.quantity",
                original_value=10,
                approved_value=8,
                adjustment_category="risk_reduction",
                rationale="Trim.",
            )

    def test_guardrail_response_without_triggering_rule_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)triggering_rule"):
            ModificationRecord(
                phase="post_rejection",
                field_changed="position_size.quantity",
                original_value=8,
                approved_value=5,
                adjustment_category="guardrail_rejection_response",
                rationale="Cure breach.",
            )


# ---------------------------------------------------------------------------
# 4. envelope_id ↔ source_provenance pattern
# ---------------------------------------------------------------------------


class TestEnvelopeIdSourceProvenance:
    def test_env_rec_with_pm_analyst_constructs(self) -> None:
        envelope = _make_analyst_envelope(envelope_id="ENV-REC-1", source_recommendation_id="REC-1")
        assert envelope.envelope_id == "ENV-REC-1"

    def test_env_sa_with_pm_strategist_position_assessment_constructs(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id="ENV-SA-3",
            source_recommendation_id="SA-3",
            recommendation_type="position_assessment",
        )
        assert envelope.envelope_id == "ENV-SA-3"
        assert envelope.recommendation_type == "position_assessment"

    def test_env_sa_ord_with_pm_strategist_pending_order_assessment_constructs(self) -> None:
        envelope = _make_strategist_envelope(
            envelope_id="ENV-SA-ORD-7",
            source_recommendation_id="SA-ORD-7",
            recommendation_type="pending_order_assessment",
        )
        assert envelope.envelope_id == "ENV-SA-ORD-7"
        assert envelope.recommendation_type == "pending_order_assessment"

    def test_env_rec_with_pm_strategist_rejected(self) -> None:
        """An ENV-REC-* envelope cannot have source_provenance=pm_strategist."""
        with pytest.raises((ValueError, TypeError)):
            PMStrategistEnvelope(
                envelope_id=EnvelopeId("ENV-REC-1"),
                invocation_id=InvocationId("inv-2026-05-05"),
                source_provenance="pm_strategist",
                source_recommendation_id=RecommendationId("SA-1"),
                recommendation_type="position_assessment",
                position_id=PositionId("POS-1"),
                verdict="approve",
                evaluation=_position_eval_all_pass(),
                modifications=(),
                concerns=(),
                rationale_narrative="x",
                anti_patterns_identified=None,
                commands=(_close_command_basic(),),
            )

    def test_env_sa_with_pm_analyst_rejected(self) -> None:
        """An ENV-SA-* envelope cannot have source_provenance=pm_analyst."""
        with pytest.raises((ValueError, TypeError)):
            PMAnalystEnvelope(
                envelope_id=EnvelopeId("ENV-SA-1"),
                invocation_id=InvocationId("inv-2026-05-05"),
                source_provenance="pm_analyst",
                source_recommendation_id=RecommendationId("REC-1"),
                recommendation_type="new_entry",
                verdict="approve",
                evaluation=_thesis_eval_all_pass(),
                modifications=(),
                concerns=(),
                rationale_narrative="x",
                anti_patterns_identified=None,
                commands=(_open_command_basic(),),
            )

    def test_pm_analyst_envelope_with_position_id_rejected(self) -> None:
        """pm_analyst envelopes forbid a populated position_id (schema's `false`)."""
        with pytest.raises((ValueError, TypeError)):
            _make_analyst_envelope(position_id=PositionId("POS-1"))


# ---------------------------------------------------------------------------
# 5. Discriminated-union routing
# ---------------------------------------------------------------------------


class TestDiscriminatedUnion:
    def test_pm_analyst_payload_routes_to_analyst_envelope(self) -> None:
        adapter: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)
        payload: dict[str, Any] = {
            "envelope_id": "ENV-REC-1",
            "invocation_id": "inv-2026-05-05",
            "source_provenance": "pm_analyst",
            "source_recommendation_id": "REC-1",
            "recommendation_type": "new_entry",
            "verdict": "approve",
            "evaluation": {
                "falsifiability": {"status": "pass"},
                "sizing_proportionality": {"status": "pass"},
                "portfolio_coherence": {"status": "pass"},
                "timing_plausibility": {"status": "pass"},
                "counterargument_consideration": {"status": "pass"},
            },
            "modifications": [],
            "concerns": [],
            "rationale_narrative": "Aligned.",
            "commands": [_OPEN_COMMAND_PAYLOAD],
        }
        envelope = adapter.validate_python(payload)
        assert isinstance(envelope, PMAnalystEnvelope)

    def test_pm_strategist_payload_routes_to_strategist_envelope(self) -> None:
        adapter: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)
        payload: dict[str, Any] = {
            "envelope_id": "ENV-SA-1",
            "invocation_id": "inv-2026-05-05",
            "source_provenance": "pm_strategist",
            "source_recommendation_id": "SA-1",
            "recommendation_type": "position_assessment",
            "position_id": "POS-NVDA-001",
            "verdict": "approve",
            "evaluation": {
                "status_classification_warrant": {"status": "pass"},
                "action_status_alignment": {"status": "pass"},
                "action_specific_justification": {"status": "pass"},
                "portfolio_coherence": {"status": "pass"},
            },
            "modifications": [],
            "concerns": [],
            "rationale_narrative": "Sound.",
            "commands": [_CLOSE_COMMAND_PAYLOAD],
        }
        envelope = adapter.validate_python(payload)
        assert isinstance(envelope, PMStrategistEnvelope)


# ---------------------------------------------------------------------------
# 6. PMCompletionRecord (sentinel) verdict-summary sum invariant
# ---------------------------------------------------------------------------


class TestPMCompletionRecord:
    def test_minimal_record_constructs(self) -> None:
        record = PMCompletionRecord(
            invocation_id=InvocationId("inv-2026-05-05"),
            timestamp=_NOW,
            envelopes_submitted=3,
            verdict_summary=VerdictSummary(
                approve=2, approve_with_modification=1, reject=0, override_with_corrective_action=0
            ),
        )
        assert record.envelopes_submitted == 3

    def test_zero_envelopes_constructs(self) -> None:
        record = PMCompletionRecord(
            invocation_id=InvocationId("inv-1"),
            timestamp=_NOW,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0, approve_with_modification=0, reject=0, override_with_corrective_action=0
            ),
        )
        assert record.envelopes_submitted == 0

    def test_sum_mismatch_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)sum"):
            PMCompletionRecord(
                invocation_id=InvocationId("inv-1"),
                timestamp=_NOW,
                envelopes_submitted=3,
                verdict_summary=VerdictSummary(
                    approve=1,
                    approve_with_modification=1,
                    reject=0,
                    override_with_corrective_action=0,
                ),
            )

    def test_sum_mismatch_with_override_rejected(self) -> None:
        """Mismatch detection extends to the override_with_corrective_action count."""
        with pytest.raises((ValueError, TypeError), match=r"(?i)sum"):
            PMCompletionRecord(
                invocation_id=InvocationId("inv-1"),
                timestamp=_NOW,
                envelopes_submitted=3,
                verdict_summary=VerdictSummary(
                    approve=1,
                    approve_with_modification=1,
                    reject=0,
                    override_with_corrective_action=0,
                ),
            )

    def test_negative_count_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            VerdictSummary(
                approve=-1,
                approve_with_modification=0,
                reject=0,
                override_with_corrective_action=0,
            )

    def test_verdict_summary_requires_override_field(self) -> None:
        """VerdictSummary requires override_with_corrective_action like the other counts.

        ALP-626 added the fourth verdict; the sentinel field is now required (no
        default) so a missing key surfaces as a parse error rather than silently
        defaulting to 0.
        """
        with pytest.raises((ValueError, TypeError), match=r"(?i)override_with_corrective_action"):
            VerdictSummary(approve=0, approve_with_modification=0, reject=0)  # type: ignore[call-arg]

    def test_verdict_summary_with_overrides_passes_sum(self) -> None:
        """A sentinel with override counts that sum to ``envelopes_submitted`` parses.

        Positive case for finding 19: ``override_with_corrective_action`` is a
        first-class contributor to the verdict_summary sum invariant.
        """
        record = PMCompletionRecord(
            invocation_id=InvocationId("inv-override"),
            timestamp=_NOW,
            envelopes_submitted=2,
            verdict_summary=VerdictSummary(
                approve=0,
                approve_with_modification=0,
                reject=0,
                override_with_corrective_action=2,
            ),
        )
        assert record.verdict_summary.override_with_corrective_action == 2
        # Sum-mismatch detection extends to overrides.
        with pytest.raises((ValueError, TypeError), match=r"(?i)sum"):
            PMCompletionRecord(
                invocation_id=InvocationId("inv-override"),
                timestamp=_NOW,
                envelopes_submitted=1,
                verdict_summary=VerdictSummary(
                    approve=0,
                    approve_with_modification=0,
                    reject=0,
                    override_with_corrective_action=2,
                ),
            )


# ---------------------------------------------------------------------------
# 7. CloseCommand risk-management subtype invariant
# ---------------------------------------------------------------------------


class TestCloseCommandInvariants:
    def test_risk_management_without_subtype_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)risk_management_subtype"):
            CloseCommand(
                command_type="close",
                position_id=PositionId("POS-1"),
                quantity="all",
                order_type="market",
                close_rationale_type="risk_management",
            )

    def test_risk_management_with_pm_directed_subtype_accepted(self) -> None:
        cmd = CloseCommand(
            command_type="close",
            position_id=PositionId("POS-1"),
            quantity="all",
            order_type="market",
            close_rationale_type="risk_management",
            risk_management_subtype="pm_directed",
        )
        assert cmd.risk_management_subtype == "pm_directed"

    def test_thesis_invalidated_no_subtype_required(self) -> None:
        cmd = CloseCommand(
            command_type="close",
            position_id=PositionId("POS-1"),
            quantity="all",
            order_type="market",
            close_rationale_type="thesis_invalidated",
            invalidation_reason="Thesis broken.",
        )
        assert cmd.risk_management_subtype is None


# ---------------------------------------------------------------------------
# 8. OMSCommand discriminated union routing on command_type
# ---------------------------------------------------------------------------


class TestOMSCommandDiscriminator:
    def test_open_routes_to_open_command(self) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        cmd = adapter.validate_python(_OPEN_COMMAND_PAYLOAD)
        assert isinstance(cmd, OpenCommand)

    def test_close_routes_to_close_command(self) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        cmd = adapter.validate_python(
            {
                "command_type": "close",
                "position_id": "POS-1",
                "quantity": "all",
                "order_type": "market",
                "close_rationale_type": "target_reached",
            }
        )
        assert isinstance(cmd, CloseCommand)

    def test_adjust_routes_to_adjust_command(self) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        cmd = adapter.validate_python(
            {
                "command_type": "adjust",
                "position_id": "POS-1",
                "adjustment_rationale": "Tighten stop.",
                "new_stop_level": {"trigger_price": 100.0, "order_type": "market"},
            }
        )
        assert isinstance(cmd, AdjustCommand)

    def test_cancel_routes_to_cancel_command(self) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        cmd = adapter.validate_python(
            {"command_type": "cancel", "order_id": "ORD-1", "cancel_reason": "stale"}
        )
        assert isinstance(cmd, CancelCommand)

    def test_add_routes_to_add_command(self) -> None:
        adapter: TypeAdapter[OMSCommand] = TypeAdapter(OMSCommand)
        cmd = adapter.validate_python(_ADD_COMMAND_PAYLOAD)
        assert isinstance(cmd, AddCommand)


# ---------------------------------------------------------------------------
# 9. AntiPattern enum + canonical strings
# ---------------------------------------------------------------------------


class TestAntiPattern:
    def test_canonical_anti_patterns_accepted(self) -> None:
        canonical = (
            "conviction_inflation",
            "sunk_cost_persistence",
            "rationalized_continuation",
            "thesis_contradiction_suppression",
            "engine_originated_closure_signal",
        )
        envelope = _make_analyst_envelope(anti_patterns_identified=canonical)
        assert envelope.anti_patterns_identified == canonical

    def test_unknown_anti_pattern_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_analyst_envelope(anti_patterns_identified=("not_a_canonical_string",))

    def test_anti_pattern_literal_accepts_canonical_set(self) -> None:
        # AntiPattern is a Literal alias — verify the canonical strings.
        assert set(get_args(AntiPattern)) == {
            "conviction_inflation",
            "sunk_cost_persistence",
            "rationalized_continuation",
            "thesis_contradiction_suppression",
            "engine_originated_closure_signal",
        }


# ---------------------------------------------------------------------------
# 10. Schema-parity tests against pm-envelope-schema.md
# ---------------------------------------------------------------------------


def _collect_pydantic_defs(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect all $defs from a Pydantic-generated schema."""
    result: dict[str, dict[str, Any]] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "$defs" in node:
                result.update(node["$defs"])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    return result


class TestPMEnvelopeSchemaParity:
    """Parity between :func:`envelope_schema` and pm-envelope-schema.md.

    The design doc encodes the discriminated union via top-level ``oneOf`` +
    per-provenance branches with conditional ``if/then`` invariants; Pydantic
    encodes it via ``discriminator`` + ``oneOf``. Byte-for-byte equality is
    not achievable; parity checks (required-field sets, enum values, regex
    patterns, criterion-key sets) catch every contract drift the design
    schema is the authoritative home for.
    """

    def test_top_level_required_matches(self) -> None:
        """Top-level required matches the design doc's top-level required."""
        design = _load_design_doc_schema()
        schema = envelope_schema()
        # Pydantic discriminated unions don't carry a top-level ``required`` —
        # required is enforced per-branch. Walk both branches and assert each
        # branch's required is a superset of the design's top-level required.
        design_top_required = set(design["required"])
        defs = _collect_pydantic_defs(schema)
        for branch in ("PMAnalystEnvelope", "PMStrategistEnvelope"):
            branch_required = set(defs[branch]["required"])
            assert design_top_required.issubset(branch_required), (
                f"{branch} required missing fields from design top-level required: "
                f"{design_top_required - branch_required}"
            )

    def test_pm_analyst_branch_required(self) -> None:
        """The pm_analyst branch carries every design-doc required field."""
        design = _load_design_doc_schema()
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        design_branch_required = set(design["$defs"]["pm_analyst_envelope"]["required"])
        pyd_required = set(defs["PMAnalystEnvelope"]["required"])
        assert design_branch_required.issubset(pyd_required), (
            f"PMAnalystEnvelope missing required fields {design_branch_required - pyd_required}"
        )

    def test_pm_strategist_branch_required(self) -> None:
        """The pm_strategist branch carries every design-doc required field."""
        design = _load_design_doc_schema()
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        design_branch_required = set(design["$defs"]["pm_strategist_envelope"]["required"])
        pyd_required = set(defs["PMStrategistEnvelope"]["required"])
        assert design_branch_required.issubset(pyd_required), (
            f"PMStrategistEnvelope missing required {design_branch_required - pyd_required}"
        )

    def test_envelope_id_pattern_matches(self) -> None:
        """envelope_id pattern at the top level matches the design doc."""
        design = _load_design_doc_schema()
        design_pattern = design["properties"]["envelope_id"]["pattern"]
        # PMAnalyst/Strategist branches each tighten the pattern; either branch's
        # pattern is a strict subset of the design's top-level pattern.
        # We check the union: ENV-(REC|SA|SA-ORD)-[0-9]+
        assert design_pattern == r"^ENV-(REC|SA|SA-ORD)-[0-9]+$"

        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pm_analyst_pattern = defs["PMAnalystEnvelope"]["properties"]["envelope_id"]["pattern"]
        pm_strategist_pattern = defs["PMStrategistEnvelope"]["properties"]["envelope_id"]["pattern"]
        assert pm_analyst_pattern == r"^ENV-REC-[0-9]+$"
        assert pm_strategist_pattern == r"^ENV-(SA|SA-ORD)-[0-9]+$"

    def test_source_recommendation_id_pattern_matches(self) -> None:
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pm_analyst_pat = defs["PMAnalystEnvelope"]["properties"]["source_recommendation_id"][
            "pattern"
        ]
        pm_strategist_pat = defs["PMStrategistEnvelope"]["properties"]["source_recommendation_id"][
            "pattern"
        ]
        assert pm_analyst_pat == r"^REC-[0-9]+$"
        assert pm_strategist_pat == r"^SA(-ORD)?-[0-9]+$"

    def test_verdict_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["properties"]["verdict"]["enum"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_enum = set(defs["PMAnalystEnvelope"]["properties"]["verdict"]["enum"])
        assert design_enum == pyd_enum
        assert design_enum == {
            "approve",
            "approve_with_modification",
            "reject",
            "override_with_corrective_action",
        }

    def test_source_provenance_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["properties"]["source_provenance"]["enum"])
        assert design_enum == {"pm_analyst", "pm_strategist"}

    def test_recommendation_type_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["properties"]["recommendation_type"]["enum"])
        assert design_enum == {"new_entry", "position_assessment", "pending_order_assessment"}

    def test_thesis_quality_evaluation_keys_match(self) -> None:
        design = _load_design_doc_schema()
        design_required = set(design["$defs"]["thesis_quality_evaluation"]["required"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_required = set(defs["ThesisQualityEvaluation"]["required"])
        assert design_required == pyd_required
        assert design_required == {
            "falsifiability",
            "sizing_proportionality",
            "portfolio_coherence",
            "timing_plausibility",
            "counterargument_consideration",
        }

    def test_position_action_evaluation_keys_match(self) -> None:
        design = _load_design_doc_schema()
        design_required = set(design["$defs"]["position_action_evaluation"]["required"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_required = set(defs["PositionActionEvaluation"]["required"])
        assert design_required == pyd_required
        assert design_required == {
            "status_classification_warrant",
            "action_status_alignment",
            "action_specific_justification",
            "portfolio_coherence",
        }

    def test_criterion_status_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["$defs"]["criterion_assessment"]["properties"]["status"]["enum"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_enum = set(defs["CriterionAssessment"]["properties"]["status"]["enum"])
        assert design_enum == pyd_enum
        assert design_enum == {"pass", "fail"}

    def test_modification_phase_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["$defs"]["modification_record"]["properties"]["phase"]["enum"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_enum = set(defs["ModificationRecord"]["properties"]["phase"]["enum"])
        assert design_enum == pyd_enum

    def test_modification_adjustment_category_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(
            design["$defs"]["modification_record"]["properties"]["adjustment_category"]["enum"]
        )
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_enum = set(defs["ModificationRecord"]["properties"]["adjustment_category"]["enum"])
        assert design_enum == pyd_enum
        assert design_enum == {
            "risk_reduction",
            "conviction_disagreement",
            "capital_constraint",
            "portfolio_balance",
            "guardrail_rejection_response",
        }

    def test_modification_required_fields_match(self) -> None:
        design = _load_design_doc_schema()
        design_required = set(design["$defs"]["modification_record"]["required"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_required = set(defs["ModificationRecord"]["required"])
        assert design_required == pyd_required

    def test_concern_record_required_matches(self) -> None:
        design = _load_design_doc_schema()
        design_required = set(design["$defs"]["concern_record"]["required"])
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        pyd_required = set(defs["ConcernRecord"]["required"])
        assert design_required == pyd_required

    def test_anti_pattern_enum_matches(self) -> None:
        design = _load_design_doc_schema()
        design_enum = set(design["properties"]["anti_patterns_identified"]["items"]["enum"])
        # Pydantic emits the AntiPattern Literal as enum values somewhere in the
        # branch's anti_patterns_identified shape. Walk and find the enum.
        schema = envelope_schema()
        defs = _collect_pydantic_defs(schema)
        # The field is on each branch.
        branch_prop = defs["PMAnalystEnvelope"]["properties"]["anti_patterns_identified"]

        # The schema may render as anyOf with a list-with-enum branch, depending
        # on Pydantic's handling of tuple[AntiPattern, ...] | None.
        def _find_enum(node: Any) -> set[str] | None:
            if isinstance(node, dict):
                if "enum" in node and isinstance(node["enum"], list):
                    return set(node["enum"])
                for v in node.values():
                    found = _find_enum(v)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = _find_enum(item)
                    if found is not None:
                        return found
            return None

        pyd_enum = _find_enum(branch_prop)
        assert pyd_enum == design_enum
        assert design_enum == {
            "conviction_inflation",
            "sunk_cost_persistence",
            "rationalized_continuation",
            "thesis_contradiction_suppression",
            "engine_originated_closure_signal",
        }


# ---------------------------------------------------------------------------
# 11. Schema accessors return well-formed schemas
# ---------------------------------------------------------------------------


class TestSchemaAccessors:
    def test_envelope_schema_returns_dict(self) -> None:
        schema = envelope_schema()
        assert isinstance(schema, dict)

    def test_envelope_schema_carries_discriminator(self) -> None:
        schema = envelope_schema()
        # The discriminated union exposes either a discriminator field or oneOf.
        assert "oneOf" in schema or "discriminator" in schema

    def test_completion_record_schema_returns_dict(self) -> None:
        schema = completion_record_schema()
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        required = set(schema.get("required", []))
        assert {
            "invocation_id",
            "timestamp",
            "envelopes_submitted",
            "verdict_summary",
        }.issubset(required)


# ---------------------------------------------------------------------------
# 12. Canonical-import hard rule — PM re-exports OMS commands from the canonical home
# ---------------------------------------------------------------------------


class TestCanonicalReexport:
    def test_pm_reexports_canonical_command_types(self) -> None:
        # After ALP-458 the canonical OMS command types live in
        # ``alphamind.commands.command_models``; PM's ``models`` shim
        # re-exports them. Identity (``is``) check confirms one canonical
        # definition.
        from alphamind.commands import command_models as canonical
        from alphamind.decision.portfolio_manager import models as pm_models

        assert pm_models.OpenCommand is canonical.OpenCommand
        assert pm_models.CloseCommand is canonical.CloseCommand
        assert pm_models.AdjustCommand is canonical.AdjustCommand
        assert pm_models.CancelCommand is canonical.CancelCommand
        assert pm_models.AddCommand is canonical.AddCommand
        assert pm_models.OMSCommand is canonical.OMSCommand


# ---------------------------------------------------------------------------
# 13. Transitional artifacts deleted — PM's old oms_command_models.py is gone
# ---------------------------------------------------------------------------


class TestTransitionalArtifactsDeleted:
    def test_pm_oms_command_models_module_deleted(self) -> None:
        # Story 02b deletes ``alphamind.decision.portfolio_manager.oms_command_models``.
        # Importing the now-absent module raises ImportError.
        with pytest.raises(ImportError):
            import alphamind.decision.portfolio_manager.oms_command_models  # noqa: F401


# ---------------------------------------------------------------------------
# 14. Frozen-ness of envelope models
# ---------------------------------------------------------------------------


class TestFrozen:
    def test_envelope_is_frozen(self) -> None:
        envelope = _make_analyst_envelope()
        with pytest.raises((ValueError, TypeError)):
            envelope.envelope_id = EnvelopeId("ENV-REC-99")

    def test_completion_record_is_frozen(self) -> None:
        record = PMCompletionRecord(
            invocation_id=InvocationId("inv-1"),
            timestamp=_NOW,
            envelopes_submitted=0,
            verdict_summary=VerdictSummary(
                approve=0, approve_with_modification=0, reject=0, override_with_corrective_action=0
            ),
        )
        with pytest.raises((ValueError, TypeError)):
            record.invocation_id = InvocationId("inv-2")


# ---------------------------------------------------------------------------
# 15. Public surface re-exports through __init__.py
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_models_module_all_contains_every_name(self) -> None:
        import alphamind.decision.portfolio_manager.models as m

        expected = {
            "PMEnvelope",
            "PMAnalystEnvelope",
            "PMStrategistEnvelope",
            "PMCompletionRecord",
            "VerdictSummary",
            "ThesisQualityEvaluation",
            "PositionActionEvaluation",
            "CriterionAssessment",
            "ModificationRecord",
            "ConcernRecord",
            "AntiPattern",
            "Verdict",
            "SourceProvenance",
            "RecommendationType",
            "AdjustmentCategory",
            "OMSCommand",
            "OpenCommand",
            "CloseCommand",
            "AdjustCommand",
            "CancelCommand",
            "AddCommand",
            "envelope_schema",
            "completion_record_schema",
        }
        assert expected.issubset(set(m.__all__))

    def test_package_init_re_exports_models(self) -> None:
        import alphamind.decision.portfolio_manager as pkg

        for name in (
            "PMEnvelope",
            "PMAnalystEnvelope",
            "PMStrategistEnvelope",
            "PMCompletionRecord",
            "VerdictSummary",
            "ThesisQualityEvaluation",
            "PositionActionEvaluation",
            "CriterionAssessment",
            "ModificationRecord",
            "ConcernRecord",
            "AntiPattern",
            "Verdict",
            "SourceProvenance",
            "RecommendationType",
            "AdjustmentCategory",
            "OMSCommand",
            "OpenCommand",
            "CloseCommand",
            "AdjustCommand",
            "CancelCommand",
            "AddCommand",
            "envelope_schema",
            "completion_record_schema",
        ):
            assert hasattr(pkg, name), f"{name} not re-exported from package __init__"
