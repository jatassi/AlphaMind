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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.decision.portfolio_manager import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
    PMEnvelope,
)
from alphamind.decision.portfolio_manager.validation import (
    validate_pm_envelope,
)
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
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
    feedback handling treats stub and real-engine rejections uniformly."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    rules_breached: tuple[_BreachedRule, ...]
    suggested_modification: str
    headroom_after_suggestion: tuple[_PerRuleHeadroomEntry, ...] = ()
    greeks: Greeks | None = None
    delta_adjusted_exposure: float | None = None
    feature_disabled: Literal["options", "short_selling", "sector"] | None = None


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
    """

    validation_state: ValidationToolState
    submission_log: tuple[SubmissionLogEntry, ...] = ()
    failed_submission_log: tuple[FailedSubmissionEntry, ...] = ()
    command_id_counter: int = 0
    invocation_id: str = ""


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
        submission_log=(),
        failed_submission_log=(),
        command_id_counter=0,
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
) -> dict[str, Any]:
    """Coerce input → run validators → process commands → log + respond.

    When ``invocation_handle`` is supplied (production composition path,
    ALP-310), every accepted envelope writes through to SQL via the Phase 2
    ``persist_envelope_outcome`` and every Layer-1 parse failure additionally
    writes one ``envelope_parse_failed`` activity log entry. When the handle
    is ``None`` (legacy fixture-only path), only the in-memory state-cell
    surfaces are mutated — preserves the engine-stub's pre-ALP-366 behavior.
    """
    # Step 1: Layer-1 — coerce to PMEnvelope.
    try:
        envelope = _validate_envelope_payload(args)
    except ValidationError as exc:
        envelope_id = str(args.get("envelope_id", "ENV-REC-INVALID"))
        synthetic_command_id = _format_command_id(
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
    )
    if not layer23.is_valid:
        suggested = layer23.errors[0].message
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

    # Step 4: append to submission log.
    state.submission_log = (
        *state.submission_log,
        SubmissionLogEntry(envelope=envelope, submission_results=submission_results),
    )

    # Step 5: SQL writeback (opt-in via invocation_handle).
    if invocation_handle is not None:
        await _persist_envelope_outcome_via_phase2(
            invocation_handle, envelope, submission_results, state_persistence_config
        )

    # Step 6: serialize.
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
) -> None:
    """Lazy import + dispatch to break the import cycle Phase 2 has on us."""
    # Import lazily so submit_envelope_mcp itself stays importable from
    # phase2.py at module-load time (phase2 imports FailedSubmissionEntry).
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_outcome(invocation_handle, envelope, submission_results, config=config)


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
    synthetic_command_id = _format_command_id(
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
    attempt_seq = _attempt_seq(envelope)
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
    command_id = _format_command_id(
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


def _attempt_seq(envelope: PMEnvelope) -> int:
    """Compute ``attempt_seq`` per oms-command-ids.md: count of post_rejection
    modifications in the envelope's modifications list."""
    return sum(1 for m in envelope.modifications if m.phase == "post_rejection")


def _command_to_validation_request(
    command: OpenCommand | AddCommand | AdjustCommand,
) -> ValidationRequest:
    """Translate a constructive (OPEN / ADD) or ADJUST command into the
    validate_guardrail request shape.

    CLOSE and CANCEL commands are short-circuited at the caller (no projected
    exposure delta) and never reach this function. ADJUST commands carry no
    new exposure on the stub envelope — for symmetry we still produce a
    request shape, but the library treats ADJUST as a metadata-only change.
    """
    if isinstance(command, OpenCommand):
        return _build_constructive_request(command, action=ValidationAction.OPEN)
    if isinstance(command, AddCommand):
        return _build_constructive_request(command, action=ValidationAction.ADD)
    # AdjustCommand — metadata-only; produce a token shape against a neutral
    # placeholder ticker. ADJUST is rare enough on stub envelopes that we
    # accept the placeholder; downstream stories may revisit when the full
    # OMS command shape (ALP-120) lands.
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker="__ADJUST__",
            asset_type=InstrumentType.EQUITY,
            direction=Direction.LONG,
        ),
        size=ValidationSize(quantity=1, dollar_value=0.0),
        action=ValidationAction.ADJUST,
    )


def _build_constructive_request(
    command: OpenCommand | AddCommand, *, action: ValidationAction
) -> ValidationRequest:
    """Translate an OPEN or ADD command into a validate_guardrail request.

    The minimal embedded OMS command shape carries asset_type / direction /
    underlying / sector. For sector-concentration and per-rule projection the
    library needs a notional sizing; the stub uses a token sizing of 1% of
    portfolio so the projection produces non-degenerate values.
    """
    instrument = ValidationInstrument(
        ticker=command.instrument.underlying,
        asset_type=_OMS_TO_VALIDATION_ASSET[command.instrument.asset_type],
        direction=_OMS_TO_VALIDATION_DIRECTION[command.instrument.direction],
    )
    # Token $1,000 (1% of $100k default portfolio). The stub does not have
    # access to the full broker-grade sizing fields.
    size = ValidationSize(quantity=1, dollar_value=1_000.0)
    return ValidationRequest(instrument=instrument, size=size, action=action)


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
        ticker = command.instrument.underlying
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


def _format_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
) -> str:
    """Format a synthetic command_id per ``oms-command-ids.md``.

    Pattern: ``inv-{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}``.
    The wrapper prefixes ``inv-`` if the supplied invocation_id does not
    already start with it so the regex on the design doc holds.
    """
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
