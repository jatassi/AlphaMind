"""Tests for domain-researcher output data model (ALP-187)."""

from __future__ import annotations

from typing import Any

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
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


def _make_finding(**kwargs: Any) -> Finding:
    defaults: dict[str, Any] = dict(
        finding_id="SA-TECH-1",
        headline="Test headline",
        tickers=("NVDA",),
        signal_type=SignalType.PRICE_ACTION,
        strength=Strength.STRONG,
        detail="Some detail",
    )
    defaults.update(kwargs)
    return Finding(**defaults)


def _make_anomaly(**kwargs: Any) -> Anomaly:
    defaults: dict[str, Any] = dict(
        anomaly_id="SA-TECH-ANOM-1",
        description="Unusual volume",
        anomaly_type=AnomalyType.VOLUME,
        tickers=("NVDA",),
        severity="investigate_now",
        suggested_question="Why spike?",
    )
    defaults.update(kwargs)
    return Anomaly(**defaults)


def _make_thesis(**kwargs: Any) -> ThesisCandidate:
    defaults: dict[str, Any] = dict(
        thesis_candidate_id="SA-TECH-TC-1",
        ticker=Symbol("NVDA"),
        direction=Direction.LONG,
        setup_type=SetupType.CATALYST,
        catalyst="Earnings beat",
        time_horizon_hours="24-48",
        conviction_sketch=ConvictionSketch.HIGH,
        conviction_justification="Strong momentum",
        key_risk="Macro reversal",
    )
    defaults.update(kwargs)
    return ThesisCandidate(**defaults)


# ---------------------------------------------------------------------------
# Tracer bullet — module loads and basic construction works
# ---------------------------------------------------------------------------


def test_sector_brief_constructs() -> None:
    brief = _make_brief()
    assert brief.invocation_id == "inv-1"
    assert brief.sector == Sector.TECH_SEMIS


# ---------------------------------------------------------------------------
# Enum membership regression tests
# ---------------------------------------------------------------------------


def test_signal_type_members() -> None:
    expected = {
        "price_action",
        "flow",
        "options",
        "fundamental",
        "sentiment",
        "technical",
        "cross_asset",
    }
    assert {m.value for m in SignalType} == expected


def test_strength_members() -> None:
    expected = {"strong", "moderate", "weak"}
    assert {m.value for m in Strength} == expected


def test_anomaly_type_members() -> None:
    expected = {"volume", "price_flow_divergence", "correlation_break", "options_skew", "other"}
    assert {m.value for m in AnomalyType} == expected


def test_direction_members() -> None:
    expected = {"long", "short"}
    assert {m.value for m in Direction} == expected


def test_setup_type_members() -> None:
    expected = {"catalyst", "mean_reversion", "momentum", "divergence", "event"}
    assert {m.value for m in SetupType} == expected


def test_conviction_sketch_members() -> None:
    expected = {"low", "moderate", "high"}
    assert {m.value for m in ConvictionSketch} == expected


def test_signal_quality_members() -> None:
    expected = {"high", "moderate", "low", "degraded"}
    assert {m.value for m in SignalQuality} == expected


# ---------------------------------------------------------------------------
# SectorBrief cross-field validator
# ---------------------------------------------------------------------------


def test_sector_brief_degraded_requires_reason() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_brief(signal_quality=SignalQuality.DEGRADED, signal_quality_reason=None)


def test_sector_brief_degraded_with_reason_accepts() -> None:
    brief = _make_brief(
        signal_quality=SignalQuality.DEGRADED, signal_quality_reason="Low volume day"
    )
    assert brief.signal_quality_reason == "Low volume day"


def test_sector_brief_non_degraded_rejects_reason_high() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_brief(signal_quality=SignalQuality.HIGH, signal_quality_reason="should not be here")


def test_sector_brief_non_degraded_rejects_reason_moderate() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_brief(signal_quality=SignalQuality.MODERATE, signal_quality_reason="oops")


def test_sector_brief_non_degraded_rejects_reason_low() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_brief(signal_quality=SignalQuality.LOW, signal_quality_reason="oops")


# ---------------------------------------------------------------------------
# Finding constraints
# ---------------------------------------------------------------------------


def test_finding_rejects_empty_tickers() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_finding(tickers=())


def test_finding_id_regex_accepts_valid() -> None:
    for fid in ("SA-TECH-1", "SA-FIN-12", "SA-ENERGY-3"):
        f = _make_finding(finding_id=fid)
        assert f.finding_id == fid


def test_finding_id_regex_rejects_lowercase_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_finding(finding_id="SA-tech-1")


def test_finding_id_regex_rejects_unknown_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_finding(finding_id="SA-FOO-1")


def test_finding_id_regex_rejects_anom_format() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_finding(finding_id="SA-TECH-ANOM-1")


def test_finding_id_regex_rejects_missing_digit() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_finding(finding_id="SA-TECH-")


# ---------------------------------------------------------------------------
# Anomaly constraints
# ---------------------------------------------------------------------------


def test_anomaly_id_regex_accepts_valid() -> None:
    for aid in ("SA-TECH-ANOM-1", "SA-FIN-ANOM-12", "SA-ENERGY-ANOM-3"):
        a = _make_anomaly(anomaly_id=aid)
        assert a.anomaly_id == aid


def test_anomaly_id_regex_rejects_lowercase_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_anomaly(anomaly_id="SA-tech-ANOM-1")


def test_anomaly_id_regex_rejects_unknown_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_anomaly(anomaly_id="SA-FOO-ANOM-1")


def test_anomaly_id_regex_rejects_finding_format() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_anomaly(anomaly_id="SA-TECH-1")


# ---------------------------------------------------------------------------
# ThesisCandidate constraints
# ---------------------------------------------------------------------------


def test_thesis_candidate_rejects_empty_ticker() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_thesis(ticker=Symbol(""))


def test_thesis_candidate_id_regex_accepts_valid() -> None:
    for tcid in ("SA-TECH-TC-1", "SA-FIN-TC-12", "SA-ENERGY-TC-3"):
        tc = _make_thesis(thesis_candidate_id=tcid)
        assert tc.thesis_candidate_id == tcid


def test_thesis_candidate_id_regex_rejects_lowercase_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_thesis(thesis_candidate_id="SA-tech-TC-1")


def test_thesis_candidate_id_regex_rejects_unknown_sector() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_thesis(thesis_candidate_id="SA-FOO-TC-1")


def test_thesis_candidate_id_regex_rejects_finding_format() -> None:
    with pytest.raises((ValueError, TypeError)):
        _make_thesis(thesis_candidate_id="SA-TECH-1")


# ---------------------------------------------------------------------------
# Hashability — all records can be added to a frozenset
# ---------------------------------------------------------------------------


def test_finding_is_hashable() -> None:
    f1 = _make_finding(finding_id="SA-TECH-1")
    f2 = _make_finding(finding_id="SA-TECH-2")
    assert f1 in frozenset([f1, f2])


def test_anomaly_is_hashable() -> None:
    a1 = _make_anomaly(anomaly_id="SA-TECH-ANOM-1")
    a2 = _make_anomaly(anomaly_id="SA-TECH-ANOM-2")
    assert a1 in frozenset([a1, a2])


def test_thesis_candidate_is_hashable() -> None:
    tc1 = _make_thesis(thesis_candidate_id="SA-TECH-TC-1")
    tc2 = _make_thesis(thesis_candidate_id="SA-TECH-TC-2")
    assert tc1 in frozenset([tc1, tc2])


def test_sector_brief_is_hashable() -> None:
    brief1 = _make_brief(invocation_id="inv-1")
    brief2 = _make_brief(invocation_id="inv-2")
    assert brief1 in frozenset([brief1, brief2])


# ---------------------------------------------------------------------------
# SECTOR_PREFIX covers every Sector member
# ---------------------------------------------------------------------------


def test_sector_prefix_covers_all_sectors() -> None:
    assert set(SECTOR_PREFIX.keys()) == set(Sector)


def test_sector_prefix_values() -> None:
    assert SECTOR_PREFIX[Sector.TECH_SEMIS] == "SA-TECH"
    assert SECTOR_PREFIX[Sector.FINANCIALS] == "SA-FIN"
    assert SECTOR_PREFIX[Sector.ENERGY] == "SA-ENERGY"
