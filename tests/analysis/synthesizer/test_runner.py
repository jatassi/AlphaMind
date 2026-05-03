"""Tests for the synthesizer runner — story 10 (ALP-210).

The runner composes the four adapters, the retrieval-store assembler, the
input-bundle assembler, and the harness into a single ``run_synthesizer``
entry point. All composed dependencies are injected via the private
``_run_synthesizer`` function so tests don't touch the Anthropic API or
the filesystem.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
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
from alphamind.analysis.synthesizer.harness import (
    ContextOverflowFailure,
    EmptyResponseFailure,
    HarnessFailure,
    HarnessSuccess,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.runner import (
    SynthesizerResult,
    _Deps,
    _run_synthesizer,
    load_synthesizer_agent_config,
)
from alphamind.config.models.agents import (
    AgentName,
    AllowedModel,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "test-inv-syn-001"
_REGIME_LABEL = "vol_expansion"


def _make_synthesizer_agent_config() -> BaseAgentConfig:
    """Build a synthesizer agent config (non-tool-loop slot)."""
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/synthesizer.md",
        latency_budget_seconds=180,
        context_token_budget=12_000,
        output_token_budget=2_000,
        tools=[],
    )


def _make_sector_brief(sector: Sector, prefix: str) -> SectorBrief:
    """Build a SectorBrief with one finding, anomaly, and thesis candidate."""
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline=f"{prefix} finding headline.",
                tickers=("AAA",),
                signal_type=SignalType.FLOW,
                strength=Strength.STRONG,
                detail="Detail.",
            ),
        ),
        anomalies=(
            Anomaly(
                anomaly_id=f"{prefix}-ANOM-1",
                description="Anomaly observation.",
                anomaly_type=AnomalyType.VOLUME,
                tickers=("AAA",),
                severity="investigate_now",
                suggested_question="What drove it?",
            ),
        ),
        thesis_candidates=(
            ThesisCandidate(
                thesis_candidate_id=f"{prefix}-TC-1",
                ticker="AAA",
                direction=Direction.LONG,
                setup_type=SetupType.MOMENTUM,
                catalyst="Earnings.",
                time_horizon_hours="24-72h",
                conviction_sketch=ConvictionSketch.MODERATE,
                conviction_justification="Pattern.",
                key_risk="Miss.",
            ),
        ),
    )


def _make_sector_briefs() -> tuple[SectorBrief, SectorBrief, SectorBrief]:
    """Three sector briefs covering all three sectors."""
    return (
        _make_sector_brief(Sector.TECH_SEMIS, "SA-TECH"),
        _make_sector_brief(Sector.FINANCIALS, "SA-FIN"),
        _make_sector_brief(Sector.ENERGY, "SA-ENERGY"),
    )


def _make_correlation_regime_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="[CR-1] regime context.\n  detail.\n",
        reference_index={"CR-1": "regime.label"},
        freshness_min=_NOW - timedelta(minutes=15),
    )


def _make_qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Macro thread.",
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
                implication="Tighter conditions.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker="NVDA",
                catalyst_name="Earnings",
                hours_to_event=18,
                thesis_impact="Direct test.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="neutral",
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
                question="What drove the volume?",
                tickers=("AAA",),
                sector=Sector.TECH_SEMIS,
                tools_used=("news_search",),
                findings=("Pre-earnings buying.",),
                assessment=Assessment.SIGNAL,
                confidence=Confidence.MODERATE,
                implication="Upside skew.",
                strengthens=("SA-TECH-1",),
                weakens=(),
            ),
        ),
    )


class _StubPortfolioReader:
    """Minimal SynthesizerPortfolioStateReader stub for tests."""

    async def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        return ()

    async def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        return ()

    async def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        return SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        )


def _make_harness_success(response_text: str = "synthesis prose.") -> HarnessSuccess:
    return HarnessSuccess(
        response_text=response_text,
        tokens_used=TokensUsed(
            input_tokens=500,
            output_tokens=300,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        tool_calls_used=2,
        wall_clock_seconds=1.5,
        stop_reason="end_turn",
    )


def _make_default_deps() -> _Deps:
    async def _stub_harness(**_kw: object) -> HarnessSuccess:
        return _make_harness_success()

    return _Deps(harness_fn=_stub_harness)


# ---------------------------------------------------------------------------
# Test 1 (tracer bullet): happy path returns populated SynthesizerResult
# ---------------------------------------------------------------------------


def test_runner_returns_result_on_happy_path() -> None:
    """Stubbed harness returns text → runner returns populated SynthesizerResult."""
    result = asyncio.run(
        _run_synthesizer(
            agent_config=_make_synthesizer_agent_config(),
            regime_label=_REGIME_LABEL,
            sector_briefs=_make_sector_briefs(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            qualitative_brief=_make_qualitative_brief(),
            adaptive_brief=_make_adaptive_brief(),
            portfolio_reader=_StubPortfolioReader(),
            invocation_id=_INVOCATION_ID,
            now_utc=_NOW,
            deps=_make_default_deps(),
        )
    )

    assert isinstance(result, SynthesizerResult)
    assert result.synthesis_text == "synthesis prose."
    assert result.tokens_used.input_tokens == 500
    assert result.tool_calls_used == 2
    assert result.wall_clock_seconds == pytest.approx(1.5)
    assert result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Test 2: retrieval store carries every upstream's reference IDs
# ---------------------------------------------------------------------------


def test_runner_builds_retrieval_store_from_inputs() -> None:
    """The returned RetrievalStore holds the ref-IDs from every upstream brief."""
    result = asyncio.run(
        _run_synthesizer(
            agent_config=_make_synthesizer_agent_config(),
            regime_label=_REGIME_LABEL,
            sector_briefs=_make_sector_briefs(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            qualitative_brief=_make_qualitative_brief(),
            adaptive_brief=_make_adaptive_brief(),
            portfolio_reader=_StubPortfolioReader(),
            invocation_id=_INVOCATION_ID,
            now_utc=_NOW,
            deps=_make_default_deps(),
        )
    )

    expected = {
        "CR-1",
        "SA-TECH-1",
        "SA-TECH-ANOM-1",
        "SA-TECH-TC-1",
        "SA-FIN-1",
        "SA-FIN-ANOM-1",
        "SA-FIN-TC-1",
        "SA-ENERGY-1",
        "SA-ENERGY-ANOM-1",
        "SA-ENERGY-TC-1",
        "QR-1",
        "QR-CW-1",
        "AR-1",
    }
    assert set(result.retrieval_store.entries) == expected


# ---------------------------------------------------------------------------
# Test 3: optional adaptive_brief=None produces no AR entries
# ---------------------------------------------------------------------------


def test_runner_omits_optional_adaptive_brief() -> None:
    """`adaptive_brief=None` → store has no AR ref-IDs and bundle has no AR section."""
    captured: dict[str, Any] = {}

    async def _capturing_harness(**kw: object) -> HarnessSuccess:
        captured.update(kw)
        return _make_harness_success()

    deps = _Deps(harness_fn=_capturing_harness)

    result = asyncio.run(
        _run_synthesizer(
            agent_config=_make_synthesizer_agent_config(),
            regime_label=_REGIME_LABEL,
            sector_briefs=_make_sector_briefs(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            qualitative_brief=_make_qualitative_brief(),
            adaptive_brief=None,
            portfolio_reader=_StubPortfolioReader(),
            invocation_id=_INVOCATION_ID,
            now_utc=_NOW,
            deps=deps,
        )
    )

    # No AR-N entries in the retrieval store.
    assert not any(ref.startswith("AR-") for ref in result.retrieval_store.entries)
    # Other upstream entries still present.
    assert "CR-1" in result.retrieval_store.entries
    assert "QR-1" in result.retrieval_store.entries
    # Input bundle (passed as user_message) has no AR section header.
    assert "Adaptive research findings" not in captured["user_message"]


# ---------------------------------------------------------------------------
# Test 4: every HarnessFailure subclass propagates fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        EmptyResponseFailure(
            "empty",
            agent_name=AgentName.synthesizer.value,
            invocation_id=_INVOCATION_ID,
        ),
        ContextOverflowFailure(
            "max_tokens",
            agent_name=AgentName.synthesizer.value,
            invocation_id=_INVOCATION_ID,
        ),
        SDKFailure(
            "sdk",
            agent_name=AgentName.synthesizer.value,
            invocation_id=_INVOCATION_ID,
        ),
        TimeoutFailure(
            "latency",
            agent_name=AgentName.synthesizer.value,
            invocation_id=_INVOCATION_ID,
        ),
    ],
)
def test_runner_propagates_harness_failures(failure: HarnessFailure) -> None:
    """A HarnessFailure raised by the harness propagates unchanged (fail-closed)."""

    async def _failing_harness(**_kw: object) -> HarnessSuccess:
        raise failure

    deps = _Deps(harness_fn=_failing_harness)

    with pytest.raises(type(failure)) as exc_info:
        asyncio.run(
            _run_synthesizer(
                agent_config=_make_synthesizer_agent_config(),
                regime_label=_REGIME_LABEL,
                sector_briefs=_make_sector_briefs(),
                correlation_regime_brief=_make_correlation_regime_brief(),
                qualitative_brief=_make_qualitative_brief(),
                adaptive_brief=_make_adaptive_brief(),
                portfolio_reader=_StubPortfolioReader(),
                invocation_id=_INVOCATION_ID,
                now_utc=_NOW,
                deps=deps,
            )
        )

    assert exc_info.value is failure


# ---------------------------------------------------------------------------
# Test 5: agents.yaml resolution uses the existing config loader
# ---------------------------------------------------------------------------


def test_runner_resolves_agent_config_from_agents_yaml() -> None:
    """The runner reads the synthesizer entry from ``config/agents.yaml``.

    Calling :func:`load_synthesizer_agent_config` exercises the existing
    :class:`AgentsConfig` validator (no parallel YAML parsing) and
    surfaces the synthesizer slot's :class:`BaseAgentConfig`.
    """
    agent_config = load_synthesizer_agent_config()

    assert isinstance(agent_config, BaseAgentConfig)
    assert agent_config.model == AllowedModel.sonnet_4_6
    assert agent_config.prompt == "prompts/analysis/synthesizer.md"


# ---------------------------------------------------------------------------
# Test 6: sector-brief freshness defaults to now_utc when not per-brief
# ---------------------------------------------------------------------------


def test_runner_passes_now_utc_as_sector_freshness() -> None:
    """Sector adapters receive ``now_utc`` as their freshness param.

    The synthesizer's retrieval store carries one ``freshness_by_source``
    timestamp per contributing source; for sector briefs (no per-brief
    freshness emitted upstream today), that value comes from ``now_utc``.
    """
    result = asyncio.run(
        _run_synthesizer(
            agent_config=_make_synthesizer_agent_config(),
            regime_label=_REGIME_LABEL,
            sector_briefs=_make_sector_briefs(),
            correlation_regime_brief=_make_correlation_regime_brief(),
            qualitative_brief=_make_qualitative_brief(),
            adaptive_brief=_make_adaptive_brief(),
            portfolio_reader=_StubPortfolioReader(),
            invocation_id=_INVOCATION_ID,
            now_utc=_NOW,
            deps=_make_default_deps(),
        )
    )

    # Three sector sources all carry freshness == now_utc.
    assert result.retrieval_store.freshness_by_source[BriefSource.SA_TECH] == _NOW
    assert result.retrieval_store.freshness_by_source[BriefSource.SA_FIN] == _NOW
    assert result.retrieval_store.freshness_by_source[BriefSource.SA_ENERGY] == _NOW
    # CR brief uses its own freshness_min (different from now_utc).
    assert result.retrieval_store.freshness_by_source[BriefSource.CR] == _NOW - timedelta(
        minutes=15
    )
