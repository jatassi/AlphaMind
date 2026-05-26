"""MCP factory + orchestration handler for the ``submit_envelope`` package — ALP-464.

Hosts the runner-facing :func:`build_submit_envelope_mcp_server` factory and
the orchestration handler :func:`_handle_submit_envelope` that threads the
per-command pipeline (:mod:`.process`) → broker routing (:mod:`.dispatch`) →
phase-2 persistence (:mod:`.persist`).

The single registered tool is
``mcp__alphamind_execution_oms_submit__submit_envelope``; per-invocation state
lives in :class:`SubmitEnvelopeState` (:mod:`.types`) captured by the tool's
closure.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from pydantic import ValidationError

from alphamind._kernel.ids import CommandId, EnvelopeId
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.protocols import BrokerDispatch
from alphamind.decision.portfolio_manager.submit_envelope.dispatch import (
    _AbandonedCommandEntry,
    _route_through_broker,
)
from alphamind.decision.portfolio_manager.submit_envelope.persist import (
    _emit_command_abandoned_via_phase2,
    _persist_envelope_outcome_via_phase2,
    _persist_envelope_parse_failure_via_phase2,
    _persist_envelope_rejection_via_phase2,
)
from alphamind.decision.portfolio_manager.submit_envelope.process import (
    PositionLookup,
    _build_envelope_level_rejection,
    _format_first_error,
    _process_commands,
    _safe_derive_pm_command_id,
    _serialize_response,
    _unwrap_envelope_args,
    _validate_envelope_payload,
)
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    FailedSubmissionEntry,
    SubmissionLogEntry,
    SubmitEnvelopeState,
)
from alphamind.decision.portfolio_manager.validation import validate_pm_envelope
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.risk_guardrails.guardrail_evaluation import LibraryConfig, MarketInputs

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
    from alphamind.state.config import StatePersistenceConfig


_SERVER_NAME = "alphamind_execution_oms_submit"
_TOOL_NAME = "submit_envelope"


# The MCP SDK validates the input schema is itself a valid JSON-Schema object
# schema with a ``properties`` block. We accept the envelope payload as an
# open object and run Pydantic validation against the discriminated PMEnvelope
# union at the handler. Mirrors the analyst-side validate_guardrail wrapper.
# The full PMEnvelope JSON Schema is available via
# ``alphamind.decision.portfolio_manager.envelope_schema()``; embedding it
# here would force the SDK's input validator to traverse the discriminated
# union, but the SDK does not handle ``$defs``/``oneOf`` discriminator chains
# — surface a permissive shape and validate at the handler instead.
_SUBMIT_ENVELOPE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
    "description": (
        "One PM envelope per pm-envelope-schema.md. Validated by Pydantic "
        "at the handler; full discriminated-union JSON Schema available via "
        "alphamind.decision.portfolio_manager.envelope_schema()."
    ),
}


def build_submit_envelope_mcp_server(  # noqa: PLR0913 — runner-facing assembler mirrors the per-invocation parameters the tool needs.
    state: SubmitEnvelopeState,
    *,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    library_config: LibraryConfig,
    library_market: MarketInputs,
    state_persistence_config: StatePersistenceConfig,
    invocation_handle: Any | None = None,
    client: TradingClient | None = None,
    queries: AccountStateQueries | None = None,
    execution_config: ExecutionConfig | None = None,
    broker_dispatch: BrokerDispatch | None = None,
) -> tuple[Mapping[str, McpSdkServerConfig], tuple[str, ...], Callable[[], SubmitEnvelopeState]]:
    """Build a per-invocation SDK MCP server bound to *state*.

    Returns ``(mcp_servers_dict, allowed_tool_names, get_current_state)`` —
    the first two are ready for direct assignment to
    ``ClaudeAgentOptions.mcp_servers`` / ``ClaudeAgentOptions.allowed_tools``;
    the third is a zero-arg callable returning the latest
    :class:`SubmitEnvelopeState` after the SDK loop completes (the harness +
    runner archive ``state.submission_log`` / ``state.failed_submission_log``
    via this callable). The single registered tool is
    ``mcp__alphamind_execution_oms_submit__submit_envelope``.

    The factory captures *state* in the tool's closure; each call rebinds the
    captured variable via ``nonlocal state`` to the post-call state returned by
    :func:`_handle_submit_envelope` — accepted commands advance
    ``state.validation_state`` (via :func:`dataclasses.replace` with
    ``with_accepted_proposal(delta)``), every call extends
    ``state.submission_log`` (post-ALP-476 frozen-dataclass cell).

    ``library_config`` and ``library_market`` are part of the runner-facing
    signature (story 08 ALP-330) but are already wired into
    ``state.validation_state`` by ``build_initial_submit_envelope_state``.
    They are accepted here so the runner passes one canonical bundle of
    library plumbing through both surfaces uniformly; the submit_envelope
    wrapper does not consult them directly.

    When ``invocation_handle`` is supplied, the wrapper additionally
    writes through every accepted envelope to SQL via the Phase 2 write
    path (ALP-366) and persists Layer-1 parse failures as
    ``envelope_parse_failed`` activity log entries. Composition pipelines
    (ALP-310) inject the handle obtained from the surrounding
    ``InvocationContext``. ``state_persistence_config`` is forwarded
    verbatim into every Phase-2 entrypoint so the engine's persistence
    knobs (sliding-window size, snapshot timeout, provenance roots) all
    resolve against the operator-supplied config (ALP-653).

    When ``client`` + ``queries`` + ``execution_config`` are supplied
    (broker-routing coordinated swap, story 03e / ALP-390), each accepted
    command additionally routes through a broker-dispatch callable before
    persistence; the persisted entry / close / add / adjust order carries
    Alpaca's real ``alpaca_order_id`` and the acknowledgment surfaces it.
    Gateway-submission failures map to ``command_abandoned`` activity-log
    entries; permanent rejections surface as synchronous OMS rejections.

    The ``broker_dispatch`` parameter is the composition-root-injected
    :class:`alphamind.commands.protocols.BrokerDispatch` implementation
    (ALP-458). When ``None``, the wrapper lazy-imports the concrete
    :func:`alphamind.execution.oms.broker_dispatch.dispatch_command_to_broker`
    for backwards compatibility with callers that haven't switched to the
    Protocol-based wiring yet.
    """
    _ = (library_config, library_market)  # accepted for runner-signature parity

    @tool(
        _TOOL_NAME,
        (
            "Submit one PM envelope to the engine; returns one submission_result "
            "per embedded command (accepted with command_id, or rejected with "
            "rejection_payload). Updates cumulative state for accepted commands."
        ),
        _SUBMIT_ENVELOPE_INPUT_SCHEMA,
    )
    async def _submit_envelope(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal state
        response, state = await _handle_submit_envelope(
            args,
            state=state,
            retrieval_store=retrieval_store,
            pre_processor_bundle=pre_processor_bundle,
            pm_view=pm_view,
            active_sectors=active_sectors,
            halt_mode=halt_mode,
            sector_resolver=sector_resolver,
            invocation_handle=invocation_handle,
            state_persistence_config=state_persistence_config,
            client=client,
            queries=queries,
            execution_config=execution_config,
            broker_dispatch=broker_dispatch,
        )
        return response

    def get_current_state() -> SubmitEnvelopeState:
        """Return the latest :class:`SubmitEnvelopeState` (post-ALP-476).

        Callers archive ``state.submission_log`` / ``state.failed_submission_log``
        after the SDK loop completes; the closure rebinds the captured cell on
        every successful invocation.
        """
        return state

    server = create_sdk_mcp_server(name=_SERVER_NAME, tools=[_submit_envelope])
    allowed = (f"mcp__{_SERVER_NAME}__{_TOOL_NAME}",)
    return {_SERVER_NAME: server}, allowed, get_current_state


async def _handle_submit_envelope(  # noqa: PLR0913 — orchestrator threads every per-invocation parameter once.
    args: dict[str, Any],
    *,
    state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    state_persistence_config: StatePersistenceConfig,
    invocation_handle: Any | None = None,
    client: TradingClient | None = None,
    queries: AccountStateQueries | None = None,
    execution_config: ExecutionConfig | None = None,
    broker_dispatch: BrokerDispatch | None = None,
) -> tuple[dict[str, Any], SubmitEnvelopeState]:
    """Coerce input → run validators → process commands → log + respond.

    Returns ``(response, new_state)`` — the new state carries the
    log/validation-state advances accumulated during processing. The MCP
    closure in :func:`build_submit_envelope_mcp_server` rebinds its captured
    cell with the returned state via ``nonlocal``.

    When ``invocation_handle`` is supplied (production composition path,
    ALP-310), every accepted envelope writes through to SQL via the Phase 2
    ``persist_envelope_outcome`` and every Layer-1 parse failure additionally
    writes one ``envelope_parse_failed`` activity log entry. When the handle
    is ``None`` (legacy fixture-only path), only the in-memory state-cell
    surfaces are mutated — preserves the wrapper's pre-ALP-366 behavior.

    When ``client`` + ``queries`` + ``execution_config`` are supplied
    (broker-routing coordinated swap, story 03e / ALP-390), each command that
    passes Layer-1/2/3 validation routes through
    :func:`dispatch_command_to_broker` before Phase 2 writeback.
    """
    # Step 0: tolerant unwrap of a single-key ``{"envelope": {...}}`` wrapper
    # (ALP-700). The LLM occasionally hands in the wrapped form despite the
    # prompt's inlined examples; the unwrap lets a single retry of the tool
    # call succeed rather than burning attempts discovering the contract.
    # ``raw_args_for_log`` preserves the LITERAL tool input (pre-unwrap) so
    # an operator combing parse-failure rows can still tell whether the LLM
    # mistakenly wrapped its payload — the unwrap is invisible to forensics.
    raw_args_for_log = dict(args)
    args = _unwrap_envelope_args(args)

    # Step 1: Layer-1 — coerce to PMEnvelope.
    try:
        envelope = _validate_envelope_payload(args)
    except ValidationError as exc:
        envelope_id = str(args.get("envelope_id", "ENV-REC-INVALID"))
        synthetic_command_id = _safe_derive_pm_command_id(
            invocation_id=state.invocation_id,
            envelope_id=envelope_id,
            command_ordinal=0,
            attempt_seq=0,
        )
        failed_entry = FailedSubmissionEntry(
            raw_args=raw_args_for_log,
            validation_error_repr=str(exc),
            command_id=synthetic_command_id,
        )
        state = dataclasses.replace(
            state,
            failed_submission_log=(*state.failed_submission_log, failed_entry),
        )
        if invocation_handle is not None:
            await _persist_envelope_parse_failure_via_phase2(
                invocation_handle, failed_entry, state_persistence_config
            )
        return _build_envelope_level_rejection(
            envelope_id=EnvelopeId(envelope_id),
            invocation_id=state.invocation_id,
            suggested_modification=_format_first_error(exc),
            log_state=state,
            log_envelope_for_record=None,
        )

    # Step 2: Layer-2/3 — run validate_pm_envelope.
    layer23 = validate_pm_envelope(
        envelope,
        retrieval_store=retrieval_store,
        pre_processor_bundle=pre_processor_bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
    )
    if not layer23.is_valid:
        suggested = layer23.errors[0].message
        if invocation_handle is not None:
            await _persist_envelope_rejection_via_phase2(
                invocation_handle, envelope, layer23.errors, state_persistence_config
            )
        return _build_envelope_level_rejection(
            envelope_id=envelope.envelope_id,
            invocation_id=state.invocation_id,
            suggested_modification=suggested,
            log_state=state,
            log_envelope_for_record=envelope,
        )

    # Step 3: process embedded commands.
    submission_results, state = _process_commands(
        envelope,
        state=state,
        sector_resolver=sector_resolver,
        position_lookup=_build_position_lookup(pm_view),
    )

    # Step 4: optionally route accepted commands through the broker adapter
    # (broker-routing coordinated swap, story 03e / ALP-390). Returns per-command
    # dispatch outcomes alongside (possibly mutated) submission results — a
    # validated-but-broker-rejected command flips from accepted → rejected, and
    # its dispatch entry carries a gateway-failure marker the writeback step
    # uses to skip persistence + emit ``command_abandoned``.
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None
    abandoned_entries: tuple[_AbandonedCommandEntry, ...] = ()
    if client is not None and queries is not None and execution_config is not None:
        submission_results, dispatch_results, abandoned_entries = await _route_through_broker(
            envelope=envelope,
            submission_results=submission_results,
            client=client,
            queries=queries,
            execution_config=execution_config,
            invocation_handle=invocation_handle,
            broker_dispatch=broker_dispatch,
        )

    # Step 5: append to submission log (post-broker outcome).
    state = dataclasses.replace(
        state,
        submission_log=(
            *state.submission_log,
            SubmissionLogEntry(envelope=envelope, submission_results=submission_results),
        ),
    )

    # Step 6: SQL writeback (opt-in via invocation_handle).
    if invocation_handle is not None:
        await _persist_envelope_outcome_via_phase2(
            invocation_handle,
            envelope,
            submission_results,
            state_persistence_config,
            dispatch_results=dispatch_results,
        )
        for abandoned in abandoned_entries:
            await _emit_command_abandoned_via_phase2(
                invocation_handle,
                envelope_id=envelope.envelope_id,
                command_id=CommandId(abandoned.command_id),
                originating_agent=envelope.source_provenance,
                command_type=abandoned.command_type,
                failure_reason=abandoned.failure_reason,
                retry_attempt_count=abandoned.retry_attempt_count,
            )

    # Step 7: serialize.
    response = {
        "content": [
            {
                "type": "text",
                "text": _serialize_response(envelope.envelope_id, submission_results),
            }
        ],
    }
    return response, state


def _build_position_lookup(pm_view: PortfolioManagerView) -> PositionLookup:
    """Build a ``position_id`` → :class:`PositionRecord` lookup from the
    PM view's typed positions tuple.

    Resolves both OPEN and PENDING positions — the PM view already filters
    positions to the same scope the LLM sees, so ADDs against positions
    outside that view surface as :class:`ValueError` rather than silently
    using a placeholder. ``pm_view.positions`` contains
    :class:`StrategistPositionView` records; each wraps a
    :class:`PositionView` via ``.position`` whose ``.record`` is the
    :class:`PositionRecord` the validator consumes.
    """
    positions_by_id = {
        view.position.position_id: view.position.record for view in pm_view.positions
    }
    return positions_by_id.get


__all__ = [
    "_SERVER_NAME",
    "_SUBMIT_ENVELOPE_INPUT_SCHEMA",
    "_TOOL_NAME",
    "_build_position_lookup",
    "_handle_submit_envelope",
    "build_submit_envelope_mcp_server",
]
