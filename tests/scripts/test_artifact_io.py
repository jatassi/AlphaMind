"""Round-trip tests for ``alphamind.scripts._artifact_io`` (ALP-287).

Every dump/load pair must reconstruct the original value verbatim so a
phase-N script can write its output and the phase-(N+1) script can
consume it without observable drift. Coverage spans the seven typed
artifacts the analysis-layer pipeline threads, plus the
``MissingArtifactError`` path the consumer scripts surface when an
upstream phase has not run yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alphamind.analysis._shared import Sector, SignalQuality
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
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.sector_assembly import SectorOutput
from alphamind.scripts._artifact_io import (
    ADAPTIVE_BRIEF_FILENAME,
    CORRELATION_REGIME_BRIEF_FILENAME,
    DISTILLATION_OUTPUTS_FILENAME,
    QUALITATIVE_BRIEF_FILENAME,
    RETRIEVAL_STORE_FILENAME,
    SECTOR_BRIEFS_FILENAME,
    UNIVERSAL_REGIME_LABEL_FILENAME,
    MissingArtifactError,
    dump_adaptive_brief,
    dump_correlation_regime_brief,
    dump_distillation_outputs,
    dump_qualitative_brief,
    dump_retrieval_store,
    dump_sector_briefs,
    dump_universal_regime_label,
    load_adaptive_brief,
    load_correlation_regime_brief,
    load_distillation_outputs,
    load_qualitative_brief,
    load_retrieval_store,
    load_sector_briefs,
    load_universal_regime_label,
    stage_artifacts_dir,
)

# ---------------------------------------------------------------------------
# Fixture builders — minimal but realistic instances of every typed artifact
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
_INVOCATION_ID = "20260503T143000Z-test"


def _make_correlation_regime_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="[CR-1] regime: vol_normalization\n[CR-2] cross-sector rotation\n",
        reference_index={"CR-1": "regime.label", "CR-2": "q7.cross_sector_rotation"},
        freshness_min=_AS_OF - timedelta(minutes=15),
    )


def _make_universal_regime_label() -> dict[str, object]:
    return {
        "regime_label": "vol_normalization",
        "transition_state": "stable",
        "prior_label": "vol_normalization",
        "invocations_held": 4,
        "indicator_agreement_count": 3,
        "vix_level": 18.0,
        "term_structure_basis": 0.5,
        "vvix_percentile": 0.55,
        "realized_vol_5d": 0.12,
    }


def _make_sector_output(audience: OutputAudience, label: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=label,
        text=f"# {label}\nbody.\n",
        tickers=("AAA", "BBB"),
        block_ids=("regime.label", "q1.volume_spike.aaa"),
        freshness_min=_AS_OF - timedelta(minutes=5),
    )


def _make_output_block() -> OutputBlock:
    return OutputBlock(
        block_id="q1.volume_spike.aaa",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=_AS_OF - timedelta(minutes=5),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={"ticker": "AAA", "current_volume_z": 2.4, "20d_avg_volume_m": 245.6},
        anomaly_flags=(
            AnomalyFlag(name="volume_spike", magnitude=2.4, severity="investigate_now"),
        ),
        regime_context="vol_normalization regime amplifies volume-spike interpretation",
    )


def _make_distillation_outputs() -> DistillationOutputs:
    return DistillationOutputs(
        sector_outputs={
            OutputAudience.SECTOR_TECH_SEMIS: _make_sector_output(
                OutputAudience.SECTOR_TECH_SEMIS, "Tech & Semis"
            ),
            OutputAudience.SECTOR_FINANCIALS: _make_sector_output(
                OutputAudience.SECTOR_FINANCIALS, "Financials"
            ),
            OutputAudience.SECTOR_ENERGY: _make_sector_output(
                OutputAudience.SECTOR_ENERGY, "Energy"
            ),
        },
        correlation_regime_brief=_make_correlation_regime_brief(),
        universal_regime_label=_make_universal_regime_label(),
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=4,
        total_anomalies=1,
        bootstrap_block_count=0,
        all_blocks=(_make_output_block(),),
    )


def _make_sector_brief(sector: Sector, prefix: str, ticker: str) -> SectorBrief:
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline=f"{ticker} unusual volume vs. peers.",
                tickers=(ticker,),
                signal_type=SignalType.FLOW,
                strength=Strength.STRONG,
                detail=f"{ticker} printed 2.4x its 30-day average volume.",
            ),
        ),
        anomalies=(
            Anomaly(
                anomaly_id=f"{prefix}-ANOM-1",
                description=f"{ticker} options flow tilted call-side.",
                anomaly_type=AnomalyType.OPTIONS_SKEW,
                tickers=(ticker,),
                severity="investigate_now",
                suggested_question="What does the news cycle say?",
            ),
        ),
        thesis_candidates=(
            ThesisCandidate(
                thesis_candidate_id=f"{prefix}-TC-1",
                ticker=ticker,
                direction=Direction.LONG,
                setup_type=SetupType.MOMENTUM,
                catalyst="Earnings within 24h.",
                time_horizon_hours="24-72h",
                conviction_sketch=ConvictionSketch.MODERATE,
                conviction_justification="Volume + relative strength.",
                key_risk="Earnings miss reverses thesis.",
            ),
        ),
    )


def _make_sector_briefs() -> tuple[SectorBrief, ...]:
    return (
        _make_sector_brief(Sector.TECH_SEMIS, "SA-TECH", "NVDA"),
        _make_sector_brief(Sector.FINANCIALS, "SA-FIN", "JPM"),
        _make_sector_brief(Sector.ENERGY, "SA-ENERGY", "XOM"),
    )


def _make_qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Hawkish rates narrative re-asserted.",
                relevance="cross-sector",
                direction=ThreadDirection.BEARISH,
                subject="rate-sensitive tech",
                time_horizon=TimeHorizon.NEAR_TERM,
                evidence=(
                    EvidenceLine(
                        source_type="news_digest",
                        observation="FOMC minutes signaled hold-longer stance.",
                        citation="ND-T1",
                    ),
                    EvidenceLine(
                        source_type="prediction_market",
                        observation="Hold-rate odds +12pp on Kalshi.",
                        citation="kalshi:fomc-hold",
                    ),
                ),
                implication="Pressure on duration-heavy growth names.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker="NVDA",
                catalyst_name="Q1 earnings",
                hours_to_event=18,
                thesis_impact="Direct test of momentum setup.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="risk-on with rates concern",
        ),
    )


def _make_adaptive_brief() -> AdaptiveBrief:
    return AdaptiveBrief(
        invocation_id=_INVOCATION_ID,
        threads_investigated_count=1,
        anomalies_triaged_count=1,
        anomalies_deferred=(),
        threads=(
            InvestigationThread(
                thread_id="AR-1",
                trigger="[SA-TECH-ANOM-1]",
                question="What drove the call-side options flow?",
                tickers=("NVDA",),
                sector=Sector.TECH_SEMIS,
                tools_used=("news_search",),
                findings=("Pre-earnings positioning.",),
                assessment=Assessment.SIGNAL,
                confidence=Confidence.MODERATE,
                implication="Upside skew confirmed.",
                strengthens=("SA-TECH-1",),
                weakens=(),
            ),
        ),
    )


def _make_retrieval_store() -> RetrievalStore:
    return RetrievalStore(
        entries={
            "SA-TECH-1": "NVDA unusual volume.",
            "QR-1": "Hawkish rates narrative.",
            "CR-1": "Regime: vol_normalization.",
        },
        freshness_by_source={
            BriefSource.SA_TECH: _AS_OF - timedelta(minutes=10),
            BriefSource.QR: _AS_OF - timedelta(minutes=8),
            BriefSource.CR: _AS_OF - timedelta(minutes=15),
        },
    )


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------


def test_stage_artifacts_dir_layout(tmp_path: Path) -> None:
    """The directory derives from ``<archive_root>/invocations/<id>/stage_artifacts``."""
    archive_root = tmp_path / "archive"
    out = stage_artifacts_dir(archive_root, _INVOCATION_ID)
    assert out == archive_root / "invocations" / _INVOCATION_ID / "stage_artifacts"


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


def test_correlation_regime_brief_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the brief verbatim."""
    brief = _make_correlation_regime_brief()
    written = dump_correlation_regime_brief(brief, tmp_path)
    assert written == tmp_path / CORRELATION_REGIME_BRIEF_FILENAME
    loaded = load_correlation_regime_brief(tmp_path)
    assert loaded == brief


def test_universal_regime_label_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the regime-label dict verbatim."""
    label = _make_universal_regime_label()
    written = dump_universal_regime_label(label, tmp_path)
    assert written == tmp_path / UNIVERSAL_REGIME_LABEL_FILENAME
    loaded = load_universal_regime_label(tmp_path)
    assert loaded == label


def test_distillation_outputs_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the full DistillationOutputs verbatim."""
    outputs = _make_distillation_outputs()
    written = dump_distillation_outputs(outputs, tmp_path)
    assert written == tmp_path / DISTILLATION_OUTPUTS_FILENAME
    loaded = load_distillation_outputs(tmp_path)
    assert loaded == outputs
    assert loaded.correlation_regime_brief == outputs.correlation_regime_brief
    assert loaded.all_blocks == outputs.all_blocks


def test_sector_briefs_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the 3-tuple of sector briefs verbatim."""
    briefs = _make_sector_briefs()
    written = dump_sector_briefs(briefs, tmp_path)
    assert written == tmp_path / SECTOR_BRIEFS_FILENAME
    loaded = load_sector_briefs(tmp_path)
    assert loaded == briefs


def test_qualitative_brief_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the qualitative brief verbatim."""
    brief = _make_qualitative_brief()
    written = dump_qualitative_brief(brief, tmp_path)
    assert written == tmp_path / QUALITATIVE_BRIEF_FILENAME
    loaded = load_qualitative_brief(tmp_path)
    assert loaded == brief


def test_adaptive_brief_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the adaptive brief verbatim."""
    brief = _make_adaptive_brief()
    written = dump_adaptive_brief(brief, tmp_path)
    assert written == tmp_path / ADAPTIVE_BRIEF_FILENAME
    loaded = load_adaptive_brief(tmp_path)
    assert loaded == brief


def test_retrieval_store_round_trip(tmp_path: Path) -> None:
    """Dump → load reconstructs the retrieval store verbatim."""
    store = _make_retrieval_store()
    written = dump_retrieval_store(store, tmp_path)
    assert written == tmp_path / RETRIEVAL_STORE_FILENAME
    loaded = load_retrieval_store(tmp_path)
    assert loaded == store


# ---------------------------------------------------------------------------
# Missing-artifact path
# ---------------------------------------------------------------------------


def test_missing_artifact_error_names_producer_script(tmp_path: Path) -> None:
    """A missing artifact surfaces a ``MissingArtifactError`` carrying the producer."""
    with pytest.raises(MissingArtifactError) as excinfo:
        load_distillation_outputs(tmp_path)
    assert excinfo.value.producer_script == "verify_distillation.py"
    assert "verify_distillation.py" in str(excinfo.value)
    assert excinfo.value.missing_path == tmp_path / DISTILLATION_OUTPUTS_FILENAME


def test_missing_qualitative_brief_names_producer(tmp_path: Path) -> None:
    """The qualitative-brief loader names ``verify_qualitative_researcher.py``."""
    with pytest.raises(MissingArtifactError) as excinfo:
        load_qualitative_brief(tmp_path)
    assert excinfo.value.producer_script == "verify_qualitative_researcher.py"


def test_missing_sector_briefs_names_producer(tmp_path: Path) -> None:
    """The sector-briefs loader names ``verify_domain_researchers.py``."""
    with pytest.raises(MissingArtifactError) as excinfo:
        load_sector_briefs(tmp_path)
    assert excinfo.value.producer_script == "verify_domain_researchers.py"


# ---------------------------------------------------------------------------
# Directory creation behavior
# ---------------------------------------------------------------------------


def test_dump_creates_missing_directory(tmp_path: Path) -> None:
    """Dumping into a non-existent directory creates it on demand."""
    target = tmp_path / "fresh" / "nested" / "stage_artifacts"
    assert not target.exists()
    dump_universal_regime_label(_make_universal_regime_label(), target)
    assert (target / UNIVERSAL_REGIME_LABEL_FILENAME).exists()
