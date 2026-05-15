"""Tests for the canonical OMS command-ID derivation utility — story 01b (ALP-371).

Drives ``alphamind.execution.oms.command_ids``: two derive functions
(PM-originated and engine-originated), the post-rejection counter, and inverse
parsers. All stateless; round-trip and worked-example coverage per
``oms-command-ids.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from alphamind._kernel.ids import (
    EnvelopeId,
    InvocationId,
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
    CriterionAssessment,
    ModificationRecord,
    OpenCommand,
    PMAnalystEnvelope,
    ThesisQualityEvaluation,
)
from alphamind.execution.oms import (
    EngineCommandIdComponents,
    PMCommandIdComponents,
    compute_attempt_seq,
    derive_engine_command_id,
    derive_pm_command_id,
    is_engine_originated,
    is_pm_originated,
    parse_engine_command_id,
    parse_pm_command_id,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _pass() -> CriterionAssessment:
    return CriterionAssessment(status="pass", note=None)


def _thesis_eval_all_pass() -> ThesisQualityEvaluation:
    return ThesisQualityEvaluation(
        falsifiability=_pass(),
        sizing_proportionality=_pass(),
        portfolio_coherence=_pass(),
        timing_plausibility=_pass(),
        counterargument_consideration=_pass(),
    )


def _open_command() -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker="NVDA", direction="long"),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=money(10_000.0)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger="NVDA",
                    comparator="<=",
                    trigger_price=price(750.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary="Long NVDA.",
            components=(
                ThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference="NVDA",
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
    )


def _make_envelope(
    *,
    modifications: tuple[ModificationRecord, ...] = (),
) -> PMAnalystEnvelope:
    overrides: dict[str, Any] = {
        "envelope_id": "ENV-REC-1",
        "invocation_id": "inv-2026-04-23T14-30Z",
        "source_provenance": "pm_analyst",
        "source_recommendation_id": "REC-1",
        "recommendation_type": "new_entry",
        "verdict": "approve" if not modifications else "approve_with_modification",
        "evaluation": _thesis_eval_all_pass(),
        "modifications": modifications,
        "concerns": (),
        "rationale_narrative": "Analyst proposal aligns with the book.",
        "anti_patterns_identified": None,
        "commands": (_open_command(),),
    }
    return PMAnalystEnvelope(**overrides)


# ---------------------------------------------------------------------------
# derive_pm_command_id
# ---------------------------------------------------------------------------


class TestDerivePMCommandId:
    def test_worked_example_first_submission(self) -> None:
        """Worked example from oms-command-ids.md: first submission is .0.0."""
        result = derive_pm_command_id(
            invocation_id="inv-2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
        )
        assert result == "inv-2026-04-23T14-30Z.ENV-REC-2.0.0"

    def test_worked_example_post_rejection_resubmission(self) -> None:
        """Worked example: post-rejection retry has attempt_seq=1."""
        result = derive_pm_command_id(
            invocation_id="inv-2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=1,
        )
        assert result == "inv-2026-04-23T14-30Z.ENV-REC-2.0.1"

    def test_prefixes_inv_when_missing(self) -> None:
        """Mirrors the existing _format_command_id gate: prefix inv- only when absent."""
        result = derive_pm_command_id(
            invocation_id="2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
        )
        assert result == "inv-2026-04-23T14-30Z.ENV-REC-2.0.0"

    def test_does_not_double_prefix(self) -> None:
        """Already-prefixed invocation_id is not re-prefixed."""
        result = derive_pm_command_id(
            invocation_id="inv-anything",
            envelope_id="ENV-SA-7",
            command_ordinal=0,
            attempt_seq=2,
        )
        assert result == "inv-anything.ENV-SA-7.0.2"

    def test_strategist_envelope_prefix(self) -> None:
        result = derive_pm_command_id(
            invocation_id="inv-X",
            envelope_id="ENV-SA-ORD-3",
            command_ordinal=0,
            attempt_seq=0,
        )
        assert result == "inv-X.ENV-SA-ORD-3.0.0"

    def test_negative_command_ordinal_raises(self) -> None:
        with pytest.raises(ValueError, match="command_ordinal"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=-1,
                attempt_seq=0,
            )

    def test_negative_attempt_seq_raises(self) -> None:
        with pytest.raises(ValueError, match="attempt_seq"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=-1,
            )

    def test_invalid_envelope_id_raises(self) -> None:
        with pytest.raises(ValueError, match="envelope_id"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="REC-1",  # missing ENV- prefix
                command_ordinal=0,
                attempt_seq=0,
            )

    def test_invalid_envelope_id_alt_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="envelope_id"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-OTHER-1",
                command_ordinal=0,
                attempt_seq=0,
            )


# ---------------------------------------------------------------------------
# derive_engine_command_id
# ---------------------------------------------------------------------------


class TestDeriveEngineCommandId:
    def test_default_command_ordinal_is_zero(self) -> None:
        result = derive_engine_command_id(monitor_session_id="abc-123", trigger_id=42)
        assert result == "MON.abc-123.42.0"

    def test_explicit_command_ordinal(self) -> None:
        result = derive_engine_command_id(
            monitor_session_id="abc-123",
            trigger_id=42,
            command_ordinal=3,
        )
        assert result == "MON.abc-123.42.3"

    def test_empty_monitor_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="monitor_session_id"):
            derive_engine_command_id(monitor_session_id="", trigger_id=1)

    def test_dotted_monitor_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="monitor_session_id"):
            derive_engine_command_id(monitor_session_id="abc.123", trigger_id=1)

    def test_negative_trigger_id_raises(self) -> None:
        with pytest.raises(ValueError, match="trigger_id"):
            derive_engine_command_id(monitor_session_id="abc-123", trigger_id=-1)

    def test_negative_command_ordinal_raises(self) -> None:
        with pytest.raises(ValueError, match="command_ordinal"):
            derive_engine_command_id(
                monitor_session_id="abc-123",
                trigger_id=1,
                command_ordinal=-1,
            )


# ---------------------------------------------------------------------------
# parse_pm_command_id
# ---------------------------------------------------------------------------


class TestParsePMCommandId:
    def test_worked_example_post_rejection(self) -> None:
        components = parse_pm_command_id("inv-2026-04-23T14-30Z.ENV-REC-2.0.1")
        assert components == PMCommandIdComponents(
            invocation_id=InvocationId("2026-04-23T14-30Z"),
            envelope_id=EnvelopeId("ENV-REC-2"),
            command_ordinal=0,
            attempt_seq=1,
        )

    def test_strategist_envelope_id(self) -> None:
        components = parse_pm_command_id("inv-X.ENV-SA-7.0.0")
        assert components.envelope_id == "ENV-SA-7"

    def test_strategist_pending_order_envelope_id(self) -> None:
        components = parse_pm_command_id("inv-X.ENV-SA-ORD-3.0.2")
        assert components.envelope_id == "ENV-SA-ORD-3"
        assert components.attempt_seq == 2

    def test_round_trip_with_derive(self) -> None:
        original = derive_pm_command_id(
            invocation_id="inv-test-id",
            envelope_id="ENV-REC-99",
            command_ordinal=0,
            attempt_seq=5,
        )
        parsed = parse_pm_command_id(original)
        roundtrip = derive_pm_command_id(
            invocation_id=f"inv-{parsed.invocation_id}",
            envelope_id=parsed.envelope_id,
            command_ordinal=parsed.command_ordinal,
            attempt_seq=parsed.attempt_seq,
        )
        assert roundtrip == original

    def test_missing_inv_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("X.ENV-REC-2.0.0")

    def test_engine_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("MON.abc.0.0")

    def test_malformed_no_envelope_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("inv-X.ENV-REC-2.0")

    def test_malformed_garbage_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("not-a-command-id")


# ---------------------------------------------------------------------------
# parse_engine_command_id
# ---------------------------------------------------------------------------


class TestParseEngineCommandId:
    def test_basic_parse(self) -> None:
        components = parse_engine_command_id("MON.abc-123.42.0")
        assert components == EngineCommandIdComponents(
            monitor_session_id="abc-123",
            trigger_id=42,
            command_ordinal=0,
        )

    def test_round_trip_with_derive(self) -> None:
        original = derive_engine_command_id(
            monitor_session_id="session-uuid",
            trigger_id=7,
            command_ordinal=2,
        )
        parsed = parse_engine_command_id(original)
        roundtrip = derive_engine_command_id(
            monitor_session_id=parsed.monitor_session_id,
            trigger_id=parsed.trigger_id,
            command_ordinal=parsed.command_ordinal,
        )
        assert roundtrip == original

    def test_pm_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_engine_command_id("inv-X.ENV-REC-2.0.0")

    def test_missing_mon_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_engine_command_id("X.abc.42.0")

    def test_malformed_garbage_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_engine_command_id("not-a-command-id")


# ---------------------------------------------------------------------------
# compute_attempt_seq
# ---------------------------------------------------------------------------


class TestComputeAttemptSeq:
    def test_no_modifications_is_zero(self) -> None:
        envelope = _make_envelope()
        assert compute_attempt_seq(envelope) == 0

    def test_one_post_rejection_is_one(self) -> None:
        modification = ModificationRecord(
            phase="post_rejection",
            field_changed="position_size",
            original_value="3%",
            approved_value="1.5%",
            adjustment_category="guardrail_rejection_response",
            rationale="Sector concentration breach prompted halving size.",
            triggering_rule="sector_concentration_pct",
        )
        envelope = _make_envelope(modifications=(modification,))
        assert compute_attempt_seq(envelope) == 1

    def test_pre_submission_only_is_zero(self) -> None:
        modification = ModificationRecord(
            phase="pre_submission",
            field_changed="position_size",
            original_value="4%",
            approved_value="3%",
            adjustment_category="conviction_disagreement",
            rationale="Reduced size due to conviction disagreement with analyst.",
        )
        envelope = _make_envelope(modifications=(modification,))
        assert compute_attempt_seq(envelope) == 0

    def test_mixed_phases_counts_post_rejection_only(self) -> None:
        pre = ModificationRecord(
            phase="pre_submission",
            field_changed="position_size",
            original_value="4%",
            approved_value="3%",
            adjustment_category="conviction_disagreement",
            rationale="Reduced size at authoring time.",
        )
        post = ModificationRecord(
            phase="post_rejection",
            field_changed="position_size",
            original_value="3%",
            approved_value="1.5%",
            adjustment_category="guardrail_rejection_response",
            rationale="Sector concentration breach.",
            triggering_rule="sector_concentration_pct",
        )
        envelope = _make_envelope(modifications=(pre, post))
        assert compute_attempt_seq(envelope) == 1


# ---------------------------------------------------------------------------
# is_pm_originated / is_engine_originated
# ---------------------------------------------------------------------------


class TestDiscriminators:
    def test_pm_id_classified_correctly(self) -> None:
        cid = "inv-2026-04-23T14-30Z.ENV-REC-2.0.0"
        assert is_pm_originated(cid) is True
        assert is_engine_originated(cid) is False

    def test_engine_id_classified_correctly(self) -> None:
        cid = "MON.abc-123.42.0"
        assert is_engine_originated(cid) is True
        assert is_pm_originated(cid) is False

    def test_garbage_rejected_by_both_without_raising(self) -> None:
        assert is_pm_originated("not-a-command-id") is False
        assert is_engine_originated("not-a-command-id") is False

    def test_pm_with_malformed_envelope_id_rejected(self) -> None:
        assert is_pm_originated("inv-X.REC-2.0.0") is False

    def test_engine_with_malformed_trigger_rejected(self) -> None:
        assert is_engine_originated("MON.abc.notanumber.0") is False

    def test_empty_string_rejected_by_both(self) -> None:
        assert is_pm_originated("") is False
        assert is_engine_originated("") is False


# ---------------------------------------------------------------------------
# Component-record frozen invariants (ALP-476 — Pydantic→frozen dataclass)
# ---------------------------------------------------------------------------


class TestComponentsAreFrozen:
    def test_pm_components_reject_attribute_assignment(self) -> None:
        import dataclasses

        components = parse_pm_command_id("inv-X.ENV-REC-2.0.0")
        with pytest.raises(dataclasses.FrozenInstanceError):
            components.attempt_seq = 99  # type: ignore[misc]

    def test_engine_components_reject_attribute_assignment(self) -> None:
        import dataclasses

        components = parse_engine_command_id("MON.abc-123.42.0")
        with pytest.raises(dataclasses.FrozenInstanceError):
            components.trigger_id = 99  # type: ignore[misc]
