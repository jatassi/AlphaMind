"""Shared test fixtures for pipeline-level tests.

Centralizes result-builder helpers and stub infrastructure that were
previously duplicated across ``test_analysis.py``,
``test_analysis_phase_emission.py``, and ``test_analysis_resume.py``.

Story ALP-789.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle as AdaptiveInputBundle,
)
from alphamind.analysis.adaptive_research.loaders import AdaptiveAnomalyInputs
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.domain_researchers.input_bundle import (
    InputBundle as DomainInputBundle,
)
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
    Finding,
    SectorBrief,
    SignalType,
    Strength,
)
from alphamind.analysis.domain_researchers.orchestrator import DomainResearchersOutput
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle as QualInputBundle,
)
from alphamind.analysis.qualitative_research.models import (
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import SynthesizerResult
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AllowedModel,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.sector_assembly import SectorOutput
from alphamind.pipeline import analysis as composition
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)

# ---------------------------------------------------------------------------
# Canonical fixture constants
# ---------------------------------------------------------------------------

INVOCATION_ID = "test-inv-pipe-001"
AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
LAST_INVOCATION_TIME = datetime(2026, 5, 3, 12, 30, 0, tzinfo=UTC)
REGIME_LABEL_VALUE = "vol_expansion"


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def build_sector_brief(sector: Sector, *, invocation_id: str = INVOCATION_ID) -> SectorBrief:
    prefix = SECTOR_PREFIX[sector]
    return SectorBrief(
        invocation_id=invocation_id,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline=f"Headline for {sector.value}.",
                tickers=("AAA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.STRONG,
                detail="Detail.",
            ),
        ),
        anomalies=(),
        thesis_candidates=(),
    )


def build_domain_runner_result(
    sector: Sector,
    *,
    invocation_id: str = INVOCATION_ID,
    as_of: datetime = AS_OF,
) -> DomainResearcherResult:
    return DomainResearcherResult(
        sector=sector,
        brief=build_sector_brief(sector, invocation_id=invocation_id),
        input_bundle=DomainInputBundle(
            sector=sector,
            invocation_id=invocation_id,
            as_of=as_of,
            distillation_text="distill",
            qualitative_text="qual",
            bundle_text="bundle",
        ),
        tokens_used=TokensUsed(
            input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0
        ),
        wall_clock_seconds=1.0,
        retry_count=0,
    )


def build_domain_output(
    *,
    invocation_id: str = INVOCATION_ID,
    as_of: datetime = AS_OF,
) -> DomainResearchersOutput:
    return DomainResearchersOutput(
        invocation_id=invocation_id,
        as_of=as_of,
        tech_semis=build_domain_runner_result(
            Sector.TECH_SEMIS, invocation_id=invocation_id, as_of=as_of
        ),
        financials=build_domain_runner_result(
            Sector.FINANCIALS, invocation_id=invocation_id, as_of=as_of
        ),
        energy=build_domain_runner_result(Sector.ENERGY, invocation_id=invocation_id, as_of=as_of),
        total_tokens_used=TokensUsed(
            input_tokens=300, output_tokens=150, cache_read_tokens=0, cache_write_tokens=0
        ),
        total_wall_clock_seconds=1.0,
        total_retry_count=0,
    )


def build_qualitative_result(
    *,
    invocation_id: str = INVOCATION_ID,
    as_of: datetime = AS_OF,
    last_invocation_time: datetime = LAST_INVOCATION_TIME,
) -> QualitativeResearcherResult:
    return QualitativeResearcherResult(
        brief=QualitativeBrief(
            invocation_id=invocation_id,
            signal_quality=SignalQuality.HIGH,
            signal_quality_reason=None,
            threads=(
                NarrativeThread(
                    thread_id="QR-1",
                    summary="Quiet day.",
                    relevance="cross-sector",
                    direction=ThreadDirection.MIXED,
                    subject="market",
                    time_horizon=TimeHorizon.NEAR_TERM,
                    evidence=(
                        EvidenceLine(
                            source_type="news",
                            observation="No major catalysts.",
                            citation="ND-1",
                        ),
                        EvidenceLine(
                            source_type="prediction_markets",
                            observation="Odds unchanged.",
                            citation="kalshi:none",
                        ),
                    ),
                    implication="No-op.",
                ),
            ),
            catalyst_watches=(),
            sentiment_snapshot=SentimentSnapshot(
                extremes="none", divergences="none", regime="neutral"
            ),
        ),
        input_bundle=QualInputBundle(
            invocation_id=invocation_id,
            as_of=as_of,
            regime_text="regime: vol_expansion",
            digest_text="digest",
            sentiment_text="sentiment",
            prediction_market_text="prediction",
            calendar_text="calendar",
            thesis_text="thesis",
            bundle_text="bundle",
        ),
        news_digest=NewsDigest(
            as_of=as_of,
            last_invocation_time=last_invocation_time,
            total_collected=0,
            total_shown=0,
            entries=(),
            digest_text="(no headlines)",
        ),
        tokens_used=TokensUsed(
            input_tokens=200, output_tokens=80, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=2.0,
        retry_count=0,
    )


def build_adaptive_result(
    *,
    invocation_id: str = INVOCATION_ID,
    as_of: datetime = AS_OF,
) -> AdaptiveResearcherResult:
    return AdaptiveResearcherResult(
        brief=AdaptiveBrief(
            invocation_id=invocation_id,
            threads_investigated_count=0,
            anomalies_triaged_count=0,
            anomalies_deferred=(),
            threads=(),
        ),
        input_bundle=AdaptiveInputBundle(
            invocation_id=invocation_id,
            as_of=as_of,
            regime_text="regime: vol_expansion",
            distillation_text="(none)",
            sector_text="(none)",
            bundle_text="bundle",
        ),
        anomaly_inputs=AdaptiveAnomalyInputs(distillation=(), sector=(), data_freshness=as_of),
        tokens_used=TokensUsed(
            input_tokens=400, output_tokens=120, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=3.0,
        retry_count=0,
    )


def build_synth_result() -> SynthesizerResult:
    return SynthesizerResult(
        synthesis_text="synthesized prose.",
        retrieval_store=RetrievalStore(entries={}, freshness_by_source={}),
        tokens_used=TokensUsed(
            input_tokens=500, output_tokens=300, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=2,
        wall_clock_seconds=4.0,
        stop_reason="end_turn",
    )


def build_sector_output(
    audience: OutputAudience, *, text: str, as_of: datetime = AS_OF
) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=audience.value,
        text=text,
        tickers=(),
        block_ids=(),
        freshness_min=as_of,
    )


def build_correlation_regime_brief(*, as_of: datetime = AS_OF) -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="[CR-1] regime ctx.\n  detail.\n",
        reference_index={"CR-1": "regime.label"},
        freshness_min=as_of,
    )


def build_distillation_outputs(
    *,
    invocation_id: str = INVOCATION_ID,
    as_of: datetime = AS_OF,
    regime_label: str = REGIME_LABEL_VALUE,
) -> DistillationOutputs:
    return DistillationOutputs(
        sector_outputs={
            OutputAudience.SECTOR_TECH_SEMIS: build_sector_output(
                OutputAudience.SECTOR_TECH_SEMIS, text="TECH-DISTILL", as_of=as_of
            ),
            OutputAudience.SECTOR_FINANCIALS: build_sector_output(
                OutputAudience.SECTOR_FINANCIALS, text="FIN-DISTILL", as_of=as_of
            ),
            OutputAudience.SECTOR_ENERGY: build_sector_output(
                OutputAudience.SECTOR_ENERGY, text="ENERGY-DISTILL", as_of=as_of
            ),
        },
        correlation_regime_brief=build_correlation_regime_brief(as_of=as_of),
        universal_regime_label={
            "regime_label": regime_label,
            "transition_flag": False,
            "confidence": "high",
            "freshness_ts": as_of.isoformat(),
        },
        invocation_id=invocation_id,
        as_of=as_of,
        total_blocks=0,
        total_anomalies=0,
        non_calibrated_block_count=0,
        all_blocks=(),
    )


def build_base_config(prompt: str) -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=prompt,
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


def build_adaptive_config() -> AdaptiveAgentConfig:
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=120,
        context_token_budget=120_000,
        output_token_budget=4_000,
        tools=[],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=2_000,
        tool_caps={},
    )


def build_agents_registry() -> dict[str, BaseAgentConfig]:
    """String-keyed agents registry covering every analysis-layer slot."""
    return {
        AgentName.tech_semis_researcher.value: build_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
        AgentName.financials_researcher.value: build_base_config(
            "prompts/analysis/financials_researcher.md"
        ),
        AgentName.energy_researcher.value: build_base_config(
            "prompts/analysis/energy_researcher.md"
        ),
        AgentName.qualitative_researcher.value: build_adaptive_config(),
        AgentName.adaptive_researcher.value: build_adaptive_config(),
        AgentName.synthesizer.value: build_base_config("prompts/analysis/synthesizer.md"),
    }


def build_sectors_registry() -> dict[str, list[str]]:
    return {
        Sector.TECH_SEMIS.value: ["NVDA"],
        Sector.FINANCIALS.value: ["JPM"],
        Sector.ENERGY.value: ["XOM"],
    }


class StubPortfolioReader:
    """Minimal in-memory portfolio reader for pipeline tests."""

    def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        return ()

    def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        return ()

    def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        return SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        )


# ---------------------------------------------------------------------------
# _CallLog + stub harness (used by test_analysis.py)
# ---------------------------------------------------------------------------


class _CallLog:
    """Captures the kwargs each analysis-pipeline stage was invoked with."""

    def __init__(self) -> None:
        self.distillation: dict[str, Any] = {}
        self.domain: dict[str, Any] = {}
        self.qualitative: dict[str, Any] = {}
        self.adaptive: dict[str, Any] = {}
        self.synthesizer: dict[str, Any] = {}
        self.order: list[str] = []


@dataclass
class _StageBehavior:
    """Per-stage success value + optional failure to raise instead."""

    value: Any
    raises: Exception | None = None


# Per-stage parameter signatures matching the production runners.
_STAGE_SIGNATURES: dict[str, tuple[str, ...]] = {
    "distillation": ("session", "config", "ticker_scope", "as_of", "invocation_id"),
    "domain": (),
    "qualitative": ("invocation_id", "as_of", "last_invocation_time"),
    "adaptive": ("invocation_id", "as_of"),
    "synthesizer": (),
}


def _make_stub(stage: str, behavior: _StageBehavior, log: _CallLog) -> Any:
    """Build an async stub that records call kwargs and either raises or returns."""
    signature = _STAGE_SIGNATURES[stage]

    async def _stub(*args: Any, **kw: Any) -> Any:
        log_target: dict[str, Any] = getattr(log, stage)
        for index, name in enumerate(signature):
            if index < len(args):
                log_target[name] = args[index]
        log_target.update(kw)
        log.order.append(stage)
        if behavior.raises is not None:
            raise behavior.raises
        return behavior.value

    return _stub


def patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: _CallLog,
    distillation_result: DistillationOutputs | None = None,
    domain_result: DomainResearchersOutput | None = None,
    qualitative_result: QualitativeResearcherResult | None = None,
    adaptive_result: AdaptiveResearcherResult | None = None,
    synth_result: SynthesizerResult | None = None,
    distillation_raises: Exception | None = None,
    domain_raises: Exception | None = None,
    qualitative_raises: Exception | None = None,
    adaptive_raises: Exception | None = None,
    synth_raises: Exception | None = None,
) -> None:
    """Monkeypatch all five analysis-pipeline runners at the composition module."""
    behaviors = {
        "distillation": _StageBehavior(
            distillation_result or build_distillation_outputs(), distillation_raises
        ),
        "domain": _StageBehavior(domain_result or build_domain_output(), domain_raises),
        "qualitative": _StageBehavior(
            qualitative_result or build_qualitative_result(), qualitative_raises
        ),
        "adaptive": _StageBehavior(adaptive_result or build_adaptive_result(), adaptive_raises),
        "synthesizer": _StageBehavior(synth_result or build_synth_result(), synth_raises),
    }
    target_by_stage = {
        "distillation": "run_external_distillation",
        "domain": "run_domain_researchers",
        "qualitative": "run_qualitative_researcher",
        "adaptive": "run_adaptive_researcher",
        "synthesizer": "run_synthesizer",
    }
    for stage, target in target_by_stage.items():
        monkeypatch.setattr(composition, target, _make_stub(stage, behaviors[stage], log))
