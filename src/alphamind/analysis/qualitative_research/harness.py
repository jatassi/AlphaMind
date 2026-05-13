"""LLM invocation harness for qualitative-researcher agent — story 04b (ALP-249).

Wraps the Claude Agent SDK call with the agent's tool allowlist registered,
runs the parser (story 03a) and validator (story 03b) on the response,
executes a single corrective retry on parse-or-validation failure, and
re-classifies failures paired with ``stop_reason: max_tokens`` as
:class:`ContextOverflowFailure`.

The shared SDK driver loop, exception hierarchy, prompt cache, diagnostic
writer, and retry-message builder live in
:mod:`alphamind.analysis._harness_core`. Only the parser/validator wiring,
tool-allowlist plumbing, and qualitative-researcher-specific options
builder live here.

Architecture note: ``invoke_qualitative_researcher`` accepts ``sdk_query_fn``
for dependency injection.  In production the default (the real
``claude_agent_sdk.query``) is used.  Tests pass a stub so no test touches
the Anthropic API.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from alphamind.analysis._harness_core import (
    _PROMPT_CACHE,
    ContextOverflowFailure,
    DiagState,
    HarnessFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    _add_tokens,
    _build_retry_message,
    _load_prompt,
    _render_raw_response,
    invoke_sdk,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.qualitative_research.parser import ParseError, parse_qualitative_brief
from alphamind.analysis.qualitative_research.validation import (
    ValidationResult,
    validate_qualitative_brief,
)
from alphamind.analysis.tools import TOOLS
from alphamind.analysis.tools._sdk_adapter import build_analysis_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig

__all__ = [
    "ContextOverflowFailure",
    "HarnessFailure",
    "HarnessSuccess",
    "MalformedOutputFailure",
    "SDKFailure",
    "TimeoutFailure",
    "invoke_qualitative_researcher",
]

# Re-export internals tests reach for via the module attribute.
__all__ += ["_PROMPT_CACHE", "_load_prompt"]

# ---------------------------------------------------------------------------
# Multi-turn budget
# ---------------------------------------------------------------------------

# Per the qualitative-research design doc: cumulative_tool_call_limit=15
# soft, with headroom for the agent's reasoning turns.  ``max_turns`` bounds
# the SDK loop covering tool calls + final text generation.
_MAX_TURNS = 25

# Real tool calls go through the in-process MCP server registered as
# ``alphamind_qualitative`` (see :func:`_resolve_tools`); the bundled CLI
# rewrites those names to ``mcp__alphamind_qualitative__<tool>`` on the wire.
# Any other ``ToolUseBlock.name`` (``ToolSearch``, ``StructuredOutput``, …)
# is an SDK-internal pseudo-event injected by the JSON-Schema output mode and
# must not count against the agent's tool budget.
_TOOL_NAME_PREFIX = "mcp__alphamind_qualitative__"


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


class HarnessSuccess(BaseModel, frozen=True):
    """Successful invocation result returned to the qualitative-researcher runner."""

    brief: QualitativeBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed  # imported from _shared, NOT redefined here
    tool_calls_used: int  # cumulative across both attempts
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# Corrective-retry messages
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the qualitative brief as a JSON payload conforming to the "
    "QualitativeBrief schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit. When "
    "`signal_quality` is `degraded`, `signal_quality_reason` must be a "
    "non-empty string; otherwise it must be `null`."
)

_CONTRACT_REF = (
    "See docs/design/03-analysis-layer/qualitative-research.md § Output § Output schema."
)


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = (
        "The prior response did not meet the parse contract for the qualitative researcher output."
    )
    return _build_retry_message(
        framing=framing,
        error_detail=f"Field: {error.field_path}\nError: {error.message}",
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for "
        "the qualitative researcher output."
    )
    first_error = result.errors[0]
    return _build_retry_message(
        framing=framing,
        error_detail=(
            f"Field: {first_error.field_path}\n"
            f"Rule: {first_error.rule}\n"
            f"Error: {first_error.message}"
        ),
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


# ---------------------------------------------------------------------------
# Parse-and-validate helper
# ---------------------------------------------------------------------------


def _parse_and_validate(
    payload: dict[str, Any] | None,
    response_text: str,
    invocation_id: str,
    universe: frozenset[str],
    stop_reason: str | None,
    attempt: int,
    diag: DiagState,
) -> tuple[QualitativeBrief | None, str | None]:
    """Parse and validate. Returns ``(brief, retry_message)``.

    ``brief=None`` means the response failed parse/validation and a
    corrective retry message is returned. Raises
    :class:`ContextOverflowFailure` immediately when paired with
    ``stop_reason == 'max_tokens'``.
    """
    try:
        brief = parse_qualitative_brief(payload, invocation_id=invocation_id)
    except ParseError as exc:
        diag.errors.append(
            {
                "stage": "parse",
                "attempt": attempt,
                "field_path": exc.field_path,
                "message": exc.message,
            }
        )
        if stop_reason == "max_tokens":
            raise ContextOverflowFailure(
                "Parse failure with stop_reason=max_tokens — context overflow, no retry",
                agent_name=diag.agent_name,
                invocation_id=diag.invocation_id,
                raw_response=_render_raw_response(payload, response_text),
            ) from exc
        return None, _build_retry_message_for_parse_error(exc)

    validation = validate_qualitative_brief(brief, universe=universe)
    if not validation.is_valid:
        for ve in validation.errors:
            diag.errors.append(
                {
                    "stage": "validation",
                    "attempt": attempt,
                    "field_path": ve.field_path,
                    "rule": ve.rule,
                    "message": ve.message,
                }
            )
        if stop_reason == "max_tokens":
            raise ContextOverflowFailure(
                "Validation failure with stop_reason=max_tokens — context overflow, no retry",
                agent_name=diag.agent_name,
                invocation_id=diag.invocation_id,
                raw_response=_render_raw_response(payload, response_text),
            )
        return None, _build_retry_message_for_validation_failure(validation)

    return brief, None


# ---------------------------------------------------------------------------
# Tool-allowlist plumbing
# ---------------------------------------------------------------------------


def _resolve_tools(
    agent_config: BaseAgentConfig,
    session: Session,
    *,
    agent_name: str,
    invocation_id: str,
) -> tuple[list[str], dict[str, Any]]:
    """Resolve ``agent_config.tools`` against the registry and build the SDK MCP server.

    Returns ``(allowed_tools, mcp_servers)``. ``allowed_tools`` carries the
    bundled CLI's ``mcp__<server>__<tool>`` wire form. An empty
    ``agent_config.tools`` yields ``([], {})``. Raises :class:`SDKFailure`
    if any tool name is not registered in
    :data:`alphamind.analysis.tools.TOOLS`.
    """
    missing = [name for name in agent_config.tools if name not in TOOLS]
    if missing:
        raise SDKFailure(
            f"Tool name(s) {missing!r} declared in agent config but not registered in "
            f"alphamind.analysis.tools.TOOLS; available: {sorted(TOOLS)!r}",
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    if not agent_config.tools:
        return [], {}
    mcp_servers, allowed = build_analysis_mcp_server(
        server_name="alphamind_qualitative",
        tool_names=agent_config.tools,
        session=session,
    )
    return allowed, mcp_servers


def _build_sdk_options(
    agent_config: BaseAgentConfig,
    *,
    prompt_text: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any],
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the qualitative-researcher invocation.

    Pins the autonomous-agent contract: ``setting_sources=[]`` blocks
    developer ``.claude/settings.json``; ``tools=[]`` disables built-in CLI
    tools; ``strict-mcp-config`` ignores plugin-level MCP servers; and only
    the allowlisted research-tool MCP server is registered.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for
    an output-token cap. ``output_format`` flips the agent into JSON-Schema
    mode so the API enforces the ``QualitativeBrief`` shape; the
    ``signal_quality_reason ↔ signal_quality`` invariant is enforced
    post-parse (the Anthropic API rejects top-level ``oneOf``).
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
        output_format={"type": "json_schema", "schema": QualitativeBrief.model_json_schema()},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_qualitative_researcher(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    session: Session,
    universe: frozenset[str],
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the qualitative-researcher agent and return a validated :class:`HarnessSuccess`.

    ``universe`` is the asset-universe ticker set used by the validator's
    catalyst-watch check. ``archive_root=None`` skips diagnostic writes.
    ``sdk_query_fn`` defaults to ``claude_agent_sdk.query``; tests inject a
    stub. Raises ``MalformedOutputFailure`` (retry exhausted),
    ``ContextOverflowFailure`` (``stop_reason=max_tokens``), ``SDKFailure``
    (auth / non-recoverable / tool-allowlist drift), or ``TimeoutFailure``.
    """
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.qualitative_researcher.value

    allowed_tools, mcp_servers = _resolve_tools(
        agent_config, session, agent_name=agent_name, invocation_id=invocation_id
    )
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
        record_tool_calls=True,
    )
    wall_start = time.monotonic()

    async def _invoke(
        prompt: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed, int]:
        outcome = await invoke_sdk(
            sdk_query_fn=sdk_query_fn,
            prompt=prompt,
            options=options,
            diag=diag,
            budget_seconds=float(agent_config.latency_budget_seconds),
            init_stall_timeout_seconds=None,
            wall_start=wall_start,
            agent_name=agent_name,
            invocation_id=invocation_id,
            on_cli_result_error="context_overflow",
            tool_name_prefix=_TOOL_NAME_PREFIX,
        )
        return (
            outcome.structured_output,
            outcome.response_text,
            outcome.stop_reason,
            outcome.tokens_used,
            outcome.tool_calls,
        )

    # ------------------------------------------------------------------
    # Attempt 1: initial call
    # ------------------------------------------------------------------
    payload1, text1, stop_reason1, tokens1, tool_calls1 = await _invoke(user_message)
    raw_response_initial = _render_raw_response(payload1, text1)
    diag.response_initial = raw_response_initial
    diag.tokens_used = tokens1
    diag.tool_calls_used = tool_calls1

    try:
        brief, retry_message = _parse_and_validate(
            payload1, text1, invocation_id, universe, stop_reason1, attempt=1, diag=diag
        )
    except ContextOverflowFailure:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=stop_reason1,
        )
        raise

    if brief is not None:
        wall_elapsed = time.monotonic() - wall_start
        diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason1)
        return HarnessSuccess(
            brief=brief,
            raw_response=raw_response_initial,
            retry_count=0,
            tokens_used=tokens1,
            tool_calls_used=tool_calls1,
            wall_clock_seconds=wall_elapsed,
        )

    # ------------------------------------------------------------------
    # Attempt 2: corrective retry
    # ------------------------------------------------------------------
    assert retry_message is not None
    diag.retry_count = 1

    payload2, text2, stop_reason2, tokens2, tool_calls2 = await _invoke(retry_message)
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    brief2, _ = _parse_and_validate(
        payload2, text2, invocation_id, universe, stop_reason2, attempt=2, diag=diag
    )

    wall_elapsed = time.monotonic() - wall_start

    if brief2 is None:
        diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
        raise MalformedOutputFailure(
            "Parse or validation failed on both initial and retry attempts. "
            f"First retry error: {diag.errors[-1].get('message', '')}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response_initial=raw_response_initial,
            raw_response_retry=raw_response_retry,
        )

    diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
    return HarnessSuccess(
        brief=brief2,
        raw_response=raw_response_retry,
        retry_count=1,
        tokens_used=diag.tokens_used,
        tool_calls_used=diag.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
    )
