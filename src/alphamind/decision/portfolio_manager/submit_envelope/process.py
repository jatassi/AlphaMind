"""Per-command pipeline for the ``submit_envelope`` package — ALP-464.

Hosts the Layer-1 parse step, the per-command processing loop, and the
result-shape builders. The orchestrator in :mod:`.server` calls
:func:`_validate_envelope_payload` → :func:`_process_commands` → (optionally)
the broker-routing helpers in :mod:`.dispatch`. Envelope-level rejections flow
through :func:`_build_envelope_level_rejection`; per-command results pass
through :func:`_build_acknowledgment` / :func:`_build_rejection_payload`.
:func:`_safe_derive_pm_command_id` and :func:`_serialize_response` live here
because :func:`_build_envelope_level_rejection` consumes both; :mod:`.server`
imports them back.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from alphamind._kernel.ids import EnvelopeId, OrderId, PositionId
from alphamind.commands.command_models import (
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
from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    Acknowledgment,
    RejectionPayload,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
    _BreachedRule,
    _PerRuleHeadroomEntry,
    _ValidationMetadata,
)
from alphamind.execution.oms.command_ids import compute_attempt_seq, derive_pm_command_id
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation import Status
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    validate_guardrail,
)

_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return ticker (equity) or underlying (option/strategy) — mirrors
    :mod:`alphamind.decision.portfolio_manager.validation`."""
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


def _instrument_direction(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return ``direction`` for equity/option; default ``"long"`` for strategy.

    :class:`StrategyInstrument` carries direction per-leg, not at the instrument
    level; the engine-stub falls back to ``"long"`` for projection until story
    03 reshapes the projection to consume real strategy fields.
    """
    if isinstance(instrument, StrategyInstrument):
        return "long"
    return instrument.direction


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


def _safe_derive_pm_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
) -> str:
    """Derive a synthetic PM command_id; tolerate Layer-1 fallback envelope ids.

    Layer-1 fallback paths can emit a non-canonical ``envelope_id`` (e.g.
    ``ENV-REC-INVALID`` when the payload omits it); :func:`derive_pm_command_id`
    raises ``ValueError`` for such inputs. We catch that and assemble a
    structurally well-formed ID so the failure-log surface stays queryable.
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


def _build_envelope_level_rejection(
    *,
    envelope_id: EnvelopeId,
    invocation_id: str,
    suggested_modification: str,
    log_state: SubmitEnvelopeState,
    log_envelope_for_record: PMEnvelope | None,
) -> dict[str, Any]:
    """Build the envelope-level rejection (Layer-1 or Layer-2/3 failure).

    Emits a single synthetic ``submission_result`` with
    ``rule="schema_invariant"`` at ``command_ordinal=0`` — keeps the response
    shape uniform with command-level rejections even when no commands ran.
    ``log_envelope_for_record`` is the parsed PMEnvelope when the rejection
    follows Layer-2/3; ``None`` when Layer-1 parse failed.
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

    # CLOSE/CANCEL produce no projected exposure delta — route directly to
    # accepted without re-running the guardrail library. Mirrors
    # validate_guardrail's own subset semantics (``ValidationAction`` excludes
    # CANCEL).
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
            order_id=OrderId(f"ORD-CLOSE-{command.position_id}"),
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
    """Translate a constructive (OPEN / ADD) or ADJUST command to a
    validate_guardrail request.

    CLOSE/CANCEL short-circuit at the caller and never reach here. ADJUST has
    no new exposure; ADD has no embedded instrument (refs existing position by
    id) — both route to the placeholder path. Real exposure projection for ADD
    lands when the OMS engine wires in the position-id resolver.
    """
    if isinstance(command, OpenCommand):
        return _build_constructive_request_from_open(command)
    # AdjustCommand and AddCommand — placeholder shape; the library treats
    # the request as metadata-only. Real exposure projection for ADD requires
    # the position-id resolver wired through the OMS submission engine.
    action = ValidationAction.ADD if isinstance(command, AddCommand) else ValidationAction.ADJUST
    # ALP-462 — Money → float at the ValidationSize surface (validation tool
    # types live in guardrail_evaluation/types.py which is outside ALP-462).
    if isinstance(command, AddCommand):
        size = ValidationSize(
            quantity=int(command.additional_quantity),
            dollar_value=float(command.additional_dollar_value),
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

    Reads instrument identity via :func:`_instrument_ticker_key` and sizing
    from :class:`PositionSize`. For :class:`OptionInstrument` sources,
    propagates strike/expiration/contract_type to :class:`ValidationInstrument`
    so ``_validate_options_fields`` can compute greeks.
    """
    instrument_kwargs: dict[str, Any] = {
        "ticker": _instrument_ticker_key(command.instrument),
        "asset_type": _OMS_TO_VALIDATION_ASSET[command.instrument.asset_type],
        "direction": _OMS_TO_VALIDATION_DIRECTION[_instrument_direction(command.instrument)],
    }
    if isinstance(command.instrument, OptionInstrument):
        # ALP-462 — Price → float at the ValidationInstrument surface.
        instrument_kwargs["strike"] = float(command.instrument.strike)
        instrument_kwargs["expiration"] = datetime.fromisoformat(
            command.instrument.expiration
        ).replace(tzinfo=UTC)
        instrument_kwargs["contract_type"] = command.instrument.contract_type
    instrument = ValidationInstrument(**instrument_kwargs)
    # ALP-462 — Money → float at the ValidationSize surface.
    size = ValidationSize(
        quantity=int(command.position_size.quantity),
        dollar_value=float(command.position_size.dollar_value),
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


def _build_acknowledgment(
    *,
    command: OpenCommand | AddCommand | AdjustCommand,
    result: ValidationResult,
) -> Acknowledgment:
    """Build an Acknowledgment for an accepted OPEN / ADD / ADJUST command.

    CLOSE/CANCEL build their own Acknowledgments at the caller — they
    short-circuit the guardrail re-run.
    """
    if isinstance(command, OpenCommand | AddCommand):
        # OpenCommand carries ``instrument``; AddCommand refs existing position
        # by id (no embedded instrument). Derive a stub ticker tag accordingly.
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
            implied_volatility=result.implied_volatility,
            delta_adjusted_exposure=result.delta_adjusted_exposure,
            per_rule_headroom=per_rule_headroom,
        )
        if isinstance(command, OpenCommand):
            return Acknowledgment(
                position_id=PositionId(f"POS-{ticker}-stub"),
                order_id=OrderId(f"ORD-{ticker}-stub"),
                validation_metadata=metadata,
            )
        return Acknowledgment(
            position_id=command.position_id,
            order_id=OrderId(f"ORD-{ticker}-stub"),
            validation_metadata=metadata,
        )
    # AdjustCommand
    return Acknowledgment(
        position_id=command.position_id,
        order_id=OrderId(f"ORD-ADJUST-{command.position_id}"),
    )


def _build_rejection_payload(*, result: ValidationResult) -> RejectionPayload:
    """Build a RejectionPayload from a FAIL ValidationResult.

    Mirrors breach-behavior.md § Hard rejection semantics — breached rules
    populate ``rules_breached``; ``suggested_modification`` is
    ``failure_guidance``; ``headroom_after_suggestion`` carries one entry per
    breached rule.
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


__all__ = [
    "_OMS_TO_VALIDATION_ASSET",
    "_OMS_TO_VALIDATION_DIRECTION",
    "_build_acknowledgment",
    "_build_constructive_request_from_open",
    "_build_envelope_level_rejection",
    "_build_rejection_payload",
    "_command_to_validation_request",
    "_format_first_error",
    "_instrument_direction",
    "_instrument_ticker_key",
    "_process_commands",
    "_process_one_command",
    "_safe_derive_pm_command_id",
    "_serialize_response",
    "_validate_envelope_payload",
]
