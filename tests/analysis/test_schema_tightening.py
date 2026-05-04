"""Tests for the discriminated-conditional-field schema tightener — ALP-288 (A)."""

from __future__ import annotations

import enum
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel

from alphamind.analysis._schema_tightening import _tighten_conditional_schema


class _Verdict(enum.StrEnum):
    SIGNAL = "signal"
    NOISE = "noise"
    INCONCLUSIVE = "inconclusive"


class _Quality(enum.StrEnum):
    HIGH = "high"
    DEGRADED = "degraded"


class _ToyChild(BaseModel):
    """Nested-discriminator fixture mirroring AdaptiveBrief.InvestigationThread."""

    verdict: _Verdict
    implication: str | None = None
    refs: tuple[str, ...] | None = None
    dismissal: str | None = None


class _ToyRoot(BaseModel):
    """Root with a top-level-discriminator fixture mirroring QualitativeBrief."""

    quality: _Quality
    quality_reason: str | None = None
    children: tuple[_ToyChild, ...] = ()


_REQUIRED_BY_VERDICT: dict[_Verdict, frozenset[str]] = {
    _Verdict.SIGNAL: frozenset({"implication", "refs"}),
    _Verdict.NOISE: frozenset({"dismissal"}),
    _Verdict.INCONCLUSIVE: frozenset(),
}

_REQUIRED_BY_QUALITY: dict[_Quality, frozenset[str]] = {
    _Quality.DEGRADED: frozenset({"quality_reason"}),
    _Quality.HIGH: frozenset(),
}


def _tightened_root_schema() -> dict[str, Any]:
    """Build a fully-tightened root schema covering both nested + top-level."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyRoot, "quality", _REQUIRED_BY_QUALITY)
    _tighten_conditional_schema(schema, _ToyChild, "verdict", _REQUIRED_BY_VERDICT)
    return schema


# ---------------------------------------------------------------------------
# Schema-shape tests
# ---------------------------------------------------------------------------


def test_nested_object_gains_oneof_branches() -> None:
    """The targeted $def grows a oneOf with one branch per enum value."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyChild, "verdict", _REQUIRED_BY_VERDICT)
    branches = schema["$defs"]["_ToyChild"]["oneOf"]
    assert len(branches) == 3
    consts = {branch["properties"]["verdict"]["const"] for branch in branches}
    assert consts == {"signal", "noise", "inconclusive"}


def test_root_object_can_be_tightened_without_dollar_defs_lookup() -> None:
    """Top-level discriminators (QualitativeBrief shape) work the same way."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyRoot, "quality", _REQUIRED_BY_QUALITY)
    assert "oneOf" in schema
    consts = {branch["properties"]["quality"]["const"] for branch in schema["oneOf"]}
    assert consts == {"high", "degraded"}


def test_required_by_value_field_strips_null_variant() -> None:
    """A field required-by-this-branch loses the null arm of its anyOf."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyChild, "verdict", _REQUIRED_BY_VERDICT)
    signal_branch = next(
        b
        for b in schema["$defs"]["_ToyChild"]["oneOf"]
        if b["properties"]["verdict"]["const"] == "signal"
    )
    refs_schema = signal_branch["properties"]["refs"]
    # Should be an array, no null variant.
    if "anyOf" in refs_schema:
        assert all(arm.get("type") != "null" for arm in refs_schema["anyOf"])
    else:
        assert refs_schema.get("type") == "array"


def test_irrelevant_field_forced_to_null() -> None:
    """A conditional field NOT required by this branch is constrained to null."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyChild, "verdict", _REQUIRED_BY_VERDICT)
    signal_branch = next(
        b
        for b in schema["$defs"]["_ToyChild"]["oneOf"]
        if b["properties"]["verdict"]["const"] == "signal"
    )
    assert signal_branch["properties"]["dismissal"] == {"type": "null"}


def test_branch_required_lists_required_by_value_fields() -> None:
    """Each branch's required-list adds the branch's required conditional fields."""
    schema = _ToyRoot.model_json_schema()
    _tighten_conditional_schema(schema, _ToyChild, "verdict", _REQUIRED_BY_VERDICT)
    branches = {
        b["properties"]["verdict"]["const"]: b for b in schema["$defs"]["_ToyChild"]["oneOf"]
    }
    assert set(branches["signal"].get("required", [])) == {"implication", "refs"}
    assert set(branches["noise"].get("required", [])) == {"dismissal"}
    assert "required" not in branches["inconclusive"] or not branches["inconclusive"]["required"]


# ---------------------------------------------------------------------------
# Validator-behavior tests (jsonschema Draft 2020-12)
# ---------------------------------------------------------------------------


def _validate(payload: dict[str, Any]) -> None:
    schema = _tightened_root_schema()
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_signal_branch_with_empty_array_validates() -> None:
    """SIGNAL with refs=[] is valid (the empty-array case the spike's #5 missed)."""
    _validate(
        {
            "quality": "high",
            "quality_reason": None,
            "children": [
                {"verdict": "signal", "implication": "ok", "refs": [], "dismissal": None},
            ],
        }
    )


def test_signal_branch_rejects_null_for_required_array() -> None:
    """SIGNAL with refs=null is rejected — the load-bearing case the spike caught."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "quality": "high",
                "quality_reason": None,
                "children": [
                    {
                        "verdict": "signal",
                        "implication": "ok",
                        "refs": None,
                        "dismissal": None,
                    },
                ],
            }
        )


def test_signal_branch_rejects_non_null_for_irrelevant_field() -> None:
    """SIGNAL with dismissal populated is rejected (only NOISE may set it)."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "quality": "high",
                "quality_reason": None,
                "children": [
                    {
                        "verdict": "signal",
                        "implication": "ok",
                        "refs": [],
                        "dismissal": "should not be here",
                    },
                ],
            }
        )


def test_noise_branch_requires_dismissal() -> None:
    """NOISE without dismissal is rejected; with dismissal it passes."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "quality": "high",
                "quality_reason": None,
                "children": [
                    {"verdict": "noise", "implication": None, "refs": None, "dismissal": None},
                ],
            }
        )
    _validate(
        {
            "quality": "high",
            "quality_reason": None,
            "children": [
                {
                    "verdict": "noise",
                    "implication": None,
                    "refs": None,
                    "dismissal": "stale",
                },
            ],
        }
    )


def test_top_level_degraded_requires_quality_reason() -> None:
    """DEGRADED without quality_reason is rejected at the root branch."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"quality": "degraded", "quality_reason": None, "children": []})
    _validate({"quality": "degraded", "quality_reason": "api partial", "children": []})


def test_top_level_high_forbids_quality_reason() -> None:
    """HIGH with a quality_reason is rejected — only DEGRADED may set it."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"quality": "high", "quality_reason": "should not be here", "children": []})


# ---------------------------------------------------------------------------
# Real-model integration tests
# ---------------------------------------------------------------------------


def test_helper_works_on_adaptive_investigation_thread() -> None:
    """Smoke test: AdaptiveBrief.InvestigationThread tightens to a 3-branch oneOf."""
    from alphamind.analysis.adaptive_research.models import (
        AdaptiveBrief,
        Assessment,
        InvestigationThread,
    )

    schema = AdaptiveBrief.model_json_schema()
    _tighten_conditional_schema(
        schema,
        InvestigationThread,
        "assessment",
        {
            Assessment.SIGNAL: frozenset({"implication", "strengthens", "weakens"}),
            Assessment.NOISE: frozenset({"dismissal_reason"}),
            Assessment.INCONCLUSIVE: frozenset({"missing"}),
        },
    )
    branches = schema["$defs"]["InvestigationThread"]["oneOf"]
    assert {b["properties"]["assessment"]["const"] for b in branches} == {
        "signal",
        "noise",
        "inconclusive",
    }


def test_helper_works_on_qualitative_brief_top_level() -> None:
    """Smoke test: QualitativeBrief signal_quality_reason invariant tightens at root."""
    from alphamind.analysis._shared import SignalQuality
    from alphamind.analysis.qualitative_research.models import QualitativeBrief

    schema = QualitativeBrief.model_json_schema()
    _tighten_conditional_schema(
        schema,
        QualitativeBrief,
        "signal_quality",
        {
            SignalQuality.HIGH: frozenset(),
            SignalQuality.MODERATE: frozenset(),
            SignalQuality.LOW: frozenset(),
            SignalQuality.DEGRADED: frozenset({"signal_quality_reason"}),
        },
    )
    branches = schema["oneOf"]
    consts = {b["properties"]["signal_quality"]["const"] for b in branches}
    assert consts == {"high", "moderate", "low", "degraded"}
    degraded = next(b for b in branches if b["properties"]["signal_quality"]["const"] == "degraded")
    assert "signal_quality_reason" in degraded["properties"]
    assert degraded["properties"]["signal_quality_reason"].get("type") == "string"


def test_helper_works_on_sector_brief_top_level() -> None:
    """Smoke test: SectorBrief signal_quality_reason invariant tightens at root."""
    from alphamind.analysis._shared import SignalQuality
    from alphamind.analysis.domain_researchers.models import SectorBrief

    schema = SectorBrief.model_json_schema()
    _tighten_conditional_schema(
        schema,
        SectorBrief,
        "signal_quality",
        {
            SignalQuality.HIGH: frozenset(),
            SignalQuality.MODERATE: frozenset(),
            SignalQuality.LOW: frozenset(),
            SignalQuality.DEGRADED: frozenset({"signal_quality_reason"}),
        },
    )
    branches = schema["oneOf"]
    assert len(branches) == 4


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


def test_missing_branch_raises_value_error() -> None:
    schema = _ToyRoot.model_json_schema()
    with pytest.raises(ValueError, match="missing branches"):
        _tighten_conditional_schema(
            schema,
            _ToyChild,
            "verdict",
            {_Verdict.SIGNAL: frozenset({"implication"})},
        )


def test_unknown_value_raises_value_error() -> None:
    class _Extra(enum.StrEnum):
        SIGNAL = "signal"
        NOISE = "noise"
        INCONCLUSIVE = "inconclusive"
        SURPRISE = "surprise"

    schema = _ToyRoot.model_json_schema()
    with pytest.raises(ValueError, match="unknown values"):
        _tighten_conditional_schema(
            schema,
            _ToyChild,
            "verdict",
            {
                _Extra.SIGNAL: frozenset({"implication", "refs"}),
                _Extra.NOISE: frozenset({"dismissal"}),
                _Extra.INCONCLUSIVE: frozenset(),
                _Extra.SURPRISE: frozenset(),
            },
        )


def test_unknown_model_class_raises_value_error() -> None:
    class _Stranger(BaseModel):
        pass

    full_cover: dict[_Verdict, frozenset[str]] = {
        _Verdict.SIGNAL: frozenset(),
        _Verdict.NOISE: frozenset(),
        _Verdict.INCONCLUSIVE: frozenset(),
    }
    schema = _ToyRoot.model_json_schema()
    with pytest.raises(ValueError, match="Could not locate"):
        _tighten_conditional_schema(schema, _Stranger, "verdict", full_cover)


def test_unknown_discriminator_field_raises_value_error() -> None:
    full_cover: dict[_Verdict, frozenset[str]] = {
        _Verdict.SIGNAL: frozenset(),
        _Verdict.NOISE: frozenset(),
        _Verdict.INCONCLUSIVE: frozenset(),
    }
    schema = _ToyRoot.model_json_schema()
    with pytest.raises(ValueError, match="no property"):
        _tighten_conditional_schema(schema, _ToyChild, "nonexistent_field", full_cover)
