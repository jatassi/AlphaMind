"""Engine-stub ``submit_envelope`` MCP wrapper — story 06c (ALP-328).

This module is **transitional**. It is the engine-stub the PM tool-use loop
calls during decision-layer invocations until the real OMS submission engine
lands in `ALP-120 <https://linear.app/alphamind-jatassi/issue/ALP-120>`_ and
the persistence layer in `ALP-119 <https://linear.app/alphamind-jatassi/issue/ALP-119>`_.
When those work trees ship, this file is replaced in a coordinated edit.

The wrapper accepts one PM envelope per tool call. It coerces the input dict
to :class:`PMEnvelope` (Layer-1 schema), runs :func:`validate_pm_envelope`
(Layer-2/3, story 06b), then re-runs :func:`validate_guardrail` per embedded
command against cumulative state held in a mutable
:class:`SubmitEnvelopeState` cell. PASS commands advance the cell via
``state.with_accepted_proposal(delta)``; FAIL commands produce a
``rejection_payload`` shaped per ``breach-behavior.md`` § Hard rejection
semantics. Every call (envelope-level rejection or per-command result list)
is appended to the submission log accessible via :func:`get_submission_log`.

Mirrors the analyst's :mod:`alphamind.risk_guardrails.state_delivery.validation_tool_mcp`
factory pattern: per-invocation server, mutable state cell captured by the tool
closure, JSON content blocks for response.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.execution.broker_adapter.order_modify import (
        AssetClass as ReplaceAssetClass,
    )
    from alphamind.execution.broker_adapter.order_modify import (
        OrderClass as ReplaceOrderClass,
    )
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.decision.portfolio_manager.models import PMEnvelope
from alphamind.decision.portfolio_manager.validation import (
    validate_pm_envelope,
)
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.execution.oms.command_ids import compute_attempt_seq, derive_pm_command_id
from alphamind.execution.oms.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    EquityInstrument,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    StrategyInstrument,
)
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import (
    Greeks,
    LibraryConfig,
    MarketInputs,
    Status,
)
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    ValidationToolState,
    validate_guardrail,
)


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return the ticker/underlying key from a canonical OMS instrument.

    Equity instruments expose ``ticker``; option/strategy instruments expose
    ``underlying``. Mirrors the dispatch helper in
    :mod:`alphamind.decision.portfolio_manager.validation`.
    """
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


def _instrument_direction(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return ``direction`` for equity/option; default ``"long"`` for strategy.

    Canonical :class:`StrategyInstrument` carries direction per-leg rather
    than at the instrument level; the engine-stub falls back to ``"long"``
    for projection purposes. Story 03 reshapes the projection to consume
    real strategy fields.
    """
    if isinstance(instrument, StrategyInstrument):
        return "long"
    return instrument.direction


_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


__all__ = [
    "Acknowledgment",
    "FailedSubmissionEntry",
    "RejectionPayload",
    "SubmissionLogEntry",
    "SubmissionResult",
    "SubmitEnvelopeState",
    "build_initial_submit_envelope_state",
    "build_submit_envelope_mcp_server",
    "get_failed_submission_log",
    "get_submission_log",
]


# ---------------------------------------------------------------------------
# Per-command result shapes — minimal Pydantic models mirroring the design doc
# ---------------------------------------------------------------------------


class _PerRuleHeadroomEntry(BaseModel):
    """One projected per-rule headroom entry; appears on Acknowledgment and
    RejectionPayload."""

    model_config = ConfigDict(frozen=True)

    rule: str
    headroom_remaining: float
    unit: str


class _ValidationMetadata(BaseModel):
    """Validation-time computation results attached to OPEN/ADD acknowledgments."""

    model_config = ConfigDict(frozen=True)

    greeks: Greeks | None = None
    implied_volatility: float | None = None
    delta_adjusted_exposure: float
    per_rule_headroom: tuple[_PerRuleHeadroomEntry, ...]


class Acknowledgment(BaseModel):
    """Engine confirmation for an accepted command.

    Per ``submit-envelope-tool-schema.md`` § acknowledgment, the populated
    fields vary by command type — OPEN / ADD carry ``validation_metadata``;
    CANCEL carries ``released_capital_usd``. Rather than encode a discriminated
    union for the engine-stub, we model the union flat and leave inapplicable
    fields ``None``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    position_id: str | None = None
    order_id: str | None = None
    validation_metadata: _ValidationMetadata | None = None
    released_capital_usd: float | None = None


class _BreachedRule(BaseModel):
    """One breached-rule entry on a RejectionPayload."""

    model_config = ConfigDict(frozen=True)

    rule: str
    current: float
    limit: float
    overage: float
    unit: str


class RejectionPayload(BaseModel):
    """Synchronous rejection record per breach-behavior.md § Hard rejection
    semantics. Identical shape to the engine-side rejection so the PM's
    feedback handling treats stub and real-engine rejections uniformly.

    ``gateway_reason`` carries the broker's
    :class:`alphamind.execution.broker_adapter.PermanentRejection` code
    (e.g. ``"insufficient_buying_power"``) when the rejection originated at
    the broker after Layer-1/2/3 validation accepted the command — story 03e
    (ALP-390) coordinated swap. ``None`` for guardrail-side rejections.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    rules_breached: tuple[_BreachedRule, ...]
    suggested_modification: str
    headroom_after_suggestion: tuple[_PerRuleHeadroomEntry, ...] = ()
    greeks: Greeks | None = None
    delta_adjusted_exposure: float | None = None
    feature_disabled: Literal["options", "short_selling", "sector"] | None = None
    gateway_reason: str | None = None


class SubmissionResult(BaseModel):
    """One per-command result mirroring submit-envelope-tool-schema.md's
    submission_result $def."""

    model_config = ConfigDict(frozen=True)

    command_ordinal: int
    status: Literal["accepted", "rejected"]
    command_id: str
    acknowledgment: Acknowledgment | None = None
    rejection_payload: RejectionPayload | None = None


# ---------------------------------------------------------------------------
# State cell + log entry types
# ---------------------------------------------------------------------------


@dataclass
class SubmissionLogEntry:
    """One ``submit_envelope`` call's record — envelope + per-command results."""

    envelope: PMEnvelope
    submission_results: tuple[SubmissionResult, ...]


@dataclass
class FailedSubmissionEntry:
    """One ``submit_envelope`` call that failed Layer-1 (Pydantic) parsing.

    ``submission_log`` only records calls that produced a parsed
    :class:`PMEnvelope`; this parallel log preserves the raw payload, the
    Pydantic error text, and the synthetic ``command_id`` for every Layer-1
    rejection so post-hoc forensics can reconstruct attempts that never
    reached command processing.
    """

    raw_args: dict[str, Any]
    validation_error_repr: str
    command_id: str


@dataclass
class SubmitEnvelopeState:
    """Mutable per-invocation cumulative state for the submit_envelope tool.

    The cell is mutated in place by the MCP closure: ``validation_state``
    advances on every accepted command via ``with_accepted_proposal(delta)``;
    ``submission_log`` appends one entry per call that parsed to a
    :class:`PMEnvelope`; ``failed_submission_log`` appends one entry per
    Layer-1 (Pydantic) parse failure; ``command_id_counter`` is not currently
    incremented (the synthetic ID format derives ordinal from the envelope's
    command index and ``attempt_seq`` from ``post_rejection`` modification
    count, both of which are deterministic from the envelope alone).

    ``invocation_id`` is required (non-empty) — it is interpolated into every
    synthetic command_id via :func:`alphamind.execution.oms.command_ids.derive_pm_command_id`;
    an empty value would surface there as malformed IDs like
    ``inv-.{envelope_id}.0.0``.
    """

    validation_state: ValidationToolState
    invocation_id: str
    submission_log: tuple[SubmissionLogEntry, ...] = ()
    failed_submission_log: tuple[FailedSubmissionEntry, ...] = ()
    command_id_counter: int = 0

    def __post_init__(self) -> None:
        if not self.invocation_id:
            msg = "SubmitEnvelopeState.invocation_id must be non-empty"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Public assemblers
# ---------------------------------------------------------------------------


def build_initial_submit_envelope_state(
    *,
    invocation_id: str,
    starting_validation_state: ValidationToolState,
) -> SubmitEnvelopeState:
    """Construct a fresh :class:`SubmitEnvelopeState` for one invocation.

    The ``starting_validation_state`` is the same cell that backs the
    analyst's / strategist's / PM's :func:`validate_guardrail` MCP tool —
    sharing the cell keeps cumulative-impact tracking unified across
    pre-submission validation and submit-time re-validation.
    """
    return SubmitEnvelopeState(
        validation_state=starting_validation_state,
        invocation_id=invocation_id,
    )


def get_submission_log(state: SubmitEnvelopeState) -> tuple[SubmissionLogEntry, ...]:
    """Return the cumulative submission log for *state*."""
    return state.submission_log


def get_failed_submission_log(
    state: SubmitEnvelopeState,
) -> tuple[FailedSubmissionEntry, ...]:
    """Return the cumulative Layer-1 parse-failure log for *state*."""
    return state.failed_submission_log


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


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
    invocation_handle: Any | None = None,
    state_persistence_config: Any | None = None,
    client: TradingClient | None = None,
    queries: AccountStateQueries | None = None,
    execution_config: ExecutionConfig | None = None,
) -> tuple[Mapping[str, McpSdkServerConfig], tuple[str, ...]]:
    """Build a per-invocation SDK MCP server bound to *state*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The single registered tool is
    ``mcp__alphamind_execution_oms_submit__submit_envelope``.

    The factory captures *state* in the tool's closure; each call mutates
    the cell — accepted commands advance ``state.validation_state`` via
    ``with_accepted_proposal(delta)``, every call appends to
    ``state.submission_log``.

    ``library_config`` and ``library_market`` are part of the runner-facing
    signature (story 08 ALP-330) but are already wired into
    ``state.validation_state`` by ``build_initial_submit_envelope_state``.
    They are accepted here so the runner passes one canonical bundle of
    library plumbing through both surfaces uniformly; the engine-stub does
    not consult them directly.

    When ``invocation_handle`` is supplied, the engine-stub additionally
    writes through every accepted envelope to SQL via the Phase 2 write
    path (ALP-366) and persists Layer-1 parse failures as
    ``envelope_parse_failed`` activity log entries. Composition pipelines
    (ALP-310) inject the handle obtained from the surrounding
    ``InvocationContext``.

    When ``client`` + ``queries`` + ``execution_config`` are supplied
    (engine-stub coordinated swap, story 03e / ALP-390), each accepted
    command additionally routes through :func:`dispatch_command_to_broker`
    before persistence; the persisted entry / close / add / adjust order
    carries Alpaca's real ``alpaca_order_id`` and the acknowledgment surfaces
    it. Gateway-submission failures map to ``command_abandoned`` activity-log
    entries; permanent rejections surface as synchronous OMS rejections.
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
        return await _handle_submit_envelope(
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
        )

    server = create_sdk_mcp_server(name=_SERVER_NAME, tools=[_submit_envelope])
    allowed = (f"mcp__{_SERVER_NAME}__{_TOOL_NAME}",)
    return {_SERVER_NAME: server}, allowed


# ---------------------------------------------------------------------------
# Handler implementation
# ---------------------------------------------------------------------------


async def _handle_submit_envelope(  # noqa: PLR0913 — engine-stub orchestrator threads every per-invocation parameter once.
    args: dict[str, Any],
    *,
    state: SubmitEnvelopeState,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
    invocation_handle: Any | None = None,
    state_persistence_config: Any | None = None,
    client: TradingClient | None = None,
    queries: AccountStateQueries | None = None,
    execution_config: ExecutionConfig | None = None,
) -> dict[str, Any]:
    """Coerce input → run validators → process commands → log + respond.

    When ``invocation_handle`` is supplied (production composition path,
    ALP-310), every accepted envelope writes through to SQL via the Phase 2
    ``persist_envelope_outcome`` and every Layer-1 parse failure additionally
    writes one ``envelope_parse_failed`` activity log entry. When the handle
    is ``None`` (legacy fixture-only path), only the in-memory state-cell
    surfaces are mutated — preserves the engine-stub's pre-ALP-366 behavior.

    When ``client`` + ``queries`` + ``execution_config`` are supplied
    (engine-stub coordinated swap, story 03e / ALP-390), each command that
    passes Layer-1/2/3 validation routes through
    :func:`dispatch_command_to_broker` before Phase 2 writeback.
    """
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
            raw_args=dict(args),
            validation_error_repr=str(exc),
            command_id=synthetic_command_id,
        )
        state.failed_submission_log = (*state.failed_submission_log, failed_entry)
        if invocation_handle is not None:
            await _persist_envelope_parse_failure_via_phase2(
                invocation_handle, failed_entry, state_persistence_config
            )
        return _build_envelope_level_rejection(
            envelope_id=envelope_id,
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
    submission_results = _process_commands(
        envelope,
        state=state,
        sector_resolver=sector_resolver,
    )

    # Step 4: optionally route accepted commands through the broker adapter
    # (engine-stub coordinated swap, story 03e / ALP-390). Returns per-command
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
            invocation_id=state.invocation_id,
            invocation_handle=invocation_handle,
        )

    # Step 5: append to submission log (post-broker outcome).
    state.submission_log = (
        *state.submission_log,
        SubmissionLogEntry(envelope=envelope, submission_results=submission_results),
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
                command_id=abandoned.command_id,
                originating_agent=envelope.source_provenance,
                command_type=abandoned.command_type,
                failure_reason=abandoned.failure_reason,
                retry_attempt_count=abandoned.retry_attempt_count,
            )

    # Step 7: serialize.
    return {
        "content": [
            {
                "type": "text",
                "text": _serialize_response(envelope.envelope_id, submission_results),
            }
        ],
    }


async def _persist_envelope_outcome_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    state_persistence_config: Any | None,
    *,
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None,
) -> None:
    """Lazy import + dispatch to break the import cycle Phase 2 has on us."""
    # Import lazily so submit_envelope_mcp itself stays importable from
    # phase2.py at module-load time (phase2 imports FailedSubmissionEntry).
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_outcome(
        invocation_handle,
        envelope,
        submission_results,
        config=config,
        dispatch_results=dispatch_results,
    )


async def _emit_command_abandoned_via_phase2(
    invocation_handle: Any,
    *,
    envelope_id: str,
    command_id: str,
    originating_agent: str,
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"],
    failure_reason: str,
    retry_attempt_count: int,
) -> None:
    """Lazy-import dispatch for ``persist_command_abandoned``.

    Used by the engine-stub coordinated swap (story 03e / ALP-390) when a
    broker dispatch returns ``GatewaySubmissionFailed`` — the command's
    writeback is skipped and the audit trail surfaces the failure.
    """
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_command_abandoned,
    )

    await persist_command_abandoned(
        invocation_handle,
        envelope_id=envelope_id,
        command_id=command_id,
        originating_agent=originating_agent,
        command_type=command_type,
        failure_reason=failure_reason,
        retry_attempt_count=retry_attempt_count,
    )


async def _persist_envelope_parse_failure_via_phase2(
    invocation_handle: Any,
    failed_entry: FailedSubmissionEntry,
    state_persistence_config: Any | None,
) -> None:
    """Lazy import + dispatch — symmetric with the accepted-envelope helper."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_parse_failure,
    )

    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_parse_failure(invocation_handle, failed_entry, config=config)


async def _persist_envelope_rejection_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    errors: Sequence[Any],
    state_persistence_config: Any | None,
) -> None:
    """Lazy import + dispatch — symmetric with the parse-failure helper."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_rejection,
    )

    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_rejection(invocation_handle, envelope, tuple(errors), config=config)


def _stub_state_persistence_config() -> Any:
    """Construct a no-op StatePersistenceConfig for callers that didn't supply one.

    Phase 2 doesn't read any knob in this story; the config is part of the
    forward-shaped signature only.
    """
    from alphamind.execution.state_persistence.config import StatePersistenceConfig

    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 1,
            "snapshot_read_timeout_seconds": 1.0,
            "pip_freeze_snapshot_root": "/tmp",
            "invocation_provenance_root": "/tmp",
        }
    )


def _validate_envelope_payload(args: dict[str, Any]) -> PMEnvelope:
    """Coerce *args* (raw JSON dict) to :class:`PMEnvelope` via the
    discriminated-union TypeAdapter."""
    return _ENVELOPE_ADAPTER.validate_python(args)


def _format_first_error(exc: ValidationError) -> str:
    """Return the first Pydantic error rendered as ``<field-path>: <message>``."""
    errs = exc.errors()
    if not errs:
        return "envelope failed schema validation"
    err = errs[0]
    path = ".".join(str(p) for p in err["loc"]) or "<root>"
    return f"{path}: {err['msg']}"


def _build_envelope_level_rejection(
    *,
    envelope_id: str,
    invocation_id: str,
    suggested_modification: str,
    log_state: SubmitEnvelopeState,
    log_envelope_for_record: PMEnvelope | None,
) -> dict[str, Any]:
    """Build the envelope-level rejection (Layer-1 or Layer-2/3 failure).

    A single synthetic ``submission_result`` carries ``rule="schema_invariant"``
    at ``command_ordinal=0`` per the design doc — the envelope did not reach
    command processing, so no per-command results exist; the synthetic ordinal
    keeps the response shape uniform with command-level rejections. The
    ``log_envelope_for_record`` is the parsed PMEnvelope when the rejection
    follows Layer-2/3 validation; ``None`` when Layer-1 (Pydantic) parse
    failed and there is no envelope to log.
    """
    synthetic_command_id = _safe_derive_pm_command_id(
        invocation_id=invocation_id,
        envelope_id=envelope_id,
        command_ordinal=0,
        attempt_seq=0,
    )
    rejection = RejectionPayload(
        rules_breached=(
            _BreachedRule(
                rule="schema_invariant",
                current=0.0,
                limit=0.0,
                overage=0.0,
                unit="ok",
            ),
        ),
        suggested_modification=suggested_modification,
    )
    result = SubmissionResult(
        command_ordinal=0,
        status="rejected",
        command_id=synthetic_command_id,
        rejection_payload=rejection,
    )
    submission_results = (result,)
    if log_envelope_for_record is not None:
        log_state.submission_log = (
            *log_state.submission_log,
            SubmissionLogEntry(
                envelope=log_envelope_for_record, submission_results=submission_results
            ),
        )
    return {
        "content": [
            {
                "type": "text",
                "text": _serialize_response(envelope_id, submission_results),
            }
        ],
    }


# ---------------------------------------------------------------------------
# Per-command processing
# ---------------------------------------------------------------------------


def _process_commands(
    envelope: PMEnvelope,
    *,
    state: SubmitEnvelopeState,
    sector_resolver: Callable[[str], str],
) -> tuple[SubmissionResult, ...]:
    """Process every command in *envelope*, in order.

    Re-runs :func:`validate_guardrail` per command against the cumulative
    state cell. PASS advances the cell; FAIL leaves it unchanged. Returns
    the per-command results in command_ordinal order.
    """
    results: list[SubmissionResult] = []
    attempt_seq = compute_attempt_seq(envelope)
    for ordinal, command in enumerate(envelope.commands):
        result = _process_one_command(
            command=command,
            command_ordinal=ordinal,
            envelope=envelope,
            attempt_seq=attempt_seq,
            state=state,
            sector_resolver=sector_resolver,
        )
        results.append(result)
    return tuple(results)


def _process_one_command(
    *,
    command: OMSCommand,
    command_ordinal: int,
    envelope: PMEnvelope,
    attempt_seq: int,
    state: SubmitEnvelopeState,
    sector_resolver: Callable[[str], str],
) -> SubmissionResult:
    """Process one embedded command — translate, validate, format result."""
    command_id = derive_pm_command_id(
        invocation_id=state.invocation_id,
        envelope_id=envelope.envelope_id,
        command_ordinal=command_ordinal,
        attempt_seq=attempt_seq,
    )

    # CLOSE / CANCEL produce no projected exposure delta; route directly to
    # accepted without re-running the guardrail library. The library's CLOSE
    # path requires an existing position id and a ticker present in market
    # data — neither of which the stub envelope carries beyond a position_id
    # for CLOSE. Skipping mirrors validate_guardrail's own subset semantics
    # (its ``ValidationAction`` enum deliberately excludes CANCEL).
    if isinstance(command, CancelCommand):
        ack = Acknowledgment(order_id=command.order_id, released_capital_usd=0.0)
        return SubmissionResult(
            command_ordinal=command_ordinal,
            status="accepted",
            command_id=command_id,
            acknowledgment=ack,
        )
    if isinstance(command, CloseCommand):
        ack = Acknowledgment(
            position_id=command.position_id,
            order_id=f"ORD-CLOSE-{command.position_id}",
        )
        return SubmissionResult(
            command_ordinal=command_ordinal,
            status="accepted",
            command_id=command_id,
            acknowledgment=ack,
        )

    request = _command_to_validation_request(command)
    result = validate_guardrail(request=request, state=state.validation_state)

    if result.overall == "PASS":
        # Advance the cell unless the command produces no exposure delta
        # (CLOSE / ADJUST under the stub semantics).
        if isinstance(command, OpenCommand | AddCommand):
            state.validation_state = state.validation_state.with_accepted_proposal(
                ProjectedDelta(
                    instrument=request.instrument,
                    size=request.size,
                    action=request.action,
                    sector=sector_resolver(request.instrument.ticker),
                    delta_adjusted_exposure=result.delta_adjusted_exposure,
                    greeks=result.greeks,
                    proposal_index=result.proposal_index_in_invocation,
                    reserves_capital=request.reserves_capital,
                    existing_position_id=None,
                )
            )
        ack = _build_acknowledgment(command=command, result=result)
        return SubmissionResult(
            command_ordinal=command_ordinal,
            status="accepted",
            command_id=command_id,
            acknowledgment=ack,
        )

    rejection = _build_rejection_payload(result=result)
    return SubmissionResult(
        command_ordinal=command_ordinal,
        status="rejected",
        command_id=command_id,
        rejection_payload=rejection,
    )


def _command_to_validation_request(
    command: OpenCommand | AddCommand | AdjustCommand,
) -> ValidationRequest:
    """Translate a constructive (OPEN / ADD) or ADJUST command into the
    validate_guardrail request shape.

    CLOSE and CANCEL commands are short-circuited at the caller (no projected
    exposure delta) and never reach this function. ADJUST commands carry no
    new exposure — they remain metadata-only, but a placeholder request is
    still produced so the per-command result-list stays uniform. Canonical
    :class:`AddCommand` has no embedded instrument (it references an existing
    position by id); the engine-stub does not look up the position from
    pm_view here, so ADD also routes to the placeholder path. Real exposure
    projection for ADD lands when the OMS submission engine wires in the
    position-id resolver (post-engine-stub).
    """
    if isinstance(command, OpenCommand):
        return _build_constructive_request_from_open(command)
    # AdjustCommand and AddCommand — placeholder shape; the library treats
    # the request as metadata-only. Real exposure projection for ADD requires
    # the position-id resolver wired through the OMS submission engine.
    action = ValidationAction.ADD if isinstance(command, AddCommand) else ValidationAction.ADJUST
    if isinstance(command, AddCommand):
        size = ValidationSize(
            quantity=int(command.additional_quantity),
            dollar_value=command.additional_dollar_value,
        )
    else:
        size = ValidationSize(quantity=1, dollar_value=0.0)
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker="__PLACEHOLDER__",
            asset_type=InstrumentType.EQUITY,
            direction=Direction.LONG,
        ),
        size=size,
        action=action,
    )


def _build_constructive_request_from_open(command: OpenCommand) -> ValidationRequest:
    """Translate an OPEN command into a validate_guardrail request.

    Reads instrument identity (ticker for equity, underlying for option /
    strategy) via :func:`_instrument_ticker_key` and sizing
    (``quantity``, ``dollar_value``) directly from
    :class:`PositionSize` per the canonical OMS command schema.

    For :class:`OptionInstrument` source instruments, propagates ``strike`` /
    ``expiration`` / ``contract_type`` to the :class:`ValidationInstrument`;
    the validation tool's ``_validate_options_fields`` requires those fields
    populated for ``asset_type=OPTIONS`` and computes greeks from them.
    """
    instrument_kwargs: dict[str, Any] = {
        "ticker": _instrument_ticker_key(command.instrument),
        "asset_type": _OMS_TO_VALIDATION_ASSET[command.instrument.asset_type],
        "direction": _OMS_TO_VALIDATION_DIRECTION[_instrument_direction(command.instrument)],
    }
    if isinstance(command.instrument, OptionInstrument):
        instrument_kwargs["strike"] = command.instrument.strike
        instrument_kwargs["expiration"] = datetime.fromisoformat(
            command.instrument.expiration
        ).replace(tzinfo=UTC)
        instrument_kwargs["contract_type"] = command.instrument.contract_type
    instrument = ValidationInstrument(**instrument_kwargs)
    size = ValidationSize(
        quantity=int(command.position_size.quantity),
        dollar_value=command.position_size.dollar_value,
    )
    return ValidationRequest(instrument=instrument, size=size, action=ValidationAction.OPEN)


_OMS_TO_VALIDATION_ASSET: Mapping[str, InstrumentType] = {
    "equity": InstrumentType.EQUITY,
    "option": InstrumentType.OPTIONS,
    "strategy": InstrumentType.STRATEGY,
}

_OMS_TO_VALIDATION_DIRECTION: Mapping[str, Direction] = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
}


# ---------------------------------------------------------------------------
# Result-shape construction
# ---------------------------------------------------------------------------


def _build_acknowledgment(
    *,
    command: OpenCommand | AddCommand | AdjustCommand,
    result: ValidationResult,
) -> Acknowledgment:
    """Build an Acknowledgment for an accepted OPEN / ADD / ADJUST command.

    CLOSE and CANCEL build their own Acknowledgments at the caller (no
    validation result available — they short-circuit the guardrail re-run).
    """
    if isinstance(command, OpenCommand | AddCommand):
        # Canonical OpenCommand carries `instrument`; canonical AddCommand
        # references the existing position by ``position_id`` and has no
        # embedded instrument. Derive a stub ticker tag accordingly.
        if isinstance(command, OpenCommand):
            ticker = _instrument_ticker_key(command.instrument)
        else:
            ticker = command.position_id
        per_rule_headroom = tuple(
            _PerRuleHeadroomEntry(
                rule=p.rule,
                headroom_remaining=p.headroom_remaining,
                unit=p.unit,
            )
            for p in result.per_rule
        )
        metadata = _ValidationMetadata(
            greeks=result.greeks,
            delta_adjusted_exposure=result.delta_adjusted_exposure,
            per_rule_headroom=per_rule_headroom,
        )
        if isinstance(command, OpenCommand):
            return Acknowledgment(
                position_id=f"POS-{ticker}-stub",
                order_id=f"ORD-{ticker}-stub",
                validation_metadata=metadata,
            )
        return Acknowledgment(
            position_id=command.position_id,
            order_id=f"ORD-{ticker}-stub",
            validation_metadata=metadata,
        )
    # AdjustCommand
    return Acknowledgment(
        position_id=command.position_id,
        order_id=f"ORD-ADJUST-{command.position_id}",
    )


def _build_rejection_payload(*, result: ValidationResult) -> RejectionPayload:
    """Build a RejectionPayload from a FAIL ValidationResult.

    Mirrors breach-behavior.md § Hard rejection semantics — every breached
    rule contributes to ``rules_breached``; ``suggested_modification`` is the
    library's failure_guidance string; ``headroom_after_suggestion`` carries
    one entry per breached rule.
    """
    failed = tuple(p for p in result.per_rule if p.status is Status.FAIL)
    rules_breached = tuple(
        _BreachedRule(
            rule=p.rule,
            current=p.current,
            limit=p.limit,
            overage=abs(p.projected_after - p.limit),
            unit=p.unit,
        )
        for p in failed
    )
    headroom = tuple(
        _PerRuleHeadroomEntry(
            rule=p.rule,
            headroom_remaining=p.headroom_remaining,
            unit=p.unit,
        )
        for p in failed
    )
    return RejectionPayload(
        rules_breached=rules_breached,
        suggested_modification=result.failure_guidance or "Revise the proposal and resubmit.",
        headroom_after_suggestion=headroom,
        greeks=result.greeks,
        delta_adjusted_exposure=result.delta_adjusted_exposure,
    )


# ---------------------------------------------------------------------------
# Synthetic command_id formatting + serialization
# ---------------------------------------------------------------------------


def _safe_derive_pm_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
) -> str:
    """Derive a synthetic PM command_id; tolerate Layer-1 fallback envelope ids.

    Layer-1 parse-failure paths can produce a synthetic ``envelope_id`` that
    does not match the canonical pattern (e.g., the literal ``ENV-REC-INVALID``
    fallback when the raw payload omits ``envelope_id`` entirely). The
    canonical :func:`derive_pm_command_id` raises ``ValueError`` for such
    inputs; this wrapper catches that case and assembles a structurally
    well-formed (regex-matching) ID anyway so the failure-log surface stays
    queryable.
    """
    try:
        return derive_pm_command_id(
            invocation_id=invocation_id,
            envelope_id=envelope_id,
            command_ordinal=command_ordinal,
            attempt_seq=attempt_seq,
        )
    except ValueError:
        prefix = invocation_id if invocation_id.startswith("inv-") else f"inv-{invocation_id}"
        return f"{prefix}.{envelope_id}.{command_ordinal}.{attempt_seq}"


def _serialize_response(envelope_id: str, results: tuple[SubmissionResult, ...]) -> str:
    """Serialize the response to JSON text per submit-envelope-tool-schema.md."""
    return json.dumps(
        {
            "envelope_id": envelope_id,
            "submission_results": [r.model_dump(mode="json") for r in results],
        }
    )


# ---------------------------------------------------------------------------
# Broker-routing helpers (story 03e / ALP-390)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AbandonedCommandEntry:
    """Per-command broker-failure marker — drives the ``command_abandoned`` emit.

    The OMS skips Phase 2 writeback for an abandoned command (no order rows
    persisted) but still emits one ``command_abandoned`` activity-log entry
    so post-hoc forensics can reconstruct why the command never reached the
    broker.
    """

    command_id: str
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
    failure_reason: str
    retry_attempt_count: int


_COMMAND_TYPE_TO_LABEL: dict[str, Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]] = {
    "open": "OPEN",
    "close": "CLOSE",
    "adjust": "ADJUST",
    "cancel": "CANCEL",
    "add": "ADD",
}


async def _route_through_broker(
    *,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    client: TradingClient,
    queries: AccountStateQueries,
    execution_config: ExecutionConfig,
    invocation_id: str,
    invocation_handle: Any | None = None,
) -> tuple[
    tuple[SubmissionResult, ...],
    tuple[BrokerDispatchResult | None, ...],
    tuple[_AbandonedCommandEntry, ...],
]:
    """Dispatch each accepted command through the broker adapter.

    Returns ``(updated_submission_results, dispatch_results, abandoned_entries)``.

    For each command:
    * Validator-rejected (already ``status="rejected"``) → unchanged, dispatch entry None.
    * Validator-accepted + broker ``Submitted`` → unchanged, dispatch entry carries ack.
    * Validator-accepted + broker ``GatewaySubmissionFailed`` → flipped to rejected
      with a ``broker_gateway_failure`` rule; dispatch entry None;
      ``_AbandonedCommandEntry`` appended for the writeback step.
    * Validator-accepted + ``PermanentRejectionError`` raised → flipped to rejected
      with the broker's ``PermanentRejection.code`` as the rule; dispatch entry None.
    """
    # Lazy imports — broker_dispatch transitively imports the broker_adapter
    # package which in turn ships an alpaca-py dependency we don't want loaded
    # for the legacy fixture-only path.
    from alphamind.execution.broker_adapter import (
        GatewaySubmissionFailed,
        Submitted,
    )
    from alphamind.execution.broker_adapter.errors import classify_alpaca_error
    from alphamind.execution.broker_adapter.order_options import (
        PermanentRejectionError,
    )
    from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker

    updated: list[SubmissionResult] = []
    dispatches: list[BrokerDispatchResult | None] = []
    abandoned: list[_AbandonedCommandEntry] = []

    for result, command in zip(submission_results, envelope.commands, strict=True):
        if result.status != "accepted":
            updated.append(result)
            dispatches.append(None)
            continue
        try:
            context_kwargs = await _dispatcher_context_for(
                command, invocation_handle=invocation_handle
            )
            outcome = await dispatch_command_to_broker(
                command,
                client=client,
                queries=queries,
                execution=execution_config,
                client_order_id=result.command_id,
                **context_kwargs,
            )
        except PermanentRejectionError as exc:
            # Single-leg options translator wraps permanent rejections in this
            # typed exception; surface the broker code in gateway_reason.
            updated.append(_to_rejection(result, code=exc.rejection.code, reason=str(exc)))
            dispatches.append(None)
            continue
        except Exception as exc:
            # Equity / mleg translators re-raise the raw alpaca-py APIError on
            # permanent failure; classify here so the engine-stub surfaces a
            # uniform broker-rejection shape regardless of which translator
            # produced the error. ``BaseException`` (CancelledError, etc.)
            # propagates so external interruptions are never re-classified as
            # broker rejections.
            rejection = classify_alpaca_error(exc)
            if rejection is None:
                raise
            updated.append(
                _to_rejection(
                    result,
                    code=rejection.code,
                    reason=(
                        f"Alpaca rejected: code={rejection.code}, "
                        f"http_status={rejection.http_status}, "
                        f"message={rejection.alpaca_message!r}"
                    ),
                )
            )
            dispatches.append(None)
            continue
        if isinstance(outcome, GatewaySubmissionFailed):
            reason = (
                f"gateway_submission_failed: {outcome.reason} "
                f"(last_error={outcome.last_error_class}, attempts={outcome.attempt_count})"
            )
            updated.append(_to_rejection(result, code="broker_gateway_failure", reason=reason))
            dispatches.append(None)
            abandoned.append(
                _AbandonedCommandEntry(
                    command_id=result.command_id,
                    command_type=_COMMAND_TYPE_TO_LABEL[command.command_type],
                    failure_reason=reason,
                    retry_attempt_count=outcome.attempt_count,
                )
            )
            continue
        assert isinstance(outcome, Submitted)
        # Replace the validator's synthetic order_id on the acknowledgment with
        # the broker's real alpaca_order_id so the submission_log surface
        # carries the broker-grade id.
        updated.append(_with_real_order_id(result, outcome.payload.alpaca_order_id))
        dispatches.append(outcome.payload)

    # invocation_id retained on the signature for future provenance threading.
    del invocation_id
    return tuple(updated), tuple(dispatches), tuple(abandoned)


async def _dispatcher_context_for(
    command: OMSCommand, *, invocation_handle: Any | None
) -> dict[str, Any]:
    """Resolve the per-command kwargs the dispatcher needs.

    For OPEN, the canonical command carries the instrument inline — no extra
    context required. For CLOSE / ADD / ADJUST, the dispatcher needs portfolio-
    state context (symbol / quantity / side / OCC / strategy legs); we read it
    from the persisted position record under *invocation_handle*'s session.
    For CANCEL, the dispatcher needs the target alpaca_order_id; we read it
    from the order record. ``CancelCommand.order_id`` is the OMS order id —
    we resolve it to the broker's alpaca_order_id via the persisted order row.
    """
    if isinstance(command, OpenCommand):
        return {}
    if invocation_handle is None:
        msg = (
            f"engine-stub broker-routing for {command.command_type!r} commands "
            "requires invocation_handle to resolve portfolio state from the "
            "persisted position record."
        )
        raise ValueError(msg)
    if isinstance(command, CloseCommand):
        return await _close_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, AddCommand):
        return await _add_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, AdjustCommand):
        return await _adjust_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, CancelCommand):
        return await _cancel_command_context(command, invocation_handle=invocation_handle)
    msg = f"unsupported OMS command variant for broker routing: {type(command).__name__}"
    raise NotImplementedError(msg)


async def _close_command_context(
    command: CloseCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve dispatcher context for a CLOSE command.

    Reads the position record by id and projects asset-specific fields
    (symbol/qty/side for equity; OCC/intent for options; legs/strategy_type
    for strategy) the dispatcher needs.
    """
    position = await _read_position(command.position_id, invocation_handle=invocation_handle)
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )

    if isinstance(position.details, EquityPositionDetails):
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_qty": position.details.share_count,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # OptionsPositionDetails stores the contract fields rather than the OCC
        # symbol; derive the OCC at the dispatcher boundary.
        from alphamind.execution.broker_adapter.order_options import build_occ_symbol

        occ = build_occ_symbol(
            position.details.underlying_ticker,
            position.details.expiration_date,
            position.details.contract_type,
            position.details.strike_price,
        )
        return {
            "position_asset_type": "option",
            "occ_symbol": occ,
            "position_qty": position.details.contract_count,
            "position_intent": (
                "sell_to_close" if position.direction.value == "LONG" else "buy_to_close"
            ),
        }
    if isinstance(position.details, StrategyPositionDetails):
        # StrategyPositionDetails carries legs and a strategy_type_label; the
        # broker translator needs the typed StrategyType, so we coerce here.
        from alphamind.execution.oms.command_models import StrategyType

        legs = _persisted_legs_to_mleg_acks(position.details.legs)
        return {
            "position_asset_type": "strategy",
            "open_legs": legs,
            "strategy_type": cast(StrategyType, position.details.strategy_type_label),
            "position_units": None,
        }
    msg = f"CLOSE references position with unsupported details: {type(position.details).__name__}"
    raise NotImplementedError(msg)


async def _add_command_context(command: AddCommand, *, invocation_handle: Any) -> dict[str, Any]:
    """Resolve dispatcher context for an ADD command.

    Reads the position record by id; threads symbol/side for equity, the
    embedded OptionInstrument for options, the open legs + strategy_type
    for strategy.
    """
    position = await _read_position(command.position_id, invocation_handle=invocation_handle)
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )

    if isinstance(position.details, EquityPositionDetails):
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # Reconstruct the OptionInstrument the dispatcher needs from the
        # persisted contract fields.
        from alphamind.execution.oms.command_models import OptionInstrument

        instrument = OptionInstrument(
            asset_type="option",
            underlying=position.details.underlying_ticker,
            strike=position.details.strike_price,
            expiration=position.details.expiration_date.isoformat(),
            contract_type=("call" if position.details.contract_type.value == "CALL" else "put"),
            direction="long" if position.direction.value == "LONG" else "short",
        )
        return {
            "position_asset_type": "option",
            "position_option_instrument": instrument,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, StrategyPositionDetails):
        from alphamind.execution.oms.command_models import StrategyType

        legs = _persisted_legs_to_mleg_acks(position.details.legs)
        return {
            "position_asset_type": "strategy",
            "open_legs": legs,
            "strategy_type": cast(StrategyType, position.details.strategy_type_label),
        }
    msg = f"ADD references position with unsupported details: {type(position.details).__name__}"
    raise NotImplementedError(msg)


async def _adjust_command_context(
    command: AdjustCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve dispatcher context for an ADJUST command.

    Reads the bracket-side protective order being modified and surfaces its
    alpaca_order_id + asset_class + order_class. The leg targeted matches
    the change-field on the command (NewStopLevel → PRICE_STOP only;
    NewTargetLevel → TAKE_PROFIT only; new_time_expiration → TIME_STOP) so
    the broker mutation stays in lockstep with the OMS-state writeback in
    :func:`_writeback_adjust`. The asset_class / order_class are derived
    from the position's persisted details — equity routes to ``us_equity``,
    options to ``us_option``, strategies to ``us_option_strategy / mleg``.
    """
    from sqlalchemy import select as _select

    from alphamind.execution.state_persistence.tables.orders import OrderRow
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )

    position = await _read_position(command.position_id, invocation_handle=invocation_handle)

    if position.bracket_id is None:
        msg = f"ADJUST references position {command.position_id!r} with no bracket"
        raise ValueError(msg)

    target_roles = _adjust_target_roles(command)
    if not target_roles:
        # Pure thesis-update / event-invalidation ADJUST — no broker mutation
        # required. The dispatcher should not be invoked; surfacing this as a
        # structural error prevents a silent no-op patch.
        msg = (
            f"ADJUST against position {command.position_id!r} carries no "
            "protective change-fields (stop / target / time); no broker leg to replace"
        )
        raise ValueError(msg)

    rows = (
        (
            await invocation_handle.session.execute(
                _select(OrderRow).where(
                    OrderRow.bracket_id == position.bracket_id,
                    OrderRow.status == "PENDING",
                )
            )
        )
        .scalars()
        .all()
    )
    target = next((r for r in rows if r.order_role in target_roles), None)
    if target is None:
        msg = (
            f"ADJUST against bracket {position.bracket_id!r} found no "
            f"PENDING protective leg matching roles {sorted(target_roles)} to replace"
        )
        raise ValueError(msg)

    # Derive broker asset_class / order_class from the position's details
    # payload. Strategy positions submit as us_option_strategy/mleg; single-leg
    # options as us_option/simple; equity as us_equity/simple. Bracket child
    # legs on equity are themselves submitted as simple orders by Alpaca on
    # cancel-and-replace (the bracket parent stays linked, the child replaces
    # in place).
    if isinstance(position.details, StrategyPositionDetails):
        target_asset_class: ReplaceAssetClass = "us_option_strategy"
        target_order_class: ReplaceOrderClass = "mleg"
    elif isinstance(position.details, OptionsPositionDetails):
        target_asset_class = "us_option"
        target_order_class = "simple"
    elif isinstance(position.details, EquityPositionDetails):
        target_asset_class = "us_equity"
        target_order_class = "simple"
    else:
        msg = (
            f"ADJUST against position {command.position_id!r} carries unsupported "
            f"details type {type(position.details).__name__}"
        )
        raise NotImplementedError(msg)

    return {
        "target_alpaca_order_id": target.alpaca_order_id,
        "target_asset_class": target_asset_class,
        "target_order_class": target_order_class,
    }


def _adjust_target_roles(command: AdjustCommand) -> frozenset[str]:
    """Return the protective-leg role(s) the ADJUST's change-fields target.

    Matches the writeback's :func:`_protective_roles_for_change_fields` so the
    broker leg mutated and the OMS leg cancelled stay in lockstep. Returns
    ``frozenset()`` when the ADJUST carries only thesis-component / event
    updates and no broker mutation is required.
    """
    roles: set[str] = set()
    if command.new_stop_level is not None:
        roles.add("PRICE_STOP")
    if command.new_target_level is not None:
        roles.add("TAKE_PROFIT")
    if command.new_time_expiration is not None:
        roles.add("TIME_STOP")
    return frozenset(roles)


async def _cancel_command_context(
    command: CancelCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve dispatcher context for a CANCEL command.

    Reads the target order record by OMS id and surfaces its alpaca_order_id.
    """
    from alphamind.execution.state_persistence.tables.orders import OrderRow

    order_row = await invocation_handle.session.get(OrderRow, command.order_id)
    if order_row is None:
        msg = f"CANCEL references missing order_id={command.order_id!r}"
        raise ValueError(msg)
    return {"target_alpaca_order_id": order_row.alpaca_order_id}


async def _read_position(position_id: str, *, invocation_handle: Any) -> Any:
    from alphamind.execution.state_persistence.tables.positions import PositionRow
    from alphamind.execution.state_persistence.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    pos_row = await invocation_handle.session.get(PositionRow, position_id)
    if pos_row is None:
        msg = f"command references missing position_id={position_id!r}"
        raise ValueError(msg)
    return position_row_to_record(pos_row)


def _persisted_legs_to_mleg_acks(legs: Any) -> tuple[Any, ...]:
    """Translate a persisted strategy's legs into broker-adapter ``MLEGLegAck`` tuple.

    The persisted ``StrategyLeg`` carries an embedded ``OptionsPositionDetails``
    on its ``options`` field plus a per-leg ``direction``; we read the OCC
    fields off ``leg.options`` and the side off ``leg.direction``.
    Ratio is always 1 for the persisted shape — strategies persist legs as a
    flat tuple at unit ratio, with proportional sizing carried at the
    position level.
    """
    from alphamind.execution.broker_adapter import MLEGLegAck
    from alphamind.execution.broker_adapter.order_options import build_occ_symbol

    acks: list[Any] = []
    for leg in legs:
        opt = leg.options
        occ = build_occ_symbol(
            opt.underlying_ticker, opt.expiration_date, opt.contract_type, opt.strike_price
        )
        leg_direction = leg.direction
        if leg_direction is None:
            msg = f"persisted strategy leg {leg.leg_id!r} has no direction set"
            raise ValueError(msg)
        side: Literal["buy", "sell"] = "buy" if leg_direction.value == "LONG" else "sell"
        intent: Literal["buy_to_open", "sell_to_open"] = (
            "buy_to_open" if side == "buy" else "sell_to_open"
        )
        acks.append(
            MLEGLegAck(
                occ_symbol=occ,
                side=side,
                ratio_qty=1,
                position_intent=intent,
            )
        )
    return tuple(acks)


def _to_rejection(result: SubmissionResult, *, code: str, reason: str) -> SubmissionResult:
    """Flip an accepted result to a broker-rejected one.

    The broker's rejection ``code`` flows into ``gateway_reason`` per the
    coordinated-swap acceptance criteria (story 03e / ALP-390); the descriptive
    ``reason`` lands in ``suggested_modification`` so the PM tool's feedback
    loop sees the same shape as a guardrail-side rejection.
    """
    return SubmissionResult(
        command_ordinal=result.command_ordinal,
        status="rejected",
        command_id=result.command_id,
        rejection_payload=RejectionPayload(
            rules_breached=(
                _BreachedRule(
                    rule=code,
                    current=0.0,
                    limit=0.0,
                    overage=0.0,
                    unit="ok",
                ),
            ),
            suggested_modification=reason,
            gateway_reason=code,
        ),
    )


def _with_real_order_id(result: SubmissionResult, alpaca_order_id: str) -> SubmissionResult:
    """Return a copy of *result* whose acknowledgment ``order_id`` is the broker's id."""
    if result.acknowledgment is None:
        return result
    new_ack = result.acknowledgment.model_copy(update={"order_id": alpaca_order_id})
    return SubmissionResult(
        command_ordinal=result.command_ordinal,
        status=result.status,
        command_id=result.command_id,
        acknowledgment=new_ack,
        rejection_payload=result.rejection_payload,
    )
