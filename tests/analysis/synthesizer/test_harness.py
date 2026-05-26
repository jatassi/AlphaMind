"""Tests for synthesizer LLM invocation harness — story 08 (ALP-208).

Tests are behaviour-driven through the public interface only:
``invoke_synthesizer`` and the exception hierarchy.  The SDK is stubbed via the
``sdk_query_fn`` dependency-injection parameter — no test calls the real
Anthropic API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.harness import (
    ContextOverflowFailure,
    EmptyResponseFailure,
    HarnessFailure,
    HarnessSuccess,
    SDKFailure,
    TimeoutFailure,
    invoke_synthesizer,
)
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)

# Canonical test invocation timestamp; date partition is "2026-05-01".
_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


_SYNTHESIS_TEXT = """\
=== REGIME CONTEXT ===
vol_normalization with steady-state transition.

=== CROSS-SOURCE CONNECTIONS ===
Funding-stress signal in financials [SA-FIN-3] aligns with prediction-market
repricing [QR-1] — both point to FOMC sensitivity.

=== CONTRADICTIONS ===
none

=== HIDDEN UNCERTAINTY ===
Adaptive thread [AR-2] flags an unresolved earnings question for NVDA.
"""


@pytest.fixture()
def agent_config() -> BaseAgentConfig:
    """A BaseAgentConfig pointing to the synthesizer prompt with no tools."""
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/synthesizer.md",
        latency_budget_seconds=30,
        context_token_budget=12_000,
        output_token_budget=2_000,
        tools=[],
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


class _StubPortfolioReader:
    """Minimal stub satisfying SynthesizerPortfolioStateReader."""

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


@pytest.fixture()
def portfolio_reader() -> _StubPortfolioReader:
    return _StubPortfolioReader()


def _make_sdk_response(
    text: str,
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
    tool_use_blocks: int = 0,
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    messages: list[Any] = []

    if tool_use_blocks:
        tool_blocks: list[Any] = [
            ToolUseBlock(
                id=f"tu-{i}",
                name="get_positions_summary",
                input={},
            )
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
# 1. Happy path — non-empty response with end_turn returns HarnessSuccess
# ---------------------------------------------------------------------------


async def test_happy_path_returns_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """A non-empty SDK response paired with stop_reason=end_turn returns
    HarnessSuccess carrying the response text."""
    stub = _make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT)])

    result = await invoke_synthesizer(
        agent_config=agent_config,
        user_message="Synthesize the briefs.",
        invocation_id="inv-test-001",
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert result.response_text == _SYNTHESIS_TEXT
    assert result.stop_reason == "end_turn"
    assert result.tool_calls_used == 0
    assert result.wall_clock_seconds >= 0.0
    assert isinstance(result.tokens_used, TokensUsed)


# ---------------------------------------------------------------------------
# 2. Empty response + end_turn raises EmptyResponseFailure
# ---------------------------------------------------------------------------


async def test_empty_end_turn_raises_empty_response(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """An empty response with stop_reason=end_turn raises EmptyResponseFailure
    carrying the raw response text and the agent identifier."""
    stub = _make_stub_query([_make_sdk_response("", stop_reason="end_turn")])

    with pytest.raises(EmptyResponseFailure) as exc_info:
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-empty-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    assert err.agent_name == "synthesizer"
    assert err.invocation_id == "inv-empty-001"
    assert err.raw_response == ""


# ---------------------------------------------------------------------------
# 3. Empty response + max_tokens raises ContextOverflowFailure
# ---------------------------------------------------------------------------


async def test_empty_max_tokens_raises_context_overflow(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """An empty response with stop_reason=max_tokens raises ContextOverflowFailure
    immediately — no retry path exists for the synthesizer."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response("", stop_reason="max_tokens")):
            yield msg

    with pytest.raises(ContextOverflowFailure):
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-overflow-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "no retry must be attempted on the synthesizer harness"


# ---------------------------------------------------------------------------
# 4. Non-empty + max_tokens returns HarnessSuccess (truncated prose accepted)
# ---------------------------------------------------------------------------


async def test_nonempty_max_tokens_returns_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """A non-empty response with stop_reason=max_tokens returns HarnessSuccess —
    Layer 4 only blocks empty output, since there's no schema to break."""
    truncated_text = "=== REGIME CONTEXT === vol_normalization. (response cut off)"
    stub = _make_stub_query([_make_sdk_response(truncated_text, stop_reason="max_tokens")])

    result = await invoke_synthesizer(
        agent_config=agent_config,
        user_message="Synthesize.",
        invocation_id="inv-trunc-001",
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    assert result.response_text == truncated_text
    assert result.stop_reason == "max_tokens"


# ---------------------------------------------------------------------------
# 5. Auth failure → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


async def test_auth_failure_raises_sdk_failure(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """CLIConnectionError raised by the SDK becomes SDKFailure naming the OAuth env var."""
    from claude_agent_sdk import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make this a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-auth-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_auth_fail_stub,
        )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 6. Timeout exceeded → TimeoutFailure
# ---------------------------------------------------------------------------


async def test_timeout_raises_timeout_failure(
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """A slow SDK stub causes the harness to raise TimeoutFailure."""
    tight_config = BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/synthesizer.md",
        latency_budget_seconds=1,
        context_token_budget=12_000,
        output_token_budget=2_000,
        tools=[],
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_synthesizer(
            agent_config=tight_config,
            user_message="Synthesize.",
            invocation_id="inv-timeout-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 7. Portfolio-state MCP tools wired into ClaudeAgentOptions
# ---------------------------------------------------------------------------


async def test_portfolio_tools_wired_into_options(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """allowed_tools contains the three portfolio-state tool names; mcp_servers
    registers the alphamind_synthesizer_portfolio server."""
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_SYNTHESIS_TEXT)):
            yield msg

    await invoke_synthesizer(
        agent_config=agent_config,
        user_message="Synthesize.",
        invocation_id="inv-tools-001",
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=_capturing_stub,
    )

    options = captured_options[0]
    expected = {
        "mcp__alphamind_synthesizer_portfolio__get_positions_summary",
        "mcp__alphamind_synthesizer_portfolio__get_active_theses_summary",
        "mcp__alphamind_synthesizer_portfolio__get_exposure_snapshot",
    }
    assert set(options.allowed_tools) == expected
    assert "alphamind_synthesizer_portfolio" in options.mcp_servers
    assert options.setting_sources == []
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


# ---------------------------------------------------------------------------
# 8. Diagnostic archive written on success
# ---------------------------------------------------------------------------


async def test_diagnostic_archive_written_on_success(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """Successful invocation writes prompt.md, user_message.md, response.md,
    errors.json, and metadata.json under the standard archive path."""
    stub = _make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT)])

    await invoke_synthesizer(
        agent_config=agent_config,
        user_message="Synthesize the briefs.",
        invocation_id="inv-archive-success",
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "2026-05-01" / "inv-archive-success" / "analysis" / "synthesizer"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    assert (diag_dir / "response.md").read_text(encoding="utf-8") == _SYNTHESIS_TEXT


# ---------------------------------------------------------------------------
# 9. Diagnostic archive written on failure
# ---------------------------------------------------------------------------


async def test_diagnostic_archive_written_on_failure(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """A failing invocation still writes the diagnostic archive."""
    stub = _make_stub_query([_make_sdk_response("", stop_reason="end_turn")])

    with pytest.raises(EmptyResponseFailure):
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-archive-failure",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    diag_dir = archive_root / "2026-05-01" / "inv-archive-failure" / "analysis" / "synthesizer"
    assert (diag_dir / "prompt.md").exists()
    assert (diag_dir / "user_message.md").exists()
    assert (diag_dir / "response.md").exists()
    assert (diag_dir / "errors.json").exists()
    assert (diag_dir / "metadata.json").exists()
    meta = json.loads((diag_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["success"] is False


# ---------------------------------------------------------------------------
# 10. metadata.json includes tool_calls_used, tokens_used, stop_reason,
#     wall_clock_seconds
# ---------------------------------------------------------------------------


async def test_metadata_json_carries_required_fields(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """metadata.json contains tool_calls_used, tokens_used, stop_reason,
    wall_clock_seconds — the documented diagnostic-record contract."""
    stub = _make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT, tool_use_blocks=2)])

    await invoke_synthesizer(
        agent_config=agent_config,
        user_message="Synthesize.",
        invocation_id="inv-meta-001",
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    meta_path = archive_root / "2026-05-01" / "inv-meta-001" / "analysis" / "synthesizer"
    meta = json.loads((meta_path / "metadata.json").read_text(encoding="utf-8"))
    assert meta["tool_calls_used"] == 2
    assert "tokens_used" in meta
    assert isinstance(meta["tokens_used"], dict)
    assert meta["stop_reason"] == "end_turn"
    assert isinstance(meta["wall_clock_seconds"], (int, float))
    assert meta["wall_clock_seconds"] >= 0.0


# ---------------------------------------------------------------------------
# 11. System prompt cached per process
# ---------------------------------------------------------------------------


async def test_system_prompt_cached_per_process(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """The synthesizer prompt file is read once per process; subsequent
    invocations hit the cache."""
    from alphamind.analysis.synthesizer import harness as harness_mod

    harness_mod._PROMPT_CACHE.clear()
    read_calls: list[str] = []
    original_read_text = Path.read_text

    def _counting_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "synthesizer.md" in str(self):
            read_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _counting_read_text):
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="call 1",
            invocation_id="inv-cache-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT)]),
        )
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="call 2",
            invocation_id="inv-cache-002",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT)]),
        )

    prompt_reads = [p for p in read_calls if "synthesizer.md" in p]
    assert len(prompt_reads) == 1, (
        f"Expected 1 prompt read, got {len(prompt_reads)}: {prompt_reads}"
    )


# ---------------------------------------------------------------------------
# 12. No corrective retry path — failure raises immediately
# ---------------------------------------------------------------------------


async def test_no_corrective_retry_path(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """An EmptyResponseFailure raises immediately without a second SDK call —
    the synthesizer harness has no parser/validator/retry path."""
    call_count = 0

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        async for msg in _async_iter(_make_sdk_response("", stop_reason="end_turn")):
            yield msg

    with pytest.raises(EmptyResponseFailure):
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-no-retry-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_stub,
        )

    assert call_count == 1, "no retry must be attempted"


# ---------------------------------------------------------------------------
# 13. Real SDK is never invoked when sdk_query_fn is supplied
# ---------------------------------------------------------------------------


async def test_real_sdk_never_called_when_stub_supplied(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    portfolio_reader: _StubPortfolioReader,
) -> None:
    """When sdk_query_fn is supplied, the real claude_agent_sdk.query is never called."""
    stub = _make_stub_query([_make_sdk_response(_SYNTHESIS_TEXT)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_synthesizer(
            agent_config=agent_config,
            user_message="Synthesize.",
            invocation_id="inv-stub-001",
            portfolio_reader=portfolio_reader,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )
        mock_real.assert_not_called()
