"""Layer-2/3 cross-field-invariant validator for PMEnvelope — ALP-327.

Implements per-envelope checks called by story 06c's ``submit_envelope`` MCP
wrapper at submission time. Mirrors :mod:`alphamind.decision.analyst.validation`
for the public :class:`ValidationResult` / :class:`ValidationError` /
:class:`ValidationWarning` types and helper-function shape, extended with the
PM's specific cross-record checks and embedded-OMS-command checks.

* **Layer 2** — structural invariants Pydantic cannot see across siblings:
  envelope_id ↔ source_recommendation_id integer bijection, verdict-conditional
  invariants (defense-in-depth on top of model validators), evaluation
  criterion-set ↔ source_provenance, modification adjustment_category ↔ phase
  pairing, embedded close command risk_management_subtype, anti_patterns_identified
  membership, halt-mode no-OPEN/ADD restriction, embedded OPEN/ADD command's
  sector ∈ active_sectors.

* **Layer 3** — referential integrity: every ``[XX-N]`` reference embedded in
  ``rationale_narrative`` and ``modifications[].rationale`` resolves in the
  per-invocation :class:`RetrievalStore`; ``source_recommendation_id`` resolves
  in the pre-processor bundle; ``position_id`` (when present) resolves in
  ``pm_view.positions``.

The validator returns the *full* error/warning inventory; the caller (story
06c's MCP wrapper) reads ``is_valid`` and surfaces the first-error message in
the rejection_payload's ``rules_breached[0].rule = "schema_invariant"``.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Iterable

from alphamind.analysis.synthesizer.models import (
    REF_ID_RE,
    find_bare_prefix_citations,
    parse_reference_id,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.command_models import (
    EquityInstrument,
    OptionInstrument,
    StrategyInstrument,
)
from alphamind.commands.pm_envelope import (
    AddCommand,
    AntiPattern,
    CloseCommand,
    OpenCommand,
    PMAnalystEnvelope,
    PMEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

__all__ = [
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "validate_pm_envelope",
]


# ValidationError / ValidationResult / ValidationWarning are re-exported from
# :mod:`alphamind.commands.validation_results` per ALP-458 — they ride from
# this validator into the execution-side Phase 2 write path's rejection
# persistence, so the wire shape lives in the commands kernel where both
# decision and execution can import without re-introducing the cycle.


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Canonical anti-pattern strings — the typed envelope's ``anti_patterns_identified``
# field is constrained to this Literal alias on parse. The validator restates as
# defense-in-depth for ``model_construct`` parse-bypass paths.
_CANONICAL_ANTI_PATTERNS: frozenset[str] = frozenset(typing.get_args(AntiPattern))


# ---------------------------------------------------------------------------
# Layer-2 helpers — envelope_id ↔ source_recommendation_id integer bijection
# ---------------------------------------------------------------------------


def _extract_id_integer(prefixed_id: str) -> str | None:
    """Return the trailing integer portion of a prefixed ID like ``ENV-REC-3``.

    The PM ID vocabulary's prefix ends at the last hyphen — the bijection
    compares the trailing integer portion of the envelope ID against the
    trailing integer portion of the source recommendation ID.
    """
    if "-" not in prefixed_id:
        return None
    tail = prefixed_id.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


def _check_envelope_id_source_provenance(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """(a) ``envelope_id`` integer matches ``source_recommendation_id`` integer.

    Pydantic's regex constraints on each field already enforce shape; this
    additional check enforces the integer bijection per
    ``docs/design/oms-command-ids.md``.
    """
    envelope_int = _extract_id_integer(envelope.envelope_id)
    source_int = _extract_id_integer(envelope.source_recommendation_id)
    if envelope_int is None or source_int is None:
        return
    if envelope_int != source_int:
        yield ValidationError(
            field_path="envelope_id",
            message=(
                f"envelope_id integer {envelope_int!r} does not match "
                f"source_recommendation_id integer {source_int!r} "
                f"(envelope_id={envelope.envelope_id!r}, "
                f"source_recommendation_id={envelope.source_recommendation_id!r})"
            ),
            criterion="envelope_id_source_recommendation_id_bijection",
        )


# ---------------------------------------------------------------------------
# Layer-2 helpers — verdict-conditional invariants (defense-in-depth)
# ---------------------------------------------------------------------------


def _check_override_with_corrective_action_invariants(
    envelope: PMEnvelope,
) -> Iterable[ValidationError]:
    """Layer-2 invariants for the ``override_with_corrective_action`` verdict — ALP-626.

    Mirrors the parse-time invariants on PMStrategistEnvelope /
    PMAnalystEnvelope as defense-in-depth, surfacing finer field-level error
    messages and protecting against ``model_construct`` parse-bypass paths.
    """
    if len(envelope.commands) < 1:
        yield ValidationError(
            field_path="commands",
            message="verdict=override_with_corrective_action requires at least one command",
            criterion="verdict_conditional_invariant",
        )
    if len(envelope.concerns) < 1:
        yield ValidationError(
            field_path="concerns",
            message="verdict=override_with_corrective_action requires at least one concern",
            criterion="verdict_conditional_invariant",
        )
    if len(envelope.modifications) != 0:
        yield ValidationError(
            field_path="modifications",
            message=(
                "verdict=override_with_corrective_action requires empty modifications "
                "(the override authors new commands; use approve_with_modification "
                "for parameter tweaks on an existing command)"
            ),
            criterion="verdict_conditional_invariant",
        )
    for i, command in enumerate(envelope.commands):
        if command.command_type not in ("close", "adjust", "cancel"):
            yield ValidationError(
                field_path=f"commands[{i}].command_type",
                message=(
                    "verdict=override_with_corrective_action embedded commands must be "
                    "CLOSE / ADJUST / CANCEL (the override is a corrective action; "
                    "new-entry OPEN / ADD is forbidden); got "
                    f"command_type={command.command_type!r}"
                ),
                criterion="verdict_conditional_invariant",
            )


def _check_reject_invariants(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """Layer-2 invariants for the ``reject`` verdict."""
    if len(envelope.commands) != 0:
        yield ValidationError(
            field_path="commands",
            message="verdict=reject requires empty commands",
            criterion="verdict_conditional_invariant",
        )
    if len(envelope.modifications) != 0:
        yield ValidationError(
            field_path="modifications",
            message="verdict=reject requires empty modifications",
            criterion="verdict_conditional_invariant",
        )
    if len(envelope.concerns) < 1:
        yield ValidationError(
            field_path="concerns",
            message="verdict=reject requires at least one concern",
            criterion="verdict_conditional_invariant",
        )


def _check_verdict_conditional_invariants(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """(b) Verdict-conditional invariants on commands / modifications / concerns.

    Pydantic's model validator on each envelope variant enforces these on
    parse; this restates them as defense-in-depth, surfacing finer
    field-level error messages and protecting against ``model_construct``
    parse-bypass paths.
    """
    if envelope.verdict == "reject":
        yield from _check_reject_invariants(envelope)
    elif envelope.verdict == "approve":
        if len(envelope.modifications) != 0:
            yield ValidationError(
                field_path="modifications",
                message="verdict=approve requires empty modifications",
                criterion="verdict_conditional_invariant",
            )
    elif envelope.verdict == "approve_with_modification":
        if len(envelope.modifications) < 1:
            yield ValidationError(
                field_path="modifications",
                message="verdict=approve_with_modification requires at least one modification",
                criterion="verdict_conditional_invariant",
            )
        if len(envelope.commands) < 1:
            yield ValidationError(
                field_path="commands",
                message="verdict=approve_with_modification requires at least one command",
                criterion="verdict_conditional_invariant",
            )
    elif envelope.verdict == "override_with_corrective_action":
        yield from _check_override_with_corrective_action_invariants(envelope)


# ---------------------------------------------------------------------------
# Layer-2 helpers — evaluation criterion-set ↔ source_provenance
# ---------------------------------------------------------------------------


def _check_evaluation_criterion_set(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """(c) ``evaluation`` criterion-set matches ``source_provenance``.

    ``pm_analyst`` envelopes carry a 5-criterion :class:`ThesisQualityEvaluation`;
    ``pm_strategist`` envelopes carry a 4-criterion :class:`PositionActionEvaluation`.
    Pydantic enforces this on parse via the typed evaluation field on each
    variant; defense-in-depth restate guards against ``model_construct``
    parse-bypass paths.
    """
    if envelope.source_provenance == "pm_analyst" and not isinstance(
        envelope.evaluation, ThesisQualityEvaluation
    ):
        yield ValidationError(
            field_path="evaluation",
            message=(
                "source_provenance=pm_analyst requires evaluation to be ThesisQualityEvaluation "
                f"(got {type(envelope.evaluation).__name__})"
            ),
            criterion="evaluation_criterion_set",
        )
    elif envelope.source_provenance == "pm_strategist" and not isinstance(
        envelope.evaluation, PositionActionEvaluation
    ):
        yield ValidationError(
            field_path="evaluation",
            message=(
                "source_provenance=pm_strategist requires evaluation to be "
                f"PositionActionEvaluation (got {type(envelope.evaluation).__name__})"
            ),
            criterion="evaluation_criterion_set",
        )


# ---------------------------------------------------------------------------
# Layer-2 helpers — modification adjustment_category ↔ phase ↔ triggering_rule
# ---------------------------------------------------------------------------


def _check_modification_adjustment_category_phase(
    envelope: PMEnvelope,
) -> Iterable[ValidationError]:
    """(d) ``modifications[].adjustment_category`` ↔ ``phase`` ↔ ``triggering_rule``.

    ``adjustment_category == "guardrail_rejection_response"`` requires
    ``phase == "post_rejection"`` and ``triggering_rule`` populated; every
    other category requires ``phase == "pre_submission"``. Pydantic enforces
    this on the :class:`ModificationRecord` model; defense-in-depth restate
    catches ``model_construct`` parse-bypass paths.
    """
    for i, mod in enumerate(envelope.modifications):
        field_prefix = f"modifications[{i}]"
        if mod.adjustment_category == "guardrail_rejection_response":
            if mod.phase != "post_rejection":
                yield ValidationError(
                    field_path=f"{field_prefix}.phase",
                    message=(
                        "adjustment_category=guardrail_rejection_response requires "
                        f"phase=post_rejection (got phase={mod.phase!r})"
                    ),
                    criterion="modification_adjustment_category_phase",
                )
            if mod.triggering_rule is None:
                yield ValidationError(
                    field_path=f"{field_prefix}.triggering_rule",
                    message=(
                        "adjustment_category=guardrail_rejection_response requires "
                        "triggering_rule to be populated"
                    ),
                    criterion="modification_adjustment_category_phase",
                )
        elif mod.phase != "pre_submission":
            yield ValidationError(
                field_path=f"{field_prefix}.phase",
                message=(
                    f"adjustment_category={mod.adjustment_category!r} requires "
                    f"phase=pre_submission (got phase={mod.phase!r})"
                ),
                criterion="modification_adjustment_category_phase",
            )


# ---------------------------------------------------------------------------
# Layer-2 helpers — embedded close command risk_management_subtype
# ---------------------------------------------------------------------------


def _check_close_command_subtype(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """(e) Embedded ``close`` commands' ``risk_management_subtype: pm_directed``.

    For each :class:`CloseCommand` with ``close_rationale_type == "risk_management"``,
    assert ``risk_management_subtype == "pm_directed"``. Pydantic enforces the
    field's required-when-conditional via the model validator on
    :class:`CloseCommand`; this check confirms the *value* is ``pm_directed``
    (not ``engine_guardrail``, which is engine-originated provenance).
    """
    for i, command in enumerate(envelope.commands):
        if not isinstance(command, CloseCommand):
            continue
        if command.close_rationale_type != "risk_management":
            continue
        if command.risk_management_subtype != "pm_directed":
            yield ValidationError(
                field_path=f"commands[{i}].risk_management_subtype",
                message=(
                    "embedded CloseCommand with close_rationale_type=risk_management requires "
                    "risk_management_subtype=pm_directed (got "
                    f"{command.risk_management_subtype!r}; engine_guardrail is engine-originated "
                    "and cannot appear in a PM envelope)"
                ),
                criterion="close_command_risk_management_subtype",
            )


# ---------------------------------------------------------------------------
# Layer-2 helpers — anti_patterns_identified canonical enum
# ---------------------------------------------------------------------------


def _check_anti_patterns_canonical(envelope: PMEnvelope) -> Iterable[ValidationError]:
    """(f) ``anti_patterns_identified[]`` strings ∈ canonical enum.

    Pydantic enforces this on parse via the ``Literal`` alias on the
    typed envelope; defense-in-depth restate guards against
    ``model_construct`` parse-bypass paths.
    """
    if envelope.anti_patterns_identified is None:
        return
    for i, value in enumerate(envelope.anti_patterns_identified):
        if value not in _CANONICAL_ANTI_PATTERNS:
            yield ValidationError(
                field_path=f"anti_patterns_identified[{i}]",
                message=(
                    f"anti_patterns_identified entry {value!r} is not a canonical anti-pattern; "
                    f"allowed values are {sorted(_CANONICAL_ANTI_PATTERNS)!r}"
                ),
                criterion="anti_patterns_canonical_enum",
            )


# ---------------------------------------------------------------------------
# Layer-2 helpers — halt mode forbids embedded OPEN / ADD commands
# ---------------------------------------------------------------------------


def _check_halt_mode_no_constructive(
    envelope: PMEnvelope, *, halt_mode: bool
) -> Iterable[ValidationError]:
    """(g) When ``halt_mode is True``, embedded OPEN or ADD commands are forbidden.

    Halt-mode is parameterized at the validator boundary per parent decision (M);
    the validator does not infer halt-mode from the envelope or pre-processor
    bundle.
    """
    if not halt_mode:
        return
    for i, command in enumerate(envelope.commands):
        if isinstance(command, OpenCommand | AddCommand):
            yield ValidationError(
                field_path=f"commands[{i}]",
                message=(
                    f"halt_mode=True forbids embedded {command.command_type!r} commands; "
                    "only CLOSE / ADJUST / CANCEL are allowed in risk-reduction mode"
                ),
                criterion="halt_mode_no_constructive_commands",
            )


# ---------------------------------------------------------------------------
# Layer-2 helpers — embedded OPEN/ADD command sector ∈ active_sectors
# ---------------------------------------------------------------------------


def _instrument_resolution_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return the ticker/underlying key used to resolve sector for an instrument.

    :class:`EquityInstrument` exposes ``ticker``; :class:`OptionInstrument` and
    :class:`StrategyInstrument` expose ``underlying``. OPEN commands carry
    exactly one of these via the discriminated :data:`Instrument` union.
    """
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


def _check_embedded_command_sector(
    envelope: PMEnvelope,
    *,
    active_sectors: frozenset[str],
    sector_resolver: Callable[[str], str],
) -> Iterable[ValidationError]:
    """(h) Embedded OPEN command sector ∈ ``active_sectors``.

    Sector is derived via ``sector_resolver`` applied to the instrument's
    ticker (equity) or underlying (option / strategy) per parent decision
    (B); the canonical :class:`PositionSize` carries no sector field.
    Canonical :class:`AddCommand` references an existing position by id
    and carries no embedded instrument, so the sector check only applies
    to OPEN.
    """
    for i, command in enumerate(envelope.commands):
        if not isinstance(command, OpenCommand):
            continue
        key = _instrument_resolution_key(command.instrument)
        sector = sector_resolver(key)
        if sector not in active_sectors:
            yield ValidationError(
                field_path=f"commands[{i}].instrument",
                message=(
                    f"embedded {command.command_type!r} command sector {sector!r} (resolved from "
                    f"instrument key {key!r}) is not in active_sectors "
                    f"{sorted(active_sectors)!r}"
                ),
                criterion="embedded_command_sector_active",
            )


# ---------------------------------------------------------------------------
# Layer-3 helpers — narrative [XX-N] reference resolution
# ---------------------------------------------------------------------------


def _check_narrative_references_against_store(
    narrative: str,
    *,
    field_path: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Resolve every canonical-prefix ``[XX-N]`` token in ``narrative``.

    Mirrors :mod:`alphamind.decision.analyst.validation`'s helper. Skips
    non-canonical prefixes (``[BREACH-1]``, ``[INV-1]``) — those don't live
    in the synthesizer's reference taxonomy and don't belong in the
    retrieval store.

    Also surfaces bare-prefix citations (ALP-521): bracketed tokens whose
    body matches a known ``ReferencePrefix`` value but carries no ``-N``
    index. The retrieval store is keyed by ``<prefix>-<index>`` so a bare
    prefix can never resolve; surfacing it as a ``bare_prefix_citation``
    error lets the corrective-retry path run.
    """
    for match in REF_ID_RE.finditer(narrative):
        ref_id = match.group(1)
        if parse_reference_id(ref_id) is None:
            continue
        if retrieval_store.lookup(ref_id) is None:
            yield ValidationError(
                field_path=field_path,
                message=(
                    f"reference {ref_id!r} cited in {field_path} does not resolve in the "
                    "synthesizer's per-invocation retrieval store"
                ),
                criterion="unknown_reference",
            )
    for prefix in find_bare_prefix_citations(narrative):
        yield ValidationError(
            field_path=field_path,
            criterion="bare_prefix_citation",
            message=(
                f"bare-prefix citation [{prefix}] in {field_path} carries no index; "
                "the retrieval store is keyed by <prefix>-<index> and cannot resolve "
                f"bare prefixes. Cite a specific brief section like [{prefix}-1]."
            ),
        )


def _check_narrative_references(
    envelope: PMEnvelope, *, retrieval_store: RetrievalStore
) -> Iterable[ValidationError]:
    """(i) ``[XX-N]`` references in ``rationale_narrative`` and ``modifications[].rationale``."""
    yield from _check_narrative_references_against_store(
        envelope.rationale_narrative,
        field_path="rationale_narrative",
        retrieval_store=retrieval_store,
    )
    for i, mod in enumerate(envelope.modifications):
        yield from _check_narrative_references_against_store(
            mod.rationale,
            field_path=f"modifications[{i}].rationale",
            retrieval_store=retrieval_store,
        )


# ---------------------------------------------------------------------------
# Layer-3 helpers — source_recommendation_id ∈ pre-processor bundle
# ---------------------------------------------------------------------------


def _check_source_recommendation_id_resolves(
    envelope: PMEnvelope, *, pre_processor_bundle: ProposalPreProcessorBundle
) -> Iterable[ValidationError]:
    """(j) ``source_recommendation_id`` resolves in the pre-processor bundle.

    For ``pm_analyst`` envelopes, ``REC-N`` matches a ``recommendation_id`` in
    ``analyst_section.recommendations[].recommendation``. The analyst section
    in watchlist mode carries no recommendations — a ``pm_analyst`` envelope
    against a watchlist-mode bundle is itself a Layer-2 invariant violation.

    For ``pm_strategist`` envelopes, ``SA-N`` resolves against the strategist
    section's position assessments and ``SA-ORD-N`` against the pending-order
    assessments.
    """
    sid = envelope.source_recommendation_id
    if isinstance(envelope, PMAnalystEnvelope):
        recommendations = pre_processor_bundle.analyst_section.recommendations
        if recommendations is None:
            yield ValidationError(
                field_path="source_recommendation_id",
                message=(
                    f"pm_analyst envelope source_recommendation_id={sid!r} cannot resolve in a "
                    "watchlist-mode pre-processor bundle (analyst proposals are absent)"
                ),
                criterion="source_recommendation_id_resolves",
            )
            return
        if not any(w.recommendation.recommendation_id == sid for w in recommendations):
            yield ValidationError(
                field_path="source_recommendation_id",
                message=(
                    f"source_recommendation_id={sid!r} does not match any recommendation_id in "
                    "the pre-processor bundle's analyst section"
                ),
                criterion="source_recommendation_id_resolves",
            )
        return
    # pm_strategist
    if envelope.recommendation_type == "position_assessment":
        candidates = pre_processor_bundle.strategist_section.position_assessments
        if not any(w.assessment.assessment_id == sid for w in candidates):
            yield ValidationError(
                field_path="source_recommendation_id",
                message=(
                    f"source_recommendation_id={sid!r} does not match any assessment_id in "
                    "the pre-processor bundle's strategist position_assessments"
                ),
                criterion="source_recommendation_id_resolves",
            )
        return
    # pending_order_assessment
    pending = pre_processor_bundle.strategist_section.pending_order_assessments
    if not any(w.pending_order_assessment.pending_order_assessment_id == sid for w in pending):
        yield ValidationError(
            field_path="source_recommendation_id",
            message=(
                f"source_recommendation_id={sid!r} does not match any "
                "pending_order_assessment_id in the pre-processor bundle's strategist section"
            ),
            criterion="source_recommendation_id_resolves",
        )


# ---------------------------------------------------------------------------
# Layer-3 helpers — position_id ∈ pm_view.positions
# ---------------------------------------------------------------------------


def _check_position_id_resolves(
    envelope: PMEnvelope, *, pm_view: PortfolioManagerView
) -> Iterable[ValidationError]:
    """(k) ``position_id`` (when present) resolves in ``pm_view.positions``.

    ``pm_analyst`` envelopes carry ``position_id=None`` — the check is a no-op
    in that case. ``pm_strategist`` envelopes always carry a populated
    ``position_id``; both ``position_assessment`` and ``pending_order_assessment``
    variants reference the position the assessment is about (for pending-order
    assessments, that's the position the order would create or modify per
    ``oms-commands.md``).
    """
    if envelope.position_id is None:
        return
    known = {view.position.position_id for view in pm_view.positions}
    if envelope.position_id not in known:
        yield ValidationError(
            field_path="position_id",
            message=(
                f"position_id={envelope.position_id!r} does not match any open position in "
                "pm_view.positions"
            ),
            criterion="position_id_resolves",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_pm_envelope(
    envelope: PMEnvelope,
    *,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
    sector_resolver: Callable[[str], str],
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on *envelope*.

    Parameters
    ----------
    envelope:
        The :class:`PMEnvelope` to validate.
    retrieval_store:
        Per-invocation :class:`RetrievalStore` assembled by the synthesizer.
        Layer 3 resolves every ``[XX-N]`` reference embedded in narrative
        fields against this store.
    pre_processor_bundle:
        The pre-processor bundle the PM consumed. Layer 3 resolves
        ``source_recommendation_id`` against this bundle's analyst /
        strategist sections.
    pm_view:
        The :class:`PortfolioManagerView` projection. Layer 3 resolves
        ``position_id`` (when present) against ``pm_view.positions``.
    active_sectors:
        The active portfolio profile's ``active_sectors`` set (subset of
        ``{"tech", "semis", "financials", "energy"}``). Embedded OPEN
        commands whose resolved sector is outside this set are an error.
    halt_mode:
        Whether the PM invocation is running in halt (risk-reduction) mode.
        When ``True``, embedded OPEN or ADD commands are forbidden.
    sector_resolver:
        ``ticker -> sector`` callable. Per parent decision (B), sector is a
        risk-side concept derived at validation time rather than a
        wire-format field on :class:`~alphamind.execution.oms.command_models.PositionSize`.
        Embedded OPEN commands' instruments are resolved via this callable
        applied to ``ticker`` (equity) or ``underlying`` (option / strategy).

    Returns
    -------
    ValidationResult
        ``is_valid`` is ``True`` iff ``errors`` is empty. Warnings never
        disqualify.
    """
    errors: list[ValidationError] = []
    errors.extend(_check_envelope_id_source_provenance(envelope))
    errors.extend(_check_verdict_conditional_invariants(envelope))
    errors.extend(_check_evaluation_criterion_set(envelope))
    errors.extend(_check_modification_adjustment_category_phase(envelope))
    errors.extend(_check_close_command_subtype(envelope))
    errors.extend(_check_anti_patterns_canonical(envelope))
    errors.extend(_check_halt_mode_no_constructive(envelope, halt_mode=halt_mode))
    errors.extend(
        _check_embedded_command_sector(
            envelope, active_sectors=active_sectors, sector_resolver=sector_resolver
        )
    )
    errors.extend(_check_narrative_references(envelope, retrieval_store=retrieval_store))
    errors.extend(
        _check_source_recommendation_id_resolves(
            envelope, pre_processor_bundle=pre_processor_bundle
        )
    )
    errors.extend(_check_position_id_resolves(envelope, pm_view=pm_view))
    return ValidationResult(
        envelope_id=envelope.envelope_id,
        errors=tuple(errors),
        warnings=(),
    )
