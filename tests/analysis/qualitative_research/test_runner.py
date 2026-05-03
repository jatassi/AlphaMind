"""Tests for the qualitative-researcher runner — story 06 (ALP-252).

All composed dependencies (inputs loader, digest renderer, bundle assembler,
harness) are injected via the private ``_run_qualitative_researcher`` function
so tests don't touch the Anthropic API, the database, or the filesystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.qualitative_research.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
)
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle,
    assemble_input_bundle,
)
from alphamind.analysis.qualitative_research.loaders import QualitativeInputs
from alphamind.analysis.qualitative_research.models import (
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import (
    QualitativeResearcherResult,
    _Deps,
    _run_qualitative_researcher,
)
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AllowedModel,
    BaseAgentConfig,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 2, 8, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "test-inv-qr-001"

_REGIME_LABEL: dict[str, Any] = {
    "regime_label": "vol_expansion",
    "transition_state": "early-weak",
    "prior_label": "low_vol_compression",
    "invocations_held": 3,
}

_UNIVERSE: frozenset[str] = frozenset({"NVDA", "AMD", "INTC", "JPM", "GS", "XOM"})

_SECTOR_ROSTER: dict[Sector, frozenset[str]] = {
    Sector.TECH_SEMIS: frozenset({"NVDA", "AMD", "INTC"}),
    Sector.FINANCIALS: frozenset({"JPM", "GS"}),
    Sector.ENERGY: frozenset({"XOM"}),
}


def _make_qualitative_agent_config() -> AdaptiveAgentConfig:
    """Build a tool-loop agent config (qualitative_researcher slot requires it)."""
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=60,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=20_000,
        tool_caps={},
    )


def _make_agents_registry() -> dict[str, BaseAgentConfig]:
    """A minimal agents registry containing the qualitative_researcher slot."""
    return {AgentName.qualitative_researcher.value: _make_qualitative_agent_config()}


def _make_qualitative_inputs() -> QualitativeInputs:
    """Build minimal QualitativeInputs (no sentiment, events, or theses)."""
    return QualitativeInputs(
        sentiment_aggregates=(),
        prediction_markets=(),
        events=(),
        theses=(),
        data_freshness=_AS_OF,
    )


def _make_news_digest() -> NewsDigest:
    return NewsDigest(
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        total_collected=0,
        total_shown=0,
        entries=(),
        digest_text="=== NEWS DIGEST ===\nNo headlines.\n",
    )


def _make_qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id=_INVOCATION_ID,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Test thread",
                relevance="high",
                direction=ThreadDirection.BULLISH,
                subject="NVDA",
                time_horizon=TimeHorizon.NEAR_TERM,
                evidence=(
                    EvidenceLine(
                        source_type="news_digest",
                        observation="strong demand",
                        citation="ND-T1",
                    ),
                    EvidenceLine(
                        source_type="prediction_market",
                        observation="market agrees",
                        citation="PM-001",
                    ),
                ),
                implication="continued upside",
            ),
        ),
        catalyst_watches=(),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="risk-on",
        ),
    )


def _make_harness_success() -> HarnessSuccess:
    return HarnessSuccess(
        brief=_make_qualitative_brief(),
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


def _make_stub_inputs_loader(
    inputs: QualitativeInputs,
) -> Callable[..., QualitativeInputs]:
    def _stub(**kw: object) -> QualitativeInputs:
        return inputs

    return _stub


def _make_stub_digest_renderer(digest: NewsDigest) -> Callable[..., NewsDigest]:
    def _stub(**kw: object) -> NewsDigest:
        return digest

    return _stub


def _make_stub_harness(result: HarnessSuccess) -> Callable[..., Any]:
    async def _stub(**kw: object) -> HarnessSuccess:
        return result

    return _stub


def _make_default_deps() -> _Deps:
    return _Deps(
        inputs_loader=_make_stub_inputs_loader(_make_qualitative_inputs()),
        digest_renderer=_make_stub_digest_renderer(_make_news_digest()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success()),
    )


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


def test_run_qualitative_researcher_returns_result() -> None:
    """Stubs return canned values → QualitativeResearcherResult returned."""
    result = asyncio.run(
        _run_qualitative_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            universal_regime_label=_REGIME_LABEL,
            sector_roster=_SECTOR_ROSTER,
            universe=_UNIVERSE,
            agents_config=_make_agents_registry(),
            deps=_make_default_deps(),
        )
    )

    assert isinstance(result, QualitativeResearcherResult)
    assert result.brief.invocation_id == _INVOCATION_ID
    assert isinstance(result.input_bundle, InputBundle)
    assert isinstance(result.news_digest, NewsDigest)
    assert result.tokens_used.input_tokens == 500
    assert result.tool_calls_used == 2
    assert result.retry_count == 0
    assert result.wall_clock_seconds >= 0.0


# ---------------------------------------------------------------------------
# Test 2: Harness failure propagates unchanged
# ---------------------------------------------------------------------------


def test_harness_failure_propagates_unchanged() -> None:
    """A HarnessFailure raised by the harness propagates up without wrapping."""
    original_failure = MalformedOutputFailure(
        "parse failed on both attempts",
        agent_name=AgentName.qualitative_researcher.value,
        invocation_id=_INVOCATION_ID,
    )

    async def _failing_harness(**kw: object) -> HarnessSuccess:
        raise original_failure

    deps = _Deps(
        inputs_loader=_make_stub_inputs_loader(_make_qualitative_inputs()),
        digest_renderer=_make_stub_digest_renderer(_make_news_digest()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_failing_harness,
    )

    with pytest.raises(MalformedOutputFailure) as exc_info:
        asyncio.run(
            _run_qualitative_researcher(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                last_invocation_time=_LAST_INVOCATION_TIME,
                universal_regime_label=_REGIME_LABEL,
                sector_roster=_SECTOR_ROSTER,
                universe=_UNIVERSE,
                agents_config=_make_agents_registry(),
                deps=deps,
            )
        )

    assert exc_info.value is original_failure


# ---------------------------------------------------------------------------
# Test 3: Config-drift raises ValueError naming the missing key
# ---------------------------------------------------------------------------


def test_config_drift_raises_value_error() -> None:
    """When the qualitative_researcher key is absent, ValueError is raised."""
    agents_config: dict[str, BaseAgentConfig] = {}  # qualitative_researcher absent

    with pytest.raises(ValueError, match="qualitative_researcher") as exc_info:
        asyncio.run(
            _run_qualitative_researcher(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                last_invocation_time=_LAST_INVOCATION_TIME,
                universal_regime_label=_REGIME_LABEL,
                sector_roster=_SECTOR_ROSTER,
                universe=_UNIVERSE,
                agents_config=agents_config,
                deps=_make_default_deps(),
            )
        )

    assert "config drift" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 4: Empty inputs (no events, sentiment, theses) — runner still completes
# ---------------------------------------------------------------------------


def test_empty_inputs_runner_completes() -> None:
    """Empty QualitativeInputs and empty NewsDigest still produce a result.

    The bundle assembler handles empty gracefully; the runner does not
    short-circuit.  This guards against a future regression where the
    runner adds a defensive non-empty check.
    """
    empty_inputs = QualitativeInputs(
        sentiment_aggregates=(),
        prediction_markets=(),
        events=(),
        theses=(),
        data_freshness=_AS_OF,
    )
    empty_digest = NewsDigest(
        as_of=_AS_OF,
        last_invocation_time=_LAST_INVOCATION_TIME,
        total_collected=0,
        total_shown=0,
        entries=(),
        digest_text="",
    )

    deps = _Deps(
        inputs_loader=_make_stub_inputs_loader(empty_inputs),
        digest_renderer=_make_stub_digest_renderer(empty_digest),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_make_stub_harness(_make_harness_success()),
    )

    result = asyncio.run(
        _run_qualitative_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            universal_regime_label=_REGIME_LABEL,
            sector_roster=_SECTOR_ROSTER,
            universe=_UNIVERSE,
            agents_config=_make_agents_registry(),
            deps=deps,
        )
    )

    assert isinstance(result, QualitativeResearcherResult)
    assert result.input_bundle.bundle_text  # bundle text always non-empty
    assert result.news_digest is empty_digest


# ---------------------------------------------------------------------------
# Test 5: Every HarnessFailure subclass propagates unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        MalformedOutputFailure(
            "parse failed",
            agent_name=AgentName.qualitative_researcher.value,
            invocation_id=_INVOCATION_ID,
        ),
        ContextOverflowFailure(
            "max_tokens hit",
            agent_name=AgentName.qualitative_researcher.value,
            invocation_id=_INVOCATION_ID,
        ),
        SDKFailure(
            "SDK error",
            agent_name=AgentName.qualitative_researcher.value,
            invocation_id=_INVOCATION_ID,
        ),
        TimeoutFailure(
            "exceeded latency budget",
            agent_name=AgentName.qualitative_researcher.value,
            invocation_id=_INVOCATION_ID,
        ),
    ],
)
def test_every_harness_failure_propagates_unchanged(failure: HarnessFailure) -> None:
    """All four HarnessFailure subclasses propagate without wrapping."""

    async def _failing_harness(**kw: object) -> HarnessSuccess:
        raise failure

    deps = _Deps(
        inputs_loader=_make_stub_inputs_loader(_make_qualitative_inputs()),
        digest_renderer=_make_stub_digest_renderer(_make_news_digest()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_failing_harness,
    )

    with pytest.raises(type(failure)) as exc_info:
        asyncio.run(
            _run_qualitative_researcher(
                invocation_id=_INVOCATION_ID,
                as_of=_AS_OF,
                last_invocation_time=_LAST_INVOCATION_TIME,
                universal_regime_label=_REGIME_LABEL,
                sector_roster=_SECTOR_ROSTER,
                universe=_UNIVERSE,
                agents_config=_make_agents_registry(),
                deps=deps,
            )
        )

    assert exc_info.value is failure


# ---------------------------------------------------------------------------
# Test 6: Composition order: inputs → digest → bundle → harness
# ---------------------------------------------------------------------------


def test_composition_order() -> None:
    """Steps execute in order: load inputs → render digest → assemble → harness."""
    call_order: list[str] = []

    def _loader(**kw: object) -> QualitativeInputs:
        call_order.append("inputs")
        return _make_qualitative_inputs()

    def _renderer(**kw: object) -> NewsDigest:
        call_order.append("digest")
        return _make_news_digest()

    def _assembler(**kw: object) -> InputBundle:
        call_order.append("bundle")
        return assemble_input_bundle(**kw)  # type: ignore[arg-type]

    async def _harness(**kw: object) -> HarnessSuccess:
        call_order.append("harness")
        return _make_harness_success()

    asyncio.run(
        _run_qualitative_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            universal_regime_label=_REGIME_LABEL,
            sector_roster=_SECTOR_ROSTER,
            universe=_UNIVERSE,
            agents_config=_make_agents_registry(),
            deps=_Deps(
                inputs_loader=_loader,
                digest_renderer=_renderer,
                bundle_assembler=_assembler,
                harness_fn=_harness,
            ),
        )
    )

    assert call_order == ["inputs", "digest", "bundle", "harness"]


# ---------------------------------------------------------------------------
# Test 7: Harness receives assembled bundle text + universe verbatim
# ---------------------------------------------------------------------------


def test_harness_receives_bundle_and_universe_verbatim() -> None:
    """The harness sees user_message == bundle_text and universe forwarded as-is."""
    received: dict[str, Any] = {}

    async def _capturing_harness(**kw: object) -> HarnessSuccess:
        received.update(kw)
        return _make_harness_success()

    deps = _Deps(
        inputs_loader=_make_stub_inputs_loader(_make_qualitative_inputs()),
        digest_renderer=_make_stub_digest_renderer(_make_news_digest()),
        bundle_assembler=assemble_input_bundle,
        harness_fn=_capturing_harness,
    )

    result = asyncio.run(
        _run_qualitative_researcher(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            universal_regime_label=_REGIME_LABEL,
            sector_roster=_SECTOR_ROSTER,
            universe=_UNIVERSE,
            agents_config=_make_agents_registry(),
            deps=deps,
        )
    )

    assert received["user_message"] == result.input_bundle.bundle_text
    assert received["universe"] is _UNIVERSE
    assert received["invocation_id"] == _INVOCATION_ID
