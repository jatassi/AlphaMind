"""LLM invocation harness for synthesizer agent — story 08 (ALP-208).

Wraps the Claude Agent SDK call, wires the per-invocation portfolio-state MCP
server (story 06b) via :func:`build_portfolio_state_mcp_server`, applies the
documented Layer-4 stop-reason check on the response, preserves the diagnostic
record, and propagates fail-closed errors to the caller.

Structurally simpler than the sibling researcher harnesses (qualitative,
adaptive) because the synthesizer's output is unstructured prose with no
producer-side schema — no parser, no validator, no corrective retry. Per-
invocation MCP construction closes over the caller-supplied
:class:`SynthesizerPortfolioStateReader`; the global
``alphamind.analysis.tools`` registry pattern does not fit that shape.

The shared SDK driver loop, exception hierarchy, prompt cache, and
diagnostic writer live in :mod:`alphamind.analysis._harness_core`. Only the
synthesizer-specific portfolio-tools wiring, options builder, and
empty-response classification live here.

Architecture note: ``invoke_synthesizer`` accepts ``sdk_query_fn`` for
dependency injection. In production the default (the real
``claude_agent_sdk.query``) is used. Tests pass a stub so no test touches the
Anthropic API.
"""

from __future__ import annotations

# Exception class names mirror sibling harness names by spec.
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    _PROMPT_CACHE,
    ContextOverflowFailure,
    DiagState,
    HarnessFailure,
    SDKFailure,
    TimeoutFailure,
    _load_prompt,
    invoke_sdk,
)
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

# Re-export internals tests reach for via the module attribute.
__all__ += ["_PROMPT_CACHE", "_load_prompt"]

# ---------------------------------------------------------------------------
# Multi-turn budget
# ---------------------------------------------------------------------------

# Synthesizer doesn't loop heavily — it reads pre-assembled briefs and emits
# prose. ``max_turns`` bounds the SDK loop covering portfolio-tool calls + the
# final text generation; ~15 leaves ample headroom for the three
# portfolio-state tools without enabling runaway loops.
_MAX_TURNS = 15


# ---------------------------------------------------------------------------
# Synthesizer-specific exception
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessSuccess:
    """Successful synthesizer invocation result returned to the runner."""

    response_text: str
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


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

    Pins the autonomous-agent contract: ``setting_sources=[]``,
    ``tools=[]``, ``strict-mcp-config``. Only the portfolio-state MCP
    server is registered (via ``mcp_servers``); the synthesizer emits
    unstructured prose so no ``output_format`` is set.
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


async def invoke_synthesizer(  # noqa: PLR0913 — signature dictated by synthesizer's parameter surface
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    portfolio_reader: SynthesizerPortfolioStateReader,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "synthesizer",
) -> HarnessSuccess:
    """Invoke the synthesizer agent and return :class:`HarnessSuccess`.

    ``portfolio_reader`` is bound to the current portfolio snapshot and
    wired as MCP tools via :func:`build_portfolio_state_mcp_server`.
    ``archive_root=None`` skips diagnostic writes; ``sdk_query_fn``
    defaults to ``claude_agent_sdk.query``. Raises ``EmptyResponseFailure``
    (clean empty response), ``ContextOverflowFailure`` (empty +
    ``max_tokens``), ``SDKFailure``, or ``TimeoutFailure``.
    """
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

    diag = DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        as_of=as_of,
        response_filename="response.md",
        record_tool_calls=True,
    )
    wall_start = time.monotonic()

    outcome = await invoke_sdk(
        sdk_query_fn=sdk_query_fn,
        prompt=user_message,
        options=options,
        diag=diag,
        budget_seconds=float(agent_config.latency_budget_seconds),
        init_stall_timeout_seconds=60.0,
        wall_start=wall_start,
        agent_name=agent_name,
        invocation_id=invocation_id,
        on_cli_result_error="sdk_failure",
        progress=progress,
        phase=phase,
    )

    response_text = outcome.response_text
    stop_reason = outcome.stop_reason
    tokens = outcome.tokens_used
    tool_calls = outcome.tool_calls

    diag.response_initial = response_text
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
    diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason)
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
