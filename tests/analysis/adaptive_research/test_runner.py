"""Tests for the adaptive-researcher runner — story 06 (ALP-264).

All composed dependencies (anomaly assembler, bundle assembler, harness) are
injected via the private ``_run_adaptive_researcher`` function so tests don't
touch the Anthropic API, the database, or the filesystem.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.harness import (
    HarnessSuccess,
    MalformedOutputFailure,
)
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle,
    assemble_input_bundle,
)
from alphamind.analysis.adaptive_research.loaders import AdaptiveAnomalyInputs
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    Assessment,
    Confidence,
    InvestigationThread,
)
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    AnomalyType,
    SectorBrief,
)
from alphamind.analysis.qualitative_research.models import (
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AllowedModel,
    BaseAgentConfig,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import OutputAudience, OutputBlock

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "test-inv-ar-001"
_UNIVERSE: frozenset[str] = frozenset({"NVDA", "AMD", "JPM", "XOM"})

_REGIME_LABEL: dict[str, Any] = {
    "regime": "vol_expansion",
    "transition_flag": "early-weak",
    "confidence": 0.82,
    "freshness_ts": "2026-05-02T11:55:00Z",
}


def _make_adaptive_agent_config() -> AdaptiveAgentConfig:
    """Build a tool-loop agent config (adaptive_researcher slot requires it)."""
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=60,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
        cumulative_tool_call_limit=20,
        cumulative_tool_token_budget=20_000,
        tool_caps={},
    )


def _make_agents_registry() -> dict[str, BaseAgentConfig]:
    """A minimal agents registry containing the adaptive_researcher slot."""
    return {AgentName.adaptive_researcher.value: _make_adaptive_agent_config()}


def _make_distillation_outputs() -> DistillationOutputs:
    """Construct a minimal DistillationOutputs the runner forwards verbatim.

    The bundle assembler reads the freshness timestamps off the produced
    DistillationAnomalyRecords, but with zero anomaly flags the data freshness
    falls through to ``as_of``.
    """
    return DistillationOutputs(
        sector_outputs={},
        correlation_regime_brief=CorrelationRegimeBrief(
            text="", reference_index={}, freshness_min=_AS_OF
        ),
        universal_regime_label=_REGIME_LABEL,
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=1,
        total_anomalies=0,
        non_calibrated_block_count=0,
        all_blocks=(
            OutputBlock(
                block_id="q1.empty",
                audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
                freshness_ts=_AS_OF,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"x": 1},
                anomaly_flags=(),
                regime_context=None,
            ),
        ),
    )


def _make_sector_brief(sector: Sector, prefix: str) -> SectorBrief:
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(),
        anomalies=(
            Anomaly(
                anomaly_id=f"{prefix}-ANOM-1",
                description="Anomaly description",
                anomaly_type=AnomalyType.VOLUME,
                tickers=("NVDA",),
                severity="note_for_context",
                suggested_question="What caused this?",
            ),
        ),
        thesis_candidates=(),
    )


def _make_sector_briefs() -> tuple[SectorBrief, ...]:
    return (
        _make_sector_brief(Sector.TECH_SEMIS, "SA-TECH"),
        _make_sector_brief(Sector.FINANCIALS, "SA-FIN"),
        _make_sector_brief(Sector.ENERGY, "SA-ENERGY"),
    )


def _make_qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="A thread",
                relevance="topical",
                direction=ThreadDirection.BULLISH,
                subject="market",
                time_horizon=TimeHorizon.NEAR_TERM,
                evidence=(
                    EvidenceLine(
                        source_type="news_digest",
                        observation="Obs",
                        citation="ND-T1",
                    ),
                    EvidenceLine(
                        source_type="prediction_market",
                        observation="Obs2",
                        citation="PM-001",
                    ),
                ),
                implication="Implication.",
            ),
        ),
        catalyst_watches=(),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="risk-on",
        ),
    )


def _make_correlation_regime_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="CORRELATION & REGIME BRIEF\n[CR-1] regime\n",
        reference_index={"CR-1": "regime.label"},
        freshness_min=_AS_OF,
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
                trigger="SA-TECH-ANOM-1",
                question="What drove the spike?",
                tickers=("NVDA",),
                sector=Sector.TECH_SEMIS,
                tools_used=("news_search",),
                findings=("found something",),
                assessment=Assessment.SIGNAL,
                confidence=Confidence.MODERATE,
                implication="Pre-earnings repositioning.",
                strengthens=("SA-TECH-1",),
                weakens=(),
            ),
        ),
    )


def _make_harness_success() -> HarnessSuccess:
    return HarnessSuccess(
        brief=_make_adaptive_brief(),
        raw_response="raw response text",
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=500,
            output_tokens=300,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=2,
        wall_clock_seconds=1.5,
    )


# ---------------------------------------------------------------------------
# Stubbing helpers
# ---------------------------------------------------------------------------


def _make_stub_anomaly_assembler(
    inputs: AdaptiveAnomalyInputs,
) -> Callable[..., AdaptiveAnomalyInputs]:
    def _stub(**_: object) -> AdaptiveAnomalyInputs:
        return inputs

    return _stub


def _make_stub_harness(result: HarnessSuccess) -> Callable[..., Any]:
    async def _stub(**_: object) -> HarnessSuccess:
        return result

    return _stub


def _make_default_anomaly_inputs() -> AdaptiveAnomalyInputs:
    return AdaptiveAnomalyInputs(distillation=(), sector=(), data_freshness=_AS_OF)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_public_names_importable() -> None:
    """The acceptance-criterion public surface resolves cleanly."""
    from alphamind.analysis.adaptive_research.runner import (
        AdaptiveResearcherResult,
        run_adaptive_researcher,
    )

    assert AdaptiveResearcherResult is not None
    assert run_adaptive_researcher is not None


async def test_run_adaptive_researcher_returns_result() -> None:
    """Stubs return canned values → AdaptiveResearcherResult returned."""
    from alphamind.analysis.adaptive_research.runner import (
        AdaptiveResearcherResult,
        _Deps,
        _run_adaptive_researcher,
    )

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success()),
    )

    result = await _run_adaptive_researcher(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_outputs=_make_distillation_outputs(),
        sector_briefs=_make_sector_briefs(),
        qualitative_brief=_make_qualitative_brief(),
        correlation_regime_brief=_make_correlation_regime_brief(),
        universal_regime_label=_REGIME_LABEL,
        universe=_UNIVERSE,
        agents_config=_make_agents_registry(),
        deps=deps,
    )

    assert isinstance(result, AdaptiveResearcherResult)
    assert result.brief.invocation_id == _INVOCATION_ID
    assert isinstance(result.input_bundle, InputBundle)
    assert isinstance(result.anomaly_inputs, AdaptiveAnomalyInputs)
    assert result.tokens_used.input_tokens == 500
    assert result.tool_calls_used == 2
    assert result.retry_count == 0
    assert result.wall_clock_seconds >= 0.0


async def test_config_drift_raises_value_error() -> None:
    """When the adaptive_researcher key is absent, ValueError is raised."""
    from alphamind.analysis.adaptive_research.runner import _Deps, _run_adaptive_researcher

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success()),
    )

    with pytest.raises(ValueError, match="adaptive_researcher") as exc_info:
        await _run_adaptive_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            sector_briefs=_make_sector_briefs(),
            qualitative_brief=_make_qualitative_brief(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            universal_regime_label=_REGIME_LABEL,
            universe=_UNIVERSE,
            agents_config={},
            deps=deps,
        )

    assert "config drift" in str(exc_info.value)


async def test_harness_failure_propagates_unchanged() -> None:
    """A MalformedOutputFailure raised by the harness propagates up unchanged."""
    from alphamind.analysis.adaptive_research.runner import _Deps, _run_adaptive_researcher

    original_failure = MalformedOutputFailure(
        "parse failed on both attempts",
        agent_name=AgentName.adaptive_researcher.value,
        invocation_id=_INVOCATION_ID,
    )

    async def _failing_harness(**_: object) -> HarnessSuccess:
        raise original_failure

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_failing_harness,
    )

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await _run_adaptive_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_outputs=_make_distillation_outputs(),
            sector_briefs=_make_sector_briefs(),
            qualitative_brief=_make_qualitative_brief(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            universal_regime_label=_REGIME_LABEL,
            universe=_UNIVERSE,
            agents_config=_make_agents_registry(),
            deps=deps,
        )

    assert exc_info.value is original_failure


async def test_wall_clock_seconds_within_loose_bound() -> None:
    """wall_clock_seconds is non-negative and approximately matches elapsed time.

    Loose bound: a stubbed sub-second harness should complete well under one
    second of wall clock; we assert below ten seconds (10x tolerance).
    """
    from alphamind.analysis.adaptive_research.runner import _Deps, _run_adaptive_researcher

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success()),
    )

    result = await _run_adaptive_researcher(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_outputs=_make_distillation_outputs(),
        sector_briefs=_make_sector_briefs(),
        qualitative_brief=_make_qualitative_brief(),
        correlation_regime_brief=_make_correlation_regime_brief(),
        universal_regime_label=_REGIME_LABEL,
        universe=_UNIVERSE,
        agents_config=_make_agents_registry(),
        deps=deps,
    )

    assert result.wall_clock_seconds >= 0.0
    assert result.wall_clock_seconds < 10.0


async def test_harness_receives_bundle_text_and_universe_verbatim() -> None:
    """The harness sees user_message == bundle_text and universe forwarded as-is."""
    from alphamind.analysis.adaptive_research.runner import _Deps, _run_adaptive_researcher

    received: dict[str, Any] = {}

    async def _capturing_harness(**kw: object) -> HarnessSuccess:
        received.update(kw)
        return _make_harness_success()

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_capturing_harness,
    )

    sector_briefs = _make_sector_briefs()
    qualitative_brief = _make_qualitative_brief()
    correlation_regime_brief = _make_correlation_regime_brief()

    result = await _run_adaptive_researcher(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_outputs=_make_distillation_outputs(),
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        universal_regime_label=_REGIME_LABEL,
        universe=_UNIVERSE,
        agents_config=_make_agents_registry(),
        deps=deps,
    )

    assert received["user_message"] == result.input_bundle.bundle_text
    assert received["universe"] is _UNIVERSE
    assert received["invocation_id"] == _INVOCATION_ID
    assert received["sector_briefs"] is sector_briefs
    assert received["qualitative_brief"] is qualitative_brief
    assert received["correlation_regime_brief"] is correlation_regime_brief


async def test_brief_returned_unmutated_from_harness() -> None:
    """The runner does not mutate the brief returned by the harness."""
    from alphamind.analysis.adaptive_research.runner import _Deps, _run_adaptive_researcher

    expected_brief = _make_adaptive_brief()
    harness_success = HarnessSuccess(
        brief=expected_brief,
        raw_response="raw",
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=10, output_tokens=20, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=0.1,
    )

    deps = _Deps(
        anomaly_assembler=_make_stub_anomaly_assembler(_make_default_anomaly_inputs()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(harness_success),
    )

    result = await _run_adaptive_researcher(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_outputs=_make_distillation_outputs(),
        sector_briefs=_make_sector_briefs(),
        qualitative_brief=_make_qualitative_brief(),
        correlation_regime_brief=_make_correlation_regime_brief(),
        universal_regime_label=_REGIME_LABEL,
        universe=_UNIVERSE,
        agents_config=_make_agents_registry(),
        deps=deps,
    )

    assert result.brief is expected_brief


def test_run_adaptive_researcher_wires_production_callables() -> None:
    """The public entry point imports the production assemblers + harness.

    Inspecting source imports is the cheapest way to assert the wiring without
    touching the SDK or DB.
    """
    import inspect

    from alphamind.analysis.adaptive_research import runner as runner_mod

    src = inspect.getsource(runner_mod)
    assert "assemble_adaptive_anomaly_inputs" in src
    assert "assemble_input_bundle" in src
    assert "invoke_adaptive_researcher" in src
