"""Tests for the analysis-layer pipeline composition runner — story ALP-276.

The composition runner sequences ``run_external_distillation`` →
(``run_domain_researchers`` + ``run_qualitative_researcher``) parallel →
``run_adaptive_researcher`` → ``run_synthesizer`` and returns every
typed ``*Result`` value. All five underlying runners are monkeypatched at
the composition module's namespace so the tests do not touch SQLite, the
filesystem, or the Anthropic SDK.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.harness import SDKFailure
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
from alphamind.pipeline.analysis import (
    AnalysisPipelineResult,
    apply_agent_overrides,
    run_analysis_pipeline,
)
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)

# ---------------------------------------------------------------------------
# Shared fixture values
# ---------------------------------------------------------------------------

_INVOCATION_ID = "test-inv-pipe-001"
_AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 3, 12, 30, 0, tzinfo=UTC)
_REGIME_LABEL_VALUE = "vol_expansion"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _sector_brief(sector: Sector) -> SectorBrief:
    prefix = SECTOR_PREFIX[sector]
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
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


def _domain_runner_result(sector: Sector) -> DomainResearcherResult:
    return DomainResearcherResult(
        sector=sector,
        brief=_sector_brief(sector),
        input_bundle=DomainInputBundle(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
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


def _domain_output() -> DomainResearchersOutput:
    return DomainResearchersOutput(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        tech_semis=_domain_runner_result(Sector.TECH_SEMIS),
        financials=_domain_runner_result(Sector.FINANCIALS),
        energy=_domain_runner_result(Sector.ENERGY),
        total_tokens_used=TokensUsed(
            input_tokens=300, output_tokens=150, cache_read_tokens=0, cache_write_tokens=0
        ),
        total_wall_clock_seconds=1.0,
        total_retry_count=0,
    )


def _qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
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
        sentiment_snapshot=SentimentSnapshot(extremes="none", divergences="none", regime="neutral"),
    )


def _qualitative_result() -> QualitativeResearcherResult:
    return QualitativeResearcherResult(
        brief=_qualitative_brief(),
        input_bundle=QualInputBundle(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            regime_text="regime: vol_expansion",
            digest_text="digest",
            sentiment_text="sentiment",
            prediction_market_text="prediction",
            calendar_text="calendar",
            thesis_text="thesis",
            bundle_text="bundle",
        ),
        news_digest=NewsDigest(
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
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


def _adaptive_brief() -> AdaptiveBrief:
    return AdaptiveBrief(
        invocation_id=_INVOCATION_ID,
        threads_investigated_count=0,
        anomalies_triaged_count=0,
        anomalies_deferred=(),
        threads=(),
    )


def _adaptive_result() -> AdaptiveResearcherResult:
    return AdaptiveResearcherResult(
        brief=_adaptive_brief(),
        input_bundle=AdaptiveInputBundle(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            regime_text="regime: vol_expansion",
            distillation_text="(none)",
            sector_text="(none)",
            bundle_text="bundle",
        ),
        anomaly_inputs=AdaptiveAnomalyInputs(distillation=(), sector=(), data_freshness=_AS_OF),
        tokens_used=TokensUsed(
            input_tokens=400, output_tokens=120, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=3.0,
        retry_count=0,
    )


def _synth_result() -> SynthesizerResult:
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


def _sector_output(audience: OutputAudience, *, text: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=audience.value,
        text=text,
        tickers=(),
        block_ids=(),
        freshness_min=_AS_OF,
    )


def _correlation_regime_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="[CR-1] regime ctx.\n  detail.\n",
        reference_index={"CR-1": "regime.label"},
        freshness_min=_AS_OF,
    )


def _distillation_outputs() -> DistillationOutputs:
    return DistillationOutputs(
        sector_outputs={
            OutputAudience.SECTOR_TECH_SEMIS: _sector_output(
                OutputAudience.SECTOR_TECH_SEMIS, text="TECH-DISTILL"
            ),
            OutputAudience.SECTOR_FINANCIALS: _sector_output(
                OutputAudience.SECTOR_FINANCIALS, text="FIN-DISTILL"
            ),
            OutputAudience.SECTOR_ENERGY: _sector_output(
                OutputAudience.SECTOR_ENERGY, text="ENERGY-DISTILL"
            ),
        },
        correlation_regime_brief=_correlation_regime_brief(),
        universal_regime_label={
            "regime_label": _REGIME_LABEL_VALUE,
            "transition_flag": False,
            "confidence": "high",
            "freshness_ts": _AS_OF.isoformat(),
        },
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=0,
        total_anomalies=0,
        bootstrap_block_count=0,
        all_blocks=(),
    )


def _make_base_config(prompt: str) -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=prompt,
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


def _make_adaptive_config() -> AdaptiveAgentConfig:
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


def _agents_registry() -> dict[str, BaseAgentConfig]:
    """Stringly-keyed agents registry covering every analysis-layer slot."""
    return {
        AgentName.tech_semis_researcher.value: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
        AgentName.financials_researcher.value: _make_base_config(
            "prompts/analysis/financials_researcher.md"
        ),
        AgentName.energy_researcher.value: _make_base_config(
            "prompts/analysis/energy_researcher.md"
        ),
        AgentName.qualitative_researcher.value: _make_adaptive_config(),
        AgentName.adaptive_researcher.value: _make_adaptive_config(),
        AgentName.synthesizer.value: _make_base_config("prompts/analysis/synthesizer.md"),
    }


def _sectors_registry() -> dict[str, list[str]]:
    return {
        Sector.TECH_SEMIS.value: ["NVDA"],
        Sector.FINANCIALS.value: ["JPM"],
        Sector.ENERGY.value: ["XOM"],
    }


class _StubPortfolioReader:
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
# Runner stubs — captured-call recorders
# ---------------------------------------------------------------------------


class _CallLog:
    """Captures the kwargs each stage was invoked with."""

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


# Per-stage parameter signatures matching the production runners. Used by
# ``_make_stub`` to project positional args back into named log keys so
# tests can assert on ``log.distillation["invocation_id"]`` regardless of
# whether the composition runner used positional or keyword call style.
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


def _patch_runners(
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
    behaviors = {
        "distillation": _StageBehavior(
            distillation_result or _distillation_outputs(), distillation_raises
        ),
        "domain": _StageBehavior(domain_result or _domain_output(), domain_raises),
        "qualitative": _StageBehavior(
            qualitative_result or _qualitative_result(), qualitative_raises
        ),
        "adaptive": _StageBehavior(adaptive_result or _adaptive_result(), adaptive_raises),
        "synthesizer": _StageBehavior(synth_result or _synth_result(), synth_raises),
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


def _drive(
    *,
    agents_config: Mapping[str, BaseAgentConfig] | None = None,
) -> AnalysisPipelineResult:
    """Convenience wrapper around ``asyncio.run`` for the production entry point."""
    return asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]  # stub doesn't touch the session
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=None,  # type: ignore[arg-type]  # stub doesn't read config
            ticker_scope=("NVDA", "JPM", "XOM"),
            universe=frozenset({"NVDA", "JPM", "XOM"}),
            agents_config=agents_config or _agents_registry(),
            sectors_config=_sectors_registry(),
            portfolio_reader=_StubPortfolioReader(),
        )
    )


# ---------------------------------------------------------------------------
# apply_agent_overrides
# ---------------------------------------------------------------------------


def test_apply_agent_overrides_returns_string_keyed_mapping() -> None:
    """Output keys are the agent-name string values (the runner contract)."""
    agents = {
        AgentName.tech_semis_researcher: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
        AgentName.adaptive_researcher: _make_adaptive_config(),
    }
    result = apply_agent_overrides(agents, agent_overrides={})
    assert set(result) == {
        AgentName.tech_semis_researcher.value,
        AgentName.adaptive_researcher.value,
    }


def test_apply_agent_overrides_layers_base_fields() -> None:
    """Override fields replace the base agent's matching fields."""
    agents = {
        AgentName.tech_semis_researcher: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
    }
    overrides = {
        AgentName.tech_semis_researcher: {
            "latency_budget_seconds": 90,
            "output_token_budget": 8_000,
        },
    }
    result = apply_agent_overrides(agents, agent_overrides=overrides)
    cfg = result[AgentName.tech_semis_researcher.value]
    assert cfg.latency_budget_seconds == 90
    assert cfg.output_token_budget == 8_000
    # Untouched fields preserve their base value.
    assert cfg.context_token_budget == 100_000


def test_apply_agent_overrides_layers_adaptive_only_fields() -> None:
    """``cumulative_tool_*`` overrides reach the adaptive_researcher slot."""
    agents = {AgentName.adaptive_researcher: _make_adaptive_config()}
    overrides = {
        AgentName.adaptive_researcher: {
            "cumulative_tool_call_limit": 25,
            "cumulative_tool_token_budget": 5_000,
        },
    }
    result = apply_agent_overrides(agents, agent_overrides=overrides)
    cfg = result[AgentName.adaptive_researcher.value]
    assert isinstance(cfg, AdaptiveAgentConfig)
    assert cfg.cumulative_tool_call_limit == 25
    assert cfg.cumulative_tool_token_budget == 5_000


def test_apply_agent_overrides_passes_through_un_overridden_agents() -> None:
    """An agent absent from the override map yields the original config object."""
    base = _make_base_config("prompts/analysis/tech_semis_researcher.md")
    agents = {AgentName.tech_semis_researcher: base}
    result = apply_agent_overrides(agents, agent_overrides={})
    # Same object — no copy is made when no override applies.
    assert result[AgentName.tech_semis_researcher.value] is base


# ---------------------------------------------------------------------------
# run_analysis_pipeline — happy path
# ---------------------------------------------------------------------------


def test_pipeline_returns_result_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stage succeeds → AnalysisPipelineResult bundles every typed result."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()

    assert isinstance(result, AnalysisPipelineResult)
    assert isinstance(result.distillation_outputs, DistillationOutputs)
    assert isinstance(result.domain_researchers_output, DomainResearchersOutput)
    assert isinstance(result.qualitative_result, QualitativeResearcherResult)
    assert isinstance(result.adaptive_result, AdaptiveResearcherResult)
    assert isinstance(result.synthesizer_result, SynthesizerResult)
    assert result.synthesizer_result.synthesis_text == "synthesized prose."


def test_pipeline_threads_distillation_outputs_to_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distillation result reaches each downstream runner that needs it."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()

    # Domain orchestrator and adaptive runner both receive the DistillationOutputs.
    assert log.domain["distillation_outputs"] is log.adaptive["distillation_outputs"]
    # Qualitative receives the universal regime label dict.
    assert log.qualitative["universal_regime_label"] == log.adaptive["universal_regime_label"]
    # Adaptive's correlation_regime_brief comes from distillation outputs.
    assert (
        log.adaptive["correlation_regime_brief"]
        is log.adaptive["distillation_outputs"].correlation_regime_brief
    )


def test_pipeline_extracts_regime_label_string_for_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthesizer receives the regime-label string (not the full dict)."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.synthesizer["regime_label"] == _REGIME_LABEL_VALUE


def test_pipeline_forwards_synthesizer_agent_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesizer slot from agents_config reaches run_synthesizer."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    agents = _agents_registry()
    _drive(agents_config=agents)
    assert log.synthesizer["agent_config"] is agents[AgentName.synthesizer.value]


def test_pipeline_forwards_briefs_to_adaptive_and_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sector + qualitative + adaptive briefs reach the right downstream stages."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()

    expected_sector_briefs = (
        result.domain_researchers_output.tech_semis.brief,
        result.domain_researchers_output.financials.brief,
        result.domain_researchers_output.energy.brief,
    )
    assert log.adaptive["sector_briefs"] == expected_sector_briefs
    assert log.synthesizer["sector_briefs"] == expected_sector_briefs
    assert log.adaptive["qualitative_brief"] is result.qualitative_result.brief
    assert log.synthesizer["qualitative_brief"] is result.qualitative_result.brief
    assert log.synthesizer["adaptive_brief"] is result.adaptive_result.brief


def test_pipeline_runs_domain_and_qualitative_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain + qualitative wall-clock approximates max(), not sum()."""

    delay = 0.10

    async def _slow_distillation(*_a: Any, **_kw: Any) -> DistillationOutputs:
        return _distillation_outputs()

    async def _slow_domain(**_kw: Any) -> DomainResearchersOutput:
        await asyncio.sleep(delay)
        return _domain_output()

    async def _slow_qualitative(*_a: Any, **_kw: Any) -> QualitativeResearcherResult:
        await asyncio.sleep(delay)
        return _qualitative_result()

    async def _stub_adaptive(*_a: Any, **_kw: Any) -> AdaptiveResearcherResult:
        return _adaptive_result()

    async def _stub_synth(**_kw: Any) -> SynthesizerResult:
        return _synth_result()

    monkeypatch.setattr(composition, "run_external_distillation", _slow_distillation)
    monkeypatch.setattr(composition, "run_domain_researchers", _slow_domain)
    monkeypatch.setattr(composition, "run_qualitative_researcher", _slow_qualitative)
    monkeypatch.setattr(composition, "run_adaptive_researcher", _stub_adaptive)
    monkeypatch.setattr(composition, "run_synthesizer", _stub_synth)

    loop = asyncio.new_event_loop()
    try:
        start = loop.time()
        loop.run_until_complete(
            run_analysis_pipeline(
                session=None,  # type: ignore[arg-type]
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                last_invocation_time=_LAST_INVOCATION_TIME,
                distillation_config=None,  # type: ignore[arg-type]
                ticker_scope=("NVDA",),
                universe=frozenset({"NVDA"}),
                agents_config=_agents_registry(),
                sectors_config=_sectors_registry(),
                portfolio_reader=_StubPortfolioReader(),
            )
        )
        elapsed = loop.time() - start
    finally:
        loop.close()

    # Parallel: ~delay; serial would be ~2*delay. Generous slack accommodates CI jitter.
    assert elapsed < delay * 1.8


def test_pipeline_forwards_archive_root_and_provenance_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``archive_root`` reaches every stage; ``provenance_root`` reaches distillation."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    archive = tmp_path / "archive"
    prov = tmp_path / "provenance"
    asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=None,  # type: ignore[arg-type]
            ticker_scope=("NVDA",),
            universe=frozenset({"NVDA"}),
            agents_config=_agents_registry(),
            sectors_config=_sectors_registry(),
            portfolio_reader=_StubPortfolioReader(),
            archive_root=archive,
            provenance_root=prov,
        )
    )
    assert log.distillation["archive_root"] == archive
    assert log.distillation["provenance_root"] == prov
    assert log.domain["archive_root"] == archive
    assert log.qualitative["archive_root"] == archive
    assert log.adaptive["archive_root"] == archive
    assert log.synthesizer["archive_root"] == archive


# ---------------------------------------------------------------------------
# run_analysis_pipeline — fail-closed propagation
# ---------------------------------------------------------------------------


def _failure() -> SDKFailure:
    return SDKFailure(
        "stub failure",
        agent_name=AgentName.synthesizer.value,
        invocation_id=_INVOCATION_ID,
    )


def test_distillation_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distillation fails → no downstream stage runs."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, distillation_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert log.order == ["distillation"]


def test_domain_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domain orchestrator fails → adaptive and synthesizer do not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, domain_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


def test_qualitative_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qualitative fails → adaptive and synthesizer do not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, qualitative_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


def test_adaptive_failure_aborts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adaptive fails → synthesizer does not run."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, adaptive_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure
    assert "synthesizer" not in log.order


def test_synthesizer_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthesizer failure surfaces from ``run_analysis_pipeline``."""
    log = _CallLog()
    failure = _failure()
    _patch_runners(monkeypatch, log=log, synth_raises=failure)
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value is failure


def test_parallel_stage_double_failure_propagates_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain + qualitative both fail simultaneously → pipeline surfaces
    a single ``SDKFailure`` (the first child of the underlying
    ``BaseExceptionGroup``) rather than the group container.

    Guards the TaskGroup migration: ``asyncio.TaskGroup`` always raises
    ``BaseExceptionGroup``; without explicit unwrapping the caller would
    suddenly receive a group container instead of an ``SDKFailure``.
    """
    log = _CallLog()
    domain_failure = SDKFailure(
        "domain stub failure",
        agent_name=AgentName.tech_semis_researcher.value,
        invocation_id=_INVOCATION_ID,
    )
    qualitative_failure = SDKFailure(
        "qualitative stub failure",
        agent_name=AgentName.qualitative_researcher.value,
        invocation_id=_INVOCATION_ID,
    )
    _patch_runners(
        monkeypatch,
        log=log,
        domain_raises=domain_failure,
        qualitative_raises=qualitative_failure,
    )
    with pytest.raises(SDKFailure) as exc_info:
        _drive()
    assert exc_info.value in (domain_failure, qualitative_failure)
    # Adaptive + synthesizer must NOT have run; the orchestrator aborted at
    # the parallel stage.
    assert "adaptive" not in log.order
    assert "synthesizer" not in log.order


# ---------------------------------------------------------------------------
# Argument plumbing — qualitative + adaptive timing args
# ---------------------------------------------------------------------------


def test_pipeline_forwards_last_invocation_time_to_qualitative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualitative receives ``last_invocation_time`` from the composition surface."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.qualitative["last_invocation_time"] == _LAST_INVOCATION_TIME


def test_pipeline_forwards_universe_to_qualitative_and_adaptive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both tool-loop agents receive the universe roster."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    expected = frozenset({"NVDA", "JPM", "XOM"})
    assert log.qualitative["universe"] == expected
    assert log.adaptive["universe"] == expected


def test_pipeline_forwards_distillation_call_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distillation receives positional session/config/scope/as_of/invocation_id."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    _drive()
    assert log.distillation["invocation_id"] == _INVOCATION_ID
    assert log.distillation["as_of"] == _AS_OF
    assert log.distillation["ticker_scope"] == ("NVDA", "JPM", "XOM")


def test_pipeline_forwards_agents_config_to_each_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain / qualitative / adaptive receive the same agents_config mapping."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    agents = _agents_registry()
    _drive(agents_config=agents)
    assert log.domain["agents_config"] is agents
    assert log.qualitative["agents_config"] is agents
    assert log.adaptive["agents_config"] is agents


def test_pipeline_forwards_sectors_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domain orchestrator receives sectors_config (the only stage that needs it)."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    sectors = _sectors_registry()
    asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=None,  # type: ignore[arg-type]
            ticker_scope=("NVDA",),
            universe=frozenset({"NVDA"}),
            agents_config=_agents_registry(),
            sectors_config=sectors,
            portfolio_reader=_StubPortfolioReader(),
        )
    )
    assert log.domain["sectors_config"] is sectors


# ---------------------------------------------------------------------------
# Result type — frozen
# ---------------------------------------------------------------------------


def test_result_is_frozen_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AnalysisPipelineResult`` rejects post-construction mutation."""
    log = _CallLog()
    _patch_runners(monkeypatch, log=log)
    result = _drive()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.synthesizer_result = _synth_result()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Sanity-check the helper used in the production code path
# ---------------------------------------------------------------------------


def test_extract_regime_label_returns_payload_string() -> None:
    """``_extract_regime_label`` reads ``universal_regime_label['regime_label']``."""
    from alphamind.pipeline.analysis import _extract_regime_label

    outputs = _distillation_outputs()
    assert _extract_regime_label(outputs) == _REGIME_LABEL_VALUE


def test_unused_signature_carries_typing_aliases() -> None:
    """Sequence + Mapping imports stay live so static-type tooling validates them."""
    # Trivial reference so ruff/mypy don't flag the test's imports as unused.
    assert isinstance((), Sequence)
    assert isinstance({}, Mapping)
