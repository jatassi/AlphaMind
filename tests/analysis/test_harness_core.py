"""Tests for ``alphamind.analysis._harness_core`` (ALP-466).

The harness core extracts the machinery duplicated across the four LLM
harnesses (``domain_researchers``, ``qualitative_research``,
``adaptive_research``, ``synthesizer``):

* Exception hierarchy: ``HarnessFailure`` and subclasses.
* Per-process system-prompt cache.
* Internal SDK signals (``_CLIResultError``, ``_StuckSDKCall``).
* Token-accounting helpers.
* Diagnostic-record writer (``DiagState``).
* Corrective-retry message builder.
* Raw-response renderer.
* The SDK driver loop + per-call ``_invoke`` wrapper, parameterised by a
  ``ParseResponse`` and ``BuildOptions`` Protocol.

The tests below pin the public surface (names and behaviours) — the per-
harness wrappers compose these.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from alphamind.analysis import _harness_core as core
from alphamind.analysis._shared import TokensUsed

# Canonical test invocation timestamp; date partition is "2026-05-01".
_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Exception hierarchy is re-exported from _harness_core
# ---------------------------------------------------------------------------


def test_harness_core_exposes_exception_hierarchy() -> None:
    """The core module exports the spec-mandated exception class names."""
    assert issubclass(core.MalformedOutputFailure, core.HarnessFailure)
    assert issubclass(core.ContextOverflowFailure, core.HarnessFailure)
    assert issubclass(core.SDKFailure, core.HarnessFailure)
    assert issubclass(core.TimeoutFailure, core.HarnessFailure)


def test_harness_failure_carries_agent_and_invocation() -> None:
    """``HarnessFailure`` stores ``agent_name`` and ``invocation_id``."""
    exc = core.HarnessFailure("boom", agent_name="agent-x", invocation_id="inv-1")
    assert exc.agent_name == "agent-x"
    assert exc.invocation_id == "inv-1"


def test_malformed_output_failure_carries_raw_responses() -> None:
    exc = core.MalformedOutputFailure(
        "boom",
        agent_name="agent-x",
        invocation_id="inv-1",
        raw_response_initial="aaa",
        raw_response_retry="bbb",
        cause=ValueError("nope"),
    )
    assert exc.raw_response_initial == "aaa"
    assert exc.raw_response_retry == "bbb"
    assert isinstance(exc.cause, ValueError)


# ---------------------------------------------------------------------------
# Prompt cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_prompt_caches_per_process(tmp_path: Path) -> None:
    """``_load_prompt`` reads each file once per process."""
    # Path resolved relative to repo root.
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "demo.md").write_text("hello", encoding="utf-8")

    core._PROMPT_CACHE.clear()
    with patch.object(core, "_REPO_ROOT", tmp_path):
        # First call reads from disk.
        text1 = await core._load_prompt("prompts/demo.md")
        assert text1 == "hello"

        # Mutate the file: second call returns the cached copy.
        (prompt_dir / "demo.md").write_text("changed", encoding="utf-8")
        text2 = await core._load_prompt("prompts/demo.md")
        assert text2 == "hello"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def test_tokens_from_usage_preserves_omitted_fields() -> None:
    previous = TokensUsed(
        input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40
    )
    # SDK omits cache fields on the assistant message; the helper preserves them.
    merged = core._tokens_from_usage({"input_tokens": 11, "output_tokens": 22}, previous)
    assert merged.input_tokens == 11
    assert merged.output_tokens == 22
    assert merged.cache_read_tokens == 30
    assert merged.cache_write_tokens == 40


def test_add_tokens_field_wise() -> None:
    a = TokensUsed(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4)
    b = TokensUsed(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40)
    s = core._add_tokens(a, b)
    assert s.input_tokens == 11
    assert s.output_tokens == 22
    assert s.cache_read_tokens == 33
    assert s.cache_write_tokens == 44


# ---------------------------------------------------------------------------
# Retry message builder
# ---------------------------------------------------------------------------


def test_build_retry_message_format() -> None:
    """``_build_retry_message`` joins parts with blank lines and contains no extras."""
    msg = core._build_retry_message(
        framing="framing line",
        error_detail="Field: x\nError: bad",
        contract_ref="See contract doc",
        directive="Re-emit the brief.",
    )
    parts = msg.split("\n\n")
    assert parts == [
        "framing line",
        "Field: x\nError: bad",
        "See contract doc",
        "Re-emit the brief.",
    ]


def test_build_retry_message_supports_extra_directive() -> None:
    """Optional trailing directives (e.g. content-preservation) join in order."""
    msg = core._build_retry_message(
        framing="F",
        error_detail="E",
        contract_ref="C",
        directive="D",
        extra_directives=("preserve prior content",),
    )
    assert msg.endswith("preserve prior content")
    assert msg.split("\n\n") == ["F", "E", "C", "D", "preserve prior content"]


# ---------------------------------------------------------------------------
# Raw-response renderer
# ---------------------------------------------------------------------------


def test_render_raw_response_with_payload_and_text() -> None:
    """When both payload and text are present, both appear in stable order."""
    rendered = core._render_raw_response({"a": 1}, "narration")
    parts = rendered.split("\n\n")
    assert parts[0] == "narration"
    assert json.loads(parts[1]) == {"a": 1}


def test_render_raw_response_payload_only() -> None:
    rendered = core._render_raw_response({"a": 1}, "")
    assert "a" in rendered
    assert json.loads(rendered) == {"a": 1}


def test_render_raw_response_payload_none() -> None:
    rendered = core._render_raw_response(None, "")
    assert rendered == "(structured_output not populated)"


# ---------------------------------------------------------------------------
# Internal SDK signals
# ---------------------------------------------------------------------------


def test_cli_result_error_carries_state() -> None:
    err = core._CLIResultError(
        error_text="overflow", partial_response="abc", stop_reason="max_tokens"
    )
    assert err.error_text == "overflow"
    assert err.partial_response == "abc"
    assert err.stop_reason == "max_tokens"


def test_stuck_sdk_call_is_exception() -> None:
    assert issubclass(core._StuckSDKCall, Exception)


# ---------------------------------------------------------------------------
# DiagState writer
# ---------------------------------------------------------------------------


def _make_diag(tmp_path: Path, **overrides: Any) -> core.DiagState:
    base: dict[str, Any] = {
        "agent_name": "demo_agent",
        "invocation_id": "inv-001",
        "prompt_text": "PROMPT",
        "user_message": "USER",
        "model": "claude-sonnet",
        "archive_root": tmp_path,
        "as_of": _AS_OF,
    }
    base.update(overrides)
    return core.DiagState(**base)


def test_diagstate_writes_two_response_files_when_retry_present(tmp_path: Path) -> None:
    diag = _make_diag(tmp_path)
    diag.response_initial = "first response"
    diag.response_retry = "retry response"
    diag.errors = [{"stage": "parse", "attempt": 1}]
    diag.retry_count = 1
    diag.write(success=True, wall_clock_seconds=1.25, stop_reason="end_turn")
    diag_dir = tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent"
    assert (diag_dir / "prompt.md").read_text() == "PROMPT"
    assert (diag_dir / "user_message.md").read_text() == "USER"
    assert (diag_dir / "response_initial.md").read_text() == "first response"
    assert (diag_dir / "response_retry.md").read_text() == "retry response"
    meta = json.loads((diag_dir / "metadata.json").read_text())
    assert meta["retry_count"] == 1
    assert meta["success"] is True
    assert meta["stop_reason"] == "end_turn"


def test_diagstate_omits_response_retry_when_none(tmp_path: Path) -> None:
    diag = _make_diag(tmp_path)
    diag.response_initial = "first"
    diag.write(success=False, wall_clock_seconds=2.0, stop_reason=None)
    diag_dir = tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent"
    assert (diag_dir / "response_initial.md").read_text() == "first"
    assert not (diag_dir / "response_retry.md").exists()


def test_diagstate_skips_write_when_archive_root_none(tmp_path: Path) -> None:
    diag = _make_diag(tmp_path, archive_root=None)
    diag.write(success=True, wall_clock_seconds=0.1, stop_reason=None)
    # No directories created.
    assert list(tmp_path.iterdir()) == []


def test_diagstate_metadata_includes_tool_calls_when_set(tmp_path: Path) -> None:
    diag = _make_diag(tmp_path)
    diag.response_initial = "ok"
    diag.tool_calls_used = 7
    diag.write(success=True, wall_clock_seconds=0.5, stop_reason="end_turn")
    diag_dir = tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent"
    meta = json.loads((diag_dir / "metadata.json").read_text())
    assert meta["tool_calls_used"] == 7


def test_diagstate_writes_single_response_file_when_no_retry_field(tmp_path: Path) -> None:
    """Synthesizer-style: a single ``response.md`` file rather than initial/retry."""
    diag = core.DiagState(
        agent_name="synthesizer",
        invocation_id="inv-002",
        prompt_text="P",
        user_message="U",
        model="m",
        archive_root=tmp_path,
        as_of=_AS_OF,
        response_filename="response.md",
    )
    diag.response_initial = "the prose"
    diag.write(success=True, wall_clock_seconds=0.5, stop_reason="end_turn")
    diag_dir = tmp_path / "2026-05-01" / "inv-002" / "analysis" / "synthesizer"
    assert (diag_dir / "response.md").read_text() == "the prose"
    assert not (diag_dir / "response_initial.md").exists()


# ---------------------------------------------------------------------------
# Collect-response driver
# ---------------------------------------------------------------------------


def _make_sdk_assistant(
    *, text: str = "", stop_reason: str | None = "end_turn", tool_use: list[Any] | None = None
) -> Any:
    from claude_agent_sdk import AssistantMessage, TextBlock

    content: list[Any] = [TextBlock(text=text)] if text else []
    if tool_use:
        content.extend(tool_use)
    return AssistantMessage(
        content=content,
        model="m",
        stop_reason=stop_reason,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            # Per the real SDK shape: per-message AssistantMessage usage
            # carries zero cache counts; the cumulative cache split
            # lands on ResultMessage. Test fixture mirrors that so the
            # plumbing assertion verifies the ResultMessage path
            # specifically rather than masking it via overwrite.
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )


def _make_sdk_result(
    *,
    is_error: bool = False,
    structured_output: dict[str, Any] | None = None,
    stop_reason: str | None = "end_turn",
    result_text: str | None = None,
    session_id: str = "sess-1",
) -> Any:
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        stop_reason=stop_reason,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 68_214,
            "cache_creation_input_tokens": 1_024,
        },
        structured_output=structured_output,
        result=result_text,
    )


def _make_stub(messages: list[Any]) -> Callable[..., AsyncIterator[Any]]:
    async def _stub(**_: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg

    return _stub


@pytest.mark.asyncio
async def test_collect_response_happy_path_returns_outcome() -> None:
    """``_collect_response`` accumulates assistant text + structured_output + tokens."""
    messages = [
        _make_sdk_assistant(text="narration"),
        _make_sdk_result(structured_output={"x": 1}),
    ]
    outcome = await core._collect_response(
        _make_stub(messages),
        prompt="ignored",
        options=None,
    )
    assert outcome.response_text == "narration"
    assert outcome.structured_output == {"x": 1}
    assert outcome.stop_reason == "end_turn"
    assert outcome.tokens_used.input_tokens == 10
    assert outcome.session_id == "sess-1"


@pytest.mark.asyncio
async def test_collect_response_raises_on_is_error() -> None:
    messages = [
        _make_sdk_result(is_error=True, result_text="overflow detected", stop_reason="max_tokens")
    ]
    with pytest.raises(core._CLIResultError) as info:
        await core._collect_response(_make_stub(messages), prompt="p", options=None)
    assert info.value.stop_reason == "max_tokens"
    assert "overflow" in info.value.error_text


@pytest.mark.asyncio
async def test_collect_response_counts_tool_uses_with_prefix() -> None:
    """When ``tool_name_prefix`` is provided, only matching ToolUseBlocks count."""
    from claude_agent_sdk import ToolUseBlock

    matching = ToolUseBlock(id="t1", name="mcp__alphamind_x__do", input={})
    other = ToolUseBlock(id="t2", name="StructuredOutput", input={})
    messages = [
        _make_sdk_assistant(tool_use=[matching, other]),
        _make_sdk_result(),
    ]
    outcome = await core._collect_response(
        _make_stub(messages),
        prompt="p",
        options=None,
        tool_name_prefix="mcp__alphamind_x__",
    )
    assert outcome.tool_calls == 1


@pytest.mark.asyncio
async def test_collect_response_counts_all_tool_uses_without_prefix() -> None:
    """Without a prefix, every ``ToolUseBlock`` counts (synthesizer behaviour)."""
    from claude_agent_sdk import ToolUseBlock

    messages = [
        _make_sdk_assistant(
            tool_use=[
                ToolUseBlock(id="t1", name="anything", input={}),
                ToolUseBlock(id="t2", name="anything2", input={}),
            ]
        ),
        _make_sdk_result(),
    ]
    outcome = await core._collect_response(
        _make_stub(messages),
        prompt="p",
        options=None,
    )
    assert outcome.tool_calls == 2


@pytest.mark.asyncio
async def test_collect_response_stall_timeout_raises_stuck() -> None:
    """When the first message takes longer than the init-stall timeout, raise _StuckSDKCall."""

    async def _silent_stub(**_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(1.0)
        if False:
            yield None  # pragma: no cover

    with pytest.raises(core._StuckSDKCall):
        await core._collect_response(
            _silent_stub, prompt="p", options=None, init_stall_timeout_seconds=0.05
        )


@pytest.mark.asyncio
async def test_collect_response_no_stall_timeout_after_first_message() -> None:
    """After the first message arrives, no further stall watchdog applies by default."""

    async def _delayed_after_first(**_: Any) -> AsyncIterator[Any]:
        yield _make_sdk_assistant(text="hi")
        await asyncio.sleep(0.2)  # long pause AFTER first message
        yield _make_sdk_result()

    # Setting init_stall_timeout shorter than the inter-message delay; if the
    # watchdog were still active we'd get _StuckSDKCall.
    outcome = await core._collect_response(
        _delayed_after_first,
        prompt="p",
        options=None,
        init_stall_timeout_seconds=0.05,
    )
    assert outcome.response_text == "hi"


@pytest.mark.asyncio
async def test_collect_response_between_message_stall_raises_stuck() -> None:
    """When between_message_stall_seconds is set, an inter-message hang raises _StuckSDKCall.

    Reproduces the verify-run failure: SDK emits initial SystemMessage then
    hangs silently until the outer budget expires with zero output tokens.
    The between-message watchdog catches that case so the retry path can
    engage with a fresh subprocess instead of burning the full 600s budget.
    """

    async def _hang_after_first(**_: Any) -> AsyncIterator[Any]:
        yield _make_sdk_assistant(text="hi")
        await asyncio.sleep(1.0)  # silence longer than between-message budget
        yield _make_sdk_result()  # pragma: no cover — watchdog fires first

    with pytest.raises(core._StuckSDKCall):
        await core._collect_response(
            _hang_after_first,
            prompt="p",
            options=None,
            init_stall_timeout_seconds=None,
            between_message_stall_seconds=0.05,
        )


# ---------------------------------------------------------------------------
# _invoke: timeout + error translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_translates_timeout_to_timeout_failure(tmp_path: Path) -> None:
    """The shared ``_invoke`` maps :class:`TimeoutError` to ``TimeoutFailure``."""

    async def _slow_stub(**_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0.5)
        if False:
            yield None  # pragma: no cover

    diag = _make_diag(tmp_path)
    with pytest.raises(core.TimeoutFailure):
        await core.invoke_sdk(
            sdk_query_fn=_slow_stub,
            prompt="p",
            options=None,
            diag=diag,
            budget_seconds=0.05,
            init_stall_timeout_seconds=None,
            wall_start=0.0,
            agent_name="x",
            invocation_id="inv-1",
            on_cli_result_error="context_overflow",
            phase="test-phase",
        )


@pytest.mark.asyncio
async def test_invoke_translates_cli_result_error_to_context_overflow(
    tmp_path: Path,
) -> None:
    """``on_cli_result_error='context_overflow'`` translates errors accordingly."""
    messages = [
        _make_sdk_result(is_error=True, result_text="ctx overflow", stop_reason="max_tokens")
    ]
    diag = _make_diag(tmp_path)
    with pytest.raises(core.ContextOverflowFailure):
        await core.invoke_sdk(
            sdk_query_fn=_make_stub(messages),
            prompt="p",
            options=None,
            diag=diag,
            budget_seconds=5.0,
            init_stall_timeout_seconds=None,
            wall_start=0.0,
            agent_name="x",
            invocation_id="inv-1",
            on_cli_result_error="context_overflow",
            phase="test-phase",
        )


@pytest.mark.asyncio
async def test_invoke_translates_cli_result_error_to_sdk_failure_when_configured(
    tmp_path: Path,
) -> None:
    """Synthesizer-mode: ``_CLIResultError`` maps to ``SDKFailure`` instead."""
    messages = [_make_sdk_result(is_error=True, result_text="cli broke", stop_reason=None)]
    diag = _make_diag(tmp_path)
    with pytest.raises(core.SDKFailure):
        await core.invoke_sdk(
            sdk_query_fn=_make_stub(messages),
            prompt="p",
            options=None,
            diag=diag,
            budget_seconds=5.0,
            init_stall_timeout_seconds=None,
            wall_start=0.0,
            agent_name="x",
            invocation_id="inv-1",
            on_cli_result_error="sdk_failure",
            phase="test-phase",
        )


@pytest.mark.asyncio
async def test_invoke_retries_once_on_stall(tmp_path: Path) -> None:
    """When ``init_stall_timeout_seconds`` is set, ``_invoke`` retries one stall before raising."""
    call_count = 0
    success_messages = [
        _make_sdk_assistant(text="hi"),
        _make_sdk_result(structured_output={"ok": True}),
    ]

    async def _stall_then_succeed(**_: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(0.5)
            if False:
                yield None  # pragma: no cover
            return
        for msg in success_messages:
            yield msg

    diag = _make_diag(tmp_path)
    outcome = await core.invoke_sdk(
        sdk_query_fn=_stall_then_succeed,
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=0.05,
        wall_start=0.0,
        agent_name="x",
        invocation_id="inv-1",
        on_cli_result_error="context_overflow",
        phase="test-phase",
    )
    assert call_count == 2
    assert outcome.structured_output == {"ok": True}


@pytest.mark.asyncio
async def test_invoke_two_consecutive_stalls_raises_timeout(tmp_path: Path) -> None:
    """Two consecutive stalls exhaust the retry and surface ``TimeoutFailure``."""

    async def _always_stall(**_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0.5)
        if False:
            yield None  # pragma: no cover

    diag = _make_diag(tmp_path)
    with pytest.raises(core.TimeoutFailure):
        await core.invoke_sdk(
            sdk_query_fn=_always_stall,
            prompt="p",
            options=None,
            diag=diag,
            budget_seconds=5.0,
            init_stall_timeout_seconds=0.05,
            wall_start=0.0,
            agent_name="x",
            invocation_id="inv-1",
            on_cli_result_error="context_overflow",
            phase="test-phase",
        )


@pytest.mark.asyncio
async def test_invoke_retries_once_on_between_message_stall(tmp_path: Path) -> None:
    """A between-message stall on attempt 1 triggers a retry, second attempt succeeds.

    Same retry contract as the init-stall path, but driven by the
    between-message watchdog. Hardens against the SDK-hang-after-first-
    message failure observed under concurrent researcher launches on
    Windows.
    """
    call_count = 0
    success_messages = [
        _make_sdk_assistant(text="hi"),
        _make_sdk_result(structured_output={"ok": True}),
    ]

    async def _hang_then_succeed(**_: Any) -> AsyncIterator[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield _make_sdk_assistant(text="initial")
            await asyncio.sleep(0.5)  # hangs after first message
            return
        for msg in success_messages:
            yield msg

    diag = _make_diag(tmp_path)
    outcome = await core.invoke_sdk(
        sdk_query_fn=_hang_then_succeed,
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        between_message_stall_seconds=0.05,
        wall_start=0.0,
        agent_name="x",
        invocation_id="inv-1",
        on_cli_result_error="context_overflow",
        phase="test-phase",
    )
    assert call_count == 2
    assert outcome.structured_output == {"ok": True}


@pytest.mark.asyncio
async def test_invoke_applies_concurrent_launch_jitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``concurrent_launch_jitter_seconds`` draws from ``random.uniform(0, value)`` and sleeps."""
    uniform_calls: list[tuple[float, float]] = []

    def _capture_uniform(lo: float, hi: float) -> float:
        uniform_calls.append((lo, hi))
        return 0.0  # don't actually wait

    monkeypatch.setattr("alphamind.analysis._harness_core.random.uniform", _capture_uniform)

    async def _stub(**_: Any) -> AsyncIterator[Any]:
        yield _make_sdk_assistant(text="hi")
        yield _make_sdk_result(structured_output={"ok": True})

    diag = _make_diag(tmp_path)
    await core.invoke_sdk(
        sdk_query_fn=_stub,
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        concurrent_launch_jitter_seconds=0.5,
        wall_start=0.0,
        agent_name="x",
        invocation_id="inv-1",
        on_cli_result_error="context_overflow",
        phase="test-phase",
    )
    assert uniform_calls == [(0, 0.5)]


@pytest.mark.asyncio
async def test_invoke_skips_jitter_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``concurrent_launch_jitter_seconds=0.0`` (default) skips the random draw entirely."""
    uniform_calls: list[tuple[float, float]] = []

    def _capture_uniform(lo: float, hi: float) -> float:
        uniform_calls.append((lo, hi))
        return 0.0

    monkeypatch.setattr("alphamind.analysis._harness_core.random.uniform", _capture_uniform)

    async def _stub(**_: Any) -> AsyncIterator[Any]:
        yield _make_sdk_assistant(text="hi")
        yield _make_sdk_result(structured_output={"ok": True})

    diag = _make_diag(tmp_path)
    await core.invoke_sdk(
        sdk_query_fn=_stub,
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        wall_start=0.0,
        agent_name="x",
        invocation_id="inv-1",
        on_cli_result_error="context_overflow",
        phase="test-phase",
    )
    assert uniform_calls == []


# ---------------------------------------------------------------------------
# Each harness module imports its exception names from _harness_core
# ---------------------------------------------------------------------------


def test_domain_researchers_re_exports_core_exceptions() -> None:
    from alphamind.analysis.domain_researchers import harness as dr_harness

    assert dr_harness.HarnessFailure is core.HarnessFailure
    assert dr_harness.MalformedOutputFailure is core.MalformedOutputFailure
    assert dr_harness.ContextOverflowFailure is core.ContextOverflowFailure
    assert dr_harness.SDKFailure is core.SDKFailure
    assert dr_harness.TimeoutFailure is core.TimeoutFailure


def test_qualitative_research_re_exports_core_exceptions() -> None:
    from alphamind.analysis.qualitative_research import harness as qr_harness

    assert qr_harness.HarnessFailure is core.HarnessFailure
    assert qr_harness.MalformedOutputFailure is core.MalformedOutputFailure
    assert qr_harness.ContextOverflowFailure is core.ContextOverflowFailure
    assert qr_harness.SDKFailure is core.SDKFailure
    assert qr_harness.TimeoutFailure is core.TimeoutFailure


def test_adaptive_research_re_exports_core_exceptions() -> None:
    from alphamind.analysis.adaptive_research import harness as ar_harness

    assert ar_harness.HarnessFailure is core.HarnessFailure
    assert ar_harness.MalformedOutputFailure is core.MalformedOutputFailure
    assert ar_harness.ContextOverflowFailure is core.ContextOverflowFailure
    assert ar_harness.SDKFailure is core.SDKFailure
    assert ar_harness.TimeoutFailure is core.TimeoutFailure


def test_synthesizer_re_exports_core_exceptions() -> None:
    from alphamind.analysis.synthesizer import harness as syn_harness

    # Synthesizer doesn't use MalformedOutputFailure (has EmptyResponseFailure instead).
    assert syn_harness.HarnessFailure is core.HarnessFailure
    assert syn_harness.ContextOverflowFailure is core.ContextOverflowFailure
    assert syn_harness.SDKFailure is core.SDKFailure
    assert syn_harness.TimeoutFailure is core.TimeoutFailure


# ---------------------------------------------------------------------------
# Each harness module imports the prompt cache / collect_response from core
# ---------------------------------------------------------------------------


def test_each_harness_uses_shared_prompt_cache() -> None:
    from alphamind.analysis.adaptive_research import harness as ar_harness
    from alphamind.analysis.domain_researchers import harness as dr_harness
    from alphamind.analysis.qualitative_research import harness as qr_harness
    from alphamind.analysis.synthesizer import harness as syn_harness

    for mod in (dr_harness, qr_harness, ar_harness, syn_harness):
        assert mod._PROMPT_CACHE is core._PROMPT_CACHE
        assert mod._load_prompt is core._load_prompt


# ---------------------------------------------------------------------------
# Output-format / prefill compat guard (per feedback_prompt_output_format_compat)
# ---------------------------------------------------------------------------


def test_no_prefill_directive_in_json_schema_harness_options() -> None:
    """JSON-schema-mode harnesses must not request a ``begin with {`` prefill.

    Per ``feedback_prompt_output_format_compat.md``: when ``output_format`` is
    set to ``json_schema`` mode, telling the model to "begin with `{`" causes
    extended thinking to hang silently. None of the four harnesses should set
    such a directive on the constructed options.
    """
    from alphamind.analysis.adaptive_research import harness as ar_harness
    from alphamind.analysis.qualitative_research import harness as qr_harness

    # The JSON-schema-mode harnesses are DR, QR, AR. Construct each module's
    # options with a benign config and confirm:
    #   1. ``output_format`` is set to ``json_schema``
    #   2. ``system_prompt`` does NOT contain a ``begin with {`` directive
    #   3. ``extra_args`` does NOT contain a prefill request
    from alphamind.config.models.agents import AllowedModel, BaseAgentConfig

    base = BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/tech_semis_researcher.md",
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )

    # Verify each harness module's _build_sdk_options respects the
    # output_format contract. QR + AR have explicit builders.
    for mod in (qr_harness, ar_harness):
        opts = mod._build_sdk_options(
            base,
            prompt_text="system prompt text",
            allowed_tools=[],
            mcp_servers={},
        )
        of = opts.output_format
        assert of is not None
        assert of["type"] == "json_schema"
        assert "begin with `{`" not in opts.system_prompt
        # extra_args is a dict-like mapping; ensure no prefill keys snuck in.
        assert "prefill" not in (opts.extra_args or {})
        assert "system-prompt-prefill" not in (opts.extra_args or {})


# ---------------------------------------------------------------------------
# ALP-497 extensions: tuple tool_name_prefix, archive_layer, progress emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_response_counts_tool_uses_with_prefix_tuple() -> None:
    """``tool_name_prefix`` accepts a tuple — the decision harnesses use this."""
    from claude_agent_sdk import ToolUseBlock

    a = ToolUseBlock(id="t1", name="mcp__alphamind_decision_validation__check", input={})
    b = ToolUseBlock(id="t2", name="mcp__alphamind_synthesizer_retrieval__fetch", input={})
    c = ToolUseBlock(id="t3", name="StructuredOutput", input={})
    messages = [
        _make_sdk_assistant(tool_use=[a, b, c]),
        _make_sdk_result(),
    ]
    outcome = await core._collect_response(
        _make_stub(messages),
        prompt="p",
        options=None,
        tool_name_prefix=(
            "mcp__alphamind_decision_validation__",
            "mcp__alphamind_synthesizer_retrieval__",
        ),
    )
    assert outcome.tool_calls == 2


def test_diagstate_writes_to_archive_layer_decision(tmp_path: Path) -> None:
    """``archive_layer="decision"`` redirects the diag dir to ``.../decision/<agent>/``."""
    diag = core.DiagState(
        agent_name="analyst",
        invocation_id="inv-dec-001",
        prompt_text="P",
        user_message="U",
        model="m",
        archive_root=tmp_path,
        as_of=_AS_OF,
        archive_layer="decision",
    )
    diag.response_initial = "ok"
    diag.write(success=True, wall_clock_seconds=0.1, stop_reason="end_turn")
    decision_dir = tmp_path / "2026-05-01" / "inv-dec-001" / "decision" / "analyst"
    assert (decision_dir / "prompt.md").read_text() == "P"
    # Analysis path is NOT populated.
    assert not (tmp_path / "2026-05-01" / "inv-dec-001" / "analysis").exists()


def test_diagstate_defaults_archive_layer_to_analysis(tmp_path: Path) -> None:
    """Default ``archive_layer`` is ``"analysis"`` — analysis-harness behaviour preserved."""
    diag = _make_diag(tmp_path)
    diag.response_initial = "ok"
    diag.write(success=True, wall_clock_seconds=0.1, stop_reason="end_turn")
    analysis_dir = tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent"
    assert (analysis_dir / "prompt.md").exists()


class _RecordingEmitterForCore:
    """Test-only ProgressEmitter substitute used by invoke_sdk tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def phase_start(self, phase: str) -> None:  # pragma: no cover - not used here
        self.events.append(("phase_start", {"phase": phase}))

    def phase_done(self, phase: str, **fields: Any) -> None:  # pragma: no cover
        self.events.append(("phase_done", {"phase": phase, **fields}))

    def agent_request(self, **fields: Any) -> None:
        self.events.append(("agent_request", fields))

    def agent_retrying(self, **fields: Any) -> None:  # pragma: no cover - not used here
        self.events.append(("agent_retrying", fields))

    def agent_response(self, **fields: Any) -> None:
        self.events.append(("agent_response", fields))


@pytest.mark.asyncio
async def test_invoke_sdk_emits_agent_request_and_response(tmp_path: Path) -> None:
    """`invoke_sdk` emits ``agent_request`` then ``agent_response`` per call."""
    messages = [
        _make_sdk_assistant(text="hi"),
        _make_sdk_result(structured_output={"ok": True}, stop_reason="end_turn"),
    ]
    diag = _make_diag(tmp_path)
    emitter = _RecordingEmitterForCore()
    await core.invoke_sdk(
        sdk_query_fn=_make_stub(messages),
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        wall_start=0.0,
        agent_name="demo_agent",
        invocation_id="inv-emit-1",
        on_cli_result_error="context_overflow",
        progress=emitter,
        phase="phase-x",
    )

    assert [evt[0] for evt in emitter.events] == ["agent_request", "agent_response"]

    request_fields = emitter.events[0][1]
    assert request_fields["phase"] == "phase-x"
    assert request_fields["agent"] == "demo_agent"
    assert request_fields["model"] == "claude-sonnet"

    response_fields = emitter.events[1][1]
    assert response_fields["phase"] == "phase-x"
    assert response_fields["agent"] == "demo_agent"
    assert response_fields["model"] == "claude-sonnet"
    assert response_fields["stop_reason"] == "end_turn"
    # 7 fixed agent_response fields (parent issue § B; cache split per
    # ALP-701 — most AlphaMind prompts hit the cache, so an operator
    # tailing the JSONL needs the three input-side counts separately to
    # distinguish "context assembled, cached" from "context-assembly
    # broken").
    assert "duration_s" in response_fields
    assert response_fields["input_tokens"] == 10
    assert response_fields["cache_read_tokens"] == 68_214
    assert response_fields["cache_write_tokens"] == 1_024
    assert response_fields["output_tokens"] == 20
    assert response_fields["tool_calls"] == 0


@pytest.mark.asyncio
async def test_invoke_sdk_emits_agent_response_on_failure(tmp_path: Path) -> None:
    """Failure path still emits ``agent_response`` so cost is recorded.

    Stop_reason carries the SDK-reported failure-stop-reason when
    available. Token kwargs default to zero — the partial accumulator
    inside ``_collect_response`` is not threaded through the failure
    exceptions, so the docstring contract is "non-null zero" rather
    than "partial tokens". Lock that contract here so a future
    refactor that tries to forward ``None`` (or omit the cache kwargs
    entirely) fails the verify-script's non-null check (see
    ``_AGENT_RESPONSE_NON_NULL_FIELDS`` in ``verify_debug_e2e.py``).
    """
    messages = [
        _make_sdk_result(is_error=True, result_text="ctx overflow", stop_reason="max_tokens"),
    ]
    diag = _make_diag(tmp_path)
    emitter = _RecordingEmitterForCore()
    with pytest.raises(core.ContextOverflowFailure):
        await core.invoke_sdk(
            sdk_query_fn=_make_stub(messages),
            prompt="p",
            options=None,
            diag=diag,
            budget_seconds=5.0,
            init_stall_timeout_seconds=None,
            wall_start=0.0,
            agent_name="demo_agent",
            invocation_id="inv-emit-2",
            on_cli_result_error="context_overflow",
            progress=emitter,
            phase="phase-fail",
        )

    kinds = [evt[0] for evt in emitter.events]
    assert kinds == ["agent_request", "agent_response"]

    response_fields = emitter.events[1][1]
    assert response_fields["stop_reason"] == "max_tokens"
    # Failure-path defaults: zero, not None — the verify script's
    # non-null contract forbids None on these fields.
    assert response_fields["input_tokens"] == 0
    assert response_fields["cache_read_tokens"] == 0
    assert response_fields["cache_write_tokens"] == 0
    assert response_fields["output_tokens"] == 0
    assert response_fields["tool_calls"] == 0


@pytest.mark.asyncio
async def test_invoke_sdk_defaults_progress_to_noop(tmp_path: Path) -> None:
    """``progress`` defaults to a NoOp; ``phase`` is still required as kwarg.

    Calls that don't pass ``progress`` continue to work — the default is
    :class:`NoOpProgressEmitter`.
    """
    messages = [
        _make_sdk_assistant(text="hi"),
        _make_sdk_result(structured_output={"ok": True}),
    ]
    diag = _make_diag(tmp_path)
    # No ``progress`` kwarg — relies on default. ``phase`` is required.
    outcome = await core.invoke_sdk(
        sdk_query_fn=_make_stub(messages),
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        wall_start=0.0,
        agent_name="demo_agent",
        invocation_id="inv-emit-3",
        on_cli_result_error="context_overflow",
        phase="some-phase",
    )
    assert outcome.structured_output == {"ok": True}


# ---------------------------------------------------------------------------
# SDK-call tracer (sdk_trace.jsonl forensic instrumentation)
# ---------------------------------------------------------------------------


def test_diagstate_diag_dir_resolves_under_archive(tmp_path: Path) -> None:
    diag = _make_diag(tmp_path)
    assert diag.diag_dir == tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent"


def test_diagstate_diag_dir_is_none_without_archive(tmp_path: Path) -> None:
    assert _make_diag(tmp_path, archive_root=None).diag_dir is None


def test_describe_sdk_message_summarises_rate_limit_event() -> None:
    """A ``RateLimitEvent`` is decoded to its full rate-limit posture."""
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo

    event = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning",
            resets_at=1779501600,
            rate_limit_type="seven_day",
            utilization=0.97,
            overage_status=None,
            overage_resets_at=None,
            overage_disabled_reason=None,
            raw={"surpassedThreshold": 0.75},
        ),
        uuid="u-1",
        session_id="s-1",
    )
    desc = core._describe_sdk_message(event)
    assert desc["type"] == "RateLimitEvent"
    assert desc["rate_limit_info"]["utilization"] == 0.97
    assert desc["rate_limit_info"]["status"] == "allowed_warning"
    assert desc["rate_limit_info"]["rate_limit_type"] == "seven_day"


def test_describe_sdk_message_summarises_assistant_blocks() -> None:
    desc = core._describe_sdk_message(_make_sdk_assistant(text="hello"))
    assert desc["type"] == "AssistantMessage"
    assert desc["blocks"] == [{"block": "TextBlock", "text_len": 5}]
    # Per ALP-701: the per-message forensic trace surfaces the cache
    # split so an operator debugging a stalled SDK call can see whether
    # a cache hit happened mid-call.
    assert desc["usage"] == {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 20,
    }


def test_describe_sdk_message_summarises_result_terminal_state() -> None:
    desc = core._describe_sdk_message(_make_sdk_result(stop_reason="end_turn"))
    assert desc["type"] == "ResultMessage"
    assert desc["stop_reason"] == "end_turn"
    assert desc["is_error"] is False


def test_describe_sdk_message_captures_tool_use_id_and_name() -> None:
    """ToolUseBlock summary carries ``id`` so the result block can be paired (ALP-703).

    The forensic trace previously only recorded the tool ``name``; without
    ``id`` an operator (or the verify_debug_e2e tool-layer health check) had
    no way to link a tool call to its result. Pairing by id lets the check
    tally per-tool quality outcomes (complete vs unavailable) end-to-end.
    """
    from claude_agent_sdk import ToolUseBlock

    use = ToolUseBlock(id="toolu_01", name="mcp__alphamind_qualitative__news_search", input={})
    desc = core._describe_sdk_message(_make_sdk_assistant(text="", tool_use=[use]))
    assert desc["blocks"] == [
        {
            "block": "ToolUseBlock",
            "tool": "mcp__alphamind_qualitative__news_search",
            "id": "toolu_01",
        }
    ]


def test_describe_sdk_message_captures_tool_result_quality() -> None:
    """ToolResultBlock summary carries ``tool_use_id`` and parsed ``quality`` (ALP-703).

    The tool envelope renders its result as a single text content block whose
    text is the JSON-encoded payload. The trace summary parses that JSON,
    pulls the envelope ``quality`` field, and surfaces it so the post-run
    tool-layer health check can tally outcomes per tool without re-parsing
    the agents' narrative response files.
    """
    from claude_agent_sdk import ToolResultBlock, UserMessage

    payload = json.dumps({"quality": "unavailable", "data_freshness": "2026-05-26T00:00:00Z"})
    result_block = ToolResultBlock(
        tool_use_id="toolu_01",
        content=[{"type": "text", "text": payload}],
        is_error=False,
    )
    user_msg = UserMessage(content=[result_block])
    desc = core._describe_sdk_message(user_msg)
    assert desc["blocks"] == [
        {
            "block": "ToolResultBlock",
            "tool_use_id": "toolu_01",
            "is_error": False,
            "quality": "unavailable",
        }
    ]


def test_describe_sdk_message_handles_unparseable_tool_result() -> None:
    """A non-JSON tool result still records the block; quality is absent (ALP-703).

    An exception path that returns plain text (or a non-envelope tool's
    output) must not break the trace summary — the verify check tolerates
    missing ``quality`` by classifying the call as ``"other"``.
    """
    from claude_agent_sdk import ToolResultBlock, UserMessage

    result_block = ToolResultBlock(
        tool_use_id="toolu_02",
        content="plain text, not JSON",
        is_error=True,
    )
    user_msg = UserMessage(content=[result_block])
    desc = core._describe_sdk_message(user_msg)
    assert desc["blocks"] == [
        {
            "block": "ToolResultBlock",
            "tool_use_id": "toolu_02",
            "is_error": True,
        }
    ]


def test_sdk_call_tracer_disabled_without_diag_dir(tmp_path: Path) -> None:
    tracer = core._SdkCallTracer(None)
    assert tracer.enabled is False
    tracer.event("call_start")
    tracer.cli_stderr("noise")
    assert list(tmp_path.iterdir()) == []


def test_sdk_call_tracer_appends_jsonl_records(tmp_path: Path) -> None:
    tracer = core._SdkCallTracer(tmp_path)
    assert tracer.enabled is True
    tracer.event("call_start")
    tracer.message(_make_sdk_assistant(text="hi"), gap_s=1.5)
    tracer.cli_stderr("cli line\n")
    records = [
        json.loads(line)
        for line in (tmp_path / "sdk_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["event"] for r in records] == ["call_start", "sdk_message", "cli_stderr"]
    assert records[1]["gap_s"] == 1.5
    assert records[1]["type"] == "AssistantMessage"
    assert records[2]["line"] == "cli line"
    assert all("ts" in r and "elapsed_s" in r for r in records)


@pytest.mark.asyncio
async def test_collect_response_writes_trace_when_tracer_enabled(tmp_path: Path) -> None:
    messages = [
        _make_sdk_assistant(text="narration"),
        _make_sdk_result(structured_output={"x": 1}),
    ]
    tracer = core._SdkCallTracer(tmp_path)
    await core._collect_response(
        _make_stub(messages),
        prompt="p",
        options=None,
        tracer=tracer,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "sdk_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events = [r["event"] for r in records]
    assert events[0] == "call_start"
    assert events[-1] == "collect_exit"
    assert events.count("sdk_message") == 2


@pytest.mark.asyncio
async def test_collect_response_no_trace_file_without_tracer(tmp_path: Path) -> None:
    await core._collect_response(
        _make_stub([_make_sdk_result(structured_output={"x": 1})]),
        prompt="p",
        options=None,
    )
    assert not (tmp_path / "sdk_trace.jsonl").exists()


@pytest.mark.asyncio
async def test_invoke_sdk_writes_sdk_trace_to_diag_dir(tmp_path: Path) -> None:
    """``invoke_sdk`` builds a tracer from ``diag.diag_dir`` and persists it."""
    diag = _make_diag(tmp_path)
    messages = [
        _make_sdk_assistant(text="ok"),
        _make_sdk_result(structured_output={"x": 1}),
    ]
    await core.invoke_sdk(
        sdk_query_fn=_make_stub(messages),
        prompt="p",
        options=None,
        diag=diag,
        budget_seconds=5.0,
        init_stall_timeout_seconds=None,
        wall_start=0.0,
        agent_name="demo_agent",
        invocation_id="inv-001",
        on_cli_result_error="sdk_failure",
        phase="demo",
    )
    trace = tmp_path / "2026-05-01" / "inv-001" / "analysis" / "demo_agent" / "sdk_trace.jsonl"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    events = [r["event"] for r in records]
    assert "attempt_start" in events
    assert "success" in events


# ---------------------------------------------------------------------------
# SDK-call semaphore (ALP-702): admits the analysis-layer fan-out concurrently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_call_semaphore_admits_analysis_layer_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK-call semaphore admits the analysis fan-out (3 sectors + qualitative).

    Regression guard for ALP-702: cap=1 (set in 2026-05-18 during the
    in-process back-to-back-stall investigation) serialized the 4 analysis
    SDK calls, inflating wall-clock from ~6 min (parallel) to ~23 min
    (serial). ALP-650 then migrated every harness to subprocess isolation,
    so each SDK call runs in a fresh ``python -m
    alphamind.analysis._sdk_subprocess_worker``. The in-process state
    degradation that motivated cap=1 cannot recur because no subprocess
    makes more than one SDK call. The cap must allow at least 4 concurrent
    acquisitions so ``domain_researchers`` + ``qualitative`` actually fan
    out under their ``asyncio.TaskGroup``.
    """
    # Reset the lazy singleton so this test is independent of any prior
    # in-process acquisitions (the production semaphore is process-global).
    monkeypatch.setattr(core, "_sdk_call_semaphore", None)

    semaphore = core._get_sdk_call_semaphore()
    # All four acquisitions must complete within the loop's next ticks; if
    # the cap is <4, at least one ``acquire()`` blocks indefinitely and the
    # wait_for trips.
    holders = [asyncio.create_task(semaphore.acquire()) for _ in range(4)]
    try:
        await asyncio.wait_for(asyncio.gather(*holders), timeout=1.0)
        # All four permits are now held; the semaphore's internal counter
        # should be exactly zero. Catches a future change that loosens the
        # cap past the analysis fan-out's actual width (e.g. cap=5+ would
        # still pass the wait_for check but the counter would be >0).
        assert semaphore._value == 0
    finally:
        # Cleanup contract: every acquired permit must be released exactly
        # once, and every still-pending task must be cancelled. wait_for's
        # timeout path leaves some tasks done-with-permit and some still
        # pending — the per-task probe distinguishes them. Short-circuit
        # AND skips ``.exception()`` on cancelled tasks (which would raise).
        for task in holders:
            if task.done() and not task.cancelled() and task.exception() is None:
                semaphore.release()
            else:
                task.cancel()
