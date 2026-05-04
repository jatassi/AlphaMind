"""Tests for adaptive-researcher LLM invocation harness — ALP-263.

Tests are behaviour-driven through the public interface only:
``invoke_adaptive_researcher`` and the exception hierarchy.  The SDK is
stubbed via the ``sdk_query_fn`` dependency-injection parameter — no test
calls the real Anthropic API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    invoke_adaptive_researcher,
)
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
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
from alphamind.config.models.agents import AdaptiveAgentConfig, AllowedModel
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Verbatim minimal-valid wire text
# ---------------------------------------------------------------------------


# A brief payload (the dict shape ``ResultMessage.structured_output`` carries
# post-ALP-288 migration) with one SIGNAL thread whose Strengthens reference
# resolves into the upstream-brief fixtures below (SA-TECH-1 lives in the
# tech sector brief's findings).
def _minimal_brief_payload(invocation_id: str = "inv-test-001") -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "threads_investigated_count": 1,
        "anomalies_triaged_count": 1,
        "anomalies_deferred": [],
        "threads": [
            {
                "thread_id": "AR-1",
                "trigger": "SA-TECH-ANOM-1",
                "question": "What drove NVDA volume spike?",
                "tickers": ["NVDA"],
                "sector": "tech_semis",
                "tools_used": ["news_search", "prediction_markets"],
                "findings": [
                    "news_search returned pre-earnings notes",
                    "prediction_markets show repricing",
                ],
                "assessment": "signal",
                "confidence": "moderate",
                "implication": "Pre-earnings repositioning.",
                "strengthens": ["SA-TECH-1"],
                "weakens": [],
            }
        ],
    }


_MINIMAL_BRIEF_PAYLOAD = _minimal_brief_payload()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_config() -> AdaptiveAgentConfig:
    """A minimal AdaptiveAgentConfig pointing to the adaptive prompt."""
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search", "prediction_markets", "earnings_commentary"],
        cumulative_tool_call_limit=20,
        cumulative_tool_token_budget=4_000,
        tool_caps={"news_search": 8, "prediction_markets": 4, "earnings_commentary": 4},
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


@pytest.fixture()
def universe() -> frozenset[str]:
    return frozenset({"NVDA", "AMD", "JPM", "BAC", "XOM", "CVX", "VLO", "MPC", "PSX"})


# ---------------------------------------------------------------------------
# Upstream-brief fixtures — sources for Layer-3 referential resolution
# ---------------------------------------------------------------------------


def _sector_brief(sector: Sector, prefix: str) -> SectorBrief:
    return SectorBrief(
        invocation_id="inv-test-001",
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline="Headline",
                tickers=("NVDA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.MODERATE,
                detail="Detail",
            ),
        ),
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
        thesis_candidates=(
            ThesisCandidate(
                thesis_candidate_id=f"{prefix}-TC-1",
                ticker="NVDA",
                direction=Direction.LONG,
                setup_type=SetupType.CATALYST,
                catalyst="Earnings",
                time_horizon_hours="48",
                conviction_sketch=ConvictionSketch.MODERATE,
                conviction_justification="Justification",
                key_risk="Risk",
            ),
        ),
    )


@pytest.fixture()
def sector_briefs() -> tuple[SectorBrief, ...]:
    return (
        _sector_brief(Sector.TECH_SEMIS, "SA-TECH"),
        _sector_brief(Sector.FINANCIALS, "SA-FIN"),
        _sector_brief(Sector.ENERGY, "SA-ENERGY"),
    )


@pytest.fixture()
def qualitative_brief() -> QualitativeBrief:
    return QualitativeBrief(
        invocation_id="inv-test-001",
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        threads=(
            NarrativeThread(
                thread_id="QR-1",
                summary="Rate hawks",
                relevance="rate-sensitive",
                direction=ThreadDirection.BEARISH,
                subject="rate-sensitive equities",
                time_horizon=TimeHorizon.IMMEDIATE,
                evidence=(
                    EvidenceLine(source_type="news", observation="Obs", citation="[ND-M1]"),
                    EvidenceLine(
                        source_type="social", observation="Obs2", citation="social_sentiment"
                    ),
                ),
                implication="Headwinds.",
            ),
        ),
        catalyst_watches=(
            CatalystWatch(
                catalyst_id="QR-CW-1",
                ticker="NVDA",
                catalyst_name="Earnings",
                hours_to_event=12,
                thesis_impact="Impact.",
            ),
        ),
        sentiment_snapshot=SentimentSnapshot(
            extremes="none",
            divergences="none",
            regime="neutral",
        ),
    )


@pytest.fixture()
def correlation_regime_brief() -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text="CORRELATION & REGIME BRIEF\n[CR-1] regime\n",
        reference_index={"CR-1": "regime.label", "CR-2": "q7.intermarket_regime.dxy_to_spx"},
        freshness_min=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# SDK-message helpers
# ---------------------------------------------------------------------------


def _make_sdk_response(
    structured_output: dict[str, Any] | None = None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
    tool_use_blocks: int = 0,
    tool_use_block_name: str = "mcp__alphamind_adaptive__news_search",
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields.

    *structured_output* is delivered on the terminating :class:`ResultMessage`
    (the post-migration JSON-mode payload path); ``None`` simulates the SDK
    failing to populate it. *text* is concatenated by the harness for the
    diagnostic record only — usually empty in JSON mode but Sonnet sometimes
    narrates between tool calls.

    When ``tool_use_blocks > 0``, an :class:`AssistantMessage` containing that
    many ``ToolUseBlock`` instances is emitted *before* the text-bearing
    assistant message. Pass ``tool_use_block_name`` to simulate the pseudo-
    events the SDK injects for ``ToolSearch`` / ``StructuredOutput``; the
    harness must filter those out of its tool counter.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    messages: list[Any] = []

    if tool_use_blocks:
        tool_blocks: list[Any] = [
            ToolUseBlock(id=f"tu-{i}", name=tool_use_block_name, input={"query": "FOMC"})
            for i in range(tool_use_blocks)
        ]
        messages.append(
            AssistantMessage(
                content=tool_blocks,
                model="claude-sonnet-4-6",
                stop_reason=None,
                usage=None,
            )
        )

    assistant = AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="claude-sonnet-4-6",
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason=stop_reason,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        structured_output=structured_output,
    )
    messages.extend([assistant, result])
    return messages


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _make_stub_query(
    responses: list[list[Any]],
) -> Callable[..., AsyncGenerator[Any]]:
    """Return a stub sdk_query_fn that yields successive responses."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncGenerator[Any]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        async for msg in _async_iter(responses[idx]):
            yield msg

    return _stub


# ---------------------------------------------------------------------------
# 0. Tracer bullet — public names import
# ---------------------------------------------------------------------------


def test_public_names_importable() -> None:
    """The acceptance-criterion public surface resolves cleanly."""
    assert invoke_adaptive_researcher is not None
    assert HarnessSuccess is not None
    assert HarnessFailure is not None
    assert MalformedOutputFailure is not None
    assert ContextOverflowFailure is not None
    assert SDKFailure is not None
    assert TimeoutFailure is not None


# ---------------------------------------------------------------------------
# 1. Happy path — invoke_adaptive_researcher returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_harness_success(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """invoke_adaptive_researcher returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.brief, AdaptiveBrief)
    assert result.retry_count == 0
    assert result.tool_calls_used == 0
    assert result.wall_clock_seconds >= 0.0
    # raw_response is the JSON-rendered structured output post-migration.
    assert "AR-1" in result.raw_response
    assert "tech_semis" in result.raw_response
    assert isinstance(result.tokens_used, TokensUsed)


# ---------------------------------------------------------------------------
# 2. One-retry recovery — first response malformed, retry valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_retry_recovers(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Parse failure followed by a corrected response returns retry_count=1."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            # ``structured_output=None`` simulates the SDK failing to populate
            # the field — a parse-stage failure that triggers the corrective
            # retry path.
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    assert isinstance(result.brief, AdaptiveBrief)
    # Corrective-retry message names the failed contract, the schema mode, and
    # carries the content-preservation directive.
    retry_prompt = captured_prompts[1]
    assert "adaptive researcher output" in retry_prompt.lower()
    assert "adaptive-research.md" in retry_prompt
    assert "AdaptiveBrief schema" in retry_prompt
    # Sonnet was observed to abandon prior analytical work on retry under the
    # bare strict directive; this directive nudges same-context preservation.
    assert "prior analytical content" in retry_prompt.lower()


@pytest.mark.asyncio
async def test_retry_call_passes_session_id_for_resume(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """The retry SDK call must set ``options.resume`` to the prior call's session_id.

    Without this, the retry runs in a fresh SDK session and the agent's prior
    response is invisible — the content-preservation directive in the retry
    message has nothing to reference and Sonnet collapses to an empty brief.
    """
    captured_options: list[Any] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        if len(captured_options) == 1:
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-test-resume",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert len(captured_options) == 2
    # First call starts a fresh session.
    assert captured_options[0].resume is None
    # Second call resumes the session_id reported by the first ResultMessage
    # (the test stub stamps `sess-1`).
    assert captured_options[1].resume == "sess-1"


# ---------------------------------------------------------------------------
# 3. Validation-failure recovery — first response off-universe ticker, retry valid
# ---------------------------------------------------------------------------

# Brief whose Strengthens reference does not resolve into upstream briefs;
# this is a Layer-3 validation failure (not a parse failure).
def _payload_with_invented_reference() -> dict[str, Any]:
    payload = _minimal_brief_payload("inv-test-001")
    payload["threads"][0]["strengthens"] = ["SA-TECH-99"]
    return payload


@pytest.mark.asyncio
async def test_validation_failure_retry_recovers(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Validation failure (invented Strengthens ref) on first attempt then valid retry."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(_payload_with_invented_reference())):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    assert isinstance(result.brief, AdaptiveBrief)
    retry_prompt = captured_prompts[1]
    # Validation retry surfaces the structural-contract framing (not parse).
    assert "structural contract" in retry_prompt.lower()
    assert "SA-TECH-99" in retry_prompt or "referential" in retry_prompt.lower()
    # Same content-preservation directive on the validation path.
    assert "prior analytical content" in retry_prompt.lower()


# ---------------------------------------------------------------------------
# 4. Both attempts malformed → MalformedOutputFailure with both raw responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_attempts_malformed_raises_with_both_raw_responses(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure with both raw responses."""
    bad_initial = {"shape": "wrong"}  # missing required AdaptiveBrief fields
    bad_retry = {"still": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_initial), _make_sdk_response(bad_retry)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    # Both raw responses are JSON-rendered structured outputs; verify each
    # carries the load-bearing key from its corresponding stubbed payload.
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "still" in err.raw_response_retry
    assert err.invocation_id == "inv-test-001"
    assert err.agent_name == "adaptive_researcher"


# ---------------------------------------------------------------------------
# 5. max_tokens + parse error → ContextOverflowFailure (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_with_parse_error_raises_context_overflow_no_retry(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Parse failure paired with stop_reason=max_tokens raises ContextOverflowFailure
    immediately without attempting a retry."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        # ``structured_output=None`` simulates the API truncating before the
        # final structured payload could be emitted — paired with max_tokens
        # this is the canonical context-overflow signal.
        async for msg in _async_iter(_make_sdk_response(None, stop_reason="max_tokens")):
            yield msg

    with pytest.raises(ContextOverflowFailure):
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "retry must not be attempted on context-overflow failure"


# ---------------------------------------------------------------------------
# 6. Tool-allowlist drift — agent yaml names a tool not in TOOLS → SDKFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_allowlist_drift_raises_sdk_failure_at_startup(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """An ``agent_config.tools`` value not in :data:`TOOLS` raises SDKFailure at startup.

    The SDK stub is *never invoked* because tool resolution happens before the
    SDK call.
    """
    drifted_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search", "social_sentiment"],
        cumulative_tool_call_limit=20,
        cumulative_tool_token_budget=4_000,
        tool_caps={"news_search": 8, "social_sentiment": 4},
    )

    sdk_called = False

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal sdk_called
        sdk_called = True
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_adaptive_researcher(
            agent_config=drifted_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )

    assert "social_sentiment" in str(exc_info.value)
    assert not sdk_called, "SDK must not be invoked when tool resolution fails"


# ---------------------------------------------------------------------------
# 7. Timeout exceeded → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_sdk_stub_raises_timeout_failure(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=1,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search"],
        cumulative_tool_call_limit=20,
        cumulative_tool_token_budget=4_000,
        tool_caps={"news_search": 8},
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_adaptive_researcher(
            agent_config=tight_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 8. SDK auth failure → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_sdk_failure_naming_env_var(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """CLIConnectionError raised by the SDK becomes SDKFailure naming the OAuth env var."""
    from claude_agent_sdk import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-auth-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_auth_fail_stub,
        )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 9. Diagnostic archive — files written under archive_root; None is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_written_when_archive_root_provided(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Diagnostic files written to archive_root/invocations/<id>/analysis/<agent>/."""
    stub = _make_stub_query([_make_sdk_response(_minimal_brief_payload("inv-diag-001"))])

    await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-diag-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-diag-001" / "analysis" / "adaptive_researcher"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response_initial.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    # No retry → no retry response file.
    assert not (diag_dir / "response_retry.md").exists()
    meta = json.loads((diag_dir / "metadata.json").read_text())
    assert meta["tool_calls_used"] == 0
    assert meta["retry_count"] == 0


@pytest.mark.asyncio
async def test_archive_root_none_skips_disk_io(
    agent_config: AdaptiveAgentConfig,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    tmp_path: Path,
) -> None:
    """Passing archive_root=None skips diagnostic writes and does not crash."""
    stub = _make_stub_query([_make_sdk_response(_minimal_brief_payload("inv-no-archive"))])

    sentinel = tmp_path / "should-not-exist"
    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-no-archive",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# 10. Diagnostic archive on retry — retry file appears
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_include_retry_on_corrective_loop(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Retry path produces a response_retry.md and an error trail in errors.json."""
    valid_retry = _minimal_brief_payload("inv-retry-001")
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(valid_retry)])

    await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-retry-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-retry-001" / "analysis" / "adaptive_researcher"
    initial_text = (diag_dir / "response_initial.md").read_text()
    retry_text = (diag_dir / "response_retry.md").read_text()
    # The initial attempt's structured_output was None — the diagnostic
    # records the rendered placeholder. The retry succeeded with the JSON
    # payload, which renders to a dict containing the AR-1 thread.
    assert "structured_output not populated" in initial_text
    assert "AR-1" in retry_text
    errors = json.loads((diag_dir / "errors.json").read_text())
    assert any(e.get("attempt") == 1 for e in errors)


# ---------------------------------------------------------------------------
# 11. tool_calls_used populated when SDK emits ToolUseBlocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_used_counts_tool_use_blocks(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """When the SDK emits ToolUseBlocks, the harness counts them into tool_calls_used."""
    stub = _make_stub_query(
        [_make_sdk_response(_minimal_brief_payload("inv-tools-001"), tool_use_blocks=3)]
    )

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-tools-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.tool_calls_used == 3


@pytest.mark.asyncio
async def test_tool_calls_used_accumulates_across_retry(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """tool_calls_used sums across the initial attempt and the retry."""
    stub = _make_stub_query(
        [
            _make_sdk_response(None, tool_use_blocks=2),
            _make_sdk_response(_minimal_brief_payload("inv-tools-retry"), tool_use_blocks=1),
        ]
    )

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-tools-retry",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.retry_count == 1
    assert result.tool_calls_used == 3


# ---------------------------------------------------------------------------
# 11b. Pseudo-event filter — ToolSearch/StructuredOutput must not count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pseudo_event_tool_blocks_excluded_from_count(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """The SDK's JSON-mode ToolSearch / StructuredOutput pseudo-events do not count.

    Real research tool calls go through the in-process MCP server and arrive
    as ``mcp__alphamind_adaptive__<tool>``; the JSON-output mode injects
    ``ToolSearch`` and ``StructuredOutput`` blocks the harness must exclude
    from the agent's tool budget. Spike output documented this inflation
    (scenario 2 reported 4 tool_calls when only 2 were real).
    """
    payload = _minimal_brief_payload("inv-pseudo-001")
    stub = _make_stub_query(
        [
            _make_sdk_response(
                payload,
                tool_use_blocks=2,
                tool_use_block_name="ToolSearch",
            )
        ]
    )

    result = await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-pseudo-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.tool_calls_used == 0


# ---------------------------------------------------------------------------
# 12. Architectural integration — full ClaudeAgentOptions accepts the
#     build_analysis_mcp_server return value, mcp_servers keyed by
#     ``alphamind_adaptive``, and allowed_tools uses the MCP wire format.
#
#     This is the architectural-integration check from the story prelude:
#     stub-only tests can let SDK option-shape errors slip through, so this
#     test instantiates the full ClaudeAgentOptions with the actual
#     build_analysis_mcp_server return value (against a real test session).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_agent_options_structure_uses_alphamind_adaptive_server(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """Pin the autonomous-agent contract on ClaudeAgentOptions.

    Catches a regression where someone disables tools, loads developer settings,
    drops the output-token cap, reduces ``max_turns`` below the budget needed
    for the multi-turn tool-use loop, or registers tools under the wrong MCP
    server name.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_minimal_brief_payload("inv-opt-001"))):
            yield msg

    await invoke_adaptive_researcher(
        agent_config=agent_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-opt-001",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    assert len(captured_options) == 1
    options = captured_options[0]

    # allowed_tools uses the bundled CLI's MCP wire format —
    # ``mcp__<server>__<tool>`` — so the permission filter matches the
    # tool name the model emits when calling an SDK MCP-server tool.
    expected_allowed = [f"mcp__alphamind_adaptive__{name}" for name in agent_config.tools]
    assert options.allowed_tools == expected_allowed
    assert options.max_turns is not None
    assert options.max_turns >= agent_config.cumulative_tool_call_limit
    assert isinstance(options.system_prompt, str)
    assert options.system_prompt
    assert options.setting_sources == []
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)

    # mcp_servers is registered under the adaptive namespace (NOT qualitative).
    assert options.mcp_servers, "mcp_servers must be populated when tools are configured"
    assert "alphamind_adaptive" in options.mcp_servers
    assert "alphamind_qualitative" not in options.mcp_servers
    server_config = options.mcp_servers["alphamind_adaptive"]
    # McpSdkServerConfig is a TypedDict with type/name/instance keys.
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_adaptive"


@pytest.mark.asyncio
async def test_mcp_servers_empty_when_no_tools_configured(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """A config with empty tools registers no MCP server — the agent runs tool-less."""
    no_tools_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=[],
        cumulative_tool_call_limit=20,
        cumulative_tool_token_budget=4_000,
        tool_caps={},
    )
    # When the agent has no registered tools, the brief's tools_used must be
    # empty too — the validator's tools_used_in_allowlist check rejects any
    # tool name not in agent_config.tools.
    no_tools_payload = _minimal_brief_payload("inv-no-tools")
    no_tools_payload["threads"][0]["tools_used"] = []
    no_tools_payload["threads"][0]["findings"] = ["reasoning-only conclusion from upstream context"]
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(no_tools_payload)):
            yield msg

    await invoke_adaptive_researcher(
        agent_config=no_tools_config,
        user_message="Produce an adaptive brief.",
        invocation_id="inv-no-tools",
        session=session,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]
    assert options.allowed_tools == []
    assert options.mcp_servers == {}


# ---------------------------------------------------------------------------
# 13. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_query_fn_is_used_real_query_never_called(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_minimal_brief_payload("inv-stub-001"))])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="Produce an adaptive brief.",
            invocation_id="inv-stub-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
        mock_real.assert_not_called()


# ---------------------------------------------------------------------------
# 14. System-prompt cache: file is read once across multiple invocations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_cached_per_process(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> None:
    """The adaptive-researcher prompt file is read once per process."""
    from alphamind.analysis.adaptive_research import harness as harness_mod

    harness_mod._PROMPT_CACHE.clear()
    read_calls: list[str] = []
    original_read_text = Path.read_text

    def _counting_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "adaptive_researcher" in str(self):
            read_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _counting_read_text):
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="call 1",
            invocation_id="inv-cache-001",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query(
                [_make_sdk_response(_minimal_brief_payload("inv-cache-001"))]
            ),
        )
        await invoke_adaptive_researcher(
            agent_config=agent_config,
            user_message="call 2",
            invocation_id="inv-cache-002",
            session=session,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query(
                [_make_sdk_response(_minimal_brief_payload("inv-cache-002"))]
            ),
        )

    prompt_reads = [p for p in read_calls if "adaptive_researcher" in p]
    assert len(prompt_reads) == 1, (
        f"Expected 1 prompt read, got {len(prompt_reads)}: {prompt_reads}"
    )
