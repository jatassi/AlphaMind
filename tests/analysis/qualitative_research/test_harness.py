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

_MINIMAL_BRIEF_TEXT = """\
QUALITATIVE BRIEF
Invocation: inv-test-001
Signal quality: HIGH

=== NARRATIVE THREADS ===
[QR-1] Prediction markets repricing toward higher FOMC-hold odds overnight.
  Relevance: financials
  Direction: bullish for soft-landing pricing
  Time horizon: immediate (<24h)
  Evidence:
    - prediction markets: hold odds 58 to 71 [from snapshot]
    - news: WSJ flagged dovish-leaning Fed speakers [from ND-M2]
  Implication: Bank-flow agents may not yet have repriced.

=== CATALYST WATCH ===

=== SENTIMENT SNAPSHOT ===
Extremes: NVDA at 91st percentile (positive)
Divergences: none
Regime: Sentiment broadly constructive.
"""


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
    text: str,
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
    tool_use_blocks: int = 0,
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields.

    When ``tool_use_blocks > 0``, an :class:`AssistantMessage` containing that
    many ``ToolUseBlock`` instances is emitted *before* the text-bearing
    assistant message, so harness-side tool counting can be exercised.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    messages: list[Any] = []

    if tool_use_blocks:
        tool_blocks: list[Any] = [
            ToolUseBlock(id=f"tu-{i}", name="news_search", input={"query": "FOMC"})
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
        content=[TextBlock(text=text)],
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

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
    assert result.raw_response == _MINIMAL_BRIEF_TEXT
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
    bad = "no structure"
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(bad)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_TEXT)):
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
    # Corrective-retry message structure: framing, contract ref, section directive.
    retry_prompt = captured_prompts[1]
    assert "qualitative researcher output" in retry_prompt.lower()
    assert "qualitative-research.md" in retry_prompt
    assert "=== NARRATIVE THREADS ===" in retry_prompt
    assert "=== CATALYST WATCH ===" in retry_prompt
    assert "=== SENTIMENT SNAPSHOT ===" in retry_prompt


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
    bad_initial = "first malformed response"
    bad_retry = "second malformed response"
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
    assert err.raw_response_initial == bad_initial
    assert err.raw_response_retry == bad_retry
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
    truncated = "QUALITATIVE BRIEF\nInvocation: x\nSignal quality: HIGH"
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response(truncated, stop_reason="max_tokens")):
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
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_TEXT)):
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

_BRIEF_WITH_OFF_UNIVERSE_TICKER = """\
QUALITATIVE BRIEF
Invocation: inv-test-001
Signal quality: HIGH

=== NARRATIVE THREADS ===
[QR-1] FOMC pricing.
  Relevance: financials
  Direction: bullish for soft-landing pricing
  Time horizon: immediate (<24h)
  Evidence:
    - prediction markets: hold odds 58 to 71 [from snapshot]
    - news: WSJ flagged dovish-leaning Fed speakers [from ND-M2]
  Implication: Bank-flow agents may not yet have repriced.

=== CATALYST WATCH ===
[QR-CW-1] OFFUNI: FOMC decision in 36h
  Thesis impact: Held thesis names FOMC as catalyst.

=== SENTIMENT SNAPSHOT ===
Extremes: NVDA at 91st percentile (positive)
Divergences: none
Regime: Sentiment broadly constructive.
"""


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
            async for msg in _async_iter(_make_sdk_response(_BRIEF_WITH_OFF_UNIVERSE_TICKER)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_TEXT)):
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

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
    bad = "no structure"
    stub = _make_stub_query([_make_sdk_response(bad), _make_sdk_response(_MINIMAL_BRIEF_TEXT)])

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
    assert (diag_dir / "response_initial.md").read_text() == bad
    assert (diag_dir / "response_retry.md").read_text() == _MINIMAL_BRIEF_TEXT
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT, tool_use_blocks=3)])

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
    bad = "no structure"
    stub = _make_stub_query(
        [
            _make_sdk_response(bad, tool_use_blocks=2),
            _make_sdk_response(_MINIMAL_BRIEF_TEXT, tool_use_blocks=1),
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
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_TEXT)):
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

    assert options.allowed_tools == list(agent_config.tools)
    assert options.max_turns is not None
    assert options.max_turns >= agent_config.cumulative_tool_call_limit
    assert isinstance(options.system_prompt, str)
    assert options.system_prompt
    assert options.setting_sources == []
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

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
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)]),
        )
        await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message="call 2",
            invocation_id="inv-cache-002",
            session=session,
            universe=universe,
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)]),
        )

    prompt_reads = [p for p in read_calls if "qualitative_researcher" in p]
    assert len(prompt_reads) == 1, (
        f"Expected 1 prompt read, got {len(prompt_reads)}: {prompt_reads}"
    )
