"""Tests for the per-upstream brief → :class:`BriefBundle` adapters — story 05a (ALP-203)."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.analysis._shared import (
    Sector,
    SignalQuality,
)
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    AnomalyType,
    ConvictionSketch,
    Direction,
    Finding,
    SectorBrief,
    SetupType,
    SignalType,
    Strength,
    ThesisCandidate,
)
from alphamind.analysis.qualitative_research.models import (
    CatalystWatch,
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.synthesizer.adapters import (
    adaptive_brief_to_bundle,
    correlation_regime_brief_to_bundle,
    qualitative_brief_to_bundle,
    sector_brief_to_bundle,
)
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.reference_extractor import extract_references
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FRESHNESS = datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _sector_brief(sector: Sector, prefix: str) -> SectorBrief:
    """Build a SectorBrief with one of each section item."""
    return SectorBrief(
        invocation_id="inv-1",
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline="First finding headline.",
                tickers=("AAA", "BBB"),
                signal_type=SignalType.FLOW,
                strength=Strength.STRONG,
                detail="Two-sentence detail with specifics.",
            ),
            Finding(
                finding_id=f"{prefix}-2",
                headline="Second finding headline.",
                tickers=("CCC",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.MODERATE,
                detail="Detail.",
            ),
        ),
        anomalies=(
            Anomaly(
                anomaly_id=f"{prefix}-ANOM-1",
                description="Volume spike.",
                anomaly_type=AnomalyType.VOLUME,
                tickers=("AAA",),
                severity="investigate_now",
                suggested_question="What drove the volume?",
            ),
        ),
        thesis_candidates=(
            ThesisCandidate(
                thesis_candidate_id=f"{prefix}-TC-1",
                ticker="AAA",
                direction=Direction.LONG,
                setup_type=SetupType.MOMENTUM,
                catalyst="Earnings tomorrow.",
                time_horizon_hours="24-72h",
                conviction_sketch=ConvictionSketch.MODERATE,
                conviction_justification="Pattern looks clean.",
                key_risk="Earnings miss.",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Sector adapter
# ---------------------------------------------------------------------------


def test_sector_brief_to_bundle_uses_sector_prefix() -> None:
    """A TECH_SEMIS brief renders headers under the SA-TECH prefix."""
    brief = _sector_brief(Sector.TECH_SEMIS, "SA-TECH")
    bundle = sector_brief_to_bundle(brief, _FRESHNESS)
    assert bundle.source is BriefSource.SA_TECH
    assert bundle.freshness == _FRESHNESS
    # All three section prefixes appear as headers.
    assert "[SA-TECH-1]" in bundle.text
    assert "[SA-TECH-2]" in bundle.text
    assert "[SA-TECH-ANOM-1]" in bundle.text
    assert "[SA-TECH-TC-1]" in bundle.text


def test_sector_brief_to_bundle_round_trips_through_extractor() -> None:
    """Rendered text re-extracts to the expected ref-ID set."""
    brief = _sector_brief(Sector.FINANCIALS, "SA-FIN")
    bundle = sector_brief_to_bundle(brief, _FRESHNESS)
    extracted = extract_references(bundle.text)
    assert set(extracted) == {
        "SA-FIN-1",
        "SA-FIN-2",
        "SA-FIN-ANOM-1",
        "SA-FIN-TC-1",
    }
    assert bundle.source is BriefSource.SA_FIN


def test_sector_brief_to_bundle_energy_round_trip() -> None:
    """ENERGY sector maps to SA_ENERGY source and the SA-ENERGY prefix."""
    brief = _sector_brief(Sector.ENERGY, "SA-ENERGY")
    bundle = sector_brief_to_bundle(brief, _FRESHNESS)
    assert bundle.source is BriefSource.SA_ENERGY
    assert set(extract_references(bundle.text)) == {
        "SA-ENERGY-1",
        "SA-ENERGY-2",
        "SA-ENERGY-ANOM-1",
        "SA-ENERGY-TC-1",
    }


def test_sector_brief_to_bundle_preserves_finding_content() -> None:
    """A finding's headline and detail land inside its own section."""
    brief = _sector_brief(Sector.TECH_SEMIS, "SA-TECH")
    bundle = sector_brief_to_bundle(brief, _FRESHNESS)
    extracted = extract_references(bundle.text)
    section = extracted["SA-TECH-1"]
    assert "First finding headline." in section
    assert "Two-sentence detail with specifics." in section


# ---------------------------------------------------------------------------
# Correlation/regime adapter
# ---------------------------------------------------------------------------


def test_correlation_brief_to_bundle_passthrough() -> None:
    """The CR adapter wraps the brief's text and uses its freshness_min."""
    cr_freshness = datetime(2026, 5, 3, 11, 30, tzinfo=UTC)
    brief = CorrelationRegimeBrief(
        text="[CR-1] regime context.\n  detail.\n[CR-2] cross-sector.\n",
        reference_index={"CR-1": "regime.label", "CR-2": "q7.cross_sector_rotation"},
        freshness_min=cr_freshness,
    )
    bundle = correlation_regime_brief_to_bundle(brief)
    assert bundle.source is BriefSource.CR
    assert bundle.freshness == cr_freshness
    assert bundle.text == brief.text


def test_correlation_brief_to_bundle_round_trips() -> None:
    """The wrapped CR text re-extracts to its CR-N set."""
    brief = CorrelationRegimeBrief(
        text="[CR-1] regime.\n[CR-2] rotation.\n[CR-3] lead-lag.\n",
        reference_index={"CR-1": "a", "CR-2": "b", "CR-3": "c"},
        freshness_min=datetime(2026, 5, 3, 11, 30, tzinfo=UTC),
    )
    bundle = correlation_regime_brief_to_bundle(brief)
    assert set(extract_references(bundle.text)) == {"CR-1", "CR-2", "CR-3"}


# ---------------------------------------------------------------------------
# Qualitative adapter
# ---------------------------------------------------------------------------


def _qualitative_brief() -> QualitativeBrief:
    """Build a QualitativeBrief with both a thread and a catalyst-watch entry."""
    return QualitativeBrief(
        invocation_id="inv-1",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Rate-expectation regime shift.",
                relevance="all sectors",
                direction=ThreadDirection.BEARISH,
                subject="rate-sensitive tech",
                time_horizon=TimeHorizon.NEAR_TERM,
                evidence=(
                    EvidenceLine(
                        source_type="news",
                        observation="Hawkish FOMC minutes.",
                        citation="ND-T1",
                    ),
                    EvidenceLine(
                        source_type="prediction_markets",
                        observation="Hold odds +12pp.",
                        citation="kalshi:fomc-hold",
                    ),
                ),
                implication="Tighter financial conditions weigh on duration.",
            ),
            NarrativeThread(
                thread_id="QR-2",
                summary="Energy supply shock.",
                relevance="energy",
                direction=ThreadDirection.BULLISH,
                subject="upstream producers",
                time_horizon=TimeHorizon.IMMEDIATE,
                evidence=(
                    EvidenceLine(
                        source_type="news",
                        observation="OPEC surprise cut.",
                        citation="ND-T2",
                    ),
                    EvidenceLine(
                        source_type="sentiment",
                        observation="Sentiment extreme positive.",
                        citation="sentiment:opec",
                    ),
                ),
                implication="Crude pops, beneficiaries rerate.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker="NVDA",
                catalyst_name="Earnings",
                hours_to_event=18,
                thesis_impact="Direct test of pricing-power thesis.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="neutral, no notable shifts",
        ),
    )


def test_qualitative_brief_to_bundle_includes_qr_cw() -> None:
    """Both QR-N threads and QR-CW-N catalyst watches render as sections."""
    brief = _qualitative_brief()
    bundle = qualitative_brief_to_bundle(brief, _FRESHNESS)
    assert bundle.source is BriefSource.QR
    assert bundle.freshness == _FRESHNESS
    extracted = extract_references(bundle.text)
    assert {"QR-1", "QR-2", "QR-CW-1"}.issubset(extracted)


def test_qualitative_brief_to_bundle_round_trips() -> None:
    """Rendered QR text re-extracts to the expected ref-ID set."""
    brief = _qualitative_brief()
    bundle = qualitative_brief_to_bundle(brief, _FRESHNESS)
    extracted = extract_references(bundle.text)
    assert set(extracted) == {"QR-1", "QR-2", "QR-CW-1"}


def test_qualitative_brief_to_bundle_preserves_evidence_and_implication() -> None:
    """A thread section retains its evidence and implication text."""
    brief = _qualitative_brief()
    bundle = qualitative_brief_to_bundle(brief, _FRESHNESS)
    section = extract_references(bundle.text)["QR-1"]
    assert "Hawkish FOMC minutes." in section
    assert "Tighter financial conditions weigh on duration." in section


# ---------------------------------------------------------------------------
# Adaptive adapter
# ---------------------------------------------------------------------------


def _adaptive_brief() -> AdaptiveBrief:
    """Build an AdaptiveBrief with one signal and one noise thread."""
    return AdaptiveBrief(
        invocation_id="inv-1",
        threads_investigated_count=2,
        anomalies_triaged_count=2,
        anomalies_deferred=(),
        threads=(
            InvestigationThread(
                thread_id="AR-1",
                trigger="[SA-TECH-ANOM-1]",
                question="What drove the NVDA volume spike?",
                tickers=("NVDA",),
                sector=Sector.TECH_SEMIS,
                tools_used=("news_search", "options_flow"),
                findings=(
                    "Pre-earnings call buying.",
                    "No material news.",
                ),
                assessment=Assessment.SIGNAL,
                confidence=Confidence.MODERATE,
                implication="Positioning suggests upside skew.",
                strengthens=("SA-TECH-3",),
                weakens=(),
            ),
            InvestigationThread(
                thread_id="AR-2",
                trigger="[SA-FIN-ANOM-1]",
                question="What drove the financials flow imbalance?",
                tickers=("XLF",),
                sector=Sector.FINANCIALS,
                tools_used=("flow_lookup",),
                findings=("Routine month-end rebalance.",),
                assessment=Assessment.NOISE,
                confidence=Confidence.HIGH,
                dismissal_reason="Mechanical month-end rebalance, not directional.",
            ),
        ),
    )


def test_adaptive_brief_to_bundle_renders_findings() -> None:
    """All AR-N threads render as sections recoverable via extract_references."""
    brief = _adaptive_brief()
    bundle = adaptive_brief_to_bundle(brief, _FRESHNESS)
    assert bundle.source is BriefSource.AR
    assert bundle.freshness == _FRESHNESS
    extracted = extract_references(bundle.text)
    assert set(extracted) == {"AR-1", "AR-2"}


def test_adaptive_brief_to_bundle_preserves_assessment_text() -> None:
    """Each thread's assessment-specific fields land in its section."""
    brief = _adaptive_brief()
    bundle = adaptive_brief_to_bundle(brief, _FRESHNESS)
    extracted = extract_references(bundle.text)
    signal_section = extracted["AR-1"]
    assert "Positioning suggests upside skew." in signal_section
    noise_section = extracted["AR-2"]
    assert "Mechanical month-end rebalance" in noise_section


def test_adaptive_brief_to_bundle_empty_threads() -> None:
    """A zero-thread (quiet-cycle) brief still produces a parseable bundle."""
    brief = AdaptiveBrief(
        invocation_id="inv-1",
        threads_investigated_count=0,
        anomalies_triaged_count=0,
        anomalies_deferred=(),
        threads=(),
    )
    bundle = adaptive_brief_to_bundle(brief, _FRESHNESS)
    assert bundle.source is BriefSource.AR
    # Empty body is a valid (zero-section) brief.
    assert extract_references(bundle.text) == {}
