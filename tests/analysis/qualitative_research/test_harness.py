"""Tests for qualitative-researcher LLM invocation harness — ALP-249.

Tests are behaviour-driven through the public interface only:
``invoke_qualitative_researcher`` and the exception hierarchy.  The SDK is
stubbed via the ``sdk_query_fn`` dependency-injection parameter — no test
calls the real Anthropic API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.qualitative_research.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    invoke_qualitative_researcher,
)
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.config.models.agents import AdaptiveAgentConfig, AllowedModel
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _minimal_brief_payload(invocation_id: str = "inv-test-001") -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "signal_quality": "high",
        "signal_quality_reason": None,
        "threads": [
            {
                "thread_id": "QR-1",
                "summary": "Prediction markets repricing toward higher FOMC-hold odds overnight.",
                "relevance": "financials",
                "direction": "bullish",
                "subject": "soft-landing pricing",
                "time_horizon": "immediate",
                "evidence": [
                    {
                        "source_type": "prediction_markets",
                        "observation": "hold odds 58 to 71",
                        "citation": "snapshot",
                    },
                    {
                        "source_type": "news",
                        "observation": "WSJ flagged dovish-leaning Fed speakers",
                        "citation": "ND-M2",
                    },
                ],
                "implication": "Bank-flow agents may not yet have repriced.",
            }
        ],
        "catalyst_watches": [],
        "sentiment_snapshot": {
            "extremes": "NVDA at 91st percentile (positive)",
            "divergences": "none",
            "regime": "Sentiment broadly constructive.",
        },
    }


_MINIMAL_BRIEF_PAYLOAD = _minimal_brief_payload()


@pytest.fixture()
def agent_config(tmp_path: Path) -> AdaptiveAgentConfig:
    """A minimal AdaptiveAgentConfig pointing to the qualitative prompt."""
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search", "prediction_markets", "earnings_commentary"],
        cumulative_tool_call_limit=15,
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
    return frozenset({"NVDA", "AMD", "JPM", "BAC", "XOM", "CVX"})


def _make_sdk_response(
    structured_output: dict[str, Any] | None = None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
    tool_use_blocks: int = 0,
    tool_use_block_name: str = "mcp__alphamind_qualitative__news_search",
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields.

    *structured_output* is delivered on the terminating :class:`ResultMessage`
    (the post-migration JSON-mode payload path); ``None`` simulates the SDK
    failing to populate it. *text* is concatenated by the harness for the
    diagnostic record only — usually empty in JSON mode but Sonnet sometimes
    narrates between tool calls.

    Pass ``tool_use_block_name`` to simulate the SDK's ``ToolSearch`` /
    ``StructuredOutput`` pseudo-events that the harness must filter out of
    its tool counter.
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
# 1. Happy-path: invoke_qualitative_researcher returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_harness_success(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """invoke_qualitative_researcher returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.brief, QualitativeBrief)
    assert result.retry_count == 0
    assert result.tool_calls_used == 0
    assert result.wall_clock_seconds >= 0.0
    # raw_response is the JSON-rendered structured output post-migration.
    assert "QR-1" in result.raw_response
    assert "soft-landing" in result.raw_response
    assert isinstance(result.tokens_used, TokensUsed)


# ---------------------------------------------------------------------------
# 2. One-retry recovery — first response malformed, retry valid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_retry_recovery_returns_retry_count_one(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Parse failure followed by a corrected response returns retry_count=1."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            # ``structured_output=None`` simulates the SDK failing to populate
            # the field — a parse-stage failure that triggers the retry path.
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    assert isinstance(result.brief, QualitativeBrief)
    # Corrective-retry message names the failed contract and the schema mode.
    retry_prompt = captured_prompts[1]
    assert "qualitative researcher output" in retry_prompt.lower()
    assert "qualitative-research.md" in retry_prompt
    assert "QualitativeBrief schema" in retry_prompt


# ---------------------------------------------------------------------------
# 3. Both attempts malformed → MalformedOutputFailure with both raw responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_attempts_malformed_raises_with_both_raw_responses(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure with both raw responses."""
    bad_initial = {"shape": "wrong"}  # missing required QualitativeBrief fields
    bad_retry = {"still": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_initial), _make_sdk_response(bad_retry)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "still" in err.raw_response_retry
    assert err.invocation_id == "inv-test-001"
    assert err.agent_name == "qualitative_researcher"


# ---------------------------------------------------------------------------
# 4. max_tokens + parse error → ContextOverflowFailure (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_with_parse_error_raises_context_overflow_no_retry(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Parse failure paired with stop_reason=max_tokens raises ContextOverflowFailure
    immediately without attempting a retry."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response(None, stop_reason="max_tokens")):
            yield msg

    with pytest.raises(ContextOverflowFailure):
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "retry must not be attempted on context-overflow failure"


# ---------------------------------------------------------------------------
# 5. Tool-allowlist drift — agent yaml names a tool not in TOOLS → SDKFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_allowlist_drift_raises_sdk_failure_at_startup(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """An ``agent_config.tools`` value not in :data:`TOOLS` raises SDKFailure at startup.

    The SDK stub is *never invoked* because tool resolution happens before the
    SDK call.
    """
    drifted_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search", "social_sentiment"],
        cumulative_tool_call_limit=15,
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
        await invoke_qualitative_researcher(
            agent_config=drifted_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_stub,
        )

    assert "social_sentiment" in str(exc_info.value)
    assert not sdk_called, "SDK must not be invoked when tool resolution fails"


# ---------------------------------------------------------------------------
# 6. Timeout exceeded → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_sdk_stub_raises_timeout_failure(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=1,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=["news_search"],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=4_000,
        tool_caps={"news_search": 8},
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_qualitative_researcher(
            agent_config=tight_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-test-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 7. Ticker validation — catalyst-watch ticker not in universe triggers a retry
# ---------------------------------------------------------------------------

def _payload_with_off_universe_ticker() -> dict[str, Any]:
    payload = _minimal_brief_payload("inv-test-001")
    payload["catalyst_watches"] = [
        {
            "catalyst_id": "QR-CW-1",
            "ticker": "OFFUNI",
            "catalyst_name": "FOMC decision",
            "hours_to_event": 36,
            "thesis_impact": "Held thesis names FOMC as catalyst.",
        }
    ]
    return payload


@pytest.mark.asyncio
async def test_off_universe_ticker_triggers_validation_retry(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """A catalyst-watch ticker not in *universe* fails validation and triggers a retry."""
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(_payload_with_off_universe_ticker())):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-test-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    # Retry message frames the structural-contract failure (validation, not parse).
    retry_prompt = captured_prompts[1]
    assert "structural contract" in retry_prompt.lower()
    assert "ticker" in retry_prompt.lower() or "OFFUNI" in retry_prompt


# ---------------------------------------------------------------------------
# 8. Diagnostic archive — files written under archive_root; None is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_written_when_archive_root_provided(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Diagnostic files written to archive_root/invocations/<id>/analysis/<agent>/."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-diag-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-diag-001" / "analysis" / "qualitative_researcher"
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
    tmp_path: Path,
) -> None:
    """Passing archive_root=None skips diagnostic writes and does not crash."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    sentinel = tmp_path / "should-not-exist"
    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-no-archive",
        session=session,
        universe=universe,
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    # Sanity check: no incidental writes to a sibling tmp path.
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# 9. Diagnostic archive on retry — retry file appears
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_include_retry_on_corrective_loop(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Retry path produces a response_retry.md and an error trail in errors.json."""
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-retry-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = (
        archive_root / "invocations" / "inv-retry-001" / "analysis" / "qualitative_researcher"
    )
    initial_text = (diag_dir / "response_initial.md").read_text()
    retry_text = (diag_dir / "response_retry.md").read_text()
    assert "structured_output not populated" in initial_text
    assert "QR-1" in retry_text
    errors = json.loads((diag_dir / "errors.json").read_text())
    assert any(e.get("attempt") == 1 for e in errors)


# ---------------------------------------------------------------------------
# 10. tool_calls_used populated when SDK emits ToolUseBlocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_used_counts_tool_use_blocks(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """When the SDK emits ToolUseBlocks, the harness counts them into tool_calls_used."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD, tool_use_blocks=3)])

    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-tools-001",
        session=session,
        universe=universe,
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
) -> None:
    """tool_calls_used sums across the initial attempt and the retry."""
    stub = _make_stub_query(
        [
            _make_sdk_response(None, tool_use_blocks=2),
            _make_sdk_response(_MINIMAL_BRIEF_PAYLOAD, tool_use_blocks=1),
        ]
    )

    result = await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-tools-retry",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.retry_count == 1
    assert result.tool_calls_used == 3


# ---------------------------------------------------------------------------
# 11. ClaudeAgentOptions structure — allowed_tools mirrors agent_config.tools,
#     max_turns supports the multi-turn tool-use loop, system_prompt/env pinned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_agent_options_structure(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """Pin the autonomous-agent contract on ClaudeAgentOptions.

    Catches a regression where someone disables tools, loads developer settings,
    drops the output-token cap, or reduces ``max_turns`` below the budget needed
    for the multi-turn tool-use loop.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-opt-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    assert len(captured_options) == 1
    options = captured_options[0]

    # allowed_tools uses the bundled CLI's MCP wire format —
    # ``mcp__<server>__<tool>`` — so the permission filter matches the
    # tool name the model emits when calling an SDK MCP-server tool.
    expected_allowed = [f"mcp__alphamind_qualitative__{name}" for name in agent_config.tools]
    assert options.allowed_tools == expected_allowed
    assert options.max_turns is not None
    assert options.max_turns >= agent_config.cumulative_tool_call_limit
    assert isinstance(options.system_prompt, str)
    assert options.system_prompt
    assert options.setting_sources == []
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


# ---------------------------------------------------------------------------
# 11b. mcp_servers registers the SDK MCP server with one tool per agent_config.tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_servers_populated_when_tools_configured(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """A non-empty agent_config.tools causes mcp_servers to register an SDK server.

    Without this registration the bundled SDK CLI returns "tool not found" when
    the model emits ``<tool_use name="news_search">``, and the agent silently
    falls back to its in-context bundle — defeating the qualitative-researcher's
    on-demand tool design.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-mcp-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]
    assert options.mcp_servers, "mcp_servers must be populated when tools are configured"
    assert "alphamind_qualitative" in options.mcp_servers
    server_config = options.mcp_servers["alphamind_qualitative"]
    # McpSdkServerConfig is a TypedDict with type/name/instance keys.
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_qualitative"


# ---------------------------------------------------------------------------
# 11c. mcp_servers is empty when no tools are configured (no spurious server)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_servers_empty_when_no_tools_configured(
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """A config with empty tools registers no MCP server — the agent runs tool-less."""
    no_tools_config = AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/qualitative_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=8_000,
        output_token_budget=1_000,
        tools=[],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=4_000,
        tool_caps={},
    )
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_qualitative_researcher(
        agent_config=no_tools_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-no-tools",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]
    assert options.allowed_tools == []
    assert options.mcp_servers == {}


# ---------------------------------------------------------------------------
# 11d. SDK MCP-server handlers preserve the ToolEnvelope (data_freshness, quality)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handlers_preserve_envelope_fields(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """The SDK MCP-server handler must surface ``data_freshness`` and ``quality``.

    Per ALP-111: every tool return must carry the envelope.  The MCP wrapping
    must not strip those fields — they must round-trip through the handler's
    JSON-serialisation.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-env-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    server_config = captured_options[0].mcp_servers["alphamind_qualitative"]
    server = server_config["instance"]

    # Drive the in-process MCP server's tools/list handler — registered tool
    # names must mirror agent_config.tools.
    from mcp.types import ListToolsRequest

    list_handler = server.request_handlers[ListToolsRequest]
    list_result = await list_handler(ListToolsRequest(method="tools/list"))
    registered_names = {t.name for t in list_result.root.tools}
    assert registered_names == set(agent_config.tools)

    # Invoke news_search through the SDK handler with empty input —
    # the tool returns UNAVAILABLE-with-envelope per its contract.
    payload = await _invoke_mcp_tool(server, "news_search", {})
    assert "data_freshness" in payload
    assert "quality" in payload


async def _invoke_mcp_tool(server: Any, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Drive the in-process MCP server's call_tool handler for *tool_name*.

    Returns the JSON payload extracted from the handler's content blocks.
    """
    import json as _json

    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool_name, arguments=args),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    content_blocks = result.root.content
    assert content_blocks, "tool returned no content blocks"
    block = content_blocks[0]
    payload = _json.loads(block.text)
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# 11e. SDK MCP-server handlers fail closed on invalid input — Pydantic raises,
#      and the SDK surfaces is_error=True (per ALP-111's fail-closed invariant).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handler_fails_closed_on_invalid_input(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """An invalid argument shape causes the handler to raise — surfaced as is_error.

    The handler must not silently coerce or swallow validation errors.  Per
    ALP-111's fail-closed propagation invariant the failure rides through.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_qualitative_researcher(
        agent_config=agent_config,
        user_message="Produce a qualitative brief.",
        invocation_id="inv-validate-001",
        session=session,
        universe=universe,
        archive_root=archive_root,
        sdk_query_fn=_capturing_stub,
    )

    server = captured_options[0].mcp_servers["alphamind_qualitative"]["instance"]

    from mcp.types import CallToolRequest, CallToolRequestParams

    # ``lookback_hours`` is typed ``int``; passing a non-numeric string fails
    # Pydantic validation.
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="news_search", arguments={"lookback_hours": "not-a-number"}
        ),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    assert result.root.isError, "validation failure must surface as is_error=True"


# ---------------------------------------------------------------------------
# 12. SDK auth failure → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_sdk_failure_naming_env_var(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """CLIConnectionError raised by the SDK becomes SDKFailure naming the OAuth env var."""
    from claude_agent_sdk import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-auth-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_auth_fail_stub,
        )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 13. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_query_fn_is_used_real_query_never_called(
    agent_config: AdaptiveAgentConfig,
    archive_root: Path,
    session: Session,
    universe: frozenset[str],
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="Produce a qualitative brief.",
            invocation_id="inv-stub-001",
            session=session,
            universe=universe,
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
) -> None:
    """The qualitative-researcher prompt file is read once per process."""
    from alphamind.analysis.qualitative_research import harness as harness_mod

    harness_mod._PROMPT_CACHE.clear()
    read_calls: list[str] = []
    original_read_text = Path.read_text

    def _counting_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "qualitative_researcher" in str(self):
            read_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _counting_read_text):
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="call 1",
            invocation_id="inv-cache-001",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)]),
        )
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="call 2",
            invocation_id="inv-cache-002",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)]),
        )

    prompt_reads = [p for p in read_calls if "qualitative_researcher" in p]
    assert len(prompt_reads) == 1, (
        f"Expected 1 prompt read, got {len(prompt_reads)}: {prompt_reads}"
    )
