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

import copy
import dataclasses
import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from alphamind._kernel.ids import EnvelopeId, OrderId, PositionId, ThesisId
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
from alphamind.commands.pm_envelope import (
    PMAnalystEnvelope,
    PMEnvelope,
)
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
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.execution.oms.command_ids import (
    compute_attempt_seq,
    derive_open_thesis_id,
    derive_pm_base_command_id,
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
    ValidationToolState,
    validate_guardrail,
)

PositionLookup = Callable[[str], PositionRecord | None]
"""Resolves a ``position_id`` to its persisted record (or ``None`` if missing)."""

OrderThesisLookup = Callable[[str], ThesisId | None]
"""Resolves a CANCEL target ``order_id`` to its originating thesis FK.

A CANCEL carries only an ``order_id``; its broker-carried link (ALP-844) reads
the originating thesis off the targeted pending order's record (``None`` if the
order is absent from the PM view or carries no thesis)."""

_ENVELOPE_ADAPTER: TypeAdapter[PMEnvelope] = TypeAdapter(PMEnvelope)


# ---------------------------------------------------------------------------
# Originating-proposal lookup (ALP-557)
# ---------------------------------------------------------------------------


class OriginatingProposalLookupError(LookupError):
    """The originating proposal for a PM envelope is absent from the bundle.

    Raised by :func:`lookup_originating_proposal_json` when an envelope's
    ``source_recommendation_id`` resolves to no analyst Recommendation /
    strategist assessment in the pre-processor bundle. This is a genuine
    invariant violation, not a graceful-degradation case: Layer-3 validation
    (``validation._check_source_recommendation_id_resolves``) already rejects
    any envelope whose ``source_recommendation_id`` cannot resolve, so by the
    time the submission log is built the proposal is guaranteed in scope. A
    miss here therefore signals a real wiring bug rather than an absent body —
    the contract is "if PM produces an envelope, the originating proposal is in
    scope at the persistence call site" (ALP-557 / parent ALP-129).
    """


def lookup_originating_proposal_json(
    bundle: ProposalPreProcessorBundle, envelope: PMEnvelope
) -> dict[str, Any]:
    """Resolve the originating proposal body for *envelope* from *bundle*.

    Keyed by ``envelope.source_recommendation_id`` against the bundle section
    matching the envelope's provenance, mirroring the resolution Layer-3
    validation performs:

    * ``pm_analyst`` (``REC-N``) → ``analyst_section.recommendations[].recommendation``
      keyed by ``recommendation_id``.
    * ``pm_strategist`` / ``position_assessment`` (``SA-N``) →
      ``strategist_section.position_assessments[].assessment`` keyed by
      ``assessment_id``.
    * ``pm_strategist`` / ``pending_order_assessment`` (``SA-ORD-N``) →
      ``strategist_section.pending_order_assessments[].pending_order_assessment``
      keyed by ``pending_order_assessment_id``.

    Returns the resolved model's ``model_dump(mode="json")``. Raises
    :class:`OriginatingProposalLookupError` (never returns an empty dict) when
    the id resolves to nothing.
    """
    sid = envelope.source_recommendation_id
    if isinstance(envelope, PMAnalystEnvelope):
        recommendations = bundle.analyst_section.recommendations or ()
        for wrapped in recommendations:
            if wrapped.recommendation.recommendation_id == sid:
                return wrapped.recommendation.model_dump(mode="json")
    elif envelope.recommendation_type == "position_assessment":
        for wrapped_pa in bundle.strategist_section.position_assessments:
            if wrapped_pa.assessment.assessment_id == sid:
                return wrapped_pa.assessment.model_dump(mode="json")
    else:  # pending_order_assessment
        for wrapped_poa in bundle.strategist_section.pending_order_assessments:
            if wrapped_poa.pending_order_assessment.pending_order_assessment_id == sid:
                return wrapped_poa.pending_order_assessment.model_dump(mode="json")
    msg = (
        f"originating proposal for envelope {envelope.envelope_id!r} "
        f"(source_recommendation_id={sid!r}) is absent from the pre-processor bundle"
    )
    raise OriginatingProposalLookupError(msg)


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


# Analyst-only leaf keys on a flat command sub-record that the ``extra="forbid"``
# OMS command sub-model has no field for. Audited against the FULL analyst
# ``Recommendation`` → ``OpenCommand`` delta (``analyst/models.py`` vs
# ``command_models.py``), not just the fields seen in production, so a verbatim
# copy of any analyst sub-block ``pm.md`` tells the PM to copy is accepted:
#
# * ``position_size`` — ``delta_adjusted_exposure`` (guardrail-populated context)
#   and ``pct_of_portfolio`` (analyst sizing rationale); the command
#   ``PositionSize`` is only ``quantity`` / ``dollar_value`` / ``premium_at_risk``.
# * ``target`` — ``dollar_pl_target`` (a required analyst ``Target`` field with no
#   command ``Target`` slot). The command additionally *requires* ``order_type``,
#   which the analyst lacks; that one the PM must author (a strip can't supply a
#   missing field), so it is correctly out of scope here.
#
# ``entry_order`` and ``instrument`` carry no analyst-only fields — their analyst
# and command shapes match field-for-field — so they need no entry here.
_ANALYST_ONLY_KEYS_BY_SUBRECORD: Mapping[str, tuple[str, ...]] = {
    "position_size": ("delta_adjusted_exposure", "pct_of_portfolio"),
    "target": ("dollar_pl_target",),
}


def _strip_analyst_only_command_fields(
    args: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Layer-0.5 tolerant strip of analyst-only fields off PM commands (ALP-736).

    The analyst ``Recommendation`` schema is a superset of the OMS command
    schema, and ``pm.md`` tells the PM to copy instrument / sizing / legs /
    thesis straight from the analyst output when authoring an OPEN command.
    Analyst-only leaf fields ride along into the ``extra="forbid"`` command
    sub-models, where Pydantic rejects the whole command — silently losing a
    PM-*approved* recommendation when the ``submit_envelope`` self-repair retry
    loop stalls on a field it doesn't know to drop (the MRVL loss on
    2026-05-28). This mirrors the ALP-700 unwrap: a narrow, well-understood
    coercion applied before the discriminated-union parse so a single tool call
    succeeds instead of burning retries (or losing the trade entirely).

    Stripped (analyst-only, forbidden on the command):

    * Flat sub-record keys per :data:`_ANALYST_ONLY_KEYS_BY_SUBRECORD` —
      ``position_size.{delta_adjusted_exposure, pct_of_portfolio}`` and
      ``target.dollar_pl_target``.
    * ``commands[].invalidation_legs[].leg_id`` — the analyst's
      ``^INV-[0-9]+$`` cross-reference id; the command legs are positional.
    * ``commands[].invalidation_legs[].order_parameters`` **only on
      ``type=event`` legs** — a soft event leg has no order block on the
      command ``EventLeg``. The analyst dumps ``order_parameters: null`` on
      event legs (the field defaults to ``None`` but still serializes), so a
      verbatim copy carries the forbidden key; price/time legs legitimately
      keep their identical ``order_parameters``.

    NOT stripped: ``entry_window``. ALP-737 added ``EntryWindow`` to
    ``OpenCommand`` field-for-field, so the PM copies the block through verbatim
    and it must reach the bracket's ``entry_window_deadline``. Stripping it
    would re-drop the analyst's patient-entry window — the exact regression
    ALP-737 fixed. (Strip-vs-thread decision resolved as *thread*, jointly with
    ALP-735 / ALP-737.)

    **Why an enumerated strip list rather than a schema-driven
    ``set(payload) - set(model.model_fields)`` diff:** the OMS command is a
    nested discriminated union (instrument, per-leg price/time/event variants),
    so a general diff would have to recursively resolve each discriminator and
    risks stripping a key that is valid on a *sibling* variant. The enumerated
    list is audited to be schema-complete for the analyst→command delta today,
    and the residual-failure WARNING in :func:`_handle_submit_envelope` is the
    backstop: if the analyst schema later grows a field this list misses, the
    command fails *loudly* to ``collector.log`` rather than silently — so the
    cost of drift is a logged parse failure, not a re-introduced silent drop.

    Returns ``(normalized_args, stripped_paths)``. When at least one field is
    stripped, *normalized_args* is a deep copy (the caller's
    ``raw_args_for_log`` keeps the literal pre-strip input for forensics) and
    *stripped_paths* names every removed key
    (e.g. ``commands[0].position_size.delta_adjusted_exposure``) so the caller
    can log what it dropped. When nothing is stripped (a clean envelope, or a
    payload without a list-valued ``commands``) the original *args* object is
    returned unchanged with an empty tuple.
    """
    commands = args.get("commands")
    if not isinstance(commands, list):
        return args, ()

    normalized = copy.deepcopy(args)
    stripped: list[str] = []
    for index, command in enumerate(normalized["commands"]):
        if not isinstance(command, dict):
            continue
        for subrecord, keys in _ANALYST_ONLY_KEYS_BY_SUBRECORD.items():
            _strip_subrecord_keys(command, subrecord, keys, index, stripped)
        _strip_invalidation_leg_keys(command, index, stripped)
    if not stripped:
        return args, ()
    return normalized, tuple(stripped)


def _strip_subrecord_keys(
    command: dict[str, Any],
    subrecord: str,
    keys: tuple[str, ...],
    index: int,
    stripped: list[str],
) -> None:
    """Pop analyst-only *keys* from a flat command sub-record (``position_size``
    / ``target``) in place, recording each removed path."""
    sub = command.get(subrecord)
    if not isinstance(sub, dict):
        return
    for key in keys:
        if key in sub:
            del sub[key]
            stripped.append(f"commands[{index}].{subrecord}.{key}")


def _strip_invalidation_leg_keys(command: dict[str, Any], index: int, stripped: list[str]) -> None:
    """Pop ``leg_id`` from every leg and ``order_parameters`` from event legs."""
    legs = command.get("invalidation_legs")
    if not isinstance(legs, list):
        return
    for leg_index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        if "leg_id" in leg:
            del leg["leg_id"]
            stripped.append(f"commands[{index}].invalidation_legs[{leg_index}].leg_id")
        if leg.get("type") == "event" and "order_parameters" in leg:
            del leg["order_parameters"]
            stripped.append(f"commands[{index}].invalidation_legs[{leg_index}].order_parameters")


def _format_first_error(exc: ValidationError) -> str:
    """Return the first Pydantic error rendered as ``<field-path>: <message>``."""
    errs = exc.errors()
    if not errs:
        return "envelope failed schema validation"
    err = errs[0]
    path = ".".join(str(p) for p in err["loc"]) or "<root>"
    return f"{path}: {err['msg']}"


_FORENSIC_REJECTION_ID_PREFIX = "ENV-REC-INVALID"
"""Head token of the forensic id minted for a rejected / malformed envelope.

A rejected or malformed envelope is NOT a real order and never reaches the
broker, so it must never route through :func:`derive_pm_command_id` (the
order-derivation contract, which now requires a real originating thesis —
ALP-844). The forensic id gets its own synthetic scheme that is deliberately
NOT a parseable PM/engine command id, so :func:`is_pm_originated` /
:func:`is_engine_originated` return ``False`` and the failure-log surface
stays distinguishable from real orders.
"""


def _safe_derive_pm_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
) -> str:
    """Mint a *forensic* id for a rejected / malformed envelope's failure log.

    A rejected or malformed envelope is not a real order — it never reaches the
    broker and has no originating thesis — so this deliberately does NOT route
    through :func:`derive_pm_command_id`. It returns a clearly-separate
    synthetic id (``ENV-REC-INVALID.{invocation}.{envelope}.{ordinal}.{seq}``)
    that is *not* a parseable order command id, keeping the order-derivation
    contract thesis-required and unbreached while leaving the failure-log
    surface queryable (ALP-844).
    """
    prefix = invocation_id if invocation_id.startswith("inv-") else f"inv-{invocation_id}"
    return f"{_FORENSIC_REJECTION_ID_PREFIX}.{prefix}.{envelope_id}.{command_ordinal}.{attempt_seq}"


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
    order_thesis_lookup: OrderThesisLookup,
) -> tuple[tuple[SubmissionResult, ...], SubmitEnvelopeState, tuple[ProjectedDelta | None, ...]]:
    """Process every command in *envelope*, in order.

    Re-runs :func:`validate_guardrail` per command against the cumulative
    state. A guardrail PASS advances ``validation_state`` (so the next command
    in the envelope sees this one's projected impact) and records the
    :class:`ProjectedDelta` it credited; FAIL leaves the state unchanged and
    records ``None``.

    Returns ``(results, state, credited_deltas)`` — the per-command results and
    the per-command credited deltas are both in command_ordinal order. The
    caller reconciles ``state.validation_state`` against the *final*
    (post-broker-routing) submission results via
    :func:`_reconcile_validation_state` so a command that PASSed guardrails but
    was then rejected by the broker releases its credit (ALP-743) rather than
    leaving phantom exposure that over-rejects a retry of the same idea.
    """
    results: list[SubmissionResult] = []
    credited_deltas: list[ProjectedDelta | None] = []
    attempt_seq = compute_attempt_seq(envelope)
    current_state = state
    for ordinal, command in enumerate(envelope.commands):
        result, current_state, credited_delta = _process_one_command(
            command=command,
            command_ordinal=ordinal,
            envelope=envelope,
            attempt_seq=attempt_seq,
            state=current_state,
            sector_resolver=sector_resolver,
            position_lookup=position_lookup,
            order_thesis_lookup=order_thesis_lookup,
        )
        results.append(result)
        credited_deltas.append(credited_delta)
    return tuple(results), current_state, tuple(credited_deltas)


def _reconcile_validation_state(
    state: SubmitEnvelopeState,
    *,
    entry_validation_state: ValidationToolState,
    submission_results: tuple[SubmissionResult, ...],
    credited_deltas: tuple[ProjectedDelta | None, ...],
) -> SubmitEnvelopeState:
    """Rebuild ``validation_state`` to credit only finally-accepted commands.

    ``_process_commands`` advances ``validation_state`` for every command that
    PASSes guardrails so later commands in the same envelope see its impact.
    But broker routing (story 03e) can flip a guardrail-accepted command to
    ``rejected`` *after* that advance — a permanent rejection or an exhausted
    gateway-retry window. Before ALP-743 the credited delta stayed, so the next
    envelope (typically a resize/retry of the very same idea) projected against
    phantom stacked exposure and over-rejected.

    Reconciliation rebuilds the cumulative state from *entry_validation_state*
    (the cell as it stood when the envelope arrived) plus only the deltas whose
    command is still ``accepted`` in the final, post-broker-routing
    *submission_results*. Commands that never credited a delta (CLOSE / CANCEL /
    ADJUST / guardrail-FAIL) carry ``None`` and are skipped regardless of
    status. When no command flipped — the fixture-only path with no broker
    routing — this reproduces the threaded end-state exactly, so it is safe to
    apply unconditionally.

    Survivors are re-indexed to a contiguous ``proposal_index`` run as they are
    re-applied. Each delta's ``proposal_index`` was frozen during Step 3 from
    the cumulative length at validation time; dropping a non-last command would
    otherwise leave a gap, and the next command credited in the invocation
    (``proposal_index = len(accumulated_deltas) + 1``) could then collide with a
    surviving delta's frozen index — two accumulated deltas sharing an index
    surface as duplicate ``prior_{index}`` proposal ids and a guardrail-library
    ``LibraryInputError`` on the next projection. Re-indexing restores the
    monotonic-contiguous invariant the append-only Step-3 path maintained.
    """
    reconciled = entry_validation_state
    next_index = len(entry_validation_state.accumulated_deltas) + 1
    for result, delta in zip(submission_results, credited_deltas, strict=True):
        if delta is not None and result.status == "accepted":
            reconciled = reconciled.with_accepted_proposal(
                delta.model_copy(update={"proposal_index": next_index})
            )
            next_index += 1
    return dataclasses.replace(state, validation_state=reconciled)


def _resolve_originating_thesis(
    command: OMSCommand,
    *,
    base_command_id: str,
    position_lookup: PositionLookup,
    order_thesis_lookup: OrderThesisLookup,
) -> ThesisId:
    """Resolve the broker-carried originating thesis FK for *command* (ALP-844).

    Every AlphaMind-derived command id carries the durable thesis FK so the
    fill it rides on is self-attributing (ADR 0002). There is no thesis-less
    path:

    * **OPEN** mints a *new* thesis via :func:`derive_open_thesis_id` — the
      single source of truth for the OPEN thesis identity (ALP-844, A2). The id
      is deterministic from the base command id, so the FK embedded here is the
      one authoritative copy: Phase-2 OPEN writeback reads it back by *parsing*
      the command id rather than re-deriving it.
    * **CLOSE / ADD / ADJUST** act on an existing position; the FK is that
      position's persisted ``thesis_id``. A real such command reaching
      submission against a missing position, or one whose ``thesis_id is None``,
      is a structural error — raise, never sentinel.
    * **CANCEL** withdraws a pending order; the FK is the order's
      ``originating_thesis_id`` (resolved by *order_thesis_lookup*).

    Raises :class:`ValueError` when the originating thesis cannot be resolved —
    the order-derivation contract must never emit a thesis-less id.
    """
    if isinstance(command, OpenCommand):
        ticker = _instrument_ticker_key(command.instrument)
        return derive_open_thesis_id(ticker, base_command_id)
    if isinstance(command, CancelCommand):
        thesis = order_thesis_lookup(command.order_id)
        if thesis is None:
            msg = (
                f"CANCEL references order_id={command.order_id!r} with no resolvable "
                "originating thesis; the broker-carried link (ALP-844) requires one."
            )
            raise ValueError(msg)
        return thesis
    # CLOSE / ADD / ADJUST — resolve the existing position's persisted thesis.
    position = position_lookup(command.position_id)
    if position is None:
        msg = (
            f"{command.command_type.upper()} references position_id={command.position_id!r} "
            "absent from the PM view; cannot resolve its originating thesis for the "
            "broker-carried link (ALP-844)."
        )
        raise ValueError(msg)
    if position.thesis_id is None:
        msg = (
            f"{command.command_type.upper()} on position_id={command.position_id!r} has no "
            "originating thesis_id; an AlphaMind-managed position always carries a thesis "
            "(ADR 0002) — refusing to derive a thesis-less command id."
        )
        raise ValueError(msg)
    return position.thesis_id


def _process_one_command(
    *,
    command: OMSCommand,
    command_ordinal: int,
    envelope: PMEnvelope,
    attempt_seq: int,
    state: SubmitEnvelopeState,
    sector_resolver: Callable[[str], str],
    position_lookup: PositionLookup,
    order_thesis_lookup: OrderThesisLookup,
) -> tuple[SubmissionResult, SubmitEnvelopeState, ProjectedDelta | None]:
    """Process one embedded command — translate, validate, format result.

    Returns ``(submission_result, new_state, credited_delta)``. A guardrail-PASS
    OPEN/ADD returns a state with an advanced ``validation_state`` and the
    :class:`ProjectedDelta` it credited; all other outcomes return the input
    state unchanged and a ``None`` credited delta. The caller threads the
    credited delta through :func:`_reconcile_validation_state` so a command
    that PASSes guardrails here but is later rejected by the broker releases
    its credit (ALP-743).
    """
    # The broker-carried thesis FK (ALP-844) rides on the same id-suffix as the
    # base command id, so resolve the OPEN's thesis off the base id (the
    # link-stripped ``inv-…`` form) before the link is appended. The embedded
    # value this mints is the single source of truth for the OPEN thesis
    # identity — Phase 2 reads it back by parsing this command id rather than
    # re-deriving it (A2). ``derive_pm_base_command_id`` is the SOLE definition
    # of the base-id format, shared with ``derive_pm_command_id`` so the two can
    # never diverge.
    base_command_id = derive_pm_base_command_id(
        invocation_id=state.invocation_id,
        envelope_id=envelope.envelope_id,
        command_ordinal=command_ordinal,
        attempt_seq=attempt_seq,
    )
    thesis_id = _resolve_originating_thesis(
        command,
        base_command_id=base_command_id,
        position_lookup=position_lookup,
        order_thesis_lookup=order_thesis_lookup,
    )
    command_id = derive_pm_command_id(
        invocation_id=state.invocation_id,
        envelope_id=envelope.envelope_id,
        command_ordinal=command_ordinal,
        attempt_seq=attempt_seq,
        thesis_id=thesis_id,
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
            None,
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
            None,
        )

    request = _command_to_validation_request(command, position_lookup=position_lookup)
    result = validate_guardrail(request=request, state=state.validation_state)

    if result.overall == "PASS":
        # Advance ``validation_state`` unless the command produces no exposure
        # delta (ADJUST is metadata-only at this layer). The credited delta is
        # returned so the caller can release it if broker routing later rejects
        # this command (ALP-743).
        credited_delta: ProjectedDelta | None = None
        new_state = state
        if isinstance(command, OpenCommand | AddCommand):
            credited_delta = ProjectedDelta(
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
            new_state = dataclasses.replace(
                state,
                validation_state=state.validation_state.with_accepted_proposal(credited_delta),
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
            credited_delta,
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
        None,
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

    Each breached rule surfaces ``projected_after`` — the cumulative basis the
    ``overage`` was measured against (ALP-743). For a near-cap proposal stacked
    on prior in-invocation passes, ``projected_after`` exceeds this command's
    single-command ``delta_adjusted_exposure``; carrying it lets the PM read the
    overage as cumulative exposure rather than inventing a broker margin
    multiplier to reconcile the two figures.
    """
    failed = tuple(p for p in result.per_rule if p.status is Status.FAIL)
    rules_breached = tuple(
        _BreachedRule(
            rule=p.rule,
            current=p.current,
            limit=p.limit,
            overage=abs(p.projected_after - p.limit),
            unit=p.unit,
            projected_after=p.projected_after,
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
    "OrderThesisLookup",
    "OriginatingProposalLookupError",
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
    "_reconcile_validation_state",
    "_resolve_originating_thesis",
    "_safe_derive_pm_command_id",
    "_serialize_response",
    "_strategy_legs_for_validation",
    "_strategy_legs_from_persisted",
    "_strip_analyst_only_command_fields",
    "_unwrap_envelope_args",
    "_validate_envelope_payload",
    "lookup_originating_proposal_json",
]
