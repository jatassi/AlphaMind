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
``submission_log.json`` diagnostic-archive file capturing the submit_envelope
wrapper's submission log, (5) a ``HarnessSuccess`` shape that carries the submission
log tuple alongside the standard fields, (6) a ``_MAX_TURNS`` cap of 40
(versus 25 elsewhere) since the PM emits one ``submit_envelope`` tool call
per envelope plus zero-or-more validation/retrieval calls per envelope.

Architecture note: ``invoke_pm`` accepts ``sdk_query_fn`` for dependency
injection. In production the default (the real ``claude_agent_sdk.query``)
is used. Tests pass a stub so no test touches the Anthropic API.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    ContextOverflowFailure,
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
from alphamind.commands.pm_envelope import PMCompletionRecord
from alphamind.commands.protocols import BrokerDispatch
from alphamind.commands.submission_log import SubmissionLogEntry
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.decision._shared import system_prompt_as_file
from alphamind.decision.portfolio_manager.parser import ParseError, parse_pm_completion_record
from alphamind.decision.portfolio_manager.submit_envelope import (
    SubmitEnvelopeState,
    build_submit_envelope_mcp_server,
)
from alphamind.decision.portfolio_manager.submit_envelope.server import (
    build_broker_routing_kwargs,
)
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
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
from alphamind.state.config import StatePersistenceConfig

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
# HarnessSuccess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessSuccess:
    """Successful PM invocation result returned to the runner.

    Per story 07 § 2, the success record carries the parsed
    :class:`PMCompletionRecord` sentinel, the retry count, the cumulative
    :class:`TokensUsed`, the tool-call count, the wall-clock seconds, the
    SDK-reported stop reason, and the submit_envelope wrapper's per-envelope
    submission log captured from :class:`SubmitEnvelopeState`'s mutable cell.
    """

    output: PMCompletionRecord
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None
    submission_log: tuple[SubmissionLogEntry, ...]


# ---------------------------------------------------------------------------
# Corrective-retry message construction
# ---------------------------------------------------------------------------

_SECTION_DIRECTIVE = (
    "Re-emit the PM completion sentinel as a JSON payload conforming to the "
    "PMCompletionRecord schema attached to this invocation. The shape is "
    "API-enforced; fix the specific field named above and resubmit."
)

_CONTRACT_REF = "See docs/design/04-decision-layer/pm-envelope-schema.md."


def _build_retry_message_for_parse_error(error: ParseError) -> str:
    return _build_retry_message(
        framing=(
            "The prior response did not meet the parse contract for the PM completion sentinel."
        ),
        error_detail=f"Field: {error.field_path}\nError: {error.message}",
        contract_ref=_CONTRACT_REF,
        directive=_SECTION_DIRECTIVE,
    )


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
    """Mutable diagnostic state accumulated during an invocation.

    ``get_submit_envelope_state`` is the zero-arg accessor returned by
    :func:`build_submit_envelope_mcp_server`; it returns the latest
    :class:`SubmitEnvelopeState` after the SDK loop completes (ALP-476
    frozen-cell threading).
    """

    agent_name: str
    invocation_id: str
    prompt_text: str
    user_message: str
    model: str
    archive_root: Path | None
    get_submit_envelope_state: Callable[[], SubmitEnvelopeState]
    as_of: datetime | None = None

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

        Path: ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/decision/portfolio_manager/``
        — date-partitioned canonical layout per ALP-689 followup. The PM-only
        ``submission_log.json`` and ``failed_submission_log.json`` are written
        alongside the standard six files; they capture the submit_envelope
        wrapper's ``state.submission_log`` (calls whose payload parsed to a
        :class:`PMEnvelope`) and ``state.failed_submission_log`` (Layer-1
        Pydantic parse failures) respectively after the SDK loop completes so
        the verify script (story 09) can inspect every envelope the PM attempted.
        """
        if self.archive_root is None:
            return
        if self.as_of is None:
            msg = "_DiagState.as_of must be set when archive_root is provided"
            raise ValueError(msg)
        diag_dir = (
            invocation_archive_dir(
                archive_root=self.archive_root,
                as_of=self.as_of,
                invocation_id=self.invocation_id,
            )
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
            "model": self.model,
            "retry_count": self.retry_count,
            "tokens_used": self.tokens_used.model_dump(),
            "tool_calls_used": self.tool_calls_used,
            "wall_clock_seconds": wall_clock_seconds,
            "stop_reason": stop_reason,
            "success": success,
        }
        (diag_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        latest_state = self.get_submit_envelope_state()
        submission_log = [
            {
                "envelope": entry.envelope.model_dump(mode="json"),
                "submission_results": [r.model_dump(mode="json") for r in entry.submission_results],
            }
            for entry in latest_state.submission_log
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
            for entry in latest_state.failed_submission_log
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
    state_persistence_config: StatePersistenceConfig,
    broker_dispatch: BrokerDispatch | None = None,
    venue_config: VenueConfig | None = None,
    execution_mode: ExecutionMode | None = None,
    execution_config: ExecutionConfig | None = None,
    invocation_handle: Any | None = None,
) -> tuple[dict[str, Any], list[str], Callable[[], SubmitEnvelopeState]]:
    """Compose the four MCP servers and merge their allowed-tool lists.

    Returns ``(merged_servers, merged_allowed_tools, get_submit_envelope_state)``
    — the first two are ready for direct assignment to
    ``ClaudeAgentOptions.mcp_servers`` / ``ClaudeAgentOptions.allowed_tools``;
    the third returns the latest :class:`SubmitEnvelopeState` after the SDK loop
    completes (post-ALP-476 frozen-cell threading).

    ``broker_dispatch`` is the composition-root-injected
    :class:`alphamind.commands.protocols.BrokerDispatch` implementation
    (ALP-458) — forwarded into the submit_envelope wrapper as an
    override seam for tests + future per-invocation dispatch shaping.

    ``venue_config`` / ``execution_mode`` / ``execution_config`` (ALP-711)
    are the picklable orchestrator-facing inputs the harness uses to
    build the Alpaca-backed ``client`` + ``queries`` the submit_envelope
    wrapper's broker-routing gate requires. All three must be non-None
    for broker routing to activate; when any is ``None`` (debug-e2e /
    log-only path) the wrapper's gate stays False and synthetic-id
    placeholders persist as before.

    ``invocation_handle`` (also ALP-711) supplies the AsyncSession the
    broker-routing code uses to resolve CLOSE / ADD / ADJUST / CANCEL
    per-command context from persisted ``positions`` and ``orders`` rows
    (see ``submit_envelope.dispatch._dispatcher_context_for``). The
    handle is built by the PM subprocess worker against its own async
    engine; the live Alpaca ``TradingClient`` is constructed alongside
    it on the worker side because both are non-picklable.

    ``defer_writeback=True`` is passed unconditionally, but its effect now
    depends on whether broker routing activated (ALP-763): when the broker
    triple is present, Step 6 performs the envelope writeback IN-TURN — right
    after broker dispatch, on the supplied ``invocation_handle`` — to close the
    fast-fill race against a deferred order-row commit, committing the full
    outcome (orders + capital reservation + ``pm_decision`` + any
    ``command_abandoned`` audit rows). The orchestrator's later
    ``dispatch_command_execution`` stage then detects that in-turn write and skips
    re-persisting the envelope (and its abandoned audit), so there is no
    duplication. On the deferred/log-only path (no broker triple) Step 6 does
    not write and ``dispatch_command_execution`` remains the sole writer.
    """
    validation_servers, validation_tools = build_validate_guardrail_mcp_server(
        initial_validation_state
    )
    retrieval_servers, retrieval_tools = build_retrieve_brief_mcp_server(retrieval_store)
    thesis_servers, thesis_tools = build_get_thesis_components_mcp_server(thesis_component_reader)
    broker_routing_kwargs = build_broker_routing_kwargs(
        venue_config, execution_mode, execution_config
    )
    submit_servers, submit_tools, get_submit_envelope_state = build_submit_envelope_mcp_server(
        initial_submit_envelope_state,
        retrieval_store=retrieval_store,
        pre_processor_bundle=pre_processor_bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
        library_config=library_config,
        library_market=library_market,
        state_persistence_config=state_persistence_config,
        broker_dispatch=broker_dispatch,
        invocation_handle=invocation_handle,
        defer_writeback=True,
        **broker_routing_kwargs,
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
    return merged_servers, merged_tools, get_submit_envelope_state


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
    prompt_path: str,
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
        submission_log=diag.get_submit_envelope_state().submission_log,
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
    as_of: datetime | None = None,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    state_persistence_config: StatePersistenceConfig,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    broker_dispatch: BrokerDispatch | None = None,
    venue_config: VenueConfig | None = None,
    execution_mode: ExecutionMode | None = None,
    execution_config: ExecutionConfig | None = None,
    invocation_handle: Any | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "pm",
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
        parity; the wrapper does not consult them directly.
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
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    agent_name = AgentName.portfolio_manager.value

    mcp_servers, allowed_tools, get_submit_envelope_state = _build_mcp_wiring(
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
        state_persistence_config=state_persistence_config,
        broker_dispatch=broker_dispatch,
        venue_config=venue_config,
        execution_mode=execution_mode,
        execution_config=execution_config,
        invocation_handle=invocation_handle,
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
            get_submit_envelope_state=get_submit_envelope_state,
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
    get_submit_envelope_state: Callable[[], SubmitEnvelopeState],
    as_of: datetime | None,
    archive_root: Path | None,
    progress: ProgressEmitter,
    phase: str,
) -> HarnessSuccess:
    """Drive the two-attempt SDK loop with *options* already constructed.

    Extracted so ``invoke_pm`` can hold the system-prompt tempfile open for
    the entire invocation (Attempts 1 and 2 share the same options/prompt_path).

    Each SDK call routes through :func:`invoke_sdk` so the single emit point
    fires ``agent_request`` / ``agent_response`` on the supplied *progress*.
    """
    diag = _DiagState(
        agent_name=agent_name,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        get_submit_envelope_state=get_submit_envelope_state,
        as_of=as_of,
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
            submission_log=diag.get_submit_envelope_state().submission_log,
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
