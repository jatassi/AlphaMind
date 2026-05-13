"""LLM invocation harness for adaptive-researcher agent — story 05 (ALP-263).

Wraps the Claude Agent SDK call with the agent's tool allowlist registered,
runs the parser (story 03b) and validator (story 03c) on the response,
executes a single corrective retry on parse-or-validation failure, and
re-classifies failures paired with ``stop_reason: max_tokens`` as
:class:`ContextOverflowFailure`.

The shared SDK driver loop, exception hierarchy, prompt cache, diagnostic
writer, and retry-message builder live in
:mod:`alphamind.analysis._harness_core`. Only the adaptive-researcher-
specific parser/validator wiring, tool-allowlist plumbing, options builder,
and session-resume retry path live here.

Architecture note: ``invoke_adaptive_researcher`` accepts ``sdk_query_fn``
for dependency injection.  In production the default (the real
``claude_agent_sdk.query``) is used.  Tests pass a stub so no test touches
the Anthropic API.
"""

from __future__ import annotations

# ruff: noqa: PLR0913
# PLR0913: ``invoke_adaptive_researcher`` has the spec-mandated public
# signature (eight named-only args + two optional); ``_parse_and_validate``
# threads the validator's three additional upstream-brief arguments through
# to the validator. Both are intentional per the story.
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
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
from alphamind.analysis._schema_tightening import _tighten_conditional_schema
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.adaptive_research.models import (
    REQUIRED_BY_ASSESSMENT,
    AdaptiveBrief,
    InvestigationThread,
)
from alphamind.analysis.adaptive_research.parser import ParseError, parse_adaptive_brief
from alphamind.analysis.adaptive_research.validation import (
    ValidationResult,
    validate_adaptive_brief,
)
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.tools import TOOLS
from alphamind.analysis.tools._sdk_adapter import build_analysis_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

__all__ = [
    "ContextOverflowFailure",
    "HarnessFailure",
    "HarnessSuccess",
    "MalformedOutputFailure",
    "SDKFailure",
    "TimeoutFailure",
    "invoke_adaptive_researcher",
]

# Re-export internals tests reach for via the module attribute.
__all__ += ["_PROMPT_CACHE", "_load_prompt"]

# ---------------------------------------------------------------------------
# Multi-turn budget
# ---------------------------------------------------------------------------

# Per the adaptive-research design doc: cumulative_tool_call_limit=25
# soft (set in agents.yaml), with headroom for the agent's reasoning turns.
# ``max_turns`` bounds the SDK loop covering tool calls + final text generation.
_MAX_TURNS = 30

# Real tool calls go through the in-process MCP server registered as
# ``alphamind_adaptive`` (see :func:`_resolve_tools`); the bundled CLI
# rewrites those names to ``mcp__alphamind_adaptive__<tool>`` on the wire.
# Any other ``ToolUseBlock.name`` (``ToolSearch``, ``StructuredOutput``, …)
# is an SDK-internal pseudo-event injected by the JSON-Schema output mode and
# must not count against the agent's tool budget.
_TOOL_NAME_PREFIX = "mcp__alphamind_adaptive__"


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


class HarnessSuccess(BaseModel, frozen=True):
    """Successful invocation result returned to the adaptive-researcher runner."""

    brief: AdaptiveBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed  # imported from _shared, NOT redefined here
    tool_calls_used: int  # cumulative across both attempts
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# Corrective-retry messages
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the adaptive research findings as a JSON payload conforming to the "
    "AdaptiveBrief schema attached to this invocation. The shape is API-enforced; "
    "fix the specific field named above and resubmit. For SIGNAL threads, "
    "`strengthens` and `weakens` are arrays — use `[]` (empty array) for the empty "
    "case, never `null`."
)

_CONTENT_PRESERVATION_DIRECTIVE = (
    "Your prior analytical content remains valid in this conversation; the retry "
    "is for the named field correction only. Re-emit the threads you already "
    "investigated with their existing trigger, question, tickers, sector, "
    "tools_used, findings, assessment, confidence, and conditional fields "
    "preserved. Do not collapse to an empty brief unless you truly investigated "
    "zero threads."
)

_CONTRACT_REF = "See docs/design/03-analysis-layer/adaptive-research.md § Output § Output schema."


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = (
        "The prior response did not meet the parse contract for the adaptive researcher output."
    )
    return _build_retry_message(
        framing=framing,
        error_detail=f"Field: {error.field_path}\nError: {error.message}",
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
        extra_directives=(_CONTENT_PRESERVATION_DIRECTIVE,),
    )


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for "
        "the adaptive researcher output."
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
        extra_directives=(_CONTENT_PRESERVATION_DIRECTIVE,),
    )


# ---------------------------------------------------------------------------
# Parse-and-validate helper
# ---------------------------------------------------------------------------


def _parse_and_validate(
    payload: dict[str, Any] | None,
    response_text: str,
    invocation_id: str,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    allowed_tools: frozenset[str],
    stop_reason: str | None,
    attempt: int,
    diag: DiagState,
) -> tuple[AdaptiveBrief | None, str | None]:
    """Parse and validate. Returns ``(brief, retry_message)``.

    ``brief=None`` means the response failed parse/validation and a
    corrective retry message is returned. Raises
    :class:`ContextOverflowFailure` immediately when paired with
    ``stop_reason == 'max_tokens'``.
    """
    try:
        brief = parse_adaptive_brief(payload, invocation_id=invocation_id)
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

    validation = validate_adaptive_brief(
        brief,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        universe=universe,
        allowed_tools=allowed_tools,
    )
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
) -> tuple[list[str], dict[str, Any], frozenset[str]]:
    """Resolve tool registry and build the SDK MCP server.

    Returns ``(allowed_tools, mcp_servers, validator_tool_allowlist)`` —
    the wire-form CLI tool names, the in-process MCP server registration,
    and the registry-name allowlist the validator uses for
    ``thread.tools_used``. Raises :class:`SDKFailure` on registry drift.
    """
    missing = [name for name in agent_config.tools if name not in TOOLS]
    if missing:
        raise SDKFailure(
            f"Tool name(s) {missing!r} declared in agent config but not registered in "
            f"alphamind.analysis.tools.TOOLS; available: {sorted(TOOLS)!r}",
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    validator_tool_allowlist = frozenset(agent_config.tools)
    if not agent_config.tools:
        return [], {}, validator_tool_allowlist
    mcp_servers, allowed = build_analysis_mcp_server(
        server_name="alphamind_adaptive",
        tool_names=agent_config.tools,
        session=session,
    )
    return allowed, mcp_servers, validator_tool_allowlist


def _build_adaptive_brief_schema() -> dict[str, Any]:
    """Generate AdaptiveBrief's JSON schema with the conditional-field tightener."""
    schema = AdaptiveBrief.model_json_schema()
    _tighten_conditional_schema(schema, InvestigationThread, "assessment", REQUIRED_BY_ASSESSMENT)
    return schema


def _build_sdk_options(
    agent_config: BaseAgentConfig,
    *,
    prompt_text: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any],
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the adaptive-researcher invocation."""
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
        output_format={"type": "json_schema", "schema": _build_adaptive_brief_schema()},
    )


# ---------------------------------------------------------------------------
# Corrective-retry execution helper
# ---------------------------------------------------------------------------


async def _run_retry_attempt(
    *,
    retry_message: str,
    raw_response_initial: str,
    tokens1: TokensUsed,
    tool_calls1: int,
    session_id_initial: str | None,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    validator_tool_allowlist: frozenset[str],
    diag: DiagState,
    wall_start: float,
    invoke: Any,
) -> HarnessSuccess:
    """Execute Attempt 2 and return :class:`HarnessSuccess` or raise.

    ``session_id_initial`` is threaded as ``options.resume`` so the prior
    assistant response stays in scope for the content-preservation
    directive in the retry message.
    """
    diag.retry_count = 1

    payload2, text2, stop_reason2, tokens2, tool_calls2, _session_id2 = await invoke(
        retry_message, resume_session_id=session_id_initial
    )
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    brief2, _ = _parse_and_validate(
        payload2,
        text2,
        diag.invocation_id,
        universe,
        sector_briefs,
        qualitative_brief,
        correlation_regime_brief,
        validator_tool_allowlist,
        stop_reason2,
        attempt=2,
        diag=diag,
    )

    wall_elapsed = time.monotonic() - wall_start

    if brief2 is None:
        diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
        raise MalformedOutputFailure(
            "Parse or validation failed on both initial and retry attempts. "
            f"First retry error: {diag.errors[-1].get('message', '')}",
            agent_name=diag.agent_name,
            invocation_id=diag.invocation_id,
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_adaptive_researcher(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    session: Session,
    universe: frozenset[str],
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the adaptive-researcher agent and return a validated :class:`HarnessSuccess`.

    Raises ``MalformedOutputFailure`` (retry exhausted),
    ``ContextOverflowFailure`` (``stop_reason=max_tokens``), ``SDKFailure``
    (auth / non-recoverable / tool-allowlist drift), or ``TimeoutFailure``.
    """
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.adaptive_researcher.value

    allowed_tools, mcp_servers, validator_tool_allowlist = _resolve_tools(
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
        *,
        resume_session_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed, int, str | None]:
        """Run one SDK call. ``resume_session_id`` continues an existing session."""
        call_options = (
            options if resume_session_id is None else replace(options, resume=resume_session_id)
        )
        outcome = await invoke_sdk(
            sdk_query_fn=sdk_query_fn,
            prompt=prompt,
            options=call_options,
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
            outcome.session_id,
        )

    # ------------------------------------------------------------------
    # Attempt 1: initial call
    # ------------------------------------------------------------------
    payload1, text1, stop_reason1, tokens1, tool_calls1, session_id1 = await _invoke(user_message)
    raw_response_initial = _render_raw_response(payload1, text1)
    diag.response_initial = raw_response_initial
    diag.tokens_used = tokens1
    diag.tool_calls_used = tool_calls1

    try:
        brief, retry_message = _parse_and_validate(
            payload1,
            text1,
            invocation_id,
            universe,
            sector_briefs,
            qualitative_brief,
            correlation_regime_brief,
            validator_tool_allowlist,
            stop_reason1,
            attempt=1,
            diag=diag,
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
    return await _run_retry_attempt(
        retry_message=retry_message,
        raw_response_initial=raw_response_initial,
        tokens1=tokens1,
        tool_calls1=tool_calls1,
        session_id_initial=session_id1,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        validator_tool_allowlist=validator_tool_allowlist,
        diag=diag,
        wall_start=wall_start,
        invoke=_invoke,
    )
