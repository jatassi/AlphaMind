"""Layer-2/3 cross-field-invariant validator for StrategistOutput — ALP-306.

Implements producer-side checks from
``docs/design/04-decision-layer/strategist-output-schema.md`` § Notes on
cross-field invariants for the strategist agent's output:

* **Layer 2** — structural invariants Pydantic cannot see across siblings
  (``action_parameters.action`` matches ``recommended_action``,
  ``assessment_id`` and ``pending_order_assessment_id`` uniqueness,
  ``linked_position_assessment_id`` referential integrity, ``remedy_flag`` ↔
  ``addressed_breaches[].breach_id`` pairing, addressed-vs-uncured breach
  disjointness, sector ∈ ``active_sectors``, defensive-posture summary
  presence).

* **Layer 3** — referential integrity: every canonical ``[XX-N]`` reference
  embedded in narrative fields must resolve to an entry in the
  per-invocation
  :class:`~alphamind.analysis.synthesizer.retrieval.RetrievalStore`.

Per parent issue ALP-116 § Pre-resolved decisions (C), heuristic anti-pattern
checks (e.g., ``sunk_cost_persistence``, ``rationalized_continuation``) are
out of scope — those are PM-side judgment calls. The harness (story 06)
treats ``not result.is_valid`` as a corrective-retry trigger; warnings never
gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from alphamind.analysis.synthesizer.models import (
    find_bare_prefix_citations,
    parse_reference_id,
)
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)
from alphamind.decision.strategist.models import (
    PendingOrderAssessment,
    PortfolioLevelObservations,
    PositionAssessment,
    StrategistOutput,
)

__all__ = [
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "validate_strategist_output",
]


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Bracketed-reference extractor: matches ``[<prefix-segments>-<digits>]``
# anywhere in narrative prose. ``parse_reference_id`` narrows further to the
# canonical synthesizer prefix taxonomy.
_REF_ID_RE = re.compile(r"\[([A-Z][A-Z0-9-]*-[0-9]+)\]")


# ---------------------------------------------------------------------------
# Layer-2 per-assessment checks
# ---------------------------------------------------------------------------


def _check_ids_unique(
    items: tuple[PositionAssessment, ...] | tuple[PendingOrderAssessment, ...],
    *,
    id_attr: str,
    collection_name: str,
    rule: str,
) -> Iterable[ValidationError]:
    """Yield a failure for each duplicate ID within *items*.

    The two id-uniqueness invariants (assessment_id, pending_order_assessment_id)
    differ only in attribute name and rendering — sharing the loop keeps the
    error messages and field paths consistent.
    """
    seen: set[str] = set()
    for i, item in enumerate(items):
        identifier: str = getattr(item, id_attr)
        if identifier in seen:
            yield ValidationError(
                field_path=f"{collection_name}[{i}].{id_attr}",
                rule=rule,
                message=(
                    f"{id_attr} {identifier!r} is duplicated within "
                    f"{collection_name}; IDs must be unique"
                ),
            )
        seen.add(identifier)


def _check_linked_position_assessment_id(
    pending_orders: tuple[PendingOrderAssessment, ...],
    *,
    assessment_ids: frozenset[str],
) -> Iterable[ValidationError]:
    """Pending-order ``linked_position_assessment_id`` resolves in the same document.

    Each populated ``linked_position_assessment_id`` must equal an
    ``assessment_id`` in ``position_assessments[]``.
    """
    for i, order in enumerate(pending_orders):
        link = order.linked_position_assessment_id
        if link is None:
            continue
        if link not in assessment_ids:
            yield ValidationError(
                field_path=f"pending_order_assessments[{i}].linked_position_assessment_id",
                rule="linked_position_assessment_id_resolves",
                message=(
                    f"linked_position_assessment_id {link!r} on "
                    f"{order.pending_order_assessment_id!r} does not match any "
                    "position_assessments[].assessment_id in this document"
                ),
            )


def _check_remedy_flag_pairing(
    assessments: tuple[PositionAssessment, ...],
    *,
    portfolio_observations: PortfolioLevelObservations,
) -> Iterable[ValidationError]:
    """``remedy_flag`` ↔ ``addressed_breaches[].breach_id`` are mutually-covering.

    Each unique ``breach_id`` in
    ``regime_transition_summary.addressed_breaches[].breach_id`` should be
    referenced by at least one ``position_assessment.remedy_flag``; conversely,
    every populated ``remedy_flag`` value should appear in
    ``addressed_breaches[].breach_id``. Mismatches in either direction are
    failures.
    """
    summary = portfolio_observations.regime_transition_summary
    addressed_ids: set[str] = (
        {b.breach_id for b in summary.addressed_breaches} if summary is not None else set()
    )
    remedy_flags: set[str] = set()
    for i, assessment in enumerate(assessments):
        remedy = assessment.remedy_flag
        if remedy is None:
            continue
        remedy_flags.add(remedy)
        if remedy not in addressed_ids:
            yield ValidationError(
                field_path=f"position_assessments[{i}].remedy_flag",
                rule="remedy_flag_pairing",
                message=(
                    f"remedy_flag {remedy!r} on {assessment.assessment_id!r} does not "
                    "appear in regime_transition_summary.addressed_breaches[].breach_id"
                ),
            )
    for breach_id in addressed_ids - remedy_flags:
        yield ValidationError(
            field_path=(
                "portfolio_level_observations.regime_transition_summary.addressed_breaches"
            ),
            rule="remedy_flag_pairing",
            message=(
                f"addressed_breach.breach_id {breach_id!r} is not referenced by any "
                "position_assessment.remedy_flag"
            ),
        )


def _check_defensive_posture_summary_present(
    output: StrategistOutput,
) -> Iterable[ValidationError]:
    """When ``mode == "defensive_posture"``, the summary block must exist.

    The schema's :class:`StrategistOutput` model_validator enforces this on
    parse; this is a defensive double-check that survives a forged output
    constructed via ``model_construct`` or any other parse-bypass path.
    """
    if (
        output.mode == "defensive_posture"
        and output.portfolio_level_observations.defensive_posture_summary is None
    ):
        yield ValidationError(
            field_path=("portfolio_level_observations.defensive_posture_summary"),
            rule="defensive_posture_summary_present",
            message=(
                "mode=defensive_posture requires "
                "portfolio_level_observations.defensive_posture_summary"
            ),
        )


def _check_addressed_uncured_disjoint(
    portfolio_observations: PortfolioLevelObservations,
) -> Iterable[ValidationError]:
    """A breach is either addressed or uncured, never both."""
    summary = portfolio_observations.regime_transition_summary
    if summary is None:
        return
    addressed_ids = {b.breach_id for b in summary.addressed_breaches}
    for j, uncured in enumerate(summary.uncured_breaches):
        if uncured.breach_id in addressed_ids:
            yield ValidationError(
                field_path=(
                    f"portfolio_level_observations.regime_transition_summary."
                    f"uncured_breaches[{j}].breach_id"
                ),
                rule="addressed_uncured_disjoint",
                message=(
                    f"breach_id {uncured.breach_id!r} appears in both addressed_breaches "
                    "and uncured_breaches; it must be one or the other"
                ),
            )


def _check_sector_active(
    assessment: PositionAssessment, *, field_prefix: str, active_sectors: frozenset[str]
) -> Iterable[ValidationError]:
    """``sector`` is a member of ``active_sectors``."""
    if assessment.sector not in active_sectors:
        yield ValidationError(
            field_path=f"{field_prefix}.sector",
            rule="sector_not_active",
            message=(
                f"sector {assessment.sector!r} is not in active_sectors "
                f"{sorted(active_sectors)!r} for {assessment.assessment_id}"
            ),
        )


def _check_action_parameters_match(
    assessment: PositionAssessment, *, field_prefix: str
) -> Iterable[ValidationError]:
    """``action_parameters.action`` must equal ``recommended_action``.

    The discriminator on the action-parameters union catches this on parse,
    but a defensive double-check is cheap — and the harness may surface
    field-level diagnostics from the validator that the parse error doesn't
    expose.
    """
    if assessment.recommended_action == "hold":
        return
    if assessment.action_parameters is None:
        return
    if assessment.action_parameters.action != assessment.recommended_action:
        yield ValidationError(
            field_path=f"{field_prefix}.action_parameters.action",
            rule="action_parameters_match",
            message=(
                f"action_parameters.action={assessment.action_parameters.action!r} "
                f"does not match recommended_action="
                f"{assessment.recommended_action!r} for {assessment.assessment_id}"
            ),
        )


# ---------------------------------------------------------------------------
# Layer-3 referential integrity
# ---------------------------------------------------------------------------


def _check_narrative_references(
    narrative: str | None,
    *,
    field_path: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Resolve every canonical-prefix ``[XX-N]`` token in ``narrative``.

    ``None`` narratives are skipped. Non-canonical prefixes (e.g. internal
    breach IDs like ``[BREACH-1]``) live outside the synthesizer's reference
    taxonomy; ``parse_reference_id`` returns ``None`` for them and the
    validator skips rather than treating them as unknown references.

    Also surfaces bare-prefix citations (ALP-521): bracketed tokens whose
    body matches a known ``ReferencePrefix`` value but carries no ``-N``
    index. The retrieval store is keyed by ``<prefix>-<index>`` so a bare
    prefix can never resolve; surfacing it as a ``bare_prefix_citation``
    error lets the corrective-retry path run.
    """
    if narrative is None:
        return
    for match in _REF_ID_RE.finditer(narrative):
        ref_id = match.group(1)
        if parse_reference_id(ref_id) is None:
            continue
        if retrieval_store.lookup(ref_id) is None:
            yield ValidationError(
                field_path=field_path,
                rule="unknown_reference",
                message=(
                    f"reference {ref_id!r} cited in {field_path} does not resolve "
                    "in the synthesizer's per-invocation retrieval store"
                ),
            )
    for prefix in find_bare_prefix_citations(narrative):
        yield ValidationError(
            field_path=field_path,
            rule="bare_prefix_citation",
            message=(
                f"bare-prefix citation [{prefix}] in {field_path} carries no index; "
                "the retrieval store is keyed by <prefix>-<index> and cannot resolve "
                f"bare prefixes. Cite a specific brief section like [{prefix}-1]."
            ),
        )


# Position-assessment narrative fields whose ``[XX-N]`` citations Layer-3
# resolves. Names mirror :class:`PositionAssessment` attributes; mandatory and
# optional fields share the same loop because ``_check_narrative_references``
# treats ``None`` narratives as no-ops.
_POSITION_NARRATIVE_FIELDS: tuple[str, ...] = (
    "status_rationale",
    "action_rationale",
    "reduce_rationale",
    "add_conviction_justification",
    "adjustment_rationale",
    "remedy_rationale",
    "cross_position_observations",
)

_PENDING_ORDER_NARRATIVE_FIELDS: tuple[str, ...] = (
    "drift_rationale",
    "action_rationale",
)

_PORTFOLIO_NARRATIVE_FIELDS: tuple[str, ...] = (
    "aggregate_thesis_health",
    "sector_balance_shifts",
    "thesis_dependency_warnings",
    "capital_allocation_observations",
)


def _check_position_assessment_references(
    assessment: PositionAssessment,
    *,
    field_prefix: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Layer-3 resolution for every narrative field on a position assessment."""
    for name in _POSITION_NARRATIVE_FIELDS:
        yield from _check_narrative_references(
            getattr(assessment, name),
            field_path=f"{field_prefix}.{name}",
            retrieval_store=retrieval_store,
        )


def _check_pending_order_references(
    order: PendingOrderAssessment,
    *,
    field_prefix: str,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Layer-3 resolution for every narrative field on a pending-order assessment."""
    for name in _PENDING_ORDER_NARRATIVE_FIELDS:
        yield from _check_narrative_references(
            getattr(order, name),
            field_path=f"{field_prefix}.{name}",
            retrieval_store=retrieval_store,
        )


def _check_portfolio_observation_references(
    observations: PortfolioLevelObservations,
    *,
    retrieval_store: RetrievalStore,
) -> Iterable[ValidationError]:
    """Layer-3 resolution for every narrative field at the portfolio level."""
    for name in _PORTFOLIO_NARRATIVE_FIELDS:
        yield from _check_narrative_references(
            getattr(observations, name),
            field_path=f"portfolio_level_observations.{name}",
            retrieval_store=retrieval_store,
        )
    if observations.defensive_posture_summary is not None:
        yield from _check_narrative_references(
            observations.defensive_posture_summary.capital_preservation_notes,
            field_path=(
                "portfolio_level_observations.defensive_posture_summary.capital_preservation_notes"
            ),
            retrieval_store=retrieval_store,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_strategist_output(
    output: StrategistOutput,
    *,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on *output*.

    Parameters
    ----------
    output:
        The parsed strategist output to validate.
    retrieval_store:
        The per-invocation :class:`RetrievalStore` assembled by the
        synthesizer. Layer 3 resolves every ``[XX-N]`` reference embedded in
        narrative fields against this store.
    active_sectors:
        The active portfolio profile's ``active_sectors`` set (subset of
        ``{"tech", "semis", "financials", "energy"}``). Position assessments
        whose ``sector`` is outside this set are a failure.

    Returns
    -------
    ValidationResult
        ``is_valid=True`` iff no errors. Warnings never disqualify.
    """
    failures: list[ValidationError] = []

    failures.extend(
        _check_ids_unique(
            output.position_assessments,
            id_attr="assessment_id",
            collection_name="position_assessments",
            rule="assessment_id_unique",
        )
    )
    failures.extend(
        _check_ids_unique(
            output.pending_order_assessments,
            id_attr="pending_order_assessment_id",
            collection_name="pending_order_assessments",
            rule="pending_order_assessment_id_unique",
        )
    )
    assessment_ids = frozenset(a.assessment_id for a in output.position_assessments)
    failures.extend(
        _check_linked_position_assessment_id(
            output.pending_order_assessments, assessment_ids=assessment_ids
        )
    )
    failures.extend(
        _check_remedy_flag_pairing(
            output.position_assessments,
            portfolio_observations=output.portfolio_level_observations,
        )
    )
    failures.extend(_check_addressed_uncured_disjoint(output.portfolio_level_observations))
    failures.extend(_check_defensive_posture_summary_present(output))
    for i, assessment in enumerate(output.position_assessments):
        field_prefix = f"position_assessments[{i}]"
        failures.extend(_check_action_parameters_match(assessment, field_prefix=field_prefix))
        failures.extend(
            _check_sector_active(
                assessment, field_prefix=field_prefix, active_sectors=active_sectors
            )
        )
        failures.extend(
            _check_position_assessment_references(
                assessment, field_prefix=field_prefix, retrieval_store=retrieval_store
            )
        )
    for j, order in enumerate(output.pending_order_assessments):
        failures.extend(
            _check_pending_order_references(
                order,
                field_prefix=f"pending_order_assessments[{j}]",
                retrieval_store=retrieval_store,
            )
        )
    failures.extend(
        _check_portfolio_observation_references(
            output.portfolio_level_observations, retrieval_store=retrieval_store
        )
    )

    return ValidationResult(
        errors=tuple(failures),
        warnings=(),
    )
