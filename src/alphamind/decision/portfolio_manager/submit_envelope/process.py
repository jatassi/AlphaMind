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

import dataclasses
import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal

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
from alphamind.execution.oms.command_ids import (
    compute_attempt_seq,
    derive_pm_command_id,
    synthesize_id_suffix,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionContractType,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    position_direction,
    resolve_ticker,
)
from alphamind.risk_guardrails.guardrail_evaluation import Status
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    ValidationStrategyLeg,
    validate_guardrail,
)

PositionLookup = Callable[[str], PositionRecord | None]
"""Resolves a ``position_id`` to its persisted record (or ``None`` if missing)."""

_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return ticker (equity) or underlying (option/strategy) — mirrors
    :mod:`alphamind.decision.portfolio_manager.validation`."""
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


def _strategy_legs_for_validation(
    instrument: StrategyInstrument,
) -> tuple[ValidationStrategyLeg, ...]:
    """Project :class:`StrategyInstrument` legs to
    :class:`ValidationStrategyLeg` tuples for the guardrail validator.

    Each leg carries its own ``direction``; the library's option-leg builder
    consumes these directly when computing per-leg greeks. A multi-leg strategy
    has no position-level direction (ALP-603) — only the per-leg directions
    projected here.
    """
    return tuple(
        ValidationStrategyLeg(
            direction=_OMS_TO_VALIDATION_DIRECTION[leg.direction],
            asset_type=InstrumentType.OPTIONS,
            strike=float(leg.strike),
            expiration=datetime.fromisoformat(leg.expiration).replace(tzinfo=UTC),
            contract_type=leg.contract_type,
            quantity=leg.quantity_ratio,
        )
        for leg in instrument.legs
    )


def _validate_envelope_payload(args: dict[str, Any]) -> PMEnvelope:
    """Coerce *args* (raw JSON dict) to :class:`PMEnvelope` via the
    discriminated-union TypeAdapter."""
    return _ENVELOPE_ADAPTER.validate_python(args)


def _unwrap_envelope_args(args: dict[str, Any]) -> dict[str, Any]:
    """Layer-0 tolerant unwrap of a single ``envelope`` wrapper key (ALP-700).

    The ``submit_envelope`` tool's input schema is permissive
    (``additionalProperties: True``, empty ``properties``) because the SDK
    validator cannot traverse the discriminated-union shape — Pydantic
    runs at the handler instead. That permissiveness lets the LLM
    occasionally hand in ``{"envelope": {<envelope fields>}}`` instead of
    inlining the envelope fields at the top level; the discriminated-union
    parse then fails with ``union_tag_not_found`` because ``source_provenance``
    is one level below where Pydantic looks. ALP-700 observed the PM agent
    burning three tool calls discovering the contract before settling on
    the inlined form.

    Trigger is intentionally narrow: only unwrap when ``envelope`` is the
    SOLE top-level key and its value is a dict. Payloads with sibling
    fields fall through to the normal Layer-1 failure path so structural
    errors are surfaced rather than masked.
    """
    if len(args) != 1:
        return args
    inner = args.get("envelope")
    if not isinstance(inner, dict):
        return args
    return inner


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
) -> tuple[dict[str, Any], SubmitEnvelopeState]:
    """Build the envelope-level rejection (Layer-1 or Layer-2/3 failure).

    Emits a single synthetic ``submission_result`` with
    ``rule="schema_invariant"`` at ``command_ordinal=0`` — keeps the response
    shape uniform with command-level rejections even when no commands ran.
    ``log_envelope_for_record`` is the parsed PMEnvelope when the rejection
    follows Layer-2/3; ``None`` when Layer-1 parse failed.

    Returns ``(response, new_state)`` — the new state carries the appended
    submission-log entry when ``log_envelope_for_record`` is supplied.
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
    new_state = log_state
    if log_envelope_for_record is not None:
        new_state = dataclasses.replace(
            log_state,
            submission_log=(
                *log_state.submission_log,
                SubmissionLogEntry(
                    envelope=log_envelope_for_record,
                    submission_results=submission_results,
                ),
            ),
        )
    response = {
        "content": [
            {
                "type": "text",
                "text": _serialize_response(envelope_id, submission_results),
            }
        ],
    }
    return response, new_state


def _process_commands(
    envelope: PMEnvelope,
    *,
    state: SubmitEnvelopeState,
    sector_resolver: Callable[[str], str],
    position_lookup: PositionLookup,
) -> tuple[tuple[SubmissionResult, ...], SubmitEnvelopeState]:
    """Process every command in *envelope*, in order.

    Re-runs :func:`validate_guardrail` per command against the cumulative
    state. PASS advances ``validation_state`` (returned in the new state
    instance); FAIL leaves it unchanged. Returns the per-command results in
    command_ordinal order alongside the post-processing state.
    """
    results: list[SubmissionResult] = []
    attempt_seq = compute_attempt_seq(envelope)
    current_state = state
    for ordinal, command in enumerate(envelope.commands):
        result, current_state = _process_one_command(
            command=command,
            command_ordinal=ordinal,
            envelope=envelope,
            attempt_seq=attempt_seq,
            state=current_state,
            sector_resolver=sector_resolver,
            position_lookup=position_lookup,
        )
        results.append(result)
    return tuple(results), current_state


def _process_one_command(
    *,
    command: OMSCommand,
    command_ordinal: int,
    envelope: PMEnvelope,
    attempt_seq: int,
    state: SubmitEnvelopeState,
    sector_resolver: Callable[[str], str],
    position_lookup: PositionLookup,
) -> tuple[SubmissionResult, SubmitEnvelopeState]:
    """Process one embedded command — translate, validate, format result.

    Returns ``(submission_result, new_state)`` — accepted OPEN/ADD commands
    return a state with an advanced ``validation_state``; all other outcomes
    return the input state unchanged.
    """
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
        return (
            SubmissionResult(
                command_ordinal=command_ordinal,
                status="accepted",
                command_id=command_id,
                acknowledgment=ack,
            ),
            state,
        )
    if isinstance(command, CloseCommand):
        ack = Acknowledgment(
            position_id=command.position_id,
            order_id=OrderId(f"ORD-CLOSE-{command.position_id}"),
        )
        return (
            SubmissionResult(
                command_ordinal=command_ordinal,
                status="accepted",
                command_id=command_id,
                acknowledgment=ack,
            ),
            state,
        )

    request = _command_to_validation_request(command, position_lookup=position_lookup)
    result = validate_guardrail(request=request, state=state.validation_state)

    if result.overall == "PASS":
        # Advance ``validation_state`` unless the command produces no exposure
        # delta (ADJUST is metadata-only at this layer).
        new_state = state
        if isinstance(command, OpenCommand | AddCommand):
            new_state = dataclasses.replace(
                state,
                validation_state=state.validation_state.with_accepted_proposal(
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
                ),
            )
        ack = _build_acknowledgment(command=command, command_id=command_id, result=result)
        return (
            SubmissionResult(
                command_ordinal=command_ordinal,
                status="accepted",
                command_id=command_id,
                acknowledgment=ack,
            ),
            new_state,
        )

    rejection = _build_rejection_payload(result=result)
    return (
        SubmissionResult(
            command_ordinal=command_ordinal,
            status="rejected",
            command_id=command_id,
            rejection_payload=rejection,
        ),
        state,
    )


def _command_to_validation_request(
    command: OpenCommand | AddCommand | AdjustCommand,
    *,
    position_lookup: PositionLookup,
) -> ValidationRequest:
    """Translate a constructive (OPEN / ADD) or ADJUST command to a
    validate_guardrail request.

    CLOSE/CANCEL short-circuit at the caller and never reach here. OPEN reads
    its instrument inline; ADD resolves the existing position via
    *position_lookup* and projects its instrument. ADJUST has no new exposure
    so it routes to a metadata-only placeholder request — the guardrail
    library treats it as such.
    """
    if isinstance(command, OpenCommand):
        return _build_constructive_request_from_open(command)
    if isinstance(command, AddCommand):
        return _build_constructive_request_from_add(command, position_lookup=position_lookup)
    # AdjustCommand — metadata-only request; ADJUST carries no projected delta.
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker=command.position_id,
            asset_type=InstrumentType.EQUITY,
            direction=Direction.LONG,
        ),
        size=ValidationSize(quantity=1, dollar_value=0.0),
        action=ValidationAction.ADJUST,
    )


def _build_constructive_request_from_open(command: OpenCommand) -> ValidationRequest:
    """Translate an OPEN command into a validate_guardrail request.

    Reads instrument identity via :func:`_instrument_ticker_key` and sizing
    from :class:`PositionSize`. For :class:`OptionInstrument` sources,
    propagates strike/expiration/contract_type to :class:`ValidationInstrument`
    so ``_validate_cross_field_invariants`` can compute greeks. For
    :class:`StrategyInstrument` sources, projects the per-leg directions /
    strikes / expirations / contract_types onto :class:`ValidationStrategyLeg`
    tuples; the position-level direction is ``None`` (ALP-603) so the validator
    builds the option-leg tuple from the per-leg fields, not a synthetic
    top-level direction.
    """
    instrument = ValidationInstrument(
        **_open_instrument_kwargs(command.instrument),
    )
    # ALP-462 — Money → float at the ValidationSize surface.
    size = ValidationSize(
        quantity=int(command.position_size.quantity),
        dollar_value=float(command.position_size.dollar_value),
    )
    return ValidationRequest(instrument=instrument, size=size, action=ValidationAction.OPEN)


def _open_instrument_kwargs(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> dict[str, Any]:
    """Build the kwargs for :class:`ValidationInstrument` from an OPEN
    command's instrument variant.

    A multi-leg STRATEGY has no position-level direction (ALP-603) — its
    directional sign rides on the per-leg :class:`ValidationStrategyLeg`
    tuples — so ``direction`` is ``None`` for a strategy; equity / single-leg
    options carry the mapped :class:`Direction`.
    """
    direction: Direction | None
    if isinstance(instrument, StrategyInstrument):
        direction = None
    else:
        direction = _OMS_TO_VALIDATION_DIRECTION[instrument.direction]
    kwargs: dict[str, Any] = {
        "ticker": _instrument_ticker_key(instrument),
        "asset_type": _OMS_TO_VALIDATION_ASSET[instrument.asset_type],
        "direction": direction,
    }
    if isinstance(instrument, OptionInstrument):
        kwargs["strike"] = float(instrument.strike)
        kwargs["expiration"] = datetime.fromisoformat(instrument.expiration).replace(tzinfo=UTC)
        kwargs["contract_type"] = instrument.contract_type
    elif isinstance(instrument, StrategyInstrument):
        kwargs["legs"] = _strategy_legs_for_validation(instrument)
    return kwargs


def _build_constructive_request_from_add(
    command: AddCommand, *, position_lookup: PositionLookup
) -> ValidationRequest:
    """Translate an ADD command into a validate_guardrail request.

    AddCommand carries no embedded instrument — it references an existing
    position by id. We resolve the position via *position_lookup* and project
    its persisted instrument details into the :class:`ValidationInstrument`
    so the library projects a real exposure delta. Missing or unsupported
    position records raise :class:`ValueError`; the orchestrator surfaces the
    failure as a structural rejection rather than a guardrail breach.
    """
    position = position_lookup(command.position_id)
    if position is None:
        msg = (
            f"AddCommand references missing position_id={command.position_id!r}; "
            "the submit_envelope wrapper expected the PortfolioManagerView "
            "snapshot to include every position the envelope references."
        )
        raise ValueError(msg)
    instrument = ValidationInstrument(
        **_add_instrument_kwargs(position),
    )
    # ALP-462 — Money → float at the ValidationSize surface.
    size = ValidationSize(
        quantity=int(command.additional_quantity),
        dollar_value=float(command.additional_dollar_value),
    )
    return ValidationRequest(instrument=instrument, size=size, action=ValidationAction.ADD)


def _add_instrument_kwargs(position: PositionRecord) -> dict[str, Any]:
    """Build the kwargs for :class:`ValidationInstrument` from a persisted
    :class:`PositionRecord` (the AddCommand resolution path).

    The position-level direction is read through :func:`position_direction` —
    a concrete :class:`Direction` for equity / single-leg options, ``None`` for
    a strategy. A multi-leg STRATEGY has no position-level direction (ALP-603)
    — its directional sign rides on the per-leg tuples — so ``direction`` is
    ``None`` for a strategy; equity / single-leg options carry the persisted
    :class:`Direction`.
    """
    details = position.details
    ticker = resolve_ticker(details)
    if ticker is None:
        msg = (
            f"AddCommand resolved position {position.position_id!r} with no "
            "underlying ticker (empty strategy legs?)"
        )
        raise ValueError(msg)
    kwargs: dict[str, Any] = {
        "ticker": ticker,
        "direction": position_direction(position),
    }
    if isinstance(details, EquityPositionDetails):
        kwargs["asset_type"] = InstrumentType.EQUITY
        return kwargs
    if isinstance(details, OptionsPositionDetails):
        kwargs["asset_type"] = InstrumentType.OPTIONS
        kwargs["strike"] = details.strike_price
        kwargs["expiration"] = _date_to_utc_datetime(details.expiration_date)
        kwargs["contract_type"] = _OPTION_CONTRACT_TYPE_TO_VALIDATION_STR[details.contract_type]
        return kwargs
    if isinstance(details, StrategyPositionDetails):
        kwargs["direction"] = None
        kwargs["asset_type"] = InstrumentType.STRATEGY
        kwargs["legs"] = _strategy_legs_from_persisted(details)
        return kwargs
    msg = f"AddCommand references position with unsupported details: {type(details).__name__}"
    raise ValueError(msg)


def _strategy_legs_from_persisted(
    details: StrategyPositionDetails,
) -> tuple[ValidationStrategyLeg, ...]:
    """Project persisted strategy legs to :class:`ValidationStrategyLeg` tuples.

    Mirrors :func:`_strategy_legs_for_validation` but reads from the persisted
    record shape rather than the wire-format ``StrategyInstrument``. Raises
    :class:`ValueError` on a persisted leg with no direction recorded or a
    non-positive ``contract_count`` — the validator cannot project an exposure
    delta from either shape, so silent fallback would hide bad persisted data.
    """
    legs: list[ValidationStrategyLeg] = []
    for leg in details.legs:
        if leg.direction is None:
            msg = f"persisted strategy leg {leg.leg_id!r} has no direction set"
            raise ValueError(msg)
        opt = leg.options
        if opt.contract_count <= 0:
            msg = (
                f"persisted strategy leg {leg.leg_id!r} has non-positive "
                f"contract_count={opt.contract_count}"
            )
            raise ValueError(msg)
        legs.append(
            ValidationStrategyLeg(
                direction=leg.direction,
                asset_type=InstrumentType.OPTIONS,
                strike=opt.strike_price,
                expiration=_date_to_utc_datetime(opt.expiration_date),
                contract_type=_OPTION_CONTRACT_TYPE_TO_VALIDATION_STR[opt.contract_type],
                quantity=int(opt.contract_count),
            )
        )
    return tuple(legs)


def _date_to_utc_datetime(d: date) -> datetime:
    """Lift a calendar :class:`date` to a UTC-tz-aware :class:`datetime`."""
    return datetime.combine(d, datetime.min.time(), tzinfo=UTC)


_OMS_TO_VALIDATION_ASSET: Mapping[str, InstrumentType] = {
    "equity": InstrumentType.EQUITY,
    "option": InstrumentType.OPTIONS,
    "strategy": InstrumentType.STRATEGY,
}

_OMS_TO_VALIDATION_DIRECTION: Mapping[str, Direction] = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
}

_OPTION_CONTRACT_TYPE_TO_VALIDATION_STR: Mapping[OptionContractType, Literal["call", "put"]] = {
    OptionContractType.CALL: "call",
    OptionContractType.PUT: "put",
}


def _build_acknowledgment(
    *,
    command: OpenCommand | AddCommand | AdjustCommand,
    command_id: str,
    result: ValidationResult,
) -> Acknowledgment:
    """Build an Acknowledgment for an accepted OPEN / ADD / ADJUST command.

    For OPEN the acknowledgment carries the same ``POS-{ticker}-{suffix}`` and
    ``ORD-{ticker}-entry-{suffix}`` identifiers Phase 2's
    :func:`alphamind.execution.write_paths.phase2.open._new_open_ids`
    mints from the matching ``command_id`` — so the LLM sees the identifiers
    that will land on the persisted rows. For ADD the ``position_id`` is
    already supplied by the command; the ``order_id`` mirrors the
    ``ORD-ADD-{position_id}-{suffix}`` shape Phase 2's add path uses for the
    primary entry order (see :func:`alphamind.execution.write_paths.phase2.add._add_order_id`).
    Broker routing (story 03e / ALP-390), when wired, swaps the ``order_id``
    for the broker's real ``alpaca_order_id`` via
    :func:`alphamind.decision.portfolio_manager.submit_envelope.dispatch._with_real_order_id`.

    CLOSE/CANCEL build their own Acknowledgments at the caller — they
    short-circuit the guardrail re-run.
    """
    if isinstance(command, AdjustCommand):
        return Acknowledgment(
            position_id=command.position_id,
            order_id=OrderId(f"ORD-ADJUST-{command.position_id}"),
        )
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
    suffix = synthesize_id_suffix(command_id)
    if isinstance(command, OpenCommand):
        ticker = _instrument_ticker_key(command.instrument)
        return Acknowledgment(
            position_id=PositionId(f"POS-{ticker}-{suffix}"),
            order_id=OrderId(f"ORD-{ticker}-entry-{suffix}"),
            validation_metadata=metadata,
        )
    # AddCommand
    return Acknowledgment(
        position_id=command.position_id,
        order_id=OrderId(f"ORD-ADD-{command.position_id}-{suffix}"),
        validation_metadata=metadata,
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
    "_OPTION_CONTRACT_TYPE_TO_VALIDATION_STR",
    "PositionLookup",
    "_add_instrument_kwargs",
    "_build_acknowledgment",
    "_build_constructive_request_from_add",
    "_build_constructive_request_from_open",
    "_build_envelope_level_rejection",
    "_build_rejection_payload",
    "_command_to_validation_request",
    "_date_to_utc_datetime",
    "_format_first_error",
    "_instrument_ticker_key",
    "_open_instrument_kwargs",
    "_process_commands",
    "_process_one_command",
    "_safe_derive_pm_command_id",
    "_serialize_response",
    "_strategy_legs_for_validation",
    "_strategy_legs_from_persisted",
    "_unwrap_envelope_args",
    "_validate_envelope_payload",
]
