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
    Symbol,
    ThesisId,
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
from alphamind.execution.oms.command_ids import (
    base_command_id,
    derive_open_thesis_id,
    synthesize_id_suffix,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# A representative broker-carried thesis FK (THE-<ticker>-<32hex>) — the
# originating-thesis segment every AlphaMind-derived command id now carries.
_THESIS_ID = "THE-NVDA-0123456789abcdef0123456789abcdef"


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
    """Format, prefixing, and validation invariants on the PM derive path.

    Round-trip / thesis-carrying behavior lives in
    ``TestPMCommandIdCarriesThesis``; these cover the base-id shape and the
    pre-existing ValueError validation.
    """

    def test_worked_example_first_submission(self) -> None:
        """Worked example from oms-command-ids.md: first submission is .0.0,
        now with the broker-carried thesis segment appended."""
        result = derive_pm_command_id(
            invocation_id="inv-2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=ThesisId(_THESIS_ID),
        )
        assert result == f"inv-2026-04-23T14-30Z.ENV-REC-2.0.0~the-{_THESIS_ID}"

    def test_prefixes_inv_when_missing(self) -> None:
        """Mirrors the existing _format_command_id gate: prefix inv- only when absent."""
        result = derive_pm_command_id(
            invocation_id="2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=ThesisId(_THESIS_ID),
        )
        assert result.startswith("inv-2026-04-23T14-30Z.ENV-REC-2.0.0~the-")

    def test_does_not_double_prefix(self) -> None:
        """Already-prefixed invocation_id is not re-prefixed."""
        result = derive_pm_command_id(
            invocation_id="inv-anything",
            envelope_id="ENV-SA-7",
            command_ordinal=0,
            attempt_seq=2,
            thesis_id=ThesisId(_THESIS_ID),
        )
        assert result.startswith("inv-anything.ENV-SA-7.0.2~the-")

    def test_negative_command_ordinal_raises(self) -> None:
        with pytest.raises(ValueError, match="command_ordinal"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=-1,
                attempt_seq=0,
                thesis_id=ThesisId(_THESIS_ID),
            )

    def test_negative_attempt_seq_raises(self) -> None:
        with pytest.raises(ValueError, match="attempt_seq"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=-1,
                thesis_id=ThesisId(_THESIS_ID),
            )

    def test_invalid_envelope_id_raises(self) -> None:
        with pytest.raises(ValueError, match="envelope_id"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="REC-1",  # missing ENV- prefix
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=ThesisId(_THESIS_ID),
            )

    def test_invalid_envelope_id_alt_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="envelope_id"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-OTHER-1",
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=ThesisId(_THESIS_ID),
            )

    def test_empty_invocation_raises(self) -> None:
        with pytest.raises(ValueError, match="invocation_id"):
            derive_pm_command_id(
                invocation_id="",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=ThesisId(_THESIS_ID),
            )


# ---------------------------------------------------------------------------
# derive_engine_command_id
# ---------------------------------------------------------------------------


class TestDeriveEngineCommandId:
    """Format, default-ordinal, and validation invariants on the engine derive
    path. Round-trip / thesis behavior lives in
    ``TestEngineCommandIdCarriesThesis``."""

    def test_default_command_ordinal_is_zero(self) -> None:
        result = derive_engine_command_id(
            monitor_session_id="abc-123",
            trigger_id=42,
            thesis_id=ThesisId(_THESIS_ID),
            invocation_id="inv-X",
        )
        assert result == f"MON.abc-123.42.0~the-{_THESIS_ID}~inv-X"

    def test_explicit_command_ordinal(self) -> None:
        result = derive_engine_command_id(
            monitor_session_id="abc-123",
            trigger_id=42,
            command_ordinal=3,
            thesis_id=ThesisId(_THESIS_ID),
            invocation_id="inv-X",
        )
        assert result.startswith("MON.abc-123.42.3~the-")

    def test_empty_monitor_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="monitor_session_id"):
            derive_engine_command_id(
                monitor_session_id="",
                trigger_id=1,
                thesis_id=ThesisId(_THESIS_ID),
                invocation_id="inv-X",
            )

    def test_dotted_monitor_session_id_raises(self) -> None:
        with pytest.raises(ValueError, match="monitor_session_id"):
            derive_engine_command_id(
                monitor_session_id="abc.123",
                trigger_id=1,
                thesis_id=ThesisId(_THESIS_ID),
                invocation_id="inv-X",
            )

    def test_negative_trigger_id_raises(self) -> None:
        with pytest.raises(ValueError, match="trigger_id"):
            derive_engine_command_id(
                monitor_session_id="abc-123",
                trigger_id=-1,
                thesis_id=ThesisId(_THESIS_ID),
                invocation_id="inv-X",
            )

    def test_negative_command_ordinal_raises(self) -> None:
        with pytest.raises(ValueError, match="command_ordinal"):
            derive_engine_command_id(
                monitor_session_id="abc-123",
                trigger_id=1,
                command_ordinal=-1,
                thesis_id=ThesisId(_THESIS_ID),
                invocation_id="inv-X",
            )


# ---------------------------------------------------------------------------
# parse_pm_command_id
# ---------------------------------------------------------------------------


class TestParsePMCommandId:
    def test_full_decomposition(self) -> None:
        components = parse_pm_command_id(f"inv-2026-04-23T14-30Z.ENV-REC-2.0.1~the-{_THESIS_ID}")
        assert components == PMCommandIdComponents(
            invocation_id=InvocationId("2026-04-23T14-30Z"),
            envelope_id=EnvelopeId("ENV-REC-2"),
            command_ordinal=0,
            attempt_seq=1,
            thesis_id=ThesisId(_THESIS_ID),
        )

    def test_strategist_pending_order_envelope_id(self) -> None:
        components = parse_pm_command_id(f"inv-X.ENV-SA-ORD-3.0.2~the-{_THESIS_ID}")
        assert components.envelope_id == "ENV-SA-ORD-3"
        assert components.attempt_seq == 2

    def test_missing_inv_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id(f"X.ENV-REC-2.0.0~the-{_THESIS_ID}")

    def test_engine_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id(f"MON.abc.0.0~the-{_THESIS_ID}~inv-X")

    def test_base_id_without_link_rejected(self) -> None:
        """A legacy base id with no broker-carried thesis segment no longer
        parses — the link is mandatory on the order-derivation contract."""
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("inv-X.ENV-REC-2.0.0")

    def test_malformed_garbage_raises(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_pm_command_id("not-a-command-id")


# ---------------------------------------------------------------------------
# parse_engine_command_id
# ---------------------------------------------------------------------------


class TestParseEngineCommandId:
    def test_full_decomposition(self) -> None:
        components = parse_engine_command_id(
            f"MON.abc-123.42.0~the-{_THESIS_ID}~inv-2026-04-23T14-30Z"
        )
        assert components == EngineCommandIdComponents(
            monitor_session_id="abc-123",
            trigger_id=42,
            command_ordinal=0,
            thesis_id=ThesisId(_THESIS_ID),
            invocation_id=InvocationId("2026-04-23T14-30Z"),
        )

    def test_pm_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_engine_command_id(f"inv-X.ENV-REC-2.0.0~the-{_THESIS_ID}")

    def test_base_id_without_link_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_id"):
            parse_engine_command_id("MON.abc-123.42.0")

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
        cid = f"inv-2026-04-23T14-30Z.ENV-REC-2.0.0~the-{_THESIS_ID}"
        assert is_pm_originated(cid) is True
        assert is_engine_originated(cid) is False

    def test_engine_id_classified_correctly(self) -> None:
        cid = f"MON.abc-123.42.0~the-{_THESIS_ID}~inv-X"
        assert is_engine_originated(cid) is True
        assert is_pm_originated(cid) is False

    def test_garbage_rejected_by_both_without_raising(self) -> None:
        assert is_pm_originated("not-a-command-id") is False
        assert is_engine_originated("not-a-command-id") is False

    def test_pm_with_malformed_envelope_id_rejected(self) -> None:
        assert is_pm_originated(f"inv-X.REC-2.0.0~the-{_THESIS_ID}") is False

    def test_engine_with_malformed_trigger_rejected(self) -> None:
        assert is_engine_originated(f"MON.abc.notanumber.0~the-{_THESIS_ID}~inv-X") is False

    def test_thesis_less_base_id_rejected_by_both(self) -> None:
        """A legacy thesis-less base id is no longer a structurally-valid
        order command id — the discriminators return False without raising,
        so forensic / out-of-band ids never masquerade as AlphaMind orders."""
        assert is_pm_originated("inv-X.ENV-REC-2.0.0") is False
        assert is_engine_originated("MON.abc-123.42.0") is False

    def test_empty_string_rejected_by_both(self) -> None:
        assert is_pm_originated("") is False
        assert is_engine_originated("") is False


# ---------------------------------------------------------------------------
# Component-record frozen invariants (ALP-476 — Pydantic→frozen dataclass)
# ---------------------------------------------------------------------------


class TestComponentsAreFrozen:
    def test_pm_components_reject_attribute_assignment(self) -> None:
        import dataclasses

        components = parse_pm_command_id(
            "inv-X.ENV-REC-2.0.0~the-THE-NVDA-0123456789abcdef0123456789abcdef"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            components.attempt_seq = 99  # type: ignore[misc]

    def test_engine_components_reject_attribute_assignment(self) -> None:
        import dataclasses

        components = parse_engine_command_id(
            "MON.abc-123.42.0~the-THE-NVDA-0123456789abcdef0123456789abcdef~inv-2026-04-23T14-30Z"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            components.trigger_id = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Broker-carried link: thesis + invocation FK embedded in the command id
# (ALP-844 / story 01b). The derived id round-trips the originating thesis
# (why) and invocation (when), so a fill self-attributes with no order row.
# ---------------------------------------------------------------------------


class TestPMCommandIdCarriesThesis:
    def test_round_trips_thesis_and_invocation(self) -> None:
        """derive_pm_command_id(..., thesis_id=T, invocation_id=I) → parse
        round-trips thesis_id == T and invocation_id == I (AC 1)."""
        cid = derive_pm_command_id(
            invocation_id="inv-2026-04-23T14-30Z",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=ThesisId(_THESIS_ID),
        )
        components = parse_pm_command_id(cid)
        assert components.thesis_id == ThesisId(_THESIS_ID)
        assert components.invocation_id == InvocationId("2026-04-23T14-30Z")

    def test_absent_thesis_raises(self) -> None:
        """An ABSENT (None) thesis raises ValueError — the thesis-less order
        path the prior attempt's sentinel created must be unrepresentable."""
        with pytest.raises(ValueError, match="thesis_id is required"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=None,  # type: ignore[arg-type]
            )

    def test_empty_thesis_raises(self) -> None:
        with pytest.raises(ValueError, match="thesis_id is required"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=ThesisId(""),
            )

    def test_malformed_thesis_raises(self) -> None:
        with pytest.raises(ValueError, match="thesis_id must match"):
            derive_pm_command_id(
                invocation_id="inv-X",
                envelope_id="ENV-REC-1",
                command_ordinal=0,
                attempt_seq=0,
                thesis_id=ThesisId("not-a-thesis"),
            )

    def test_dotted_ticker_thesis_round_trips(self) -> None:
        """A multi-share-class ticker thesis (THE-BRK.B-…) round-trips —
        the dot does not make the grammar ambiguous."""
        thesis = ThesisId("THE-BRK.B-0123456789abcdef0123456789abcdef")
        cid = derive_pm_command_id(
            invocation_id="inv-X",
            envelope_id="ENV-SA-ORD-3",
            command_ordinal=0,
            attempt_seq=2,
            thesis_id=thesis,
        )
        assert parse_pm_command_id(cid).thesis_id == thesis

    def test_attempt_seq_distinguishes_replays_thesis_held_constant(self) -> None:
        """Two derivations differing only in attempt_seq are distinct;
        identical inputs (thesis included) are replay-idempotent (AC 4)."""
        kwargs = {
            "invocation_id": "inv-X",
            "envelope_id": "ENV-REC-2",
            "command_ordinal": 0,
            "thesis_id": ThesisId(_THESIS_ID),
        }
        first = derive_pm_command_id(attempt_seq=0, **kwargs)  # type: ignore[arg-type]
        retry = derive_pm_command_id(attempt_seq=1, **kwargs)  # type: ignore[arg-type]
        again = derive_pm_command_id(attempt_seq=0, **kwargs)  # type: ignore[arg-type]
        assert first != retry
        assert first == again


class TestEngineCommandIdCarriesThesis:
    def test_round_trips_thesis_and_invocation(self) -> None:
        """derive_engine_command_id(..., thesis_id=T, invocation_id=I) → parse
        round-trips thesis_id == T and invocation_id == I (AC 2)."""
        cid = derive_engine_command_id(
            monitor_session_id="mon-20260423T143000Z-abcd1234",
            trigger_id=7,
            thesis_id=ThesisId(_THESIS_ID),
            invocation_id="inv-2026-04-23T14-30Z",
        )
        components = parse_engine_command_id(cid)
        assert components.thesis_id == ThesisId(_THESIS_ID)
        assert components.invocation_id == InvocationId("2026-04-23T14-30Z")
        assert components.monitor_session_id == "mon-20260423T143000Z-abcd1234"
        assert components.trigger_id == 7

    def test_absent_thesis_raises(self) -> None:
        with pytest.raises(ValueError, match="thesis_id is required"):
            derive_engine_command_id(
                monitor_session_id="mon-1",
                trigger_id=1,
                thesis_id=None,  # type: ignore[arg-type]
                invocation_id="inv-X",
            )

    def test_absent_invocation_raises(self) -> None:
        with pytest.raises(ValueError, match="invocation_id"):
            derive_engine_command_id(
                monitor_session_id="mon-1",
                trigger_id=1,
                thesis_id=ThesisId(_THESIS_ID),
                invocation_id="",
            )

    def test_trigger_and_ordinal_distinguish_replays(self) -> None:
        """A fresh trigger_id / command_ordinal yields a distinct id;
        identical inputs are replay-idempotent (AC 4, engine side)."""
        base = {
            "monitor_session_id": "mon-1",
            "thesis_id": ThesisId(_THESIS_ID),
            "invocation_id": "inv-X",
        }
        t7 = derive_engine_command_id(trigger_id=7, **base)  # type: ignore[arg-type]
        t8 = derive_engine_command_id(trigger_id=8, **base)  # type: ignore[arg-type]
        t7_again = derive_engine_command_id(trigger_id=7, **base)  # type: ignore[arg-type]
        assert t7 != t8
        assert t7 == t7_again


class TestBrokerCarriedLinkBudget:
    def test_worst_case_pm_id_within_128(self) -> None:
        """A worst-case PM id (dot-bearing 10-char ticker thesis, deep
        strategist envelope, multi-digit ordinal/seq) is ≤ 128 chars (AC 3)."""
        cid = derive_pm_command_id(
            invocation_id="inv-20260423T143000Z-deadbeef",
            envelope_id="ENV-SA-ORD-999999",
            command_ordinal=999,
            attempt_seq=999,
            thesis_id=ThesisId("THE-ABCDEFGH.I-0123456789abcdef0123456789abcdef"),
        )
        assert len(cid) <= 128
        assert parse_pm_command_id(cid).thesis_id == ThesisId(
            "THE-ABCDEFGH.I-0123456789abcdef0123456789abcdef"
        )

    def test_worst_case_engine_id_within_128(self) -> None:
        """A worst-case engine id (mon-session, multi-digit trigger,
        dot-bearing thesis, full invocation) is ≤ 128 chars (AC 3)."""
        cid = derive_engine_command_id(
            monitor_session_id="mon-20260423T143000Z-deadbeef",
            trigger_id=99999999,
            thesis_id=ThesisId("THE-ABCDEFGH.I-0123456789abcdef0123456789abcdef"),
            invocation_id="inv-20260423T143000Z-deadbeef",
        )
        assert len(cid) <= 128
        assert parse_engine_command_id(cid).thesis_id == ThesisId(
            "THE-ABCDEFGH.I-0123456789abcdef0123456789abcdef"
        )


class TestIdSuffixIgnoresBrokerCarriedLink:
    """The Phase-2 minted thesis / position / order ids hash the command id via
    ``synthesize_id_suffix``; embedding the broker-carried link must NOT shift
    them, or the embedded thesis FK would diverge from the persisted thesis_id.
    """

    def test_base_command_id_strips_link(self) -> None:
        cid = derive_pm_command_id(
            invocation_id="inv-X",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=ThesisId(_THESIS_ID),
        )
        assert base_command_id(cid) == "inv-X.ENV-REC-2.0.0"

    def test_base_command_id_is_identity_when_no_link(self) -> None:
        assert base_command_id("inv-X.ENV-REC-2.0.0") == "inv-X.ENV-REC-2.0.0"

    def test_suffix_unchanged_by_link(self) -> None:
        base = "inv-X.ENV-REC-2.0.0"
        linked = derive_pm_command_id(
            invocation_id="inv-X",
            envelope_id="ENV-REC-2",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=ThesisId(_THESIS_ID),
        )
        assert synthesize_id_suffix(linked) == synthesize_id_suffix(base)


class TestDeriveOpenThesisId:
    """``derive_open_thesis_id`` is the SOLE place the ``THE-{ticker}-{suffix}``
    OPEN thesis identity is constructed (ALP-844, A2). The value it mints is the
    one embedded in a PM OPEN command id, so it must round-trip through
    ``parse_pm_command_id`` — the link is the single source of truth.
    """

    def test_round_trips_through_parse_pm_command_id(self) -> None:
        """The thesis ``derive_open_thesis_id`` mints for an OPEN equals the
        thesis ``parse_pm_command_id`` reads back out of the command id derived
        with it — minted-once, read-everywhere."""
        ticker = "NVDA"
        base = "inv-2026-05-08T12:00:00Z-aaaa.ENV-REC-1.0.0"
        thesis_id = derive_open_thesis_id(ticker, base)
        command_id = derive_pm_command_id(
            invocation_id="inv-2026-05-08T12:00:00Z-aaaa",
            envelope_id="ENV-REC-1",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=thesis_id,
        )
        assert parse_pm_command_id(command_id).thesis_id == thesis_id

    def test_strips_link_so_base_and_full_command_id_are_equivalent(self) -> None:
        """``synthesize_id_suffix`` strips the embedded link first, so passing
        the base id or the full linked command id mints the same thesis — the
        property that breaks the circularity (the link depends on the suffix,
        the suffix depends only on the base)."""
        ticker = "NVDA"
        base = "inv-X.ENV-REC-1.0.0"
        from_base = derive_open_thesis_id(ticker, base)
        command_id = derive_pm_command_id(
            invocation_id="inv-X",
            envelope_id="ENV-REC-1",
            command_ordinal=0,
            attempt_seq=0,
            thesis_id=from_base,
        )
        assert derive_open_thesis_id(ticker, command_id) == from_base

    def test_matches_the_ticker_suffix_grammar(self) -> None:
        """The minted thesis is ``THE-{ticker}-{32hex}`` — the same grammar the
        broker-carried-link parser validates."""
        thesis_id = derive_open_thesis_id("NVDA", "inv-X.ENV-REC-1.0.0")
        assert thesis_id == ThesisId(f"THE-NVDA-{synthesize_id_suffix('inv-X.ENV-REC-1.0.0')}")
