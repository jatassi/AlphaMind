"""LLM invocation harness for domain researcher agents — story 07 (ALP-198).

Wraps the Claude Agent SDK call, runs the parser (story 04) and validator
(story 05) on the response, executes a single corrective retry on
parse-or-validation failure, and re-classifies failures paired with
``stop_reason: max_tokens`` as :class:`ContextOverflowFailure`.

This module is the integration point where the SDK, the system prompt,
the input bundle, the parser, and the validator compose into a single call
surface used by the per-sector runner (story 10).

Architecture note: ``invoke_domain_researcher`` accepts ``sdk_query_fn``
for dependency injection.  In production the default (the real
``claude_agent_sdk.query``) is used.  Tests pass a stub so no test
touches the Anthropic API.
"""

from __future__ import annotations

# ruff: noqa: N818  # Exception class names are spec-mandated (ALP-198 story scope)
import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from alphamind.analysis._schema_tightening import _tighten_conditional_schema
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.models import (
    REQUIRED_BY_SIGNAL_QUALITY,
    SectorBrief,
)
from alphamind.analysis.domain_researchers.parser import ParseError, parse_brief
from alphamind.analysis.domain_researchers.validation import (
    ValidationResult,
    validate_brief,
)
from alphamind.config.models.agents import AgentName, BaseAgentConfig

__all__ = [
    "ContextOverflowFailure",
    "HarnessFailure",
    "HarnessSuccess",
    "MalformedOutputFailure",
    "SDKFailure",
    "TimeoutFailure",
    "invoke_domain_researcher",
]

# ---------------------------------------------------------------------------
# Per-process system-prompt cache
# ---------------------------------------------------------------------------

# Maps prompt file path → loaded prompt text.  No invalidation needed:
# the pipeline process restarts on agents.yaml edits per the deploy-time
# vs. invocation-time classification in configuration-management.md.
# The lock gates the read-then-write so concurrent orchestrator coroutines
# (three sectors at once) can't redundantly re-read the same file.
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
# Agent-name derivation
# ---------------------------------------------------------------------------

_SECTOR_TO_AGENT: dict[Sector, AgentName] = {
    Sector.TECH_SEMIS: AgentName.tech_semis_researcher,
    Sector.FINANCIALS: AgentName.financials_researcher,
    Sector.ENERGY: AgentName.energy_researcher,
}


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class HarnessFailure(Exception):
    """Base class for all harness-level failures.

    Every subclass carries *agent_name* and *invocation_id* so the caller
    (per-sector runner, story 10) has full context for the failure log.
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
    """Non-recoverable SDK error (auth, model API error, network post-retry)."""

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


class _StuckSDKCall(Exception):
    """Internal signal: SDK produced no message before the init-stall timeout.

    A healthy SDK call emits ``SystemMessage`` within a few seconds of
    spawning the CLI subprocess. A multi-minute silence with zero messages
    indicates a stalled subprocess or backend admit-rate starvation when
    several researcher invocations race for the same OAuth token. ``_invoke``
    retries once on this signal before giving up.
    """


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


class HarnessSuccess(BaseModel, frozen=True):
    """Successful invocation result returned to the per-sector runner."""

    brief: SectorBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed  # imported from _shared, NOT redefined here
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# SDK response accumulation helpers
# ---------------------------------------------------------------------------


def _record_failure_diag(
    diag: Any,
    wall_start: float,
    *,
    stop_reason: str | None,
) -> None:
    """Write the on-disk diagnostic for a failed invocation."""
    diag.write(
        success=False,
        wall_clock_seconds=time.monotonic() - wall_start,
        stop_reason=stop_reason,
    )


def _convert_invoke_error(
    exc: Exception,
    *,
    diag: Any,
    wall_start: float,
    agent_name: str,
    invocation_id: str,
    budget_seconds: float,
    init_stall_timeout: float,
) -> HarnessFailure:
    """Map an SDK-call exception to the matching :class:`HarnessFailure`.

    Centralizes the diag-write + exception-translation that ``_invoke``
    runs on every failure path. The retry-on-stall loop in ``_invoke``
    only invokes this for *terminal* failures (second stall attempt or any
    non-stall error), so every call here corresponds to one diag write.
    """
    from claude_agent_sdk import ClaudeSDKError, CLIConnectionError

    if isinstance(exc, _StuckSDKCall):
        _record_failure_diag(diag, wall_start, stop_reason=None)
        return TimeoutFailure(
            f"SDK call stalled before producing any message on two consecutive "
            f"attempts ({init_stall_timeout}s init timeout). Likely OAuth-token "
            f"concurrency starvation or local CLI subprocess hang.",
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    if isinstance(exc, TimeoutError):
        _record_failure_diag(diag, wall_start, stop_reason=None)
        return TimeoutFailure(
            f"Invocation exceeded latency budget of {budget_seconds}s",
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
    if isinstance(exc, _CLIResultError):
        _record_failure_diag(diag, wall_start, stop_reason=exc.stop_reason)
        return ContextOverflowFailure(
            f"CLI returned is_error=True: {exc.error_text}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            raw_response=exc.partial_response,
        )
    if isinstance(exc, CLIConnectionError):
        _record_failure_diag(diag, wall_start, stop_reason=None)
        return SDKFailure(
            f"Authentication or connection failure — ensure CLAUDE_CODE_OAUTH_TOKEN "
            f"is set and valid. Underlying error: {exc}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            cause=exc,
        )
    if isinstance(exc, ClaudeSDKError):
        _record_failure_diag(diag, wall_start, stop_reason=None)
        return SDKFailure(
            f"Non-recoverable SDK error: {exc}",
            agent_name=agent_name,
            invocation_id=invocation_id,
            cause=exc,
        )
    raise exc  # pragma: no cover  # caller should not pass other exception types


def _absorb_assistant_message(message: Any, text_parts: list[str]) -> None:
    """Append every TextBlock in *message*'s content to *text_parts* in place."""
    from claude_agent_sdk import TextBlock

    for block in message.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)


def _absorb_result_metadata(
    message: Any,
    *,
    stop_reason: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> tuple[str | None, int, int, int, int]:
    """Update accumulator state from a ``ResultMessage`` — pure transformation."""
    if message.stop_reason:
        stop_reason = message.stop_reason
    if message.usage:
        usage = message.usage
        input_tokens = usage.get("input_tokens", input_tokens)
        output_tokens = usage.get("output_tokens", output_tokens)
        cache_read_tokens = usage.get("cache_read_input_tokens", cache_read_tokens)
        cache_write_tokens = usage.get("cache_creation_input_tokens", cache_write_tokens)
    return stop_reason, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens


async def _next_message(
    async_iter: AsyncIterator[Any],
    *,
    init_stall_timeout_seconds: float | None,
) -> Any:
    """Pull the next SDK message, applying the init-stall timeout if requested.

    Returns ``None`` on iterator exhaustion. Raises :class:`_StuckSDKCall`
    when ``init_stall_timeout_seconds`` is set and no message arrives in
    that window — the caller passes ``None`` after the first message to
    drop the watchdog.
    """
    if init_stall_timeout_seconds is None:
        try:
            return await anext(async_iter)
        except StopAsyncIteration:
            return None
    try:
        return await asyncio.wait_for(anext(async_iter), timeout=init_stall_timeout_seconds)
    except StopAsyncIteration:
        return None
    except TimeoutError as exc:
        raise _StuckSDKCall(
            f"No SDK message received within {init_stall_timeout_seconds}s"
        ) from exc


async def _collect_response(
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    *,
    prompt: str,
    options: Any,
    init_stall_timeout_seconds: float | None = None,
) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed]:
    """Drive the SDK generator to completion.

    Returns ``(structured_output, response_text, stop_reason, tokens_used)``.

    ``structured_output`` is the dict the API delivers on ``ResultMessage``
    when ``output_format`` is set; ``None`` when the SDK did not populate it
    (the harness's parse path treats ``None`` as a parse failure). The
    concatenated ``response_text`` is preserved alongside for diagnostic-
    record forensics — JSON-mode runs typically have empty text but Sonnet
    occasionally narrates between turns.

    When ``init_stall_timeout_seconds`` is set, the wait for the *first*
    SDK message is bounded by that timeout. A healthy call emits a
    ``SystemMessage`` within a few seconds of spawn; multi-minute silence
    with zero messages signals a stuck local CLI subprocess or backend
    admit-rate starvation, in which case :class:`_StuckSDKCall` is raised
    so the caller can retry. After the first message arrives the timeout
    no longer applies — extended thinking can take minutes between
    messages and is not a stall.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage

    text_parts: list[str] = []
    structured_output: dict[str, Any] | None = None
    stop_reason: str | None = None
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0

    # ``max_turns=1`` (set on options) is load-bearing for this loop:
    # we overwrite per-message usage rather than summing across turns,
    # which is correct only when there's exactly one assistant turn.
    query_iter = sdk_query_fn(prompt=prompt, options=options)
    async_iter = aiter(query_iter)
    pending_stall = init_stall_timeout_seconds
    try:
        while True:
            message = await _next_message(async_iter, init_stall_timeout_seconds=pending_stall)
            if message is None:
                break
            pending_stall = None  # only the first message is watchdogged
            if isinstance(message, AssistantMessage):
                _absorb_assistant_message(message, text_parts)
                if message.stop_reason:
                    stop_reason = message.stop_reason
                if message.usage:
                    usage = message.usage
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                    cache_write_tokens = usage.get("cache_creation_input_tokens", 0)
            elif isinstance(message, ResultMessage):
                (
                    stop_reason,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                ) = _absorb_result_metadata(
                    message,
                    stop_reason=stop_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
                raw_so = getattr(message, "structured_output", None)
                if isinstance(raw_so, dict):
                    structured_output = raw_so
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

    response_text = "".join(text_parts)
    tokens = TokensUsed(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    return structured_output, response_text, stop_reason, tokens


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the sector brief as a JSON payload conforming to the "
    "SectorBrief schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit. When "
    "`signal_quality` is `degraded`, `signal_quality_reason` must be a "
    "non-empty string; otherwise it must be `null`. Reference IDs must use "
    "the sector's prefix (`SA-TECH`, `SA-FIN`, or `SA-ENERGY`)."
)

_CONTRACT_REF = (
    "See docs/design/03-analysis-layer/domain-researchers/tech-semis.md "
    "§ Domain researcher output contract."
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
    framing = "The prior response did not meet the parse contract for the domain researcher output."
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for the domain researcher output."
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
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Parse-and-validate helper
# ---------------------------------------------------------------------------


def _parse_and_validate(
    payload: dict[str, Any] | None,
    response_text: str,
    sector: Sector,
    invocation_id: str,
    stop_reason: str | None,
    attempt: int,
    diag: _DiagState,
) -> tuple[SectorBrief | None, str | None]:
    """Parse the structured *payload* and validate the result.

    Returns ``(brief, retry_message)``.  When the brief is ``None``, a
    corrective-retry message is returned.  Raises
    :class:`ContextOverflowFailure` immediately when the failure is paired
    with ``stop_reason == 'max_tokens'``.

    *response_text* is the concatenated text-block content from the same
    SDK call, carried forward only for the ContextOverflowFailure raw_response
    field — JSON-mode runs may have empty text but the diagnostic must still
    carry whatever the model said.
    """
    try:
        brief = parse_brief(payload, sector, invocation_id=invocation_id)
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

    validation = validate_brief(brief)
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


def _render_raw_response(payload: dict[str, Any] | None, response_text: str) -> str:
    """Format the SDK response for HarnessSuccess.raw_response and the diagnostic."""
    parts: list[str] = []
    if response_text:
        parts.append(response_text)
    if payload is not None:
        parts.append(json.dumps(payload, indent=2, sort_keys=True))
    else:
        parts.append("(structured_output not populated)")
    return "\n\n".join(parts)


def _build_sector_brief_schema() -> dict[str, Any]:
    """Generate SectorBrief's JSON schema with the conditional-field tightener.

    A single schema serves all three sectors — the harness sets ``sector``
    on the payload pre-validate so the API only checks the closed-set enum.
    The tightener wraps the brief in a per-signal_quality ``oneOf`` so a
    DEGRADED branch with ``signal_quality_reason: null`` is rejected
    pre-parse.
    """
    schema = SectorBrief.model_json_schema()
    _tighten_conditional_schema(
        schema, SectorBrief, "signal_quality", REQUIRED_BY_SIGNAL_QUALITY
    )
    return schema


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_domain_researcher(
    *,
    agent_config: BaseAgentConfig,
    sector: Sector,
    user_message: str,
    invocation_id: str,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke a domain researcher agent and return a validated :class:`HarnessSuccess`.

    Parameters
    ----------
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        latency budget, output token budget).
    sector:
        Which sector this researcher is analyzing.  Used for parsing and
        validation, and to derive the agent name.
    user_message:
        The input bundle passed as the user turn.
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate
        the diagnostic archive directory.
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
        Authentication or non-recoverable SDK error.
    TimeoutFailure
        Invocation exceeded ``agent_config.latency_budget_seconds``.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, CLIConnectionError

    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = _SECTOR_TO_AGENT[sector].value
    prompt_text = await _load_prompt(agent_config.prompt)

    # ``setting_sources=[]`` keeps the SDK from loading developer
    # ``.claude/settings.json`` (hooks/permissions) — this agent must run
    # system_prompt + user_message only.  ``CLAUDE_CODE_MAX_OUTPUT_TOKENS``
    # is the only path the CLI exposes for an output-token cap (no
    # ``max_tokens`` field on ``ClaudeAgentOptions``, no ``--max-tokens``
    # CLI flag). ``output_format`` flips the agent into JSON-Schema mode so
    # the API enforces the ``SectorBrief`` shape post-generation; the dict
    # surfaces on ``ResultMessage.structured_output``.
    options = ClaudeAgentOptions(
        system_prompt=prompt_text,
        model=agent_config.model,
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)},
        output_format={"type": "json_schema", "schema": _build_sector_brief_schema()},
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

    async def _invoke(
        prompt: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed]:
        """Run one SDK call with stall-retry and the configured timeout.

        Retries once if the SDK call stalls before producing any message —
        a healthy call emits a ``SystemMessage`` within seconds of spawn,
        so a 60s init silence signals either an unhealthy local CLI
        subprocess or backend admit-rate starvation when multiple sibling
        researcher invocations race for the same OAuth token. Other failure
        modes (budget timeout, CLI error, auth failure) are not retried.
        """
        init_stall_timeout = 60.0
        budget = float(agent_config.latency_budget_seconds)
        for stall_attempt in (1, 2):
            try:
                return await asyncio.wait_for(
                    _collect_response(
                        sdk_query_fn,
                        prompt=prompt,
                        options=options,
                        init_stall_timeout_seconds=init_stall_timeout,
                    ),
                    timeout=budget,
                )
            except _StuckSDKCall as exc:
                if stall_attempt == 1:
                    continue
                raise _convert_invoke_error(
                    exc,
                    diag=diag,
                    wall_start=wall_start,
                    agent_name=agent_name,
                    invocation_id=invocation_id,
                    budget_seconds=budget,
                    init_stall_timeout=init_stall_timeout,
                ) from exc
            except (
                TimeoutError,
                _CLIResultError,
                CLIConnectionError,
                ClaudeSDKError,
            ) as exc:
                raise _convert_invoke_error(
                    exc,
                    diag=diag,
                    wall_start=wall_start,
                    agent_name=agent_name,
                    invocation_id=invocation_id,
                    budget_seconds=budget,
                    init_stall_timeout=init_stall_timeout,
                ) from exc
        raise AssertionError(  # pragma: no cover
            "unreachable: stall retry loop exhausted without returning or raising"
        )

    # ------------------------------------------------------------------
    # Attempt 1: initial call
    # ------------------------------------------------------------------
    payload1, text1, stop_reason1, tokens1 = await _invoke(user_message)
    raw_response_initial = _render_raw_response(payload1, text1)
    diag.response_initial = raw_response_initial
    diag.tokens_used = tokens1

    try:
        brief, retry_message = _parse_and_validate(
            payload1, text1, sector, invocation_id, stop_reason1, attempt=1, diag=diag
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
            wall_clock_seconds=wall_elapsed,
        )

    # ------------------------------------------------------------------
    # Attempt 2: corrective retry
    # ------------------------------------------------------------------
    assert retry_message is not None
    diag.retry_count = 1

    payload2, text2, stop_reason2, tokens2 = await _invoke(retry_message)
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = TokensUsed(
        input_tokens=tokens1.input_tokens + tokens2.input_tokens,
        output_tokens=tokens1.output_tokens + tokens2.output_tokens,
        cache_read_tokens=tokens1.cache_read_tokens + tokens2.cache_read_tokens,
        cache_write_tokens=tokens1.cache_write_tokens + tokens2.cache_write_tokens,
    )

    brief2, _ = _parse_and_validate(
        payload2, text2, sector, invocation_id, stop_reason2, attempt=2, diag=diag
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
        wall_clock_seconds=wall_elapsed,
    )
