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

import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
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
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.retrieval_tools import build_retrieve_brief_mcp_server
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.decision._shared import system_prompt_as_file
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.parser import ParseError, parse_analyst_output
from alphamind.decision.analyst.validation import (
    ValidationResult,
    validate_analyst_output,
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
    "invoke_analyst",
]


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
# HarnessSuccess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessSuccess:
    """Successful analyst invocation result returned to the runner."""

    output: AnalystOutput
    raw_response: str
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the analyst output as a JSON payload conforming to the "
    "AnalystOutput schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit."
)

_CONTRACT_REF = "See docs/design/04-decision-layer/analyst-output-schema.md."


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    return _build_retry_message(
        framing="The prior response did not meet the parse contract for the analyst output.",
        error_detail=f"Field: {error.field_path}\nError: {error.message}",
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    first_error = result.errors[0]
    return _build_retry_message(
        framing="The prior response did not meet the structural contract for the analyst output.",
        error_detail=(
            f"Field: {first_error.field_path}\n"
            f"Rule: {first_error.rule}\n"
            f"Error: {first_error.message}"
        ),
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


_RETRY_PROMPT_DELIMITER = "\n\n--- Retry diagnostic ---\n\n"


def _compose_retry_prompt(original_user_message: str, retry_diagnostic: str) -> str:
    """Prepend the original user_message to the retry diagnostic.

    Without the user_message in the retry SDK call, the model loses portfolio /
    synthesizer / guardrail context and falls back to regurgitating the system
    prompt's example block (observed in ALP-311 strategist post-mortem).
    """
    return original_user_message + _RETRY_PROMPT_DELIMITER + retry_diagnostic


# ---------------------------------------------------------------------------
# Validator context + parse-and-validate helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ValidatorContext:
    """Per-invocation inputs the analyst's Layer-2/3 validator needs.

    The retrieval store, active-sector set, and live-close map always travel
    together through the harness's parse/validate path; bundling them keeps the
    helper signatures narrow. ``underlying_prices`` is the guardrail library's
    latest ``ohlcv_bars`` close per ticker (ALP-742 bracket-coherence checks).
    """

    retrieval_store: RetrievalStore
    active_sectors: frozenset[str]
    underlying_prices: Mapping[str, float]


def _parse_and_validate(
    payload: dict[str, Any] | None,
    response_text: str,
    invocation_id: str,
    validator: _ValidatorContext,
    stop_reason: str | None,
    attempt: int,
    diag: DiagState,
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
        underlying_prices=validator.underlying_prices,
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
    # The analyst's prompt does not document the batch tool — and the
    # agents.yaml `tools` allowlist for `analyst` does not include
    # `validate_guardrail_batch` (the batch tool is strategist/PM-scoped per
    # ALP-625's parent feature). Opt out of the batch tool here so the
    # analyst's MCP surface matches the agents.yaml authoritative list.
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(
        initial_validation_state, include_batch_tool=False
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
    prompt_path: str,
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
    diag: DiagState,
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


async def invoke_analyst(  # noqa: PLR0913 — public signature is fixed by ALP-298 spec plus ALP-497's progress / phase kwargs
    *,
    agent_config: BaseAgentConfig,
    user_message: str,
    invocation_id: str,
    initial_validation_state: ValidationToolState,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "analyst",
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
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.analyst.value

    # The latest-close map the ALP-742 bracket checks compare geometry against
    # rides in on the same ValidationToolState the validate_guardrail tool uses;
    # no new subprocess-serialized parameter is needed.
    validator = _ValidatorContext(
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        underlying_prices=initial_validation_state.library_market.underlying_prices,
    )
    mcp_servers, allowed_tools = _build_mcp_wiring(
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
    )
    prompt_text = await _load_prompt(agent_config.prompt)

    with system_prompt_as_file(prompt_text) as prompt_path:
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
            invocation_id=invocation_id,
            prompt_text=prompt_text,
            options=options,
            sdk_query_fn=sdk_query_fn,
            validator=validator,
            as_of=as_of,
            archive_root=archive_root,
            progress=progress,
            phase=phase,
        )


async def _run_invocation(  # noqa: PLR0913 — internal helper threading runner state.
    *,
    agent_config: BaseAgentConfig,
    agent_name: str,
    user_message: str,
    invocation_id: str,
    prompt_text: str,
    options: Any,
    sdk_query_fn: Callable[..., AsyncIterator[Any]],
    validator: _ValidatorContext,
    as_of: datetime | None,
    archive_root: Path | None,
    progress: ProgressEmitter,
    phase: str,
) -> HarnessSuccess:
    """Drive the two-attempt SDK loop with *options* already constructed.

    Extracted so ``invoke_analyst`` can hold the tempfile open for the entire
    invocation (Attempts 1 and 2 share the same options/prompt_path) while
    keeping ``invoke_analyst``'s body under the C901/PLR0915 thresholds.

    Each SDK call routes through :func:`invoke_sdk` so the single emit point
    fires ``agent_request`` / ``agent_response`` on the supplied *progress*.
    """
    diag = DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        as_of=as_of,
        record_tool_calls=True,
        archive_layer="decision",
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
            init_stall_timeout_seconds=60.0,
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
