"""LLM invocation harness for qualitative-researcher agent — story 04b (ALP-249).

Wraps the Claude Agent SDK call with the agent's tool allowlist registered,
runs the parser (story 03a) and validator (story 03b) on the response,
executes a single corrective retry on parse-or-validation failure, and
re-classifies failures paired with ``stop_reason: max_tokens`` as
:class:`ContextOverflowFailure`.

Structurally mirrors :mod:`alphamind.analysis.domain_researchers.harness`;
the differences are confined to: parser/validator imports, retry-message
text, the tools-allowlist plumbing, and ``tool_calls_used`` accounting.

Architecture note: ``invoke_qualitative_researcher`` accepts ``sdk_query_fn``
for dependency injection.  In production the default (the real
``claude_agent_sdk.query``) is used.  Tests pass a stub so no test touches
the Anthropic API.
"""

from __future__ import annotations

# ruff: noqa: N818  # Exception class names are spec-mandated (ALP-249 story scope)
import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

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

# ---------------------------------------------------------------------------
# Per-process system-prompt cache
# ---------------------------------------------------------------------------

# Maps prompt file path → loaded prompt text.  No invalidation needed:
# the pipeline process restarts on agents.yaml edits per the deploy-time
# vs. invocation-time classification in configuration-management.md.
# The lock gates the read-then-write so concurrent orchestrator coroutines
# can't redundantly re-read the same file.
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

# Per the qualitative-research design doc: cumulative_tool_call_limit=15
# soft, with headroom for the agent's reasoning turns.  ``max_turns`` bounds
# the SDK loop covering tool calls + final text generation.
_MAX_TURNS = 25


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class HarnessFailure(Exception):
    """Base class for all harness-level failures.

    Every subclass carries *agent_name* and *invocation_id* so the caller
    (story 06's runner) has full context for the failure log.
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


class MalformedOutputFailure(HarnessFailure):
    """Exhausted retries on parse / Layer-2 / Layer-3 failure.

    Carries the raw response text from both attempts when a retry occurred,
    plus the underlying error trail.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_name: str,
        invocation_id: str,
        raw_response_initial: str | None = None,
        raw_response_retry: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, agent_name=agent_name, invocation_id=invocation_id)
        self.raw_response_initial = raw_response_initial
        self.raw_response_retry = raw_response_retry
        self.cause = cause


class ContextOverflowFailure(HarnessFailure):
    """stop_reason: max_tokens paired with any structural failure.

    The pipeline aborts immediately — no corrective retry is attempted.
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
    """Non-recoverable SDK error (auth, model API error, network post-retry,
    or tool-allowlist drift)."""

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
    """Invocation exceeded the per-call timeout (agent_config.latency_budget_seconds)."""


class _CLIResultError(Exception):
    """Internal signal: ResultMessage carried is_error=True.

    Raised from inside ``_collect_response`` so the caller (``_invoke``)
    can convert into the appropriate ``HarnessFailure`` subclass with full
    agent-name / invocation-id context. Not part of the public API.
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
    """Successful invocation result returned to the qualitative-researcher runner."""

    brief: QualitativeBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed  # imported from _shared, NOT redefined here
    tool_calls_used: int  # cumulative across both attempts
    wall_clock_seconds: float


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


def _add_tokens(a: TokensUsed, b: TokensUsed) -> TokensUsed:
    """Sum two :class:`TokensUsed` instances field-wise."""
    return TokensUsed(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
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
    ``stop_reason`` is ``None`` when the SDK did not surface it (treated as
    ``end_turn`` by the harness per the spec: "missing metadata → malformed_output,
    not context_overflow"). ``tool_calls`` counts ``ToolUseBlock`` instances in
    assistant messages — this is the qualitative-researcher's tool-budget metric.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    text_parts: list[str] = []
    stop_reason: str | None = None
    tokens = TokensUsed(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    tool_calls = 0

    async for message in sdk_query_fn(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls += 1
            stop_reason, tokens = _absorb_metadata(message, stop_reason=stop_reason, tokens=tokens)
        elif isinstance(message, ResultMessage):
            stop_reason, tokens = _absorb_metadata(message, stop_reason=stop_reason, tokens=tokens)
            if message.is_error:
                raise _CLIResultError(
                    error_text=message.result or "(no result text)",
                    partial_response="".join(text_parts),
                    stop_reason=stop_reason,
                )
            break

    return "".join(text_parts), stop_reason, tokens, tool_calls


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Emit a single corrected qualitative brief. "
    "No prose preceding or following the structured content. "
    "Use exactly the section headers "
    "=== NARRATIVE THREADS ===, === CATALYST WATCH ===, === SENTIMENT SNAPSHOT ==="
)

_CONTRACT_REF = (
    "See docs/design/03-analysis-layer/qualitative-research.md § Output § Output schema."
)


def _build_retry_message(framing: str, error_detail: str) -> str:
    """Construct a corrective-retry message.

    Per llm-output-validation.md § Corrective-retry message construction:
    - Explicit framing line naming which contract failed
    - First error only (caller extracts it)
    - Contract reference
    - Directive with exact section headers
    - Does NOT contain: full error list, analytical guidance, raw input data
    """
    return "\n\n".join([framing, error_detail, _CONTRACT_REF, _SECTION_DIRECTIVE])


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = (
        "The prior response did not meet the parse contract for the qualitative researcher output."
    )
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for "
        "the qualitative researcher output."
    )
    first_error = result.errors[0]
    error_detail = (
        f"Field: {first_error.field_path}\nRule: {first_error.rule}\nError: {first_error.message}"
    )
    return _build_retry_message(framing, error_detail)


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

    response_initial: str = ""
    response_retry: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: TokensUsed = field(
        default_factory=lambda: TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        )
    )
    retry_count: int = 0
    tool_calls_used: int = 0

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None:
        """Flush the diagnostic record to disk, if archive_root is set."""
        if self.archive_root is None:
            return
        diag_dir = (
            self.archive_root / "invocations" / self.invocation_id / "analysis" / self.agent_name
        )
        diag_dir.mkdir(parents=True, exist_ok=True)

        (diag_dir / "prompt.md").write_text(self.prompt_text, encoding="utf-8")
        (diag_dir / "user_message.md").write_text(self.user_message, encoding="utf-8")
        (diag_dir / "response_initial.md").write_text(self.response_initial, encoding="utf-8")
        if self.response_retry is not None:
            (diag_dir / "response_retry.md").write_text(self.response_retry, encoding="utf-8")
        (diag_dir / "errors.json").write_text(json.dumps(self.errors, indent=2), encoding="utf-8")
        metadata: dict[str, Any] = {
            "model": self.model,
            "retry_count": self.retry_count,
            "tokens_used": self.tokens_used.model_dump(),
            "tool_calls_used": self.tool_calls_used,
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Parse-and-validate helper
# ---------------------------------------------------------------------------


def _parse_and_validate(
    response: str,
    invocation_id: str,
    universe: frozenset[str],
    stop_reason: str | None,
    attempt: int,
    diag: _DiagState,
) -> tuple[QualitativeBrief | None, str | None]:
    """Parse *response* and validate the result.

    Returns ``(brief, retry_message)``.  When the brief is ``None``, a
    corrective-retry message is returned.  Raises
    :class:`ContextOverflowFailure` immediately when the failure is paired
    with ``stop_reason == 'max_tokens'``.
    """
    try:
        brief = parse_qualitative_brief(response, invocation_id=invocation_id)
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
                raw_response=response,
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
                raw_response=response,
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
    """Resolve agent_config.tools against the registry and build the SDK MCP server.

    Returns ``(allowed_tools, mcp_servers)`` ready for
    :class:`ClaudeAgentOptions`.  ``allowed_tools`` carries the bundled
    CLI's ``mcp__<server>__<tool>`` wire form so the permission filter
    matches what the model emits.  ``mcp_servers`` is keyed by the
    qualitative-research server name and registers each tool's handler
    via the :mod:`alphamind.analysis.tools._sdk_adapter` decorator wrap.

    Raises :class:`SDKFailure` immediately if any name in
    ``agent_config.tools`` is not registered in
    :data:`alphamind.analysis.tools.TOOLS` — fail loudly rather than
    silently dropping tool privileges.  An empty ``agent_config.tools``
    yields ``([], {})``: no MCP server is registered.
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

    ``setting_sources=[]`` keeps the SDK from loading developer
    ``.claude/settings.json`` (hooks/permissions) — this agent must run
    system_prompt + user_message + the allowlisted tools only.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for
    an output-token cap.  ``mcp_servers`` registers the in-process SDK
    MCP server that backs the tool callables; without it the SDK CLI
    returns "tool not found" when the model emits a tool_use block.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=prompt_text,
        model=agent_config.model,
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
        max_turns=_MAX_TURNS,
        setting_sources=[],
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)},
    )


# ---------------------------------------------------------------------------
# Corrective-retry execution helper
# ---------------------------------------------------------------------------


async def _run_retry_attempt(
    *,
    retry_message: str,
    response1: str,
    tokens1: TokensUsed,
    tool_calls1: int,
    universe: frozenset[str],
    diag: _DiagState,
    wall_start: float,
    invoke: Any,
) -> HarnessSuccess:
    """Execute Attempt 2 and return :class:`HarnessSuccess` or raise.

    Extracted to keep ``invoke_qualitative_researcher`` below the C901/PLR0915
    thresholds.  All mutable state is passed explicitly.
    """
    diag.retry_count = 1

    response2, stop_reason2, tokens2, tool_calls2 = await invoke(retry_message)
    diag.response_retry = response2
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    brief2, _ = _parse_and_validate(
        response2, diag.invocation_id, universe, stop_reason2, attempt=2, diag=diag
    )

    wall_elapsed = time.monotonic() - wall_start

    if brief2 is None:
        diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
        raise MalformedOutputFailure(
            "Parse or validation failed on both initial and retry attempts. "
            f"First retry error: {diag.errors[-1].get('message', '')}",
            agent_name=diag.agent_name,
            invocation_id=diag.invocation_id,
            raw_response_initial=response1,
            raw_response_retry=response2,
        )

    diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
    return HarnessSuccess(
        brief=brief2,
        raw_response=response2,
        retry_count=1,
        tokens_used=diag.tokens_used,
        tool_calls_used=diag.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
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

    Parameters
    ----------
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        tool allowlist, latency budget, output token budget).
    user_message:
        The pre-assembled input bundle passed as the user turn.
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate
        the diagnostic archive directory and to populate the parsed brief.
    session:
        SQLAlchemy session passed to each tool's ``callable_factory``.
    universe:
        Asset-universe ticker set used by the validator's catalyst-watch
        ticker check.
    archive_root:
        Root path for the invocation archive.  Pass ``None`` to skip
        diagnostic writes (e.g. in testing contexts that don't need them).
    sdk_query_fn:
        Callable matching the signature of ``claude_agent_sdk.query``.
        Defaults to the real SDK function.  Inject a stub in tests.

    Raises
    ------
    MalformedOutputFailure
        Parse or validation failure after exhausting the one allowed retry.
    ContextOverflowFailure
        Any structural failure paired with ``stop_reason: max_tokens``.
    SDKFailure
        Authentication, non-recoverable SDK error, or tool-allowlist drift.
    TimeoutFailure
        Invocation exceeded ``agent_config.latency_budget_seconds``.
    """
    from claude_agent_sdk import ClaudeSDKError, CLIConnectionError

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

    diag = _DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
    )
    wall_start = time.monotonic()

    def _flush_failure(stop_reason: str | None = None) -> None:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=stop_reason,
        )

    async def _invoke(prompt: str) -> tuple[str, str | None, TokensUsed, int]:
        """Run one SDK call with the configured timeout."""
        try:
            return await asyncio.wait_for(
                _collect_response(sdk_query_fn, prompt=prompt, options=options),
                timeout=float(agent_config.latency_budget_seconds),
            )
        except TimeoutError as exc:
            _flush_failure()
            raise TimeoutFailure(
                f"Invocation exceeded latency budget of {agent_config.latency_budget_seconds}s",
                agent_name=agent_name,
                invocation_id=invocation_id,
            ) from exc
        except _CLIResultError as exc:
            _flush_failure(exc.stop_reason)
            raise ContextOverflowFailure(
                f"CLI returned is_error=True: {exc.error_text}",
                agent_name=agent_name,
                invocation_id=invocation_id,
                raw_response=exc.partial_response,
            ) from exc
        except CLIConnectionError as exc:
            _flush_failure()
            raise SDKFailure(
                f"Authentication or connection failure — ensure CLAUDE_CODE_OAUTH_TOKEN "
                f"is set and valid. Underlying error: {exc}",
                agent_name=agent_name,
                invocation_id=invocation_id,
                cause=exc,
            ) from exc
        except ClaudeSDKError as exc:
            _flush_failure()
            raise SDKFailure(
                f"Non-recoverable SDK error: {exc}",
                agent_name=agent_name,
                invocation_id=invocation_id,
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Attempt 1: initial call
    # ------------------------------------------------------------------
    response1, stop_reason1, tokens1, tool_calls1 = await _invoke(user_message)
    diag.response_initial = response1
    diag.tokens_used = tokens1
    diag.tool_calls_used = tool_calls1

    try:
        brief, retry_message = _parse_and_validate(
            response1, invocation_id, universe, stop_reason1, attempt=1, diag=diag
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
            raw_response=response1,
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
        response1=response1,
        tokens1=tokens1,
        tool_calls1=tool_calls1,
        universe=universe,
        diag=diag,
        wall_start=wall_start,
        invoke=_invoke,
    )
