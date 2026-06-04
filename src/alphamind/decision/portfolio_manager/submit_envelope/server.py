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
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from pydantic import ValidationError

from alphamind._kernel.ids import CommandId, EnvelopeId, ThesisId
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.pm_envelope import PMEnvelope
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
    OrderThesisLookup,
    PositionLookup,
    _build_envelope_level_rejection,
    _format_first_error,
    _process_commands,
    _reconcile_validation_state,
    _safe_derive_pm_command_id,
    _serialize_response,
    _strip_analyst_only_command_fields,
    _unwrap_envelope_args,
    _validate_envelope_payload,
)
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    FailedSubmissionEntry,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
)
from alphamind.decision.portfolio_manager.validation import validate_pm_envelope
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.risk_guardrails.guardrail_evaluation import LibraryConfig, MarketInputs

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.config.models.main import ExecutionMode
    from alphamind.config.models.venue import VenueConfig
    from alphamind.execution.broker_adapter import AccountStateQueries, QuoteSource
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
    from alphamind.state.config import StatePersistenceConfig


_SERVER_NAME = "alphamind_execution_oms_submit"
_TOOL_NAME = "submit_envelope"

logger = logging.getLogger(__name__)


def build_broker_routing_kwargs(
    venue_config: VenueConfig | None,
    execution_mode: ExecutionMode | None,
    execution_config: ExecutionConfig | None,
) -> dict[str, Any]:
    """Construct broker-routing kwargs for :func:`build_submit_envelope_mcp_server`.

    ALP-711 — when the orchestrator's production gate is open
    (``context.debug_e2e is None``) the full triple is passed and this
    helper builds the Alpaca-backed :class:`alpaca.trading.client.TradingClient`
    + :class:`alphamind.execution.broker_adapter.queries.AccountStateQueries`
    needed by ``_handle_submit_envelope``'s broker-routing gate. When any
    leg is ``None`` (debug-e2e / log-only path) returns ``{}`` so the gate
    stays False and accepted commands fall through to the synthetic-id
    placeholder path.

    Lives in ``submit_envelope/`` (the PM MCP composition root, per
    ``.importlinter``'s ``decision-not-execution`` architectural carve-out)
    rather than the harness because the live ``TradingClient`` is not
    picklable across the PM subprocess boundary — the picklable triple
    traverses the boundary and this helper reconstructs the client inside
    the worker process where the alpaca-py instance is constructed once
    per invocation.
    """
    if venue_config is None or execution_mode is None or execution_config is None:
        return {}
    # Lazy imports — broker_adapter ships an alpaca-py dependency the
    # fixture-only path doesn't load. Mirrors the lazy-import pattern at
    # ``dispatch.py:_route_through_broker``.
    from alphamind.execution.broker_adapter.client_factory import (
        AlpacaClientFactory,
    )
    from alphamind.execution.broker_adapter.client_factory import (
        ExecutionMode as ClientFactoryExecutionMode,
    )
    from alphamind.execution.broker_adapter.queries import AccountStateQueries
    from alphamind.execution.broker_adapter.quotes import AlpacaQuoteSource

    mode_literal: ClientFactoryExecutionMode = "live" if execution_mode.value == "live" else "paper"
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    client = factory.build_trading_client()
    return {
        "client": client,
        "queries": AccountStateQueries(client),
        "execution_config": execution_config,
        # ALP-738 — live-quote source for re-pricing enter-now equity entries
        # into marketable limits. Built in the worker (the SDK client is not
        # picklable across the PM subprocess boundary, same as the TradingClient).
        "quote_source": AlpacaQuoteSource(factory.build_stock_data_client()),
    }


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
    quote_source: QuoteSource | None = None,
    broker_dispatch: BrokerDispatch | None = None,
    defer_writeback: bool = False,
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

    ``defer_writeback`` (ALP-711) gates Step 6 (in-tool writeback). When
    ``False`` (the engine-stub default), an accepted envelope writes
    through to SQL immediately inside the tool handler — the standalone
    composition path tests + the continuous monitor rely on this. When
    ``True``, Step 6 is skipped and the orchestrator's ``dispatch_phase2``
    becomes the sole writer of the per-envelope outcome. The scheduler
    orchestrator's PM-submit path passes ``True`` because it threads a
    handle for broker-routing reads (``_dispatcher_context_for`` needs a
    session to resolve CLOSE/ADD/ADJUST/CANCEL context from the persisted
    position / order rows) without authorizing duplicate persistence.
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
            quote_source=quote_source,
            broker_dispatch=broker_dispatch,
            defer_writeback=defer_writeback,
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
    quote_source: QuoteSource | None = None,
    broker_dispatch: BrokerDispatch | None = None,
    defer_writeback: bool = False,
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

    # Step 0.5: tolerant strip of analyst-only fields the PM copies verbatim
    # from the analyst Recommendation into an OPEN command (ALP-736). The
    # analyst schema is a superset of the OMS command schema, so
    # ``delta_adjusted_exposure`` / ``leg_id`` / event-leg ``order_parameters``
    # ride along into the ``extra="forbid"`` command sub-models and reject the
    # whole command — silently losing a PM-*approved* recommendation when the
    # self-repair retry loop stalls on a field it doesn't know to drop.
    # ``entry_window`` is deliberately preserved (ALP-737 threads it to the
    # bracket). The strip is logged so the normalization is never invisible.
    args, stripped_paths = _strip_analyst_only_command_fields(args)
    if stripped_paths:
        logger.info(
            "submit_envelope normalized %d analyst-only field(s) off PM command(s) "
            "before validation: %s",
            len(stripped_paths),
            ", ".join(stripped_paths),
        )

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
        # Surface every parse failure to the operator log (ALP-736). The
        # production scheduler path runs with ``defer_writeback=True``, so the
        # ``envelope_parse_failed`` activity-log write below is skipped there and
        # the rejection would otherwise be visible ONLY in
        # ``failed_submission_log.json`` — the silent-drop the issue describes.
        # A WARNING reaches ``collector.log`` regardless of the writeback gate so
        # a lost PM-approved recommendation is never buried.
        logger.warning(
            "submit_envelope Layer-1 parse failure (command_id=%s): %s — the "
            "PM-authored envelope was not submitted; see failed_submission_log "
            "for the full payload.",
            synthetic_command_id,
            _format_first_error(exc),
        )
        state = dataclasses.replace(
            state,
            failed_submission_log=(*state.failed_submission_log, failed_entry),
        )
        if invocation_handle is not None and not defer_writeback:
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
        if invocation_handle is not None and not defer_writeback:
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

    # Step 3: process embedded commands. Capture the validation-state cell as
    # it stood on envelope entry so Step 4.5 can rebuild it against the final
    # (post-broker-routing) outcome (ALP-743). Between Step 3 and Step 4.5
    # ``state.validation_state`` carries the Step-3 advances for every
    # guardrail-PASS command, including ones broker routing will reject — any
    # step inserted in that window that reads ``validation_state`` would observe
    # that pre-reconciliation (phantom-inflated) cell, so read it only after
    # Step 4.5.
    entry_validation_state = state.validation_state
    submission_results, state, credited_deltas = _process_commands(
        envelope,
        state=state,
        sector_resolver=sector_resolver,
        position_lookup=_build_position_lookup(pm_view),
        order_thesis_lookup=_build_order_thesis_lookup(pm_view),
    )

    # Step 4: optionally re-price enter-now entries + route accepted commands
    # through the broker adapter (story 03e / ALP-390, ALP-738). Extracted to a
    # helper so this orchestrator stays under the complexity gate; see its
    # docstring for the broker-rejection / enter-now-rewrite semantics.
    (
        envelope,
        submission_results,
        dispatch_results,
        abandoned_entries,
        reprice_markers,
    ) = await _maybe_route_accepted_commands(
        envelope=envelope,
        submission_results=submission_results,
        client=client,
        queries=queries,
        execution_config=execution_config,
        quote_source=quote_source,
        invocation_handle=invocation_handle,
        broker_dispatch=broker_dispatch,
    )

    # Step 4.5: reconcile the cumulative validation-state cell against the
    # post-broker-routing outcome (ALP-743). A command that PASSed guardrails
    # (advancing the cell in Step 3) but was then rejected by the broker in
    # Step 4 must release the delta it credited, so a resize/retry of the same
    # idea is evaluated against the true book rather than phantom stacked
    # exposure. Re-applies only the deltas of finally-accepted commands.
    state = _reconcile_validation_state(
        state,
        entry_validation_state=entry_validation_state,
        submission_results=submission_results,
        credited_deltas=credited_deltas,
    )

    # Step 5: append to submission log (post-broker outcome). ALP-711 scope (C):
    # ``dispatch_results`` rides on the log entry so the orchestrator's
    # Phase 2 dispatcher can forward broker outcomes (real Alpaca order ids,
    # broker rejection codes) to :func:`persist_envelope_outcome`; without it
    # the order persists with NO broker id (NULL, ALP-847 — never a placeholder).
    state = dataclasses.replace(
        state,
        submission_log=(
            *state.submission_log,
            SubmissionLogEntry(
                envelope=envelope,
                submission_results=submission_results,
                dispatch_results=dispatch_results,
                abandoned_entries=abandoned_entries,
                reprice_markers=reprice_markers,
            ),
        ),
    )

    # Step 6: SQL writeback (opt-in via invocation_handle).
    #
    # ALP-836 — atomicity-first. On the production broker-active path
    # (``invocation_handle`` present + broker triple wired) the per-command durable
    # ``orders`` rows + capital reservations were ALREADY committed before each
    # broker dispatch inside ``_route_through_broker`` (pre-commit), then backfilled
    # with the real broker id (or torn down on rejection) — each in its own
    # retryable transaction. So here we only finalize the envelope-level audit:
    # the once-per-envelope ``pm_decision``, one ``command_abandoned`` per
    # broker-rejected command, and the (integrity-guarded) ``phase2_completed_at``
    # stamp. ``dispatch_phase2`` then detects the already-persisted envelope (by
    # the pre-committed order rows) and skips it.
    #
    # The non-broker in-tool path (``defer_writeback=False``, no broker triple —
    # the continuous monitor / standalone composition / debug-e2e) keeps the
    # single-pass writeback on the handle's session, committed by the surrounding
    # ``InvocationContext``. The deferred non-broker path writes nothing here;
    # ``dispatch_phase2`` is the sole writer.
    broker_routing_active = (
        client is not None and queries is not None and execution_config is not None
    )
    if broker_routing_active and invocation_handle is not None:
        from alphamind.execution.write_paths.phase2.atomic import (
            finalize_broker_envelope,
            session_factory_from_handle,
        )

        accepted_command_ids = tuple(
            r.command_id for r in submission_results if r.status == "accepted"
        )
        await finalize_broker_envelope(
            session_factory_from_handle(invocation_handle),
            invocation_id=invocation_handle.invocation_id,
            envelope=envelope,
            accepted_command_ids=accepted_command_ids,
            abandoned_entries=abandoned_entries,
            reprice_markers=reprice_markers,
        )
    elif invocation_handle is not None and not defer_writeback:
        await _persist_envelope_outcome_via_phase2(
            invocation_handle,
            envelope,
            submission_results,
            state_persistence_config,
            dispatch_results=dispatch_results,
            reprice_markers=reprice_markers,
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


async def _maybe_route_accepted_commands(
    *,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    client: TradingClient | None,
    queries: AccountStateQueries | None,
    execution_config: ExecutionConfig | None,
    quote_source: QuoteSource | None,
    invocation_handle: Any | None,
    broker_dispatch: BrokerDispatch | None,
) -> tuple[
    PMEnvelope,
    tuple[SubmissionResult, ...],
    tuple[BrokerDispatchResult | None, ...] | None,
    tuple[_AbandonedCommandEntry, ...],
    tuple[Any, ...],
]:
    """Re-price enter-now entries, then route accepted commands to the broker.

    Returns ``(envelope, submission_results, dispatch_results, abandoned_entries,
    reprice_markers)``. A no-op (broker triple absent — the fixture-only path)
    returns the inputs unchanged with ``dispatch_results=None``, no abandoned
    entries, and empty ``reprice_markers``.

    When the broker triple (``client`` + ``queries`` + ``execution_config``) is
    present (broker-routing coordinated swap, story 03e / ALP-390):

    * Step 3.5 (ALP-738): re-price "enter-now" equity entries (a limit with no
      analyst ``entry_window``) into marketable limits through the live touch so
      a working short thesis fills at the quote instead of resting above a
      falling market. Rewriting the envelope here — before both broker dispatch
      and Phase-2 writeback — keeps the persisted order row and the broker order
      in agreement. ``submission_results`` validated against the original
      commands stay aligned by ordinal (guardrails don't read ``limit_price``).
      A no-op when no enter-now entry is present or no quote source was wired.
    * dispatch returns per-command outcomes alongside (possibly mutated)
      submission results — a validated-but-broker-rejected command flips from
      accepted → rejected, and its dispatch entry carries a gateway-failure
      marker the writeback step uses to skip persistence + emit
      ``command_abandoned``.
    * ALP-765: ``reprice_markers`` carries one dict per repriced enter-now entry
      so the caller can stamp the ``pm_decision`` audit row and ``SubmissionLogEntry``
      with the execution-layer price movement.
    """
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None
    abandoned_entries: tuple[_AbandonedCommandEntry, ...] = ()
    reprice_markers: tuple[Any, ...] = ()
    if client is None or queries is None or execution_config is None:
        return envelope, submission_results, dispatch_results, abandoned_entries, reprice_markers
    if quote_source is not None:
        from alphamind.execution.broker_adapter.entry_pricing import (
            rewrite_enter_now_entries,
        )

        old_commands = envelope.commands
        new_commands = await rewrite_enter_now_entries(
            old_commands,
            quote_source=quote_source,
            bps_through_touch=execution_config.marketable_entry_bps_through_touch,
        )
        if new_commands != old_commands:
            envelope = envelope.model_copy(update={"commands": new_commands})
            markers: list[Any] = []
            for old_cmd, new_cmd in zip(old_commands, new_commands, strict=True):
                if old_cmd is not new_cmd:
                    old_lp = getattr(getattr(old_cmd, "entry_order", None), "limit_price", None)
                    new_lp = getattr(getattr(new_cmd, "entry_order", None), "limit_price", None)
                    ticker = getattr(getattr(new_cmd, "instrument", None), "ticker", None)
                    if old_lp is not None and new_lp is not None and ticker is not None:
                        markers.append(
                            {
                                "ticker": ticker,
                                "analyst_price": str(old_lp),
                                "marketable_price": str(new_lp),
                            }
                        )
            reprice_markers = tuple(markers)
    submission_results, dispatch_results, abandoned_entries = await _route_through_broker(
        envelope=envelope,
        submission_results=submission_results,
        client=client,
        queries=queries,
        execution_config=execution_config,
        invocation_handle=invocation_handle,
        broker_dispatch=broker_dispatch,
        quote_source=quote_source,
    )
    return envelope, submission_results, dispatch_results, abandoned_entries, reprice_markers


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


def _build_order_thesis_lookup(pm_view: PortfolioManagerView) -> OrderThesisLookup:
    """Build an ``order_id`` → originating-thesis FK lookup for CANCEL routing.

    A CANCEL carries only an ``order_id``; its broker-carried link (ALP-844)
    needs the originating thesis off the targeted pending order. Scans the PM
    view's per-position ``pending_orders`` — the same scope the LLM sees — so a
    CANCEL against an order outside that view resolves to ``None`` and the
    derive site raises rather than minting a thesis-less id.
    """
    thesis_by_order_id: dict[str, ThesisId] = {}
    for view in pm_view.positions:
        for order in view.pending_orders:
            if order.originating_thesis_id is not None:
                thesis_by_order_id[order.order_id] = order.originating_thesis_id
    return thesis_by_order_id.get


__all__ = [
    "_SERVER_NAME",
    "_SUBMIT_ENVELOPE_INPUT_SCHEMA",
    "_TOOL_NAME",
    "_build_order_thesis_lookup",
    "_build_position_lookup",
    "_handle_submit_envelope",
    "build_submit_envelope_mcp_server",
]
