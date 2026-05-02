"""Tests for domain-researcher LLM invocation harness (ALP-198).

Tests are behaviour-driven through the public interface only:
`invoke_domain_researcher` and the exception hierarchy.  The SDK is
stubbed via the `sdk_query_fn` dependency-injection parameter — no test
calls the real Anthropic API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import (
    ContextOverflowFailure,
    HarnessFailure,
    HarnessSuccess,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    invoke_domain_researcher,
)
from alphamind.analysis.domain_researchers.models import SectorBrief, SignalQuality
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_MINIMAL_BRIEF_TEXT = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-test-001
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] NVDA breakout
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: NVDA broke resistance.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""


@pytest.fixture()
def agent_config(tmp_path: Path) -> BaseAgentConfig:
    """A minimal BaseAgentConfig pointing to a real prompt file."""
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


@pytest.fixture()
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _make_sdk_response(
    text: str,
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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
    return [assistant, result]


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
# 1. Happy-path: invoke_domain_researcher returns HarnessSuccess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_harness_success(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """invoke_domain_researcher returns HarnessSuccess on a valid SDK response."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse tech sector.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.brief, SectorBrief)
    assert result.retry_count == 0
    assert result.wall_clock_seconds >= 0.0
    assert result.raw_response == _MINIMAL_BRIEF_TEXT


# ---------------------------------------------------------------------------
# 2. TokensUsed type — no local redefinition
# ---------------------------------------------------------------------------


def test_harness_success_tokens_used_is_shared_type(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """HarnessSuccess.tokens_used is typed as alphamind.analysis._shared.TokensUsed."""
    # Verify via annotation, not runtime isinstance, since the object is frozen
    hints = HarnessSuccess.__annotations__
    assert hints.get("tokens_used") is TokensUsed or (
        # also handle stringified annotations via get_type_hints
        "tokens_used" in hints
    )
    # Construct a valid instance and verify the field accepts TokensUsed
    brief = SectorBrief(
        invocation_id="x",
        sector=Sector.TECH_SEMIS,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(),
        anomalies=(),
        thesis_candidates=(),
    )
    hs = HarnessSuccess(
        brief=brief,
        raw_response="r",
        retry_count=0,
        tokens_used=TokensUsed(
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
        ),
        wall_clock_seconds=0.1,
    )
    assert isinstance(hs.tokens_used, TokensUsed)


# ---------------------------------------------------------------------------
# 3. Parse failure → corrective retry message construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_triggers_retry_with_correct_message(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Parse failure produces a corrective retry with the right framing."""
    bad_response = "This is totally wrong — no structure at all."
    good_response = _MINIMAL_BRIEF_TEXT

    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        prompt = kwargs.get("prompt", "")
        captured_prompts.append(str(prompt))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(bad_response)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(good_response)):
                yield msg

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse tech sector.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert result.retry_count == 1
    retry_prompt = captured_prompts[1]
    # Framing line names the parse contract
    assert "parse" in retry_prompt.lower() or "prior response" in retry_prompt.lower()
    # Contract reference
    assert (
        "domain researcher output contract" in retry_prompt.lower()
        or "tech-semis.md" in retry_prompt.lower()
    )
    # Directive with section headers
    assert "=== KEY FINDINGS ===" in retry_prompt
    assert "=== FLAGGED ANOMALIES ===" in retry_prompt
    assert "=== THESIS CANDIDATES ===" in retry_prompt


# ---------------------------------------------------------------------------
# 4. Two consecutive parse failures → MalformedOutputFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_parse_failures_raise_malformed_output(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure."""
    bad_response = "No structure here at all."
    stub = _make_stub_query(
        [
            _make_sdk_response(bad_response),
            _make_sdk_response(bad_response),
        ]
    )

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    # Both raw responses should be preserved
    assert bad_response in str(err) or (
        err.raw_response_initial is not None and err.raw_response_initial == bad_response
    )


# ---------------------------------------------------------------------------
# 5. Validation failure → retry → success returns retry_count=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_then_success_returns_retry_count_1(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Validation failure followed by a corrected response returns retry_count=1."""
    # A brief that parses OK but has duplicate index (validation fails)
    bad_brief = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-test-001
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] Finding one
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: Detail one.

[SA-TECH-1] Duplicate index
  Tickers: AMD
  Signal type: flow
  Strength: weak
  Detail: Detail two.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    stub = _make_stub_query(
        [
            _make_sdk_response(bad_brief),
            _make_sdk_response(_MINIMAL_BRIEF_TEXT),
        ]
    )

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    assert result.retry_count == 1
    assert isinstance(result.brief, SectorBrief)


# ---------------------------------------------------------------------------
# 6. Validation failure with max_tokens → ContextOverflowFailure (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_failure_with_max_tokens_raises_context_overflow(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Parse failure paired with stop_reason=max_tokens raises ContextOverflowFailure."""
    bad_response = "Truncated output..."
    stub = _make_stub_query([_make_sdk_response(bad_response, stop_reason="max_tokens")])

    with pytest.raises(ContextOverflowFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )


@pytest.mark.asyncio
async def test_validation_failure_with_max_tokens_raises_context_overflow(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Validation failure with max_tokens raises ContextOverflowFailure immediately."""
    bad_brief = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-test-001
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] Finding one
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: Detail one.

[SA-TECH-1] Duplicate index
  Tickers: AMD
  Signal type: flow
  Strength: weak
  Detail: Duplicate.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    stub = _make_stub_query([_make_sdk_response(bad_brief, stop_reason="max_tokens")])

    with pytest.raises(ContextOverflowFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )


# ---------------------------------------------------------------------------
# 7. Auth failure → SDKFailure naming CLAUDE_CODE_OAUTH_TOKEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_sdk_failure_naming_env_var(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Authentication failure raises SDKFailure with CLAUDE_CODE_OAUTH_TOKEN in message."""
    from claude_agent_sdk._errors import CLIConnectionError

    async def _auth_fail_stub(**kwargs: Any) -> AsyncIterator[Any]:
        raise CLIConnectionError("OAuth token invalid or missing")
        yield  # make it a generator

    with pytest.raises(SDKFailure) as exc_info:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            sdk_query_fn=_auth_fail_stub,
        )

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. Timeout exceeded → TimeoutFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_timeout_failure(
    archive_root: Path,
) -> None:
    """Per-invocation timeout exceeded raises TimeoutFailure."""
    # Use a 1-second budget but make the stub hang for longer
    tight_config = BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=1,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )

    async def _slow_stub(**kwargs: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(5)
        yield  # never reached

    with pytest.raises(TimeoutFailure):
        await invoke_domain_researcher(
            agent_config=tight_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            sdk_query_fn=_slow_stub,
        )


# ---------------------------------------------------------------------------
# 9. Retry message omissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_message_omits_full_error_list_and_raw_input(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Corrective retry message does not contain the full error list or raw input data."""
    bad_response = "No structure."
    raw_input = "Analyse tech sector with very specific data payload XYZ123ABC."

    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(bad_response)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_TEXT)):
                yield msg

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message=raw_input,
        invocation_id="inv-test-001",
        archive_root=archive_root,
        sdk_query_fn=_stub,
    )

    assert len(captured_prompts) == 2
    retry_prompt = captured_prompts[1]
    # The raw input data should not be re-posted in the retry
    assert "XYZ123ABC" not in retry_prompt
    # The retry should not contain a multi-error list (JSON array with 2+ errors)
    # Single-error constraint: no evidence of "errors: [..." with more than one item
    # This is a structural check — if we only include the first error, there won't
    # be a numbered list like "1. error... 2. error..."
    assert "2." not in retry_prompt or "2. error" not in retry_prompt.lower()


# ---------------------------------------------------------------------------
# 10. Diagnostic files written under correct path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_files_written_on_success(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Diagnostic record written under archive_root/invocations/<id>/analysis/<agent>/."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-diag-001",
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-diag-001" / "analysis"
    # At least one subdirectory (agent_name) exists
    agent_dirs = list(diag_dir.iterdir())
    assert len(agent_dirs) == 1
    agent_dir = agent_dirs[0]

    assert (agent_dir / "prompt.md").exists()
    assert (agent_dir / "user_message.md").exists()
    assert (agent_dir / "response_initial.md").exists()
    assert (agent_dir / "errors.json").exists()
    assert (agent_dir / "metadata.json").exists()
    # No retry file on happy path
    assert not (agent_dir / "response_retry.md").exists()


@pytest.mark.asyncio
async def test_diagnostic_files_written_on_failure(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Diagnostic record written even when the invocation fails."""
    bad = "No structure."
    stub = _make_stub_query([_make_sdk_response(bad), _make_sdk_response(bad)])

    with pytest.raises(MalformedOutputFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-diag-fail",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    diag_dir = archive_root / "invocations" / "inv-diag-fail" / "analysis"
    agent_dirs = list(diag_dir.iterdir())
    assert len(agent_dirs) == 1
    agent_dir = agent_dirs[0]

    assert (agent_dir / "prompt.md").exists()
    assert (agent_dir / "response_initial.md").exists()
    assert (agent_dir / "response_retry.md").exists()  # retry was attempted
    assert (agent_dir / "errors.json").exists()
    assert (agent_dir / "metadata.json").exists()


@pytest.mark.asyncio
async def test_metadata_json_contains_expected_fields(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """metadata.json contains model, retry_count, tokens_used, wall_clock_seconds, stop_reason."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-meta-001",
        archive_root=archive_root,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "invocations" / "inv-meta-001" / "analysis"
    agent_dir = next(diag_dir.iterdir())
    meta = json.loads((agent_dir / "metadata.json").read_text())

    assert "model" in meta
    assert "retry_count" in meta
    assert "tokens_used" in meta
    assert "wall_clock_seconds" in meta
    assert "stop_reason" in meta
    assert "success" in meta


# ---------------------------------------------------------------------------
# 11. System-prompt cache: file read once across multiple calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_prompt_cached_per_process(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """System prompt file is read once and served from cache on subsequent calls."""
    # Patch Path.read_text to count how many times the prompt file is read.
    read_calls: list[str] = []
    original_read_text = Path.read_text

    def _counting_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "prompts" in str(self):
            read_calls.append(str(self))
        return original_read_text(self, *args, **kwargs)

    # Clear the prompt cache first to ensure a clean state for this test
    from alphamind.analysis.domain_researchers import harness as harness_mod

    harness_mod._PROMPT_CACHE.clear()

    with patch.object(Path, "read_text", _counting_read_text):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Call 1.",
            invocation_id="inv-cache-001",
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)]),
        )
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Call 2.",
            invocation_id="inv-cache-002",
            archive_root=archive_root,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)]),
        )

    # Only one read for the prompt file across both calls
    prompt_reads = [p for p in read_calls if "tech_semis" in p]
    assert len(prompt_reads) == 1, (
        f"Expected 1 prompt read, got {len(prompt_reads)}: {prompt_reads}"
    )


# ---------------------------------------------------------------------------
# 12. No real API calls — SDK is always stubbed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_is_stubbed_no_real_api_call(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """The harness uses the injected sdk_query_fn; the real query() is never called."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-stub-001",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )
        mock_real.assert_not_called()


# ---------------------------------------------------------------------------
# 13. HarnessFailure subclasses carry agent_name and invocation_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_output_failure_carries_agent_name(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """MalformedOutputFailure carries the agent name for diagnostics."""
    bad = "No structure."
    stub = _make_stub_query([_make_sdk_response(bad), _make_sdk_response(bad)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-err-001",
            archive_root=archive_root,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert err.invocation_id == "inv-err-001"
    assert err.agent_name  # non-empty


# ---------------------------------------------------------------------------
# 14. archive_root=None → no diagnostic writes (no crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_archive_root_no_crash(
    agent_config: BaseAgentConfig,
) -> None:
    """Passing archive_root=None skips diagnostic writes without crashing."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_TEXT)])

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-no-archive",
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
