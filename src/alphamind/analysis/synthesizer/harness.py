"""LLM invocation harness for synthesizer agent — story 08 (ALP-208).

Wraps the Claude Agent SDK call, wires the per-invocation portfolio-state MCP
server (story 06b) via :func:`build_portfolio_state_mcp_server`, applies the
documented Layer-4 stop-reason check on the response, preserves the diagnostic
record, and propagates fail-closed errors to the caller.

Structurally simpler than the sibling researcher harnesses (qualitative, adaptive)
because the synthesizer's output is unstructured prose with no producer-side
schema — no parser, no validator, no corrective retry. Per-invocation MCP
construction closes over the caller-supplied
:class:`SynthesizerPortfolioStateReader`; the global ``alphamind.analysis.tools``
registry pattern does not fit that shape.

Architecture note: ``invoke_synthesizer`` accepts ``sdk_query_fn`` for
dependency injection. In production the default (the real
``claude_agent_sdk.query``) is used. Tests pass a stub so no test touches the
Anthropic API.
"""

from __future__ import annotations

# ruff: noqa: N818  # Exception class names mirror sibling harness names by spec.
import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.portfolio_tools import build_portfolio_state_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.portfolio_state.consumers.synthesizer import SynthesizerPortfolioStateReader

__all__ = [
    "ContextOverflowFailure",
    "EmptyResponseFailure",
    "HarnessFailure",
    "HarnessSuccess",
    "SDKFailure",
    "TimeoutFailure",
    "invoke_synthesizer",
]

# ---------------------------------------------------------------------------
# Per-process system-prompt cache
# ---------------------------------------------------------------------------

# Maps prompt file path → loaded prompt text. No invalidation needed: the
# pipeline process restarts on agents.yaml edits per the deploy-time vs.
# invocation-time classification in configuration-management.md. The lock
# gates the read-then-write so concurrent orchestrator coroutines can't
# redundantly re-read the same file.
_PROMPT_CACHE: dict[str, str] = {}
_PROMPT_CACHE_LOCK = asyncio.Lock()

_REPO_ROOT = Path(__file__).resolve().parents[4]


async def _load_prompt(prompt_path: str) -> str:
    """Load system prompt from *prompt_path*, caching per process."""
    async with _PROMPT_CACHE_LOCK:
        if prompt_path not in _PROMPT_CACHE:
            _PROMPT_CACHE[prompt_path] = (_REPO_ROOT / prompt_path).read_text(encoding="utf-8")
        return _PROMPT_CACHE[prompt_path]


# ---------------------------------------------------------------------------
# Multi-turn budget
# ---------------------------------------------------------------------------

# Synthesizer doesn't loop heavily — it reads pre-assembled briefs and emits
# prose. ``max_turns`` bounds the SDK loop covering portfolio-tool calls + the
# final text generation; ~15 leaves ample headroom for the three
# portfolio-state tools without enabling runaway loops.
_MAX_TURNS = 15


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class HarnessFailure(Exception):
    """Base class for all harness-level failures.

    Every subclass carries *agent_name* and *invocation_id* so the caller
    (the runner, story 10) has full context for the failure log.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
    ) -> None:
        super().__init__(message)
        self.agent_name = agent_name
        self.invocation_id = invocation_id


class EmptyResponseFailure(HarnessFailure):
    """Non-error SDK response with no text content paired with end_turn.

    The synthesizer has no parse/validate stage, so this replaces the
    qualitative-research harness's ``MalformedOutputFailure``: any response
    that ended cleanly but produced no usable prose is a structural failure.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        raw_response: str | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.raw_response = raw_response


class ContextOverflowFailure(HarnessFailure):
    """Empty response paired with stop_reason=max_tokens.

    The pipeline aborts immediately — synthesizer output is unstructured
    prose, so a non-empty truncation still returns ``HarnessSuccess``; only
    *empty* output paired with ``max_tokens`` indicates context overflow.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        raw_response: str | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.raw_response = raw_response


class SDKFailure(HarnessFailure):
    """Non-recoverable SDK error (auth, model API error, network)."""

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.cause = cause


class TimeoutFailure(HarnessFailure):
    """Invocation exceeded ``agent_config.latency_budget_seconds``."""


class _CLIResultError(Exception):
    """Internal signal: ResultMessage carried is_error=True.

    Raised from inside ``_collect_response`` so the caller can convert into
    the appropriate ``HarnessFailure`` subclass with full agent-name /
    invocation-id context. Not part of the public API.
    """

    def __init__(
        self,
        *,
        error_text: str,
        partial_response: str,
        stop_reason: str | None,
    ) -> None:
        super().__init__(error_text)
        self.error_text = error_text
        self.partial_response = partial_response
        self.stop_reason = stop_reason


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


class HarnessSuccess(BaseModel, frozen=True):
    """Successful synthesizer invocation result returned to the runner."""

    response_text: str
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


# ---------------------------------------------------------------------------
# SDK response accumulation helpers
# ---------------------------------------------------------------------------


def _tokens_from_usage(usage: dict[str, Any], previous: TokensUsed) -> TokensUsed:
    """Merge an SDK ``usage`` dict into *previous*, preserving fields the SDK omits."""
    return TokensUsed(
        input_tokens=usage.get("input_tokens", previous.input_tokens),
        output_tokens=usage.get("output_tokens", previous.output_tokens),
        cache_read_tokens=usage.get("cache_read_input_tokens", previous.cache_read_tokens),
        cache_write_tokens=usage.get("cache_creation_input_tokens", previous.cache_write_tokens),
    )


def _absorb_metadata(
    message: Any,
    *,
    stop_reason: str | None,
    tokens: TokensUsed,
) -> tuple[str | None, TokensUsed]:
    """Update accumulator state from any message that carries metadata — pure transformation."""
    if message.stop_reason:
        stop_reason = message.stop_reason
    if message.usage:
        tokens = _tokens_from_usage(message.usage, tokens)
    return stop_reason, tokens


async def _collect_response(
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    *,
    prompt: str,
    options: Any,
) -> tuple[str, str | None, TokensUsed, int]:
    """Drive the SDK generator to completion.

    Returns ``(response_text, stop_reason, tokens_used, tool_calls)``.
    ``stop_reason`` is ``None`` when the SDK did not surface it.
    ``tool_calls`` counts ``ToolUseBlock`` instances in assistant messages.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    text_parts: list[str] = []
    stop_reason: str | None = None
    tokens = TokensUsed(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    tool_calls = 0

    query_iter = sdk_query_fn(prompt=prompt, options=options)
    try:
        async for message in query_iter:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls += 1
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
                )
            elif isinstance(message, ResultMessage):
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
                )
                if message.is_error:
                    raise _CLIResultError(
                        error_text=message.result or "(no result text)",
                        partial_response="".join(text_parts),
                        stop_reason=stop_reason,
                    )
                break
    finally:
        # Close from this task; GC-time aclose() races the SDK reader
        # and prints "asynchronous generator is already running" to stderr.
        await cast(AsyncGenerator[Any], query_iter).aclose()

    return "".join(text_parts), stop_reason, tokens, tool_calls


# ---------------------------------------------------------------------------
# Diagnostic state
# ---------------------------------------------------------------------------


@dataclass
class _DiagState:
    """Mutable diagnostic state accumulated during an invocation."""

    agent_name: str
    invocation_id: str
    prompt_text: str
    user_message: str
    model: str
    archive_root: Path | None

    response_text: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: TokensUsed = field(
        default_factory=lambda: TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        )
    )
    tool_calls_used: int = 0

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None:
        """Flush the diagnostic record to disk if archive_root is set."""
        if self.archive_root is None:
            return
        diag_dir = (
            self.archive_root
            / INVOCATIONS_DIRNAME
            / self.invocation_id
            / "analysis"
            / self.agent_name
        )
        diag_dir.mkdir(parents=True, exist_ok=True)

        (diag_dir / "prompt.md").write_text(self.prompt_text, encoding="utf-8")
        (diag_dir / "user_message.md").write_text(self.user_message, encoding="utf-8")
        (diag_dir / "response.md").write_text(self.response_text, encoding="utf-8")
        (diag_dir / "errors.json").write_text(json.dumps(self.errors, indent=2), encoding="utf-8")
        metadata: dict[str, Any] = {
            "model": self.model,
            "tokens_used": self.tokens_used.model_dump(),
            "tool_calls_used": self.tool_calls_used,
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# SDK options builder
# ---------------------------------------------------------------------------


def _build_sdk_options(
    agent_config: BaseAgentConfig,
    *,
    prompt_text: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any],
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the synthesizer invocation.

    ``setting_sources=[]`` keeps the SDK from loading developer
    ``.claude/settings.json`` (hooks/permissions); ``tools=[]`` disables all
    built-in CLI tools (Bash/Read/Edit/etc.); ``strict-mcp-config`` tells the
    CLI to ignore plugin-level MCP servers (e.g. Linear, GitHub registered
    via user-scope plugins) and only use ``--mcp-config``. Together these
    guarantee the agent runs system_prompt + user_message + the allowlisted
    portfolio-state tools only.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for an
    output-token cap. ``mcp_servers`` registers the in-process SDK MCP server
    that backs the portfolio-state tool callables.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=prompt_text,
        model=agent_config.model,
        tools=[],
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
        max_turns=_MAX_TURNS,
        setting_sources=[],
        extra_args={"strict-mcp-config": None},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_synthesizer(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    portfolio_reader: SynthesizerPortfolioStateReader,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the synthesizer agent and return :class:`HarnessSuccess`.

    Parameters
    ----------
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        latency budget, output token budget).
    user_message:
        The pre-assembled input bundle passed as the user turn.
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate the
        diagnostic archive directory.
    portfolio_reader:
        :class:`SynthesizerPortfolioStateReader` instance bound to the current
        portfolio snapshot. The harness wires its three methods as MCP tools
        via :func:`build_portfolio_state_mcp_server`.
    archive_root:
        Root path for the invocation archive. Pass ``None`` to skip diagnostic
        writes (e.g. in testing contexts that don't need them).
    sdk_query_fn:
        Callable matching ``claude_agent_sdk.query``. Defaults to the real SDK
        function. Inject a stub in tests.

    Raises
    ------
    EmptyResponseFailure
        Non-error response with no text content paired with stop_reason=end_turn.
    ContextOverflowFailure
        Empty response paired with stop_reason=max_tokens.
    SDKFailure
        Authentication or non-recoverable SDK error.
    TimeoutFailure
        Invocation exceeded ``agent_config.latency_budget_seconds``.
    """
    from claude_agent_sdk import ClaudeSDKError, CLIConnectionError

    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.synthesizer.value

    mcp_servers, allowed_tools = build_portfolio_state_mcp_server(portfolio_reader)
    prompt_text = await _load_prompt(agent_config.prompt)
    options = _build_sdk_options(
        agent_config,
        prompt_text=prompt_text,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
    )

    diag = _DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
    )
    wall_start = time.monotonic()

    def _flush_failure(stop_reason: str | None) -> None:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=stop_reason,
        )

    try:
        response_text, stop_reason, tokens, tool_calls = await asyncio.wait_for(
            _collect_response(sdk_query_fn, prompt=user_message, options=options),
            timeout=float(agent_config.latency_budget_seconds),
        )
    except TimeoutError as exc:
        _flush_failure(None)
        raise TimeoutFailure(
            f"Invocation exceeded latency budget of {agent_config.latency_budget_seconds}s",
            agent_name=agent_name,
            invocation_id=invocation_id,
        ) from exc
    except _CLIResultError as exc:
        _flush_failure(exc.stop_reason)
        raise SDKFailure(
            f"CLI returned is_error=True: {exc.error_text}",
            agent_name=agent_name,
            invocation_id=invocation_id,
        ) from exc
    except CLIConnectionError as exc:
        _flush_failure(None)
        raise SDKFailure(
            f"Authentication or connection failure — ensure CLAUDE_CODE_OAUTH_TOKEN "
            f"is set and valid. Underlying error: {exc}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            cause=exc,
        ) from exc
    except ClaudeSDKError as exc:
        _flush_failure(None)
        raise SDKFailure(
            f"Non-recoverable SDK error: {exc}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            cause=exc,
        ) from exc

    diag.response_text = response_text
    diag.tokens_used = tokens
    diag.tool_calls_used = tool_calls
    wall_elapsed = time.monotonic() - wall_start

    # Layer-4 stop-reason check. The synthesizer's output is unstructured
    # prose, so a non-empty response (any stop_reason) returns success.
    # Empty + max_tokens classifies as context overflow; empty + anything
    # else classifies as a non-truncated empty response. No retry path.
    if response_text:
        diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason)
        return HarnessSuccess(
            response_text=response_text,
            tokens_used=tokens,
            tool_calls_used=tool_calls,
            wall_clock_seconds=wall_elapsed,
            stop_reason=stop_reason,
        )

    is_overflow = stop_reason == "max_tokens"
    diag.errors.append(
        {
            "stage": "stop_reason_check",
            "classification": "context_overflow" if is_overflow else "empty_response",
            "stop_reason": stop_reason,
        }
    )
    _flush_failure(stop_reason)
    if is_overflow:
        raise ContextOverflowFailure(
            "Empty response with stop_reason=max_tokens — context overflow, no retry",
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response=response_text,
        )
    raise EmptyResponseFailure(
        f"Empty response with stop_reason={stop_reason!r} — no usable prose",
        agent_name=agent_name,
        invocation_id=invocation_id,
        raw_response=response_text,
    )
