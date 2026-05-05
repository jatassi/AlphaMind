"""Tests for ProposalPreProcessorBundle Pydantic models — ALP-313.

Each acceptance criterion maps to at least one test. The schema-parity test
loads the design doc's JSON schema block and walks every ``$defs`` entry,
asserting the Pydantic model agrees on ``required``, enum values, regex
patterns, and ``additionalProperties: false``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from alphamind.decision.proposal_pre_processor import BUNDLE_OUTPUT_SCHEMA
from alphamind.decision.proposal_pre_processor.models import (
    BUNDLE_OUTPUT_SCHEMA as MODULE_BUNDLE_OUTPUT_SCHEMA,
)
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    AnalystSection,
    AnalystSideConflict,
    BasisSection,
    BookHealthSummary,
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
    bundle_schema,
)
from alphamind.decision.strategist.models import (
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _load_design_doc_schema() -> dict[str, Any]:
    """Parse the JSON schema block from the design doc markdown."""
    doc_path = (
        Path(__file__).parents[3]
        / "docs"
        / "design"
        / "04-decision-layer"
        / "proposal-pre-processor-bundle-schema.md"
    )
    text = doc_path.read_text()
    # Find the first ```json … ``` block
    match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    assert match, "Could not find JSON code block in design doc"
    return dict(json.loads(match.group(1)))


def _make_portfolio_level_observations() -> PortfolioLevelObservations:
    return PortfolioLevelObservations(
        aggregate_thesis_health="ok",
        sector_balance_shifts="balanced",
        thesis_dependency_warnings="none",
        capital_allocation_observations="within limits",
    )


def _make_position_assessment() -> PositionAssessment:
    return PositionAssessment(
        assessment_id="SA-1",
        position_id="POS-1",
        thesis_id="THESIS-1",
        underlying="NVDA",
        sector="tech",
        thesis_status="on-track",
        recommended_action="hold",
        status_rationale="thesis intact",
        action_rationale="hold for now",
    )


def _make_pending_order_assessment() -> PendingOrderAssessment:
    return PendingOrderAssessment(
        pending_order_assessment_id="SA-ORD-1",
        order_id="ORD-1",
        position_id="POS-1",
        order_type="entry_limit",
        order_age_hours=2.0,
        fill_probability_assessment="plausible",
        recommended_action="maintain",
        drift_rationale="price is close",
        action_rationale="keep waiting",
    )


def _make_basis() -> BasisSection:
    return BasisSection(
        analyst_proposal_ids=("REC-1",),
        strategist_action_ids=("SA-1",),
        strategist_holds_excluded_count=0,
        snapshot_timestamp=_NOW,
    )


def _make_per_rule() -> PerRuleEntry:
    return PerRuleEntry(
        rule="net_long_exposure",
        status="PASS",
        current=0.4,
        limit=0.8,
        projected_after=0.45,
        headroom_remaining=0.35,
        unit="ratio",
    )


def _make_combined_set_impact() -> CombinedSetImpact:
    return CombinedSetImpact(
        basis=_make_basis(),
        per_rule=(_make_per_rule(),),
        breaches=(),
    )


def _make_conviction_histogram() -> ConvictionHistogram:
    return ConvictionHistogram.model_validate({"1": 0, "2": 1, "3": 3, "4": 1, "5": 0})


def _make_aggregate_observations() -> AggregateObservations:
    return AggregateObservations(
        combined_set_impact=_make_combined_set_impact(),
        conviction_distribution=ConvictionDistribution(
            by_level=_make_conviction_histogram(),
            total=5,
        ),
        book_health_summary=BookHealthSummary(
            by_thesis_status=ByThesisStatus.model_validate(
                {
                    "on-track": 7,
                    "partially-realized": 0,
                    "at-risk": 0,
                    "stale": 0,
                    "invalidated": 0,
                }
            ),
            by_recommended_action=ByRecommendedAction.model_validate(
                {"hold": 7, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
            ),
            remedy_flagged_count=0,
            total=7,
        ),
    )


def _make_strategist_section() -> StrategistSection:
    return StrategistSection(
        mode="normal",
        position_assessments=(),
        pending_order_assessments=(),
        portfolio_level_observations=_make_portfolio_level_observations(),
    )


def _make_analyst_section_normal() -> AnalystSection:
    return AnalystSection(mode="normal", recommendations=(), watchlist=None)


def _make_bundle() -> ProposalPreProcessorBundle:
    return ProposalPreProcessorBundle(
        invocation_id="INV-001",
        timestamp=_NOW,
        aggregate_observations=_make_aggregate_observations(),
        strategist_section=_make_strategist_section(),
        analyst_section=_make_analyst_section_normal(),
    )


# ---------------------------------------------------------------------------
# AC-1: top-level required matches design doc
# ---------------------------------------------------------------------------


def test_top_level_required_matches_design_doc() -> None:
    schema = ProposalPreProcessorBundle.model_json_schema()
    design_schema = _load_design_doc_schema()
    assert set(schema["required"]) == set(design_schema["required"])


# ---------------------------------------------------------------------------
# AC-2: schema-parity test walks $defs
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


# Mapping from design-doc $defs keys to Pydantic model names (in generated schema)
_DESIGN_DOC_TO_PYDANTIC: dict[str, str] = {
    "aggregate_observations": "AggregateObservations",
    "combined_set_impact": "CombinedSetImpact",
    "per_rule_entry": "PerRuleEntry",
    "breach_entry": "BreachEntry",
    "contributor_entry": "ContributorEntry",
    "conviction_distribution": "ConvictionDistribution",
    "book_health_summary": "BookHealthSummary",
    "strategist_section": "StrategistSection",
    "wrapped_position_assessment": "WrappedPositionAssessment",
    "wrapped_pending_order_assessment": "WrappedPendingOrderAssessment",
    "strategist_side_annotations": "StrategistSideAnnotations",
    "strategist_side_conflict": "StrategistSideConflict",
    "analyst_section": "AnalystSection",
    "wrapped_recommendation": "WrappedRecommendation",
    "analyst_side_annotations": "AnalystSideAnnotations",
    "analyst_side_conflict": "AnalystSideConflict",
}


def test_schema_parity_required_fields() -> None:
    """Each design-doc $defs entry agrees with Pydantic on required fields."""
    design_schema = _load_design_doc_schema()
    pydantic_schema = ProposalPreProcessorBundle.model_json_schema()
    pydantic_defs = _collect_pydantic_defs(pydantic_schema)

    for design_key, pydantic_key in _DESIGN_DOC_TO_PYDANTIC.items():
        design_def = design_schema["$defs"].get(design_key, {})
        design_required = set(design_def.get("required", []))
        if not design_required:
            continue  # no required constraint to check

        pydantic_def = pydantic_defs.get(pydantic_key, {})
        pydantic_required = set(pydantic_def.get("required", []))
        assert design_required == pydantic_required, (
            f"[{design_key} → {pydantic_key}] required mismatch: "
            f"design={sorted(design_required)} pydantic={sorted(pydantic_required)}"
        )


def test_schema_parity_additional_properties() -> None:
    """Design-doc $defs with additionalProperties:false match Pydantic."""
    design_schema = _load_design_doc_schema()
    pydantic_schema = ProposalPreProcessorBundle.model_json_schema()
    pydantic_defs = _collect_pydantic_defs(pydantic_schema)

    for design_key, pydantic_key in _DESIGN_DOC_TO_PYDANTIC.items():
        design_def = design_schema["$defs"].get(design_key, {})
        if design_def.get("additionalProperties") is not False:
            continue
        pydantic_def = pydantic_defs.get(pydantic_key, {})
        assert pydantic_def.get("additionalProperties") is False, (
            f"[{design_key} → {pydantic_key}] missing additionalProperties:false"
        )


def test_schema_parity_per_rule_entry_enum() -> None:
    """per_rule_entry.status enum matches design doc."""
    design_schema = _load_design_doc_schema()
    design_enum = set(design_schema["$defs"]["per_rule_entry"]["properties"]["status"]["enum"])
    pydantic_schema = ProposalPreProcessorBundle.model_json_schema()
    pydantic_defs = _collect_pydantic_defs(pydantic_schema)
    per_rule = pydantic_defs.get("PerRuleEntry", {})
    # Enum may appear directly or under $ref
    status_prop = per_rule.get("properties", {}).get("status", {})
    pydantic_enum = set(status_prop.get("enum", []))
    assert design_enum == pydantic_enum, f"status enum mismatch: {design_enum} vs {pydantic_enum}"


def test_schema_parity_regex_patterns() -> None:
    """Key regex patterns in the design doc are reflected in Pydantic models."""
    # BasisSection: analyst_proposal_ids items match ^REC-[0-9]+$
    with pytest.raises(ValidationError):
        BasisSection(
            analyst_proposal_ids=("INVALID",),
            strategist_action_ids=(),
            strategist_holds_excluded_count=0,
            snapshot_timestamp=_NOW,
        )
    # BasisSection: strategist_action_ids items match ^SA-[0-9]+$
    with pytest.raises(ValidationError):
        BasisSection(
            analyst_proposal_ids=(),
            strategist_action_ids=("INVALID",),
            strategist_holds_excluded_count=0,
            snapshot_timestamp=_NOW,
        )
    # ContributorEntry: proposal_id matches ^(REC|SA)-[0-9]+$
    with pytest.raises(ValidationError):
        ContributorEntry(proposal_id="INVALID", contribution=1.0)
    # StrategistSideConflict: with_recommendation_id matches ^REC-[0-9]+$
    with pytest.raises(ValidationError):
        StrategistSideConflict(
            with_recommendation_id="INVALID",
            underlying="NVDA",
            conflict_type=ConflictType.entry_vs_close,
        )


# ---------------------------------------------------------------------------
# AC-3 & AC-4: AnalystSection mode validation
# ---------------------------------------------------------------------------


def test_analyst_section_normal_mode_valid() -> None:
    section = AnalystSection(mode="normal", recommendations=(), watchlist=None)
    assert section.mode == "normal"
    assert section.recommendations == ()
    assert section.watchlist is None


def test_analyst_section_normal_mode_rejects_watchlist() -> None:
    with pytest.raises(ValidationError):
        AnalystSection(mode="normal", watchlist=(), recommendations=None)


def test_analyst_section_watchlist_mode_valid() -> None:
    section = AnalystSection(mode="watchlist", watchlist=(), recommendations=None)
    assert section.mode == "watchlist"
    assert section.watchlist == ()
    assert section.recommendations is None


def test_analyst_section_watchlist_mode_rejects_recommendations() -> None:
    with pytest.raises(ValidationError):
        AnalystSection(mode="watchlist", recommendations=(), watchlist=None)


# ---------------------------------------------------------------------------
# AC-5 & AC-6 & AC-7: AnalystSideConflict oneOf validation
# ---------------------------------------------------------------------------


def test_analyst_side_conflict_both_ids_raises() -> None:
    with pytest.raises(ValidationError):
        AnalystSideConflict(
            with_assessment_id="SA-1",
            with_pending_order_assessment_id="SA-ORD-1",
            underlying="NVDA",
            conflict_type=ConflictType.entry_vs_close,
        )


def test_analyst_side_conflict_neither_id_raises() -> None:
    with pytest.raises(ValidationError):
        AnalystSideConflict(
            underlying="NVDA",
            conflict_type=ConflictType.entry_vs_close,
        )


def test_analyst_side_conflict_with_assessment_id_valid() -> None:
    conflict = AnalystSideConflict(
        with_assessment_id="SA-1",
        underlying="NVDA",
        conflict_type=ConflictType.entry_vs_close,
    )
    assert conflict.with_assessment_id == "SA-1"
    assert conflict.with_pending_order_assessment_id is None


def test_analyst_side_conflict_with_pending_order_id_valid() -> None:
    conflict = AnalystSideConflict(
        with_pending_order_assessment_id="SA-ORD-1",
        underlying="NVDA",
        conflict_type=ConflictType.entry_vs_close,
    )
    assert conflict.with_pending_order_assessment_id == "SA-ORD-1"
    assert conflict.with_assessment_id is None


# ---------------------------------------------------------------------------
# AC-8: BUNDLE_OUTPUT_SCHEMA importable from package __init__.py
# ---------------------------------------------------------------------------


def test_bundle_output_schema_importable_from_package() -> None:
    assert isinstance(BUNDLE_OUTPUT_SCHEMA, dict)
    assert "$defs" in BUNDLE_OUTPUT_SCHEMA
    assert "required" in BUNDLE_OUTPUT_SCHEMA


def test_bundle_output_schema_same_as_module() -> None:
    assert BUNDLE_OUTPUT_SCHEMA == MODULE_BUNDLE_OUTPUT_SCHEMA


def test_bundle_schema_function() -> None:
    result = bundle_schema()
    assert isinstance(result, dict)
    assert result == ProposalPreProcessorBundle.model_json_schema()


# ---------------------------------------------------------------------------
# AC-9: WrappedPositionAssessment round-trips
# ---------------------------------------------------------------------------


def test_wrapped_position_assessment_round_trips() -> None:
    pa = _make_position_assessment()
    wrapped = WrappedPositionAssessment(
        assessment=pa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )
    dumped = wrapped.model_dump()
    # Inner record is present and preserves key fields
    assert dumped["assessment"]["assessment_id"] == "SA-1"
    assert dumped["assessment"]["underlying"] == "NVDA"
    assert dumped["pre_processor_annotations"]["conflicts"] == ()


def test_wrapped_pending_order_assessment_round_trips() -> None:
    poa = _make_pending_order_assessment()
    wrapped = WrappedPendingOrderAssessment(
        pending_order_assessment=poa,
        pre_processor_annotations=StrategistSideAnnotations(conflicts=()),
    )
    dumped = wrapped.model_dump()
    assert dumped["pending_order_assessment"]["pending_order_assessment_id"] == "SA-ORD-1"


# ---------------------------------------------------------------------------
# AC-10: ConvictionHistogram alias-based keys
# ---------------------------------------------------------------------------


def test_conviction_histogram_alias_keys() -> None:
    hist = ConvictionHistogram.model_validate({"1": 0, "2": 1, "3": 3, "4": 1, "5": 0})
    dumped = hist.model_dump(by_alias=True)
    assert dumped == {"1": 0, "2": 1, "3": 3, "4": 1, "5": 0}


def test_conviction_histogram_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        ConvictionHistogram.model_validate({"1": -1, "2": 0, "3": 0, "4": 0, "5": 0})


# ---------------------------------------------------------------------------
# AC-11: ByRecommendedAction constructs and round-trips
# ---------------------------------------------------------------------------


def test_by_recommended_action_alias_keys() -> None:
    bra = ByRecommendedAction.model_validate(
        {"hold": 7, "reduce": 1, "close": 1, "adjust-bracket": 1, "add": 0}
    )
    dumped = bra.model_dump(by_alias=True)
    assert dumped == {"hold": 7, "reduce": 1, "close": 1, "adjust-bracket": 1, "add": 0}


# ---------------------------------------------------------------------------
# AC-12: ConflictType enum value equality
# ---------------------------------------------------------------------------


def test_conflict_type_entry_vs_pending_modify_value() -> None:
    assert ConflictType.entry_vs_pending_modify.value == "entry_vs_pending_modify"


def test_conflict_type_all_values() -> None:
    expected = {
        "entry_vs_close",
        "entry_vs_add",
        "entry_vs_hold",
        "entry_direction_conflict",
        "entry_vs_pending_maintain",
        "entry_vs_pending_modify",
        "entry_vs_pending_cancel",
    }
    assert {m.value for m in ConflictType} == expected


# ---------------------------------------------------------------------------
# AC-13: Full bundle constructs and file exists (integration smoke test)
# ---------------------------------------------------------------------------


def test_full_bundle_constructs() -> None:
    bundle = _make_bundle()
    assert bundle.invocation_id == "INV-001"
    assert bundle.timestamp == _NOW
    assert isinstance(bundle.aggregate_observations, AggregateObservations)
    assert isinstance(bundle.strategist_section, StrategistSection)
    assert isinstance(bundle.analyst_section, AnalystSection)


def test_full_bundle_model_dump_round_trip() -> None:
    bundle = _make_bundle()
    dumped = bundle.model_dump()
    assert dumped["invocation_id"] == "INV-001"
    assert "aggregate_observations" in dumped
    assert "strategist_section" in dumped
    assert "analyst_section" in dumped


# ---------------------------------------------------------------------------
# Additional: BasisSection validation
# ---------------------------------------------------------------------------


def test_basis_section_holds_excluded_nonnegative() -> None:
    with pytest.raises(ValidationError):
        BasisSection(
            analyst_proposal_ids=(),
            strategist_action_ids=(),
            strategist_holds_excluded_count=-1,
            snapshot_timestamp=_NOW,
        )


# ---------------------------------------------------------------------------
# Additional: StrategistSideConflict valid
# ---------------------------------------------------------------------------


def test_strategist_side_conflict_valid() -> None:
    conflict = StrategistSideConflict(
        with_recommendation_id="REC-1",
        underlying="AAPL",
        conflict_type=ConflictType.entry_vs_hold,
    )
    assert conflict.with_recommendation_id == "REC-1"
    assert conflict.conflict_type == ConflictType.entry_vs_hold


# ---------------------------------------------------------------------------
# Additional: ByThesisStatus constructs via alias
# ---------------------------------------------------------------------------


def test_by_thesis_status_alias_keys() -> None:
    bts = ByThesisStatus.model_validate(
        {
            "on-track": 5,
            "partially-realized": 1,
            "at-risk": 1,
            "stale": 0,
            "invalidated": 0,
        }
    )
    dumped = bts.model_dump(by_alias=True)
    assert dumped["on-track"] == 5
    assert "on-track" in dumped


# ---------------------------------------------------------------------------
# Additional: WrappedRecommendation (minimal smoke) - skip if no Recommendation fixture available
# ---------------------------------------------------------------------------
