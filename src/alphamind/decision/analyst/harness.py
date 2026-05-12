"""LLM invocation harness for analyst agent — story 07 (ALP-298).

Wraps the Claude Agent SDK call, registers the two MCP servers
(:mod:`validate_guardrail` from story 04 + :mod:`retrieve_brief` from the
synthesizer's existing factory), runs the parser (story 05a) and validator
(story 05b) on the response, executes a single corrective retry on
parse-or-validation failure, and re-classifies failures paired with
``stop_reason: max_tokens`` as :class:`ContextOverflowFailure`.

Structurally mirrors :mod:`alphamind.analysis.qualitative_research.harness`;
the differences are confined to: parser/validator imports, two MCP servers
instead of one, retry-message text, the analyst-specific tool-allowlist, and
the JSON-Schema mode targeting :meth:`AnalystOutput.model_json_schema`.

Architecture note: ``invoke_analyst`` accepts ``sdk_query_fn`` for dependency
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
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.parser import ParseError, parse_analyst_output
from alphamind.decision.analyst.validation import (
    ValidationResult,
    validate_analyst_output,
)
from alphamind.execution.state_persistence.invocation_paths import INVOCATIONS_DIRNAME
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
    "invoke_analyst",
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

# Bounds the SDK loop covering tool calls + final text generation. The analyst
# typically calls ``retrieve_brief`` and ``validate_guardrail`` a handful of
# times per recommendation; 25 leaves ample headroom while preventing runaway
# loops. Mirrors the qualitative-researcher cap.
_MAX_TURNS = 25

# Real tool calls go through the two in-process MCP servers registered as
# ``alphamind_decision_validation`` (story 04) and
# ``alphamind_synthesizer_retrieval`` (synthesizer's factory); the bundled
# CLI rewrites those names to ``mcp__<server>__<tool>`` on the wire. Any
# other ``ToolUseBlock.name`` (``ToolSearch``, ``StructuredOutput``, …) is an
# SDK-internal pseudo-event injected by the JSON-Schema output mode and must
# not count against the agent's tool budget.
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__",
    "mcp__alphamind_synthesizer_retrieval__",
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
    """Successful analyst invocation result returned to the runner."""

    output: AnalystOutput
    raw_response: str
    retry_count: int
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
    "Re-emit the analyst output as a JSON payload conforming to the "
    "AnalystOutput schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit."
)

_CONTRACT_REF = "See docs/design/04-decision-layer/analyst-output-schema.md."


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
    framing = "The prior response did not meet the parse contract for the analyst output."
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = "The prior response did not meet the structural contract for the analyst output."
    first_error = result.errors[0]
    error_detail = (
        f"Field: {first_error.field_path}\nRule: {first_error.rule}\nError: {first_error.message}"
    )
    return _build_retry_message(framing, error_detail)


_RETRY_PROMPT_DELIMITER = "\n\n--- Retry diagnostic ---\n\n"


def _compose_retry_prompt(original_user_message: str, retry_diagnostic: str) -> str:
    """Prepend the original user_message to the retry diagnostic.

    Without the user_message in the retry SDK call, the model loses portfolio /
    synthesizer / guardrail context and falls back to regurgitating the system
    prompt's example block (observed in ALP-311 strategist post-mortem).
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

        Path: ``<archive_root>/invocations/<invocation_id>/decision/analyst/``
        — mirrors the qualitative-research pattern with the layer/agent
        segments switched to ``decision/analyst``.
        """
        if self.archive_root is None:
            return
        diag_dir = (
            self.archive_root
            / INVOCATIONS_DIRNAME
            / self.invocation_id
            / "decision"
            / self.agent_name
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
# Validator context + parse-and-validate helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ValidatorContext:
    """Per-invocation inputs the analyst's Layer-3 validator needs.

    The retrieval store and active-sector set always travel together through
    the harness's parse/validate path; bundling them keeps the helper
    signatures narrow.
    """

    retrieval_store: RetrievalStore
    active_sectors: frozenset[str]


def _parse_and_validate(
    payload: dict[str, Any] | None,
    response_text: str,
    invocation_id: str,
    validator: _ValidatorContext,
    stop_reason: str | None,
    attempt: int,
    diag: _DiagState,
) -> tuple[AnalystOutput | None, str | None]:
    """Parse the structured *payload* and validate the result.

    Returns ``(output, retry_message)``. When the output is ``None``, a
    corrective-retry message is returned. Raises
    :class:`ContextOverflowFailure` immediately when the failure is paired
    with ``stop_reason == 'max_tokens'``.
    """
    try:
        output = parse_analyst_output(payload, invocation_id=invocation_id)
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

    validation = validate_analyst_output(
        output,
        retrieval_store=validator.retrieval_store,
        active_sectors=validator.active_sectors,
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

    return output, None


def _render_raw_response(payload: dict[str, Any] | None, response_text: str) -> str:
    """Format the SDK response for HarnessSuccess.raw_response and the diagnostic.

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


def _build_mcp_wiring(
    *,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
) -> tuple[dict[str, Any], list[str]]:
    """Compose the two MCP servers and merge their allowed-tool lists.

    Returns ``(merged_servers, merged_allowed_tools)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``.
    """
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(
        initial_validation_state
    )
    retrieval_servers, retrieval_tools = build_retrieve_brief_mcp_server(retrieval_store)
    return (
        {**validation_servers, **retrieval_servers},
        [*validation_tools, *retrieval_tools],
    )


_INCOMPAT_KEYWORDS: frozenset[str] = frozenset({"format", "discriminator"})
_NAMED_CHILD_CONTAINERS: frozenset[str] = frozenset({"properties", "$defs"})


def _strip_anthropic_incompat_keys(obj: Any) -> Any:
    """Strip JSON Schema keywords the Anthropic API JSON-Schema mode silently rejects.

    Empirically determined via direct SDK testing: the Anthropic API silently
    falls back to text-output mode (the model emits JSON in TextBlocks rather
    than calling the SDK-injected ``StructuredOutput`` tool, leaving
    ``ResultMessage.structured_output`` ``None``) when the supplied schema
    contains either of:

    - ``format`` keys (notably ``"date-time"`` and ``"date"``) — Pydantic emits
      these for ``datetime`` / ``date`` fields. The analyst's schema has six
      such occurrences (``AnalystOutput.timestamp``,
      ``InvalidationLeg.condition.deadline``, ``EntryWindow.deadline``,
      ``GuardrailValidationResult.checked_at``, ``InstrumentOption.expiration``,
      ``StrategyLeg.expiration``).
    - ``discriminator`` keyword — Pydantic emits this for
      ``Annotated[Union[...], Discriminator(...)]``. The analyst's schema has
      one such occurrence on ``Recommendation.instrument`` (the
      equity/option/strategy union).

    Stripping these does not weaken validation: the ``datetime`` Python type
    coerces ISO-8601 strings on parse; the ``oneOf`` array still enforces
    union membership without the ``discriminator`` performance hint. The keys
    are purely metadata for the API's schema-binding step.

    Sibling agents already in JSON-Schema mode (qualitative-research,
    adaptive-research) emit neither key — qualitative has no ``datetime``
    fields and no discriminated unions; adaptive likewise lacks date-typed
    fields and uses an enum-based assessment field rather than a Pydantic
    ``Discriminator`` annotation.

    Walker is keyword-aware: when descending into ``properties`` or ``$defs``
    (whose dict keys are user-supplied names, not JSON Schema keywords), the
    recursion preserves every key and only strips inside the value sub-schemas.
    Outside those containers, dict keys are treated as JSON Schema keywords
    and the incompatible ones are removed. This guards against a future
    schema field that happens to be literally named ``format`` or
    ``discriminator``.
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
    """Build :class:`ClaudeAgentOptions` for the analyst invocation.

    ``setting_sources=[]`` keeps the SDK from loading developer
    ``.claude/settings.json`` (hooks/permissions); ``tools=[]`` disables all
    built-in CLI tools (Bash/Read/Edit/etc.); ``strict-mcp-config`` tells the
    CLI to ignore plugin-level MCP servers and only use ``--mcp-config``.
    Together these guarantee the agent runs system_prompt + user_message +
    the two allowlisted decision-layer tools only.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for an
    output-token cap. ``mcp_servers`` registers the two in-process SDK MCP
    servers that back the tool callables.
    ``output_format`` flips the agent into JSON-Schema mode so the API
    enforces the :class:`AnalystOutput` shape post-generation; the dict
    surfaces on ``ResultMessage.structured_output``. The schema is passed
    through :func:`_strip_anthropic_incompat_keys` to remove keywords that
    the API silently rejects (see that function's docstring).
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
            "schema": _strip_anthropic_incompat_keys(AnalystOutput.model_json_schema()),
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
    validator: _ValidatorContext,
    diag: _DiagState,
    wall_start: float,
    invoke: Any,
) -> HarnessSuccess:
    """Execute Attempt 2 and return :class:`HarnessSuccess` or raise.

    Extracted to keep ``invoke_analyst`` below the C901/PLR0915 thresholds.
    All mutable state is passed explicitly.
    """
    diag.retry_count = 1

    retry_prompt = _compose_retry_prompt(diag.user_message, retry_message)
    payload2, text2, stop_reason2, tokens2, tool_calls2 = await invoke(retry_prompt)
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    output2, _ = _parse_and_validate(
        payload2,
        text2,
        diag.invocation_id,
        validator,
        stop_reason2,
        attempt=2,
        diag=diag,
    )

    wall_elapsed = time.monotonic() - wall_start

    if output2 is None:
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
        output=output2,
        raw_response=raw_response_retry,
        retry_count=1,
        tokens_used=diag.tokens_used,
        tool_calls_used=diag.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
        stop_reason=stop_reason2,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def invoke_analyst(
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> HarnessSuccess:
    """Invoke the analyst agent and return :class:`HarnessSuccess`.

    Parameters
    ----------
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        latency budget, output-token budget).
    user_message:
        The pre-assembled input bundle passed as the user turn.
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate the
        diagnostic archive directory and to populate the parsed output.
    initial_validation_state:
        Per-invocation :class:`ValidationToolState` the validate_guardrail
        MCP wrapper closes over. Cumulative-impact tracking happens inside
        the wrapper's mutable cell — the harness does not see those updates.
    retrieval_store:
        Per-invocation :class:`RetrievalStore` the retrieve_brief MCP tool
        and the validator's Layer-3 references both consume.
    active_sectors:
        The active portfolio profile's ``active_sectors`` set, threaded into
        the validator for the sector-in-active-set check.
    archive_root:
        Root path for the invocation archive. Pass ``None`` to skip
        diagnostic writes.
    sdk_query_fn:
        Callable matching ``claude_agent_sdk.query``. Defaults to the real
        SDK function. Inject a stub in tests.

    Raises
    ------
    MalformedOutputFailure
        Parse or validation failure after exhausting the one allowed retry.
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

    agent_name = AgentName.analyst.value

    validator = _ValidatorContext(retrieval_store=retrieval_store, active_sectors=active_sectors)
    mcp_servers, allowed_tools = _build_mcp_wiring(
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
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
        output, retry_message = _parse_and_validate(
            payload1,
            text1,
            invocation_id,
            validator,
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
            raw_response=raw_response_initial,
            retry_count=0,
            tokens_used=tokens1,
            tool_calls_used=tool_calls1,
            wall_clock_seconds=wall_elapsed,
            stop_reason=stop_reason1,
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
        validator=validator,
        diag=diag,
        wall_start=wall_start,
        invoke=_invoke,
    )
