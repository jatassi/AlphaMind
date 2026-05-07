"""LLM invocation harness for the portfolio-manager agent — story 07 (ALP-329).

Wraps the Claude Agent SDK call, registers the FOUR MCP servers
(:mod:`validate_guardrail` from analyst story 04, :mod:`retrieve_brief` from
the synthesizer's existing factory, :mod:`get_thesis_components` from PM
story 05, :mod:`submit_envelope` from PM story 06c), runs the completion-
sentinel parser (story 06a) on the response, executes a single corrective
retry on parse failure, and re-classifies failures paired with
``stop_reason: max_tokens`` as :class:`ContextOverflowFailure`.

Per parent decision (D), the PM's structured output is the thin completion
sentinel (:class:`PMCompletionRecord`) — envelopes flow through
``submit_envelope`` tool calls inside the SDK loop, not the structured-output
payload. The Layer-2/3 envelope validator (story 06b) runs inside the
``submit_envelope`` MCP wrapper, not directly from the harness.

Structurally mirrors :mod:`alphamind.decision.strategist.harness`; the
differences are confined to: (1) two additional MCP-server factories merged
into ``mcp_servers`` / ``allowed_tools``, (2) the parser is the sentinel-only
one, (3) no Layer-3 validator is invoked from the harness path, (4) a
``submission_log.json`` diagnostic-archive file capturing the engine-stub's
submission log, (5) a ``HarnessSuccess`` shape that carries the submission
log tuple alongside the standard fields, (6) a ``_MAX_TURNS`` cap of 40
(versus 25 elsewhere) since the PM emits one ``submit_envelope`` tool call
per envelope plus zero-or-more validation/retrieval calls per envelope.

Architecture note: ``invoke_pm`` accepts ``sdk_query_fn`` for dependency
injection. In production the default (the real ``claude_agent_sdk.query``)
is used. Tests pass a stub so no test touches the Anthropic API.
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

from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.retrieval_tools import build_retrieve_brief_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision.portfolio_manager.models import PMCompletionRecord
from alphamind.decision.portfolio_manager.parser import ParseError, parse_pm_completion_record
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.execution.oms.submit_envelope_mcp import (
    SubmissionLogEntry,
    SubmitEnvelopeState,
    build_submit_envelope_mcp_server,
)
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    PortfolioManagerView,
)
from alphamind.portfolio_state.consumers.portfolio_manager_thesis_mcp import (
    build_get_thesis_components_mcp_server,
)
from alphamind.risk_guardrails.guardrail_evaluation import LibraryConfig, MarketInputs
from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState
from alphamind.risk_guardrails.state_delivery.validation_tool_mcp import (
    build_validate_guardrail_mcp_server,
)

__all__ = [
    "ContextOverflowFailure",
    "HarnessFailure",
    "HarnessSuccess",
    "MalformedOutputFailure",
    "SDKFailure",
    "TimeoutFailure",
    "invoke_pm",
]


# ---------------------------------------------------------------------------
# Per-process system-prompt cache
# ---------------------------------------------------------------------------

# Maps prompt file path → loaded prompt text. No invalidation needed: the
# pipeline process restarts on agents.yaml edits per the deploy-time vs.
# invocation-time classification in configuration-management.md. Mirrors the
# analyst's pattern.
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

# Bounds the SDK loop covering tool calls + final sentinel generation. The PM
# emits one ``submit_envelope`` per envelope plus zero-or-more
# ``validate_guardrail`` / ``retrieve_brief`` / ``get_thesis_components``
# calls per envelope. 40 leaves comfortable headroom for an upper-end
# 6-envelope decision day with 4-5 tool calls per envelope.
_MAX_TURNS = 40

# Real tool calls go through the four in-process MCP servers registered as
# ``alphamind_decision_validation``, ``alphamind_synthesizer_retrieval``,
# ``alphamind_portfolio_state_thesis_components``, and
# ``alphamind_execution_oms_submit``; the bundled CLI rewrites those names to
# ``mcp__<server>__<tool>`` on the wire. Any other ``ToolUseBlock.name``
# (``ToolSearch``, ``StructuredOutput``, …) is an SDK-internal pseudo-event
# injected by the JSON-Schema output mode and must not count against the
# agent's tool budget.
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__",
    "mcp__alphamind_synthesizer_retrieval__",
    "mcp__alphamind_portfolio_state_thesis_components__",
    "mcp__alphamind_execution_oms_submit__",
)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class HarnessFailure(Exception):
    """Base class for all harness-level failures.

    Every subclass carries *agent_name* and *invocation_id* so the caller
    (the runner, story 08) has full context for the failure log.
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
    """Exhausted retries on parse failure.

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
    """Structural failure paired with ``stop_reason: max_tokens``.

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
    """Non-recoverable SDK error (auth, model API error, network, CLI error)."""

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
    """Successful PM invocation result returned to the runner.

    Per story 07 § 2, the success record carries the parsed
    :class:`PMCompletionRecord` sentinel, the retry count, the cumulative
    :class:`TokensUsed`, the tool-call count, the wall-clock seconds, the
    SDK-reported stop reason, and the engine-stub's per-envelope submission
    log captured from :class:`SubmitEnvelopeState`'s mutable cell.
    """

    output: PMCompletionRecord
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None
    submission_log: tuple[SubmissionLogEntry, ...]


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
) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed, int]:
    """Drive the SDK generator to completion.

    Returns ``(structured_output, response_text, stop_reason, tokens_used,
    tool_calls)``.

    ``structured_output`` is the dict the API delivers on ``ResultMessage``
    when ``output_format`` is set; ``None`` when the SDK did not populate it.
    The concatenated ``response_text`` is preserved alongside for diagnostic
    forensics — JSON-mode runs typically have empty text but Sonnet/Opus
    occasionally narrate between tool calls.

    ``tool_calls`` counts only ``ToolUseBlock``s whose ``name`` starts with
    one of :data:`_TOOL_NAME_PREFIXES`; the SDK's JSON-Schema output mode
    injects ``ToolSearch`` and ``StructuredOutput`` pseudo-events that would
    otherwise inflate the count.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    text_parts: list[str] = []
    structured_output: dict[str, Any] | None = None
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
                    elif isinstance(block, ToolUseBlock) and block.name.startswith(
                        _TOOL_NAME_PREFIXES
                    ):
                        tool_calls += 1
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
                )
            elif isinstance(message, ResultMessage):
                stop_reason, tokens = _absorb_metadata(
                    message, stop_reason=stop_reason, tokens=tokens
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
        # Close from this task; GC-time aclose() races the SDK reader and
        # prints "asynchronous generator is already running" to stderr.
        await cast(AsyncGenerator[Any], query_iter).aclose()

    return structured_output, "".join(text_parts), stop_reason, tokens, tool_calls


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the PM completion sentinel as a JSON payload conforming to the "
    "PMCompletionRecord schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit."
)

_CONTRACT_REF = "See docs/design/04-decision-layer/pm-envelope-schema.md."


def _build_retry_message(framing: str, error_detail: str) -> str:
    """Construct a corrective-retry message.

    Per llm-output-validation.md § Corrective-retry message construction:
    - Explicit framing line naming which contract failed
    - First error only (caller extracts it)
    - Contract reference
    - Directive
    - Does NOT contain: full error list, analytical guidance, raw input data
    """
    return "\n\n".join([framing, error_detail, _CONTRACT_REF, _SECTION_DIRECTIVE])


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = "The prior response did not meet the parse contract for the PM completion sentinel."
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


_RETRY_PROMPT_DELIMITER = "\n\n--- Retry diagnostic ---\n\n"


def _compose_retry_prompt(original_user_message: str, retry_diagnostic: str) -> str:
    """Prepend the original user_message to the retry SDK call.

    Without the user_message in the retry SDK call, the model loses portfolio /
    pre-processor / synthesizer / guardrail context and falls back to
    regurgitating the system prompt's example block (observed in ALP-311
    strategist post-mortem; the same defense applies here).
    """
    return original_user_message + _RETRY_PROMPT_DELIMITER + retry_diagnostic


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
    submit_envelope_state: SubmitEnvelopeState

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
        """Flush the diagnostic record to disk if archive_root is set.

        Path: ``<archive_root>/invocations/<invocation_id>/decision/portfolio_manager/``
        — mirrors the strategist pattern with the agent segment switched to
        ``portfolio_manager``. The PM-only ``submission_log.json`` and
        ``failed_submission_log.json`` are written alongside the standard six
        files; they capture the engine-stub's ``state.submission_log`` (calls
        whose payload parsed to a :class:`PMEnvelope`) and
        ``state.failed_submission_log`` (Layer-1 Pydantic parse failures)
        respectively after the SDK loop completes so the verify script
        (story 09) can inspect every envelope the PM attempted.
        """
        if self.archive_root is None:
            return
        diag_dir = (
            self.archive_root / "invocations" / self.invocation_id / "decision" / self.agent_name
        )
        diag_dir.mkdir(parents=True, exist_ok=True)

        (diag_dir / "prompt.md").write_text(self.prompt_text, encoding="utf-8")
        (diag_dir / "user_message.md").write_text(self.user_message, encoding="utf-8")
        (diag_dir / "response_initial.md").write_text(self.response_initial, encoding="utf-8")
        if self.response_retry is not None:
            (diag_dir / "response_retry.md").write_text(self.response_retry, encoding="utf-8")
        (diag_dir / "errors.json").write_text(json.dumps(self.errors, indent=2), encoding="utf-8")
        metadata: dict[str, Any] = {
            "invocation_id": self.invocation_id,
            "model": self.model,
            "retry_count": self.retry_count,
            "tokens_used": self.tokens_used.model_dump(),
            "tool_calls_used": self.tool_calls_used,
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        submission_log = [
            {
                "envelope": entry.envelope.model_dump(mode="json"),
                "submission_results": [r.model_dump(mode="json") for r in entry.submission_results],
            }
            for entry in self.submit_envelope_state.submission_log
        ]
        (diag_dir / "submission_log.json").write_text(
            json.dumps(submission_log, indent=2), encoding="utf-8"
        )

        failed_submission_log = [
            {
                "command_id": entry.command_id,
                "validation_error_repr": entry.validation_error_repr,
                "raw_args": entry.raw_args,
            }
            for entry in self.submit_envelope_state.failed_submission_log
        ]
        (diag_dir / "failed_submission_log.json").write_text(
            json.dumps(failed_submission_log, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Parse helper
# ---------------------------------------------------------------------------


def _parse_payload(
    payload: dict[str, Any] | None,
    response_text: str,
    invocation_id: str,
    stop_reason: str | None,
    attempt: int,
    diag: _DiagState,
) -> tuple[PMCompletionRecord | None, str | None]:
    """Parse the structured *payload*.

    Returns ``(output, retry_message)``. When the output is ``None``, a
    corrective-retry message is returned. Raises
    :class:`ContextOverflowFailure` immediately when the failure is paired
    with ``stop_reason == 'max_tokens'``.
    """
    try:
        output = parse_pm_completion_record(payload, invocation_id=invocation_id)
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

    return output, None


def _render_raw_response(payload: dict[str, Any] | None, response_text: str) -> str:
    """Format the SDK response for diagnostic archive and exception payloads.

    The structured-output dict is the load-bearing artifact; any text the
    agent emitted alongside (rare in JSON mode but seen under provocation)
    is preserved as a leading section so forensic review is not lossy.
    """
    parts: list[str] = []
    if response_text:
        parts.append(response_text)
    if payload is not None:
        parts.append(json.dumps(payload, indent=2, sort_keys=True))
    else:
        parts.append("(structured_output not populated)")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# MCP wiring helper
# ---------------------------------------------------------------------------


def _build_mcp_wiring(  # noqa: PLR0913 — runner-facing signature mirrors per-invocation parameters
    *,
    initial_validation_state: ValidationToolState,
    initial_submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
) -> tuple[dict[str, Any], list[str]]:
    """Compose the four MCP servers and merge their allowed-tool lists.

    Returns ``(merged_servers, merged_allowed_tools)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``.
    """
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(
        initial_validation_state
    )
    retrieval_servers, retrieval_tools = build_retrieve_brief_mcp_server(retrieval_store)
    thesis_servers, thesis_tools = build_get_thesis_components_mcp_server(thesis_component_reader)
    submit_servers, submit_tools = build_submit_envelope_mcp_server(
        initial_submit_envelope_state,
        retrieval_store=retrieval_store,
        pre_processor_bundle=pre_processor_bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
        library_config=library_config,
        library_market=library_market,
    )
    merged_servers: dict[str, Any] = {
        **validation_servers,
        **retrieval_servers,
        **thesis_servers,
        **submit_servers,
    }
    merged_tools: list[str] = [
        *validation_tools,
        *retrieval_tools,
        *thesis_tools,
        *submit_tools,
    ]
    return merged_servers, merged_tools


_INCOMPAT_KEYWORDS: frozenset[str] = frozenset({"format", "discriminator"})
_NAMED_CHILD_CONTAINERS: frozenset[str] = frozenset({"properties", "$defs"})


def _strip_anthropic_incompat_keys(obj: Any) -> Any:
    """Strip JSON Schema keywords the Anthropic API JSON-Schema mode silently rejects.

    Empirically determined via direct SDK testing (analyst harness): the
    Anthropic API silently falls back to text-output mode (the model emits
    JSON in TextBlocks rather than calling the SDK-injected
    ``StructuredOutput`` tool, leaving ``ResultMessage.structured_output``
    ``None``) when the supplied schema contains either of:

    - ``format`` keys (notably ``"date-time"`` and ``"date"``) — Pydantic emits
      these for ``datetime`` / ``date`` fields. :class:`PMCompletionRecord`
      has one such occurrence on ``timestamp``.
    - ``discriminator`` keyword — Pydantic emits this for
      ``Annotated[Union[...], Discriminator(...)]``. :class:`PMCompletionRecord`
      itself has none, but the function is shape-faithful to the analyst /
      strategist version so the schema-stripping contract is uniform across
      the three decision-layer agents.

    Stripping these does not weaken validation: the ``datetime`` Python type
    coerces ISO-8601 strings on parse; the keys are purely metadata for the
    API's schema-binding step.
    """
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if key in _INCOMPAT_KEYWORDS:
                continue
            if key in _NAMED_CHILD_CONTAINERS and isinstance(value, dict):
                result[key] = {
                    name: _strip_anthropic_incompat_keys(sub) for name, sub in value.items()
                }
            else:
                result[key] = _strip_anthropic_incompat_keys(value)
        return result
    if isinstance(obj, list):
        return [_strip_anthropic_incompat_keys(x) for x in obj]
    return obj


def _build_sdk_options(
    agent_config: BaseAgentConfig,
    *,
    prompt_text: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any],
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the PM invocation.

    ``setting_sources=[]`` keeps the SDK from loading developer
    ``.claude/settings.json`` (hooks/permissions); ``tools=[]`` disables all
    built-in CLI tools (Bash/Read/Edit/etc.); ``strict-mcp-config`` tells the
    CLI to ignore plugin-level MCP servers and only use ``--mcp-config``.
    Together these guarantee the agent runs ``system_prompt + user_message +
    the four allowlisted decision-layer tools`` only.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for an
    output-token cap. ``output_format`` flips the agent into JSON-Schema mode
    so the API enforces the :class:`PMCompletionRecord` shape post-generation;
    the dict surfaces on ``ResultMessage.structured_output``. The schema is
    passed through :func:`_strip_anthropic_incompat_keys` to remove keywords
    the API silently rejects.
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
        output_format={
            "type": "json_schema",
            "schema": _strip_anthropic_incompat_keys(PMCompletionRecord.model_json_schema()),
        },
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
    diag: _DiagState,
    wall_start: float,
    invoke: Any,
) -> HarnessSuccess:
    """Execute Attempt 2 and return :class:`HarnessSuccess` or raise.

    Extracted to keep ``invoke_pm`` below the C901/PLR0915 thresholds. All
    mutable state is passed explicitly.
    """
    diag.retry_count = 1

    retry_prompt = _compose_retry_prompt(diag.user_message, retry_message)
    payload2, text2, stop_reason2, tokens2, tool_calls2 = await invoke(retry_prompt)
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    output2, _ = _parse_payload(
        payload2,
        text2,
        diag.invocation_id,
        stop_reason2,
        attempt=2,
        diag=diag,
    )

    wall_elapsed = time.monotonic() - wall_start

    if output2 is None:
        diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
        raise MalformedOutputFailure(
            "Parse failed on both initial and retry attempts. "
            f"First retry error: {diag.errors[-1].get('message', '')}",
            agent_name=diag.agent_name,
            invocation_id=diag.invocation_id,
            raw_response_initial=raw_response_initial,
            raw_response_retry=raw_response_retry,
        )

    diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
    return HarnessSuccess(
        output=output2,
        retry_count=1,
        tokens_used=diag.tokens_used,
        tool_calls_used=diag.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
        stop_reason=stop_reason2,
        submission_log=diag.submit_envelope_state.submission_log,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_pm(  # noqa: PLR0913 — public signature is fixed by ALP-329 § 3.
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    initial_submit_envelope_state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the PM agent and return :class:`HarnessSuccess`.

    Parameters
    ----------
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        latency budget, output-token budget).
    user_message:
        The pre-assembled input bundle passed as the user turn (story 04).
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate the
        diagnostic archive directory and to populate the parsed sentinel.
    initial_validation_state:
        Per-invocation :class:`ValidationToolState` the validate_guardrail
        MCP wrapper closes over. Cumulative-impact tracking happens inside
        the wrapper's mutable cell — the harness does not see those updates.
    initial_submit_envelope_state:
        Per-invocation :class:`SubmitEnvelopeState` the submit_envelope MCP
        wrapper closes over. The post-loop ``submission_log`` is captured on
        ``HarnessSuccess`` and persisted to ``submission_log.json``.
    retrieval_store:
        Per-invocation :class:`RetrievalStore` the retrieve_brief MCP tool
        and the submit_envelope wrapper's Layer-3 references both consume.
    thesis_component_reader:
        Per-invocation :class:`PortfolioManagerThesisComponentReader` the
        get_thesis_components MCP tool wraps.
    pre_processor_bundle:
        The pre-processor bundle the PM is reasoning over; threaded into the
        submit_envelope wrapper for the Layer-3 ``source_recommendation_id``
        resolution check.
    pm_view:
        The :class:`PortfolioManagerView` projection; threaded into the
        submit_envelope wrapper for the Layer-3 ``position_id`` resolution
        check.
    active_sectors:
        The active portfolio profile's ``active_sectors`` set, threaded into
        the submit_envelope wrapper for the sector-in-active-set check.
    halt_mode:
        Whether the invocation is running in halt (risk-reduction) mode;
        forwarded to the submit_envelope wrapper.
    sector_resolver:
        ``ticker → sector`` mapping the submit_envelope wrapper consults
        when advancing :class:`ProjectedDelta` for accepted commands.
    library_config, library_market:
        Forwarded to the submit_envelope wrapper for runner-signature
        parity; the engine-stub does not consult them directly.
    archive_root:
        Root path for the invocation archive. Pass ``None`` to skip
        diagnostic writes.
    sdk_query_fn:
        Callable matching ``claude_agent_sdk.query``. Defaults to the real
        SDK function. Inject a stub in tests.

    Raises
    ------
    MalformedOutputFailure
        Parse failure after exhausting the one allowed retry.
    ContextOverflowFailure
        Any structural failure paired with ``stop_reason: max_tokens``.
    SDKFailure
        Authentication, non-recoverable SDK error, or CLI-error result.
    TimeoutFailure
        Invocation exceeded ``agent_config.latency_budget_seconds``.
    """
    from claude_agent_sdk import ClaudeSDKError, CLIConnectionError

    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.portfolio_manager.value

    mcp_servers, allowed_tools = _build_mcp_wiring(
        initial_validation_state=initial_validation_state,
        initial_submit_envelope_state=initial_submit_envelope_state,
        retrieval_store=retrieval_store,
        thesis_component_reader=thesis_component_reader,
        pre_processor_bundle=pre_processor_bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
        library_config=library_config,
        library_market=library_market,
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
        submit_envelope_state=initial_submit_envelope_state,
    )
    wall_start = time.monotonic()

    def _flush_failure(stop_reason: str | None = None) -> None:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=stop_reason,
        )

    async def _invoke(
        prompt: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed, int]:
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
            raise SDKFailure(
                f"CLI returned is_error=True: {exc.error_text}",
                agent_name=agent_name,
                invocation_id=invocation_id,
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
    payload1, text1, stop_reason1, tokens1, tool_calls1 = await _invoke(user_message)
    raw_response_initial = _render_raw_response(payload1, text1)
    diag.response_initial = raw_response_initial
    diag.tokens_used = tokens1
    diag.tool_calls_used = tool_calls1

    try:
        output, retry_message = _parse_payload(
            payload1,
            text1,
            invocation_id,
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

    if output is not None:
        wall_elapsed = time.monotonic() - wall_start
        diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason1)
        return HarnessSuccess(
            output=output,
            retry_count=0,
            tokens_used=tokens1,
            tool_calls_used=tool_calls1,
            wall_clock_seconds=wall_elapsed,
            stop_reason=stop_reason1,
            submission_log=diag.submit_envelope_state.submission_log,
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
        diag=diag,
        wall_start=wall_start,
        invoke=_invoke,
    )
