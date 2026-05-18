"""LLM invocation harness for the strategist agent — story 06 (ALP-307).

Wraps the Claude Agent SDK call, registers the two MCP servers
(:mod:`validate_guardrail` from analyst story 04 + :mod:`retrieve_brief` from
the synthesizer's existing factory), runs the parser (story 05a) and validator
(story 05b) on the response, executes a single corrective retry on
parse-or-validation failure, and re-classifies failures paired with
``stop_reason: max_tokens`` as :class:`ContextOverflowFailure`.

Structurally mirrors :mod:`alphamind.decision.analyst.harness`; the differences
are confined to: parser/validator imports, the StrategistOutput type fed to
``output_format``, the strategist-specific contract reference, and the
:class:`HarnessSuccess` shape (carrying ``validation_result`` and ``metadata``
per the story-06 spec rather than the analyst's flat token/tool-call counters).

Architecture note: ``run_strategist_harness`` accepts ``sdk_query_fn`` for
dependency injection. In production the default (the real
``claude_agent_sdk.query``) is used. Tests pass a stub so no test touches the
Anthropic API.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alphamind._kernel.invocations import INVOCATIONS_DIRNAME
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
    HarnessFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    _add_tokens,
    _render_raw_response,
    invoke_sdk,
)
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.retrieval_tools import build_retrieve_brief_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision._shared import system_prompt_as_file
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.parser import ParseError, parse_strategist_output
from alphamind.decision.strategist.validation import (
    ValidationResult,
    validate_strategist_output,
)
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
    "run_strategist_harness",
]


# ---------------------------------------------------------------------------
# Multi-turn budget
# ---------------------------------------------------------------------------

# Bounds the SDK loop covering tool calls + final text generation. The
# strategist typically calls ``retrieve_brief`` and ``validate_guardrail`` a
# handful of times per assessment; 25 leaves ample headroom while preventing
# runaway loops. Mirrors the analyst cap.
_MAX_TURNS = 25

# Real tool calls go through the two in-process MCP servers registered as
# ``alphamind_decision_validation`` and ``alphamind_synthesizer_retrieval``;
# the bundled CLI rewrites those names to ``mcp__<server>__<tool>`` on the
# wire. Any other ``ToolUseBlock.name`` (``ToolSearch``, ``StructuredOutput``,
# ...) is an SDK-internal pseudo-event injected by the JSON-Schema output mode
# and must not count against the agent's tool budget.
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__",
    "mcp__alphamind_synthesizer_retrieval__",
)


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessSuccess:
    """Successful strategist invocation result returned to the runner.

    Per story-06 § 6, the success record carries the parsed
    :class:`StrategistOutput`, the :class:`ValidationResult`, the cumulative
    :class:`TokensUsed`, and a ``metadata`` dict capturing
    ``invocation_id``, ``mode``, ``model``, an ``agent_config_snapshot`` of
    the budget knobs, ``total_tokens``, and ``attempts``.
    """

    output: StrategistOutput
    validation_result: ValidationResult
    tokens_used: TokensUsed
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the strategist output as a JSON payload conforming to the "
    "StrategistOutput schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit."
)

_CONTRACT_REF = "See docs/design/04-decision-layer/strategist-output-schema.md."


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
    framing = "The prior response did not meet the parse contract for the strategist output."
    error_detail = f"Field: {error.field_path}\nError: {error.message}"
    return _build_retry_message(framing, error_detail)


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = "The prior response did not meet the structural contract for the strategist output."
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
    agent_config_snapshot: dict[str, Any]

    response_initial: str = ""
    response_retry: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: TokensUsed = field(
        default_factory=lambda: TokensUsed(
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
        )
    )
    attempts: int = 0
    tool_calls_used: int = 0
    mode: str | None = None

    def write(
        self,
        *,
        success: bool,
        wall_clock_seconds: float,
        stop_reason: str | None,
    ) -> None:
        """Flush the diagnostic record to disk if archive_root is set.

        Path: ``<archive_root>/invocations/<invocation_id>/decision/strategist/``
        — mirrors the analyst pattern with the agent segment switched to
        ``strategist``.
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
            "invocation_id": self.invocation_id,
            "mode": self.mode,
            "model": self.model,
            "agent_config_snapshot": self.agent_config_snapshot,
            "total_tokens": self.tokens_used.input_tokens + self.tokens_used.output_tokens,
            "attempts": self.attempts,
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
    """Per-invocation inputs the strategist's Layer-3 validator needs."""

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
) -> tuple[StrategistOutput | None, ValidationResult | None, str | None]:
    """Parse the structured *payload* and validate the result.

    Returns ``(output, validation_result, retry_message)``. When ``output`` is
    ``None``, a corrective-retry message is returned and ``validation_result``
    is ``None``. Raises :class:`ContextOverflowFailure` immediately when the
    failure is paired with ``stop_reason == 'max_tokens'``.
    """
    try:
        output = parse_strategist_output(payload, invocation_id=invocation_id)
    except ParseError as exc:
        diag.errors.append(
            {
                "attempt": attempt,
                "kind": "parse",
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
        return None, None, _build_retry_message_for_parse_error(exc)

    validation = validate_strategist_output(
        output,
        retrieval_store=validator.retrieval_store,
        active_sectors=validator.active_sectors,
    )
    if not validation.is_valid:
        for vf in validation.errors:
            diag.errors.append(
                {
                    "attempt": attempt,
                    "kind": "validate",
                    "field_path": vf.field_path,
                    "rule": vf.rule,
                    "message": vf.message,
                }
            )
        if stop_reason == "max_tokens":
            raise ContextOverflowFailure(
                "Validation failure with stop_reason=max_tokens — context overflow, no retry",
                agent_name=diag.agent_name,
                invocation_id=diag.invocation_id,
                raw_response=_render_raw_response(payload, response_text),
            )
        return None, None, _build_retry_message_for_validation_failure(validation)

    return output, validation, None


# ---------------------------------------------------------------------------
# MCP wiring helper
# ---------------------------------------------------------------------------


def _build_mcp_wiring(
    *,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
) -> tuple[dict[str, Any], list[str]]:
    """Compose the two MCP servers and merge their allowed-tool lists."""
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(validation_state)
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
      these for ``datetime`` / ``date`` fields. ``StrategistOutput`` has
      several such occurrences (``timestamp``, ``GuardrailValidationResult.checked_at``,
      ...).
    - ``discriminator`` keyword — Pydantic emits this for
      ``Annotated[Union[...], Discriminator(...)]``. ``StrategistOutput`` uses
      one on ``PositionAssessment.action_parameters``.

    Stripping these does not weaken validation: the ``datetime`` Python type
    coerces ISO-8601 strings on parse; the ``oneOf`` array still enforces
    union membership without the ``discriminator`` performance hint. The keys
    are purely metadata for the API's schema-binding step.

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
    prompt_path: str,
    allowed_tools: list[str],
    mcp_servers: dict[str, Any],
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the strategist invocation.

    ``setting_sources=[]`` keeps the SDK from loading developer
    ``.claude/settings.json`` (hooks/permissions); ``tools=[]`` disables all
    built-in CLI tools; ``strict-mcp-config`` tells the CLI to ignore
    plugin-level MCP servers and only use ``--mcp-config``. Together these
    guarantee the agent runs ``system_prompt + user_message + the two
    allowlisted decision-layer tools`` only.
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only path the CLI exposes for an
    output-token cap. ``output_format`` flips the agent into JSON-Schema mode
    so the API enforces the :class:`StrategistOutput` shape post-generation;
    the dict surfaces on ``ResultMessage.structured_output``. The schema is
    passed through :func:`_strip_anthropic_incompat_keys` to remove keywords
    the API silently rejects.

    ``system_prompt`` is passed in file-mode (the SDK serializes it as
    ``--system-prompt-file <path>`` instead of ``--system-prompt <text>``) so
    the combined cmdline stays under Windows ``CreateProcessW``'s 32,767
    character limit — see :mod:`alphamind.decision._shared.prompt_file` for
    the full rationale.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt={"type": "file", "path": prompt_path},
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
            "schema": _strip_anthropic_incompat_keys(StrategistOutput.model_json_schema()),
        },
    )


# ---------------------------------------------------------------------------
# Success-record assembly
# ---------------------------------------------------------------------------


def _agent_config_snapshot(agent_config: BaseAgentConfig) -> dict[str, Any]:
    """Capture the budget knobs the diagnostic archive needs from *agent_config*.

    Limited to the fields the runner can reasonably reconstruct from
    ``agents.yaml``; the full prompt-path / tool list are not snapshotted to
    keep the archive footprint small.
    """
    return {
        "model": agent_config.model.value,
        "latency_budget_seconds": agent_config.latency_budget_seconds,
        "context_token_budget": agent_config.context_token_budget,
        "output_token_budget": agent_config.output_token_budget,
    }


def _build_metadata(
    *,
    invocation_id: str,
    mode: str,
    agent_config: BaseAgentConfig,
    tokens_used: TokensUsed,
    attempts: int,
) -> dict[str, Any]:
    """Build the ``HarnessSuccess.metadata`` dict per story-06 § 6."""
    return {
        "invocation_id": invocation_id,
        "mode": mode,
        "model": agent_config.model.value,
        "agent_config_snapshot": _agent_config_snapshot(agent_config),
        "total_tokens": tokens_used.input_tokens + tokens_used.output_tokens,
        "attempts": attempts,
    }


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
) -> tuple[StrategistOutput, ValidationResult, str | None]:
    """Execute Attempt 2 and return ``(output, validation_result, stop_reason)`` or raise.

    Extracted to keep ``run_strategist_harness`` below the C901/PLR0915
    thresholds. All mutable state is passed explicitly. The caller composes
    the :class:`HarnessSuccess` so it owns ``agent_config``.
    """
    diag.attempts = 2

    retry_prompt = _compose_retry_prompt(diag.user_message, retry_message)
    payload2, text2, stop_reason2, tokens2, tool_calls2 = await invoke(retry_prompt)
    raw_response_retry = _render_raw_response(payload2, text2)
    diag.response_retry = raw_response_retry
    diag.tokens_used = _add_tokens(tokens1, tokens2)
    diag.tool_calls_used = tool_calls1 + tool_calls2

    output2, validation2, _ = _parse_and_validate(
        payload2,
        text2,
        diag.invocation_id,
        validator,
        stop_reason2,
        attempt=2,
        diag=diag,
    )

    if output2 is None or validation2 is None:
        wall_elapsed = time.monotonic() - wall_start
        diag.write(success=False, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
        raise MalformedOutputFailure(
            "Parse or validation failed on both initial and retry attempts. "
            f"First retry error: {diag.errors[-1].get('message', '')}",
            agent_name=diag.agent_name,
            invocation_id=diag.invocation_id,
            raw_response_initial=raw_response_initial,
            raw_response_retry=raw_response_retry,
        )

    diag.mode = output2.mode
    return output2, validation2, stop_reason2


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_strategist_harness(  # noqa: PLR0913 — public signature is fixed by ALP-307 story 06 § 1.
    *,
    user_message: str,
    system_prompt: str,
    invocation_id: str,
    agent_config: BaseAgentConfig,
    validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "strategist",
) -> HarnessSuccess:
    """Invoke the strategist agent and return :class:`HarnessSuccess`.

    Parameters
    ----------
    user_message:
        The pre-assembled input bundle passed as the user turn (story 04).
    system_prompt:
        The strategist system prompt loaded by the runner (story 07).
    invocation_id:
        Stable identifier for this pipeline invocation; used to locate the
        diagnostic archive directory and to populate the parsed output.
    agent_config:
        Per-agent LLM configuration from agents.yaml (model, prompt path,
        latency budget, output-token budget).
    validation_state:
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
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.strategist.value

    validator = _ValidatorContext(retrieval_store=retrieval_store, active_sectors=active_sectors)
    mcp_servers, allowed_tools = _build_mcp_wiring(
        validation_state=validation_state,
        retrieval_store=retrieval_store,
    )

    with system_prompt_as_file(system_prompt) as prompt_path:
        options = _build_sdk_options(
            agent_config,
            prompt_path=prompt_path,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
        )
        return await _run_invocation(
            agent_config=agent_config,
            agent_name=agent_name,
            user_message=user_message,
            system_prompt=system_prompt,
            invocation_id=invocation_id,
            options=options,
            sdk_query_fn=sdk_query_fn,
            validator=validator,
            archive_root=archive_root,
            progress=progress,
            phase=phase,
        )


async def _run_invocation(  # noqa: PLR0913 — internal helper threading runner state.
    *,
    agent_config: BaseAgentConfig,
    agent_name: str,
    user_message: str,
    system_prompt: str,
    invocation_id: str,
    options: Any,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    validator: _ValidatorContext,
    archive_root: Path | None,
    progress: ProgressEmitter,
    phase: str,
) -> HarnessSuccess:
    """Drive the two-attempt SDK loop with *options* already constructed.

    Extracted so ``run_strategist_harness`` can hold the system-prompt
    tempfile open for the entire invocation (Attempts 1 and 2 share the same
    options/prompt_path).

    Each SDK call routes through :func:`invoke_sdk` so the single emit point
    fires ``agent_request`` / ``agent_response`` on the supplied *progress*.
    """
    diag = _DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=system_prompt,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        agent_config_snapshot=_agent_config_snapshot(agent_config),
    )
    wall_start = time.monotonic()

    async def _invoke(
        prompt: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed, int]:
        """Run one SDK call routed through the shared :func:`invoke_sdk` driver."""
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
            on_cli_result_error="sdk_failure",
            tool_name_prefix=_TOOL_NAME_PREFIXES,
            progress=progress,
            phase=phase,
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
    diag.attempts = 1
    payload1, text1, stop_reason1, tokens1, tool_calls1 = await _invoke(user_message)
    raw_response_initial = _render_raw_response(payload1, text1)
    diag.response_initial = raw_response_initial
    diag.tokens_used = tokens1
    diag.tool_calls_used = tool_calls1

    try:
        output, validation, retry_message = _parse_and_validate(
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

    if output is not None and validation is not None:
        wall_elapsed = time.monotonic() - wall_start
        diag.mode = output.mode
        diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason1)
        return HarnessSuccess(
            output=output,
            validation_result=validation,
            tokens_used=tokens1,
            metadata=_build_metadata(
                invocation_id=invocation_id,
                mode=output.mode,
                agent_config=agent_config,
                tokens_used=tokens1,
                attempts=1,
            ),
        )

    # ------------------------------------------------------------------
    # Attempt 2: corrective retry
    # ------------------------------------------------------------------
    assert retry_message is not None
    output2, validation2, stop_reason2 = await _run_retry_attempt(
        retry_message=retry_message,
        raw_response_initial=raw_response_initial,
        tokens1=tokens1,
        tool_calls1=tool_calls1,
        validator=validator,
        diag=diag,
        wall_start=wall_start,
        invoke=_invoke,
    )
    wall_elapsed = time.monotonic() - wall_start
    diag.write(success=True, wall_clock_seconds=wall_elapsed, stop_reason=stop_reason2)
    return HarnessSuccess(
        output=output2,
        validation_result=validation2,
        tokens_used=diag.tokens_used,
        metadata=_build_metadata(
            invocation_id=invocation_id,
            mode=output2.mode,
            agent_config=agent_config,
            tokens_used=diag.tokens_used,
            attempts=2,
        ),
    )
