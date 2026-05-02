"""Tests for domain-researcher structural validator (ALP-196)."""

from __future__ import annotations

from typing import Any

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    AnomalyType,
    ConvictionSketch,
    Direction,
    Finding,
    SectorBrief,
    SetupType,
    SignalQuality,
    SignalType,
    Strength,
    ThesisCandidate,
)
from alphamind.analysis.domain_researchers.validation import (
    ValidationError,
    ValidationResult,
    validate_brief,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_brief(**kwargs: Any) -> SectorBrief:
    defaults: dict[str, Any] = dict(
        invocation_id="inv-1",
        sector=Sector.TECH_SEMIS,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(),
        anomalies=(),
        thesis_candidates=(),
    )
    defaults.update(kwargs)
    return SectorBrief(**defaults)


def _make_finding(finding_id: str, **kwargs: Any) -> Finding:
    defaults: dict[str, Any] = dict(
        headline="Test headline",
        tickers=("NVDA",),
        signal_type=SignalType.PRICE_ACTION,
        strength=Strength.STRONG,
        detail="Some detail",
    )
    defaults.update(kwargs)
    return Finding(finding_id=finding_id, **defaults)


def _make_anomaly(anomaly_id: str, **kwargs: Any) -> Anomaly:
    defaults: dict[str, Any] = dict(
        description="Unusual volume",
        anomaly_type=AnomalyType.VOLUME,
        tickers=("NVDA",),
        severity="investigate_now",
        suggested_question="Why spike?",
    )
    defaults.update(kwargs)
    return Anomaly(anomaly_id=anomaly_id, **defaults)


def _make_thesis(thesis_candidate_id: str, **kwargs: Any) -> ThesisCandidate:
    defaults: dict[str, Any] = dict(
        ticker="NVDA",
        direction=Direction.LONG,
        setup_type=SetupType.CATALYST,
        catalyst="Earnings beat",
        time_horizon_hours="24-48",
        conviction_sketch=ConvictionSketch.HIGH,
        conviction_justification="Strong momentum",
        key_risk="Macro reversal",
    )
    defaults.update(kwargs)
    return ThesisCandidate(thesis_candidate_id=thesis_candidate_id, **defaults)


# ---------------------------------------------------------------------------
# Tracer bullet — valid brief passes
# ---------------------------------------------------------------------------


def test_valid_brief_passes() -> None:
    brief = _make_brief()
    result = validate_brief(brief)
    assert result.is_valid is True
    assert result.errors == ()


# ---------------------------------------------------------------------------
# ValidationError and ValidationResult shapes
# ---------------------------------------------------------------------------


def test_validation_error_is_pydantic_model() -> None:
    err = ValidationError(
        field_path="findings[0].finding_id",
        rule="reference_prefix_consistency",
        message="Wrong prefix",
    )
    assert err.field_path == "findings[0].finding_id"
    assert err.rule == "reference_prefix_consistency"
    assert err.message == "Wrong prefix"


def test_validation_result_is_pydantic_model() -> None:
    result = ValidationResult(is_valid=True, errors=())
    assert result.is_valid is True
    assert result.errors == ()


# ---------------------------------------------------------------------------
# reference_prefix_consistency
# ---------------------------------------------------------------------------


def test_finding_wrong_prefix_fails() -> None:
    # Tech-semis brief but FIN prefix on finding
    brief = _make_brief(
        findings=(_make_finding("SA-FIN-1"),),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    assert len(result.errors) >= 1
    rules = {e.rule for e in result.errors}
    assert "reference_prefix_consistency" in rules


def test_anomaly_wrong_prefix_fails() -> None:
    brief = _make_brief(
        anomalies=(_make_anomaly("SA-FIN-ANOM-1"),),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "reference_prefix_consistency" in rules


def test_thesis_candidate_wrong_prefix_fails() -> None:
    brief = _make_brief(
        thesis_candidates=(_make_thesis("SA-FIN-TC-1"),),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "reference_prefix_consistency" in rules


def test_correct_prefix_passes_for_each_sector() -> None:
    for sector, prefix in [
        (Sector.TECH_SEMIS, "SA-TECH"),
        (Sector.FINANCIALS, "SA-FIN"),
        (Sector.ENERGY, "SA-ENERGY"),
    ]:
        brief = _make_brief(
            sector=sector,
            findings=(_make_finding(f"{prefix}-1"),),
            anomalies=(_make_anomaly(f"{prefix}-ANOM-1"),),
            thesis_candidates=(_make_thesis(f"{prefix}-TC-1"),),
        )
        result = validate_brief(brief)
        assert result.is_valid is True, f"sector={sector} failed: {result.errors}"


# ---------------------------------------------------------------------------
# findings_sequential_indexing
# ---------------------------------------------------------------------------


def test_findings_gap_fails() -> None:
    brief = _make_brief(
        findings=(
            _make_finding("SA-TECH-1"),
            _make_finding("SA-TECH-3"),  # gap: missing 2
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "findings_sequential_indexing" in rules


def test_findings_duplicate_fails() -> None:
    brief = _make_brief(
        findings=(
            _make_finding("SA-TECH-1"),
            _make_finding("SA-TECH-1"),  # duplicate
            _make_finding("SA-TECH-2"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "findings_sequential_indexing" in rules


def test_findings_not_starting_at_1_fails() -> None:
    brief = _make_brief(
        findings=(_make_finding("SA-TECH-2"),),  # starts at 2
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "findings_sequential_indexing" in rules


def test_findings_sequential_multiple_passes() -> None:
    brief = _make_brief(
        findings=(
            _make_finding("SA-TECH-1"),
            _make_finding("SA-TECH-2"),
            _make_finding("SA-TECH-3"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is True


def test_findings_empty_passes() -> None:
    brief = _make_brief(findings=())
    result = validate_brief(brief)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# anomalies_sequential_indexing
# ---------------------------------------------------------------------------


def test_anomalies_gap_fails() -> None:
    brief = _make_brief(
        anomalies=(
            _make_anomaly("SA-TECH-ANOM-1"),
            _make_anomaly("SA-TECH-ANOM-3"),  # gap
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "anomalies_sequential_indexing" in rules


def test_anomalies_duplicate_fails() -> None:
    brief = _make_brief(
        anomalies=(
            _make_anomaly("SA-TECH-ANOM-1"),
            _make_anomaly("SA-TECH-ANOM-1"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "anomalies_sequential_indexing" in rules


def test_anomalies_not_starting_at_1_fails() -> None:
    brief = _make_brief(
        anomalies=(_make_anomaly("SA-TECH-ANOM-2"),),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "anomalies_sequential_indexing" in rules


def test_anomalies_sequential_passes() -> None:
    brief = _make_brief(
        anomalies=(
            _make_anomaly("SA-TECH-ANOM-1"),
            _make_anomaly("SA-TECH-ANOM-2"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is True


def test_anomalies_empty_passes() -> None:
    brief = _make_brief(anomalies=())
    result = validate_brief(brief)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# thesis_candidates_sequential_indexing
# ---------------------------------------------------------------------------


def test_thesis_candidates_gap_fails() -> None:
    brief = _make_brief(
        thesis_candidates=(
            _make_thesis("SA-TECH-TC-1"),
            _make_thesis("SA-TECH-TC-3"),  # gap
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "thesis_candidates_sequential_indexing" in rules


def test_thesis_candidates_duplicate_fails() -> None:
    brief = _make_brief(
        thesis_candidates=(
            _make_thesis("SA-TECH-TC-1"),
            _make_thesis("SA-TECH-TC-1"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "thesis_candidates_sequential_indexing" in rules


def test_thesis_candidates_not_starting_at_1_fails() -> None:
    brief = _make_brief(
        thesis_candidates=(_make_thesis("SA-TECH-TC-2"),),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "thesis_candidates_sequential_indexing" in rules


def test_thesis_candidates_sequential_passes() -> None:
    brief = _make_brief(
        thesis_candidates=(
            _make_thesis("SA-TECH-TC-1"),
            _make_thesis("SA-TECH-TC-2"),
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is True


def test_thesis_candidates_empty_passes() -> None:
    brief = _make_brief(thesis_candidates=())
    result = validate_brief(brief)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# invocation_id_not_empty
# ---------------------------------------------------------------------------


def test_empty_invocation_id_fails() -> None:
    # SectorBrief allows empty string at model level so we bypass with model_construct
    brief = SectorBrief.model_construct(
        invocation_id="",
        sector=Sector.TECH_SEMIS,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(),
        anomalies=(),
        thesis_candidates=(),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    rules = {e.rule for e in result.errors}
    assert "invocation_id_not_empty" in rules


def test_nonempty_invocation_id_passes() -> None:
    brief = _make_brief(invocation_id="inv-abc-123")
    result = validate_brief(brief)
    assert result.is_valid is True


# ---------------------------------------------------------------------------
# Multiple errors accumulated
# ---------------------------------------------------------------------------


def test_multiple_errors_accumulated() -> None:
    # Wrong prefix AND gap in findings → multiple errors
    brief = _make_brief(
        findings=(
            _make_finding("SA-FIN-1"),  # wrong prefix for TECH_SEMIS
            _make_finding("SA-FIN-3"),  # wrong prefix AND gap
        ),
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    assert len(result.errors) >= 2


def test_field_path_populated_on_error() -> None:
    brief = _make_brief(
        findings=(_make_finding("SA-FIN-1"),),  # wrong prefix for TECH_SEMIS
    )
    result = validate_brief(brief)
    assert result.is_valid is False
    err = next(e for e in result.errors if e.rule == "reference_prefix_consistency")
    assert "finding" in err.field_path.lower()
