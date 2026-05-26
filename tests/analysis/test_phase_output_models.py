"""Round-trip tests for the analysis-layer phase-output Pydantic boundary models.

Story ALP-691. Verifies:

1. ``DomainResearcherOutputModel`` round-trips ``DomainResearcherResult`` via
   ``from_domain`` / ``to_domain`` and through JSON serialization.
2. ``QualitativeResearcherResultModel`` round-trips ``QualitativeResearcherResult``.
3. ``AdaptiveResearcherResultModel`` round-trips ``AdaptiveResearcherResult``.
4. ``SynthesizerResultModel`` (with ``RetrievalStoreModel``) round-trips
   ``SynthesizerResult`` — including a populated ``freshness_by_source`` dict
   with multiple ``BriefSource`` enum keys.

Each test uses non-trivial sample values (not all-empty defaults) to exercise the
serialization paths properly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle as AdaptiveInputBundle,
)
from alphamind.analysis.adaptive_research.loaders import AdaptiveAnomalyInputs
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.domain_researchers.input_bundle import (
    InputBundle as DomainInputBundle,
)
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
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
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle as QualInputBundle,
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
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import SynthesizerResult

# ---------------------------------------------------------------------------
# Shared sample values
# ---------------------------------------------------------------------------

_INVOCATION_ID = "test-inv-phase-model-001"
_AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 3, 12, 30, 0, tzinfo=UTC)
_TOKENS = TokensUsed(
    input_tokens=1000,
    output_tokens=200,
    cache_read_tokens=500,
    cache_write_tokens=100,
)


# ---------------------------------------------------------------------------
# Helpers — domain researcher
# ---------------------------------------------------------------------------


def _finding(sector: Sector, n: int = 1) -> Finding:
    prefix = SECTOR_PREFIX[sector]
    return Finding(
        finding_id=f"{prefix}-{n}",
        headline=f"Test finding {n} for {sector}",
        tickers=("NVDA", "AMD"),
        signal_type=SignalType.PRICE_ACTION,
        strength=Strength.STRONG,
        detail="Significant breakout above resistance with heavy volume.",
    )


def _anomaly(sector: Sector, n: int = 1) -> Anomaly:
    prefix = SECTOR_PREFIX[sector]
    return Anomaly(
        anomaly_id=f"{prefix}-ANOM-{n}",
        description="Unusual options activity — large put spread opened.",
        anomaly_type=AnomalyType.OPTIONS_SKEW,
        tickers=("NVDA",),
        severity="investigate_now",
        suggested_question="Is a large holder hedging or initiating a directional bet?",
    )


def _thesis_candidate(sector: Sector, n: int = 1) -> ThesisCandidate:
    prefix = SECTOR_PREFIX[sector]
    return ThesisCandidate(
        thesis_candidate_id=f"{prefix}-TC-{n}",
        ticker="NVDA",
        direction=Direction.LONG,
        setup_type=SetupType.MOMENTUM,
        catalyst="Earnings beat consensus by 15%.",
        time_horizon_hours="24-48h",
        conviction_sketch=ConvictionSketch.HIGH,
        conviction_justification="Strong price/flow alignment with sector tailwinds.",
        key_risk="Broader market selloff on macro data.",
    )


def _sector_brief(sector: Sector) -> SectorBrief:
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(_finding(sector, 1), _finding(sector, 2)),
        anomalies=(_anomaly(sector, 1),),
        thesis_candidates=(_thesis_candidate(sector, 1),),
    )


def _domain_result(sector: Sector) -> DomainResearcherResult:
    bundle = DomainInputBundle(
        sector=sector,
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_text="sector distillation text",
        qualitative_text="sector qualitative text",
        bundle_text="full sector bundle text",
    )
    return DomainResearcherResult(
        sector=sector,
        brief=_sector_brief(sector),
        input_bundle=bundle,
        tokens_used=_TOKENS,
        wall_clock_seconds=3.14,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# Helpers — qualitative researcher
# ---------------------------------------------------------------------------


def _qualitative_result() -> QualitativeResearcherResult:
    evidence_1 = EvidenceLine(
        source_type="news",
        observation="Tech stocks rallied on AI optimism.",
        citation="Reuters, 2026-05-03",
    )
    evidence_2 = EvidenceLine(
        source_type="options_flow",
        observation="Large call buying in NVDA.",
        citation="Bloomberg options desk, 2026-05-03",
    )
    thread = NarrativeThread(
        thread_id="QR-1",
        summary="AI-driven momentum in tech sector persists.",
        relevance="Sector-level tailwind for long tech positions.",
        direction=ThreadDirection.BULLISH,
        subject="NVDA / semiconductor sector",
        time_horizon=TimeHorizon.NEAR_TERM,
        evidence=(evidence_1, evidence_2),
        implication="Sustains sector outperformance through next catalyst.",
    )
    catalyst = CatalystWatch(
        catalyst_id="QR-CW-1",
        ticker="NVDA",
        catalyst_name="Earnings release",
        hours_to_event=36,
        thesis_impact="Validates AI infrastructure demand thesis.",
    )
    sentiment = SentimentSnapshot(
        extremes="Put/call ratio at 0.45 — elevated call buying across tech.",
        divergences="Semis leading broader tech by 2.3% on same macro data.",
        regime="Risk-on with AI sentiment driving flows.",
    )
    brief = QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(thread,),
        catalyst_watches=(catalyst,),
        sentiment_snapshot=sentiment,
    )
    bundle = QualInputBundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_text="regime: vol_expansion",
        digest_text="digest text",
        sentiment_text="sentiment text",
        prediction_market_text="prediction market text",
        calendar_text="calendar text",
        thesis_text="thesis text",
        bundle_text="full qualitative bundle text",
    )
    digest = NewsDigest(
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        total_shown=5,
        total_collected=20,
        entries=(),
        digest_text="(5 headlines shown)",
    )
    return QualitativeResearcherResult(
        brief=brief,
        input_bundle=bundle,
        news_digest=digest,
        tokens_used=_TOKENS,
        tool_calls_used=3,
        wall_clock_seconds=5.0,
        retry_count=1,
    )


# ---------------------------------------------------------------------------
# Helpers — adaptive researcher
# ---------------------------------------------------------------------------


def _adaptive_result() -> AdaptiveResearcherResult:
    thread = InvestigationThread(
        thread_id="AR-1",
        trigger="SA-TECH-ANOM-1",
        question="Is NVDA's unusual options activity a directional bet?",
        tickers=("NVDA",),
        sector=Sector.TECH_SEMIS,
        tools_used=("get_options_chain", "get_flow_summary"),
        findings=("Large put spread opened at 120 strike.", "Likely protective hedge."),
        assessment=Assessment.NOISE,
        confidence=Confidence.MODERATE,
        dismissal_reason="Flow pattern consistent with long-holder hedging, not directional.",
    )
    brief = AdaptiveBrief(
        invocation_id=_INVOCATION_ID,
        threads_investigated_count=1,
        anomalies_triaged_count=3,
        anomalies_deferred=("SA-FIN-ANOM-2", "SA-ENERGY-ANOM-1"),
        threads=(thread,),
    )
    bundle = AdaptiveInputBundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_text="regime: vol_expansion",
        distillation_text="(none)",
        sector_text="(none)",
        bundle_text="full adaptive bundle text",
    )
    anomaly_inputs = AdaptiveAnomalyInputs(
        distillation=(),
        sector=(),
        data_freshness=_AS_OF,
    )
    return AdaptiveResearcherResult(
        brief=brief,
        input_bundle=bundle,
        anomaly_inputs=anomaly_inputs,
        tokens_used=_TOKENS,
        tool_calls_used=5,
        wall_clock_seconds=8.2,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# Helpers — synthesizer
# ---------------------------------------------------------------------------


def _retrieval_store() -> RetrievalStore:
    return RetrievalStore(
        entries={
            "SA-TECH-1": "NVDA finding section text.",
            "SA-FIN-1": "JPM finding section text.",
            "QR-1": "Qualitative thread text.",
            "AR-1": "Adaptive thread text.",
        },
        freshness_by_source={
            BriefSource.SA_TECH: datetime(2026, 5, 3, 13, 0, tzinfo=UTC),
            BriefSource.SA_FIN: datetime(2026, 5, 3, 13, 5, tzinfo=UTC),
            BriefSource.QR: datetime(2026, 5, 3, 13, 10, tzinfo=UTC),
            BriefSource.AR: datetime(2026, 5, 3, 13, 15, tzinfo=UTC),
        },
    )


def _synthesizer_result() -> SynthesizerResult:
    return SynthesizerResult(
        synthesis_text="The analysis confirms a bullish bias in tech...",
        retrieval_store=_retrieval_store(),
        tokens_used=_TOKENS,
        tool_calls_used=7,
        wall_clock_seconds=12.5,
        stop_reason="end_turn",
    )


# ===========================================================================
# Test 1: DomainResearcherOutputModel round-trip
# ===========================================================================


class TestDomainResearcherOutputModel:
    """``DomainResearcherOutputModel`` round-trips through from_domain/to_domain and JSON."""

    def test_from_domain_to_domain_tech_semis(self) -> None:
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        dc = _domain_result(Sector.TECH_SEMIS)
        model = DomainResearcherOutputModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_from_domain_to_domain_financials(self) -> None:
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        dc = _domain_result(Sector.FINANCIALS)
        model = DomainResearcherOutputModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_from_domain_to_domain_energy(self) -> None:
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        dc = _domain_result(Sector.ENERGY)
        model = DomainResearcherOutputModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        dc = _domain_result(Sector.TECH_SEMIS)
        model = DomainResearcherOutputModel.from_domain(dc)
        json_str = model.model_dump_json()
        recovered_model = DomainResearcherOutputModel.model_validate_json(json_str)
        assert recovered_model == model

    def test_model_is_frozen(self) -> None:
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        dc = _domain_result(Sector.TECH_SEMIS)
        model = DomainResearcherOutputModel.from_domain(dc)
        with pytest.raises(ValidationError):
            model.sector = "energy"  # type: ignore[assignment, misc]


# ===========================================================================
# Test 2: QualitativeResearcherResultModel round-trip
# ===========================================================================


class TestQualitativeResearcherResultModel:
    """``QualitativeResearcherResultModel`` round-trips through from_domain/to_domain and JSON."""

    def test_from_domain_to_domain(self) -> None:
        from alphamind.analysis.qualitative_research.models import (
            QualitativeResearcherResultModel,
        )

        dc = _qualitative_result()
        model = QualitativeResearcherResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        from alphamind.analysis.qualitative_research.models import (
            QualitativeResearcherResultModel,
        )

        dc = _qualitative_result()
        model = QualitativeResearcherResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        recovered_model = QualitativeResearcherResultModel.model_validate_json(json_str)
        assert recovered_model == model

    def test_model_is_frozen(self) -> None:
        from alphamind.analysis.qualitative_research.models import (
            QualitativeResearcherResultModel,
        )

        dc = _qualitative_result()
        model = QualitativeResearcherResultModel.from_domain(dc)
        with pytest.raises(ValidationError):
            model.retry_count = 99  # type: ignore[misc]


# ===========================================================================
# Test 3: AdaptiveResearcherResultModel round-trip
# ===========================================================================


class TestAdaptiveResearcherResultModel:
    """``AdaptiveResearcherResultModel`` round-trips through from_domain/to_domain and JSON."""

    def test_from_domain_to_domain(self) -> None:
        from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

        dc = _adaptive_result()
        model = AdaptiveResearcherResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

        dc = _adaptive_result()
        model = AdaptiveResearcherResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        recovered_model = AdaptiveResearcherResultModel.model_validate_json(json_str)
        assert recovered_model == model

    def test_model_is_frozen(self) -> None:
        from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

        dc = _adaptive_result()
        model = AdaptiveResearcherResultModel.from_domain(dc)
        with pytest.raises(ValidationError):
            model.tool_calls_used = 99  # type: ignore[misc]


# ===========================================================================
# Test 4: SynthesizerResultModel round-trip (with RetrievalStoreModel)
# ===========================================================================


class TestSynthesizerResultModel:
    """``SynthesizerResultModel`` and ``RetrievalStoreModel`` round-trips."""

    def test_from_domain_to_domain(self) -> None:
        from alphamind.analysis.synthesizer.models import SynthesizerResultModel

        dc = _synthesizer_result()
        model = SynthesizerResultModel.from_domain(dc)
        recovered = model.to_domain()
        assert recovered == dc

    def test_json_round_trip(self) -> None:
        from alphamind.analysis.synthesizer.models import SynthesizerResultModel

        dc = _synthesizer_result()
        model = SynthesizerResultModel.from_domain(dc)
        json_str = model.model_dump_json()
        recovered_model = SynthesizerResultModel.model_validate_json(json_str)
        assert recovered_model == model

    def test_retrieval_store_round_trip_with_multiple_sources(self) -> None:
        """RetrievalStoreModel preserves all BriefSource enum keys in freshness_by_source."""
        from alphamind.analysis.synthesizer.models import RetrievalStoreModel

        store = _retrieval_store()
        model = RetrievalStoreModel.from_domain(store)
        recovered = model.to_domain()
        assert recovered.entries == store.entries
        assert recovered.freshness_by_source == store.freshness_by_source
        # Verify all 4 sources are preserved as BriefSource enum keys
        assert set(recovered.freshness_by_source.keys()) == {
            BriefSource.SA_TECH,
            BriefSource.SA_FIN,
            BriefSource.QR,
            BriefSource.AR,
        }

    def test_retrieval_store_json_round_trip(self) -> None:
        """RetrievalStoreModel JSON serializes BriefSource keys as strings."""
        from alphamind.analysis.synthesizer.models import RetrievalStoreModel

        store = _retrieval_store()
        model = RetrievalStoreModel.from_domain(store)
        json_str = model.model_dump_json()
        recovered_model = RetrievalStoreModel.model_validate_json(json_str)
        assert recovered_model == model

    def test_model_is_frozen(self) -> None:
        from alphamind.analysis.synthesizer.models import SynthesizerResultModel

        dc = _synthesizer_result()
        model = SynthesizerResultModel.from_domain(dc)
        with pytest.raises(ValidationError):
            model.stop_reason = "mutated"  # type: ignore[misc]
