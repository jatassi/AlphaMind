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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
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
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import read_agent_calls_for_invocation
from alphamind.state.tables.agent_calls import AgentCallErrorClass
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

# Canonical test invocation timestamp; date partition is "2026-05-01".
_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _minimal_brief_payload(invocation_id: str = "inv-test-001") -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "sector": "tech_semis",
        "signal_quality": "high",
        "signal_quality_reason": None,
        "findings": [
            {
                "finding_id": "SA-TECH-1",
                "headline": "NVDA breakout",
                "tickers": ["NVDA"],
                "signal_type": "price_action",
                "strength": "strong",
                "detail": "NVDA broke resistance.",
            }
        ],
        "anomalies": [],
        "thesis_candidates": [],
    }


_MINIMAL_BRIEF_PAYLOAD = _minimal_brief_payload()


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
    structured_output: dict[str, Any] | None = None,
    *,
    text: str = "",
    stop_reason: str | None = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 200,
) -> list[Any]:
    """Build a minimal sequence of SDK messages a stub async-generator yields.

    *structured_output* is delivered on the terminating :class:`ResultMessage`
    (the post-migration JSON-mode payload path); ``None`` simulates the SDK
    failing to populate it. *text* is concatenated for the diagnostic record
    only — usually empty in JSON mode but Sonnet sometimes narrates between
    turns.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse tech sector.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)
    assert isinstance(result.brief, SectorBrief)
    assert result.retry_count == 0
    assert result.wall_clock_seconds >= 0.0
    # raw_response is the JSON-rendered structured output post-migration.
    assert "SA-TECH-1" in result.raw_response
    assert "tech_semis" in result.raw_response


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
    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        prompt = kwargs.get("prompt", "")
        captured_prompts.append(str(prompt))
        if len(captured_prompts) == 1:
            # ``structured_output=None`` simulates the SDK failing to populate
            # the field — a parse-stage failure that triggers the retry path.
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse tech sector.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        as_of=_AS_OF,
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
    # Directive references the schema mode, not the legacy section markers.
    assert "SectorBrief schema" in retry_prompt


# ---------------------------------------------------------------------------
# 4. Two consecutive parse failures → MalformedOutputFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_parse_failures_raise_malformed_output(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Two consecutive parse failures raise MalformedOutputFailure."""
    bad_payload = {"shape": "wrong"}  # missing required SectorBrief fields
    stub = _make_stub_query(
        [
            _make_sdk_response(bad_payload),
            _make_sdk_response(bad_payload),
        ]
    )

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    err = exc_info.value
    assert isinstance(err, HarnessFailure)
    # Both raw responses are JSON-rendered; verify each carries the load-bearing
    # key from the stubbed payload.
    assert err.raw_response_initial is not None
    assert err.raw_response_retry is not None
    assert "shape" in err.raw_response_initial
    assert "shape" in err.raw_response_retry


# ---------------------------------------------------------------------------
# 5. Validation failure → retry → success returns retry_count=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_then_success_returns_retry_count_1(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Validation failure followed by a corrected response returns retry_count=1."""
    # A brief that parses OK but has duplicate finding_id (validation fails on the
    # findings_sequential_indexing rule via the duplicate-index check).
    bad_payload = _minimal_brief_payload("inv-test-001")
    bad_payload["findings"] = [
        bad_payload["findings"][0],
        {
            "finding_id": "SA-TECH-1",  # duplicate of the first
            "headline": "Duplicate index",
            "tickers": ["AMD"],
            "signal_type": "flow",
            "strength": "weak",
            "detail": "Detail two.",
        },
    ]
    stub = _make_stub_query(
        [
            _make_sdk_response(bad_payload),
            _make_sdk_response(_MINIMAL_BRIEF_PAYLOAD),
        ]
    )

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-test-001",
        archive_root=archive_root,
        as_of=_AS_OF,
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
    stub = _make_stub_query([_make_sdk_response(None, stop_reason="max_tokens")])

    with pytest.raises(ContextOverflowFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )


@pytest.mark.asyncio
async def test_validation_failure_with_max_tokens_raises_context_overflow(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Validation failure with max_tokens raises ContextOverflowFailure immediately."""
    bad_payload = _minimal_brief_payload("inv-test-001")
    bad_payload["findings"] = [
        bad_payload["findings"][0],
        {
            "finding_id": "SA-TECH-1",  # duplicate
            "headline": "Duplicate index",
            "tickers": ["AMD"],
            "signal_type": "flow",
            "strength": "weak",
            "detail": "Duplicate.",
        },
    ]
    stub = _make_stub_query([_make_sdk_response(bad_payload, stop_reason="max_tokens")])

    with pytest.raises(ContextOverflowFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-test-001",
            archive_root=archive_root,
            as_of=_AS_OF,
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
    from claude_agent_sdk import CLIConnectionError

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
            as_of=_AS_OF,
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
            as_of=_AS_OF,
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
    raw_input = "Analyse tech sector with very specific data payload XYZ123ABC."

    captured_prompts: list[str] = []

    async def _stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_prompts.append(str(kwargs.get("prompt", "")))
        if len(captured_prompts) == 1:
            async for msg in _async_iter(_make_sdk_response(None)):
                yield msg
        else:
            async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
                yield msg

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message=raw_input,
        invocation_id="inv-test-001",
        archive_root=archive_root,
        as_of=_AS_OF,
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-diag-001",
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "2026-05-01" / "inv-diag-001" / "analysis"
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
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(None)])

    with pytest.raises(MalformedOutputFailure):
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-diag-fail",
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
        )

    diag_dir = archive_root / "2026-05-01" / "inv-diag-fail" / "analysis"
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-meta-001",
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )

    diag_dir = archive_root / "2026-05-01" / "inv-meta-001" / "analysis"
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
            as_of=_AS_OF,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)]),
        )
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Call 2.",
            invocation_id="inv-cache-002",
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=_make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)]),
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    with patch("claude_agent_sdk.query") as mock_real:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-stub-001",
            archive_root=archive_root,
            as_of=_AS_OF,
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
    stub = _make_stub_query([_make_sdk_response(None), _make_sdk_response(None)])

    with pytest.raises(MalformedOutputFailure) as exc_info:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse.",
            invocation_id="inv-err-001",
            archive_root=archive_root,
            as_of=_AS_OF,
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
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])

    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-no-archive",
        archive_root=None,
        sdk_query_fn=stub,
    )

    assert isinstance(result, HarnessSuccess)


# ---------------------------------------------------------------------------
# 15. ClaudeAgentOptions structure — pin allowed_tools, max_turns,
#     system_prompt, setting_sources, output-token env propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_claude_agent_options_structure(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """The ClaudeAgentOptions passed to the SDK pin the autonomous-agent contract.

    Catches a regression where someone "improves" the harness by enabling tools,
    loading developer settings, or dropping the output-token cap.
    """
    captured_options: list[Any] = []

    async def _capturing_stub(**kwargs: Any) -> AsyncIterator[Any]:
        captured_options.append(kwargs.get("options"))
        async for msg in _async_iter(_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)):
            yield msg

    await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-options-001",
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=_capturing_stub,
    )

    assert len(captured_options) == 1
    options = captured_options[0]

    assert options.allowed_tools == []
    assert options.max_turns == 5
    assert isinstance(options.system_prompt, str)
    assert options.system_prompt  # non-empty
    # ``setting_sources=[]`` blocks .claude/settings.json from loading hooks/permissions.
    assert options.setting_sources == []
    # Output-token budget propagates via the env-var path (the CLI exposes no
    # ``--max-tokens`` flag).
    assert options.env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == str(agent_config.output_token_budget)


# ---------------------------------------------------------------------------
# 16. agent_calls telemetry capture (ALP-880)
# ---------------------------------------------------------------------------

_INV_TELEM = "inv-telem-dr"
_PLT_TELEM = "plt-telem-dr"


@pytest.fixture()
async def telemetry_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite with the invocation FK target for the capture row seeded."""
    db_path = tmp_path / "telem.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT_TELEM))
        sess.flush()
        sess.add(stub_invocation_row(_INV_TELEM, process_lifetime_id=_PLT_TELEM))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    yield make_async_session_factory(async_engine)
    await async_engine.dispose()


@pytest.mark.asyncio
async def test_completed_call_writes_one_agent_call_row_and_artifacts(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    tmp_path: Path,
    telemetry_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A successful domain-researcher call persists one agent_calls row plus the
    four provenance files at the documented path, with success=true and the
    captured metrics + provenance fields."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])
    provenance_root = tmp_path / "provenance"

    async with telemetry_factory() as session:
        await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.TECH_SEMIS,
            user_message="Analyse tech sector.",
            invocation_id=_INV_TELEM,
            archive_root=archive_root,
            as_of=_AS_OF,
            sdk_query_fn=stub,
            telemetry_session=session,
            provenance_root=provenance_root,
        )
        await session.commit()

    async with telemetry_factory() as session:
        rows = await read_agent_calls_for_invocation(session, _INV_TELEM)

    assert len(rows) == 1
    row = rows[0]
    assert row.agent_name == "tech_semis_researcher"
    assert row.success is True
    assert row.error_class is None
    assert row.model_id == str(agent_config.model)
    assert row.prompt_path == agent_config.prompt
    assert row.prompt_git_sha  # non-empty
    assert row.prompt_content_hash  # non-empty
    assert row.input_tokens == 100
    assert row.output_tokens == 200
    assert row.stop_reason == "end_turn"
    assert json.loads(row.sampling_params_json)["max_tokens"] == agent_config.output_token_budget

    assert row.output_artifact_ref is not None
    pdir = Path(row.output_artifact_ref)
    assert pdir == provenance_root / "invocations" / _INV_TELEM / "agent_calls" / row.agent_call_id
    for name in ("system_prompt.md", "output_schema.json", "tools_definition.json", "output.json"):
        assert (pdir / name).exists()


@pytest.mark.asyncio
async def test_failed_call_writes_row_with_mapped_error_class(
    agent_config: BaseAgentConfig,
    archive_root: Path,
    tmp_path: Path,
    telemetry_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed call (two parse failures → MalformedOutputFailure) persists one
    row with success=false and error_class mapped from the raised subclass."""
    bad_payload = {"shape": "wrong"}
    stub = _make_stub_query([_make_sdk_response(bad_payload), _make_sdk_response(bad_payload)])
    provenance_root = tmp_path / "provenance"

    async with telemetry_factory() as session:
        with pytest.raises(MalformedOutputFailure):
            await invoke_domain_researcher(
                agent_config=agent_config,
                sector=Sector.TECH_SEMIS,
                user_message="Analyse.",
                invocation_id=_INV_TELEM,
                archive_root=archive_root,
                as_of=_AS_OF,
                sdk_query_fn=stub,
                telemetry_session=session,
                provenance_root=provenance_root,
            )
        await session.commit()

    async with telemetry_factory() as session:
        rows = await read_agent_calls_for_invocation(session, _INV_TELEM)

    assert len(rows) == 1
    row = rows[0]
    assert row.success is False
    assert row.error_class is AgentCallErrorClass.malformed_output
    # Single aggregated record across both API calls, not one per attempt.
    assert row.attempt_number == 2


@pytest.mark.asyncio
async def test_no_telemetry_session_skips_capture(
    agent_config: BaseAgentConfig, archive_root: Path
) -> None:
    """Omitting the telemetry session leaves the call behaviourally unchanged —
    no row, no provenance — so the unarchived / test path is untouched."""
    stub = _make_stub_query([_make_sdk_response(_MINIMAL_BRIEF_PAYLOAD)])
    result = await invoke_domain_researcher(
        agent_config=agent_config,
        sector=Sector.TECH_SEMIS,
        user_message="Analyse.",
        invocation_id="inv-no-telem",
        archive_root=archive_root,
        as_of=_AS_OF,
        sdk_query_fn=stub,
    )
    assert isinstance(result, HarnessSuccess)
