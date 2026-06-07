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

The shared SDK driver loop, exception hierarchy, prompt cache, diagnostic
writer, and retry-message builder live in
:mod:`alphamind.analysis._harness_core`. Only the parser/validator wiring
and the domain-researcher-specific options builder live here.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    _PROMPT_CACHE,
    ContextOverflowFailure,
    DiagState,
    HarnessFailure,
    MalformedOutputFailure,
    SDKFailure,
    TimeoutFailure,
    _build_retry_message,
    _load_prompt,
    _render_raw_response,
    capture_agent_call,
    invoke_sdk,
)
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.models import SectorBrief
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

# Re-export internal names tests reach for via ``harness._PROMPT_CACHE`` /
# ``harness._load_prompt`` so existing per-process cache-clear hooks keep
# working unchanged.
__all__ += ["_PROMPT_CACHE", "_load_prompt"]


_SECTOR_TO_AGENT: dict[Sector, AgentName] = {
    Sector.TECH_SEMIS: AgentName.tech_semis_researcher,
    Sector.FINANCIALS: AgentName.financials_researcher,
    Sector.ENERGY: AgentName.energy_researcher,
}


# ---------------------------------------------------------------------------
# HarnessSuccess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessSuccess:
    """Successful invocation result returned to the per-sector runner."""

    brief: SectorBrief
    raw_response: str
    retry_count: int  # 0 or 1
    tokens_used: TokensUsed  # imported from _shared, NOT redefined here
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# Corrective-retry messages
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


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    framing = "The prior response did not meet the parse contract for the domain researcher output."
    return _build_retry_message(
        framing=framing,
        error_detail=f"Field: {error.field_path}\nError: {error.message}",
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


def _build_retry_message_for_validation_failure(result: ValidationResult) -> str:
    framing = (
        "The prior response did not meet the structural contract for the domain researcher output."
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
    sector: Sector,
    invocation_id: str,
    stop_reason: str | None,
    attempt: int,
    diag: DiagState,
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


# ---------------------------------------------------------------------------
# SDK options builder
# ---------------------------------------------------------------------------


def _build_sdk_options(
    agent_config: BaseAgentConfig,
    *,
    prompt_text: str,
) -> Any:
    """Build :class:`ClaudeAgentOptions` for the domain-researcher invocation.

    Pins the autonomous-agent contract: ``setting_sources=[]`` blocks
    developer ``.claude/settings.json``; ``tools=[]`` disables built-in CLI
    tools; ``strict-mcp-config`` ignores plugin-level MCP servers. The
    domain researcher runs system_prompt + user_message only — no tool
    allowlist. ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is the only CLI path for
    an output-token cap. ``output_format`` flips into JSON-Schema mode so
    the API enforces ``SectorBrief``; the ``signal_quality_reason ↔
    signal_quality`` invariant is enforced post-parse.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=prompt_text,
        model=agent_config.model,
        tools=[],
        allowed_tools=[],
        # JSON-Schema output mode emits a synthetic ``StructuredOutput`` tool
        # call as turn 1; the SDK injects a ``ToolResultBlock`` and the model
        # needs turn 2 to emit the closing ``end_turn``. ``max_turns=1``
        # starves the close-out and the SDK reports ``is_error=True`` even
        # when the structured payload was produced successfully.
        #
        # ``max_turns=2`` covers only the happy path. If the first
        # ``StructuredOutput`` payload fails schema validation, the SDK returns
        # a ``ToolResultBlock`` with ``is_error=True`` and the model spends a
        # corrective turn re-emitting it — which overruns a 2-turn budget and
        # aborts with ``error_max_turns`` even though the retry's payload
        # validated (observed 2026-05-31, financials researcher). ``max_turns=5``
        # leaves slack for a couple of structured-output correction cycles.
        max_turns=5,
        setting_sources=[],
        extra_args={"strict-mcp-config": None},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)},
        output_format={"type": "json_schema", "schema": SectorBrief.model_json_schema()},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


# Stall watchdog for the SDK's first message. A healthy call emits a
# ``SystemMessage`` within seconds of spawn; multi-minute silence indicates
# an unhealthy local CLI subprocess or backend admit-rate starvation when
# multiple sibling researcher invocations race for the same OAuth token.
_INIT_STALL_TIMEOUT_SECONDS = 60.0

# Between-message stall watchdog disabled (2026-05-18). The previous 180s
# threshold was firing falsely against Sonnet 4.6's extended-thinking
# gaps — the model can spend 3-6 minutes silently building structured
# output before any AssistantMessage hits the stream (verified by the
# subprocess-isolation diagnostic: BOTH stall-retry attempts fired at
# 180s with zero output tokens against the byte-identical
# user_message.md the in-isolation run succeeded with). The outer
# ``latency_budget_seconds`` already bounds the call; the init watchdog
# still catches the CLI-spawn-failure case fast. Confirmed 2026-05-21 by
# the sdk_trace.jsonl instrumentation: healthy calls show 150s+ silent
# extended-thinking gaps between SDK messages.
_BETWEEN_MESSAGE_STALL_SECONDS: float | None = None

# Launch jitter applied to each domain researcher's SDK call. The
# orchestrator fans out three sectors under :class:`asyncio.TaskGroup`,
# which spawns three SDK subprocesses within milliseconds of each other.
# Random jitter on ``[0, 0.5s]`` spreads the subprocess spawns out enough
# to reduce OAuth-token concurrency contention without measurably extending
# wall clock. The deterministic 4-way stall observed on 2026-05-18 is
# handled by the global SDK-call semaphore in
# :func:`alphamind.analysis._harness_core.invoke_sdk`, not by larger jitter.
_LAUNCH_JITTER_SECONDS = 0.5


async def invoke_domain_researcher(  # noqa: PLR0913 — signature dictated by domain researcher's parameter surface
    *,
    agent_config: BaseAgentConfig,
    sector: Sector,
    user_message: str,
    invocation_id: str,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "domain_researchers",
    telemetry_session: AsyncSession | None = None,
    provenance_root: Path | None = None,
) -> HarnessSuccess:
    """Invoke a domain researcher agent and return a validated :class:`HarnessSuccess`.

    ``sector`` drives parser/validator selection and the agent-name
    derivation. ``archive_root=None`` skips diagnostic writes;
    ``sdk_query_fn`` defaults to ``claude_agent_sdk.query`` (tests inject
    a stub). Raises ``MalformedOutputFailure`` (retry exhausted),
    ``ContextOverflowFailure`` (``stop_reason=max_tokens``), ``SDKFailure``
    (auth / non-recoverable), or ``TimeoutFailure``.
    """
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = _SECTOR_TO_AGENT[sector].value
    prompt_text = await _load_prompt(agent_config.prompt)
    options = _build_sdk_options(agent_config, prompt_text=prompt_text)

    diag = DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        as_of=as_of,
        prompt_path=agent_config.prompt,
        output_schema=SectorBrief.model_json_schema(),
        sampling_params={"max_tokens": agent_config.output_token_budget},
    )
    wall_start = time.monotonic()

    async def _invoke_once(
        prompt: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, TokensUsed]:
        outcome = await invoke_sdk(
            sdk_query_fn=sdk_query_fn,
            prompt=prompt,
            options=options,
            diag=diag,
            budget_seconds=float(agent_config.latency_budget_seconds),
            init_stall_timeout_seconds=_INIT_STALL_TIMEOUT_SECONDS,
            between_message_stall_seconds=_BETWEEN_MESSAGE_STALL_SECONDS,
            concurrent_launch_jitter_seconds=_LAUNCH_JITTER_SECONDS,
            wall_start=wall_start,
            agent_name=agent_name,
            invocation_id=invocation_id,
            on_cli_result_error="context_overflow",
            progress=progress,
            phase=phase,
        )
        return (
            outcome.structured_output,
            outcome.response_text,
            outcome.stop_reason,
            outcome.tokens_used,
        )

    async with capture_agent_call(
        diag, telemetry_session=telemetry_session, provenance_root=provenance_root
    ):
        # --------------------------------------------------------------
        # Attempt 1: initial call
        # --------------------------------------------------------------
        payload1, text1, stop_reason1, tokens1 = await _invoke_once(user_message)
        raw_response_initial = _render_raw_response(payload1, text1)
        diag.response_initial = raw_response_initial
        diag.tokens_used = tokens1
        diag.output_payload = payload1

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

        # --------------------------------------------------------------
        # Attempt 2: corrective retry
        # --------------------------------------------------------------
        assert retry_message is not None
        diag.retry_count = 1

        payload2, text2, stop_reason2, tokens2 = await _invoke_once(retry_message)
        raw_response_retry = _render_raw_response(payload2, text2)
        diag.response_retry = raw_response_retry
        diag.output_payload = payload2
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
