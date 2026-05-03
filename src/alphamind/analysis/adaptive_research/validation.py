"""Structural validator for adaptive-researcher output — ALP-258.

Implements Layer-2 + Layer-3 producer-side checks from
`docs/design/testing/llm-output-validation.md` for the adaptive-researcher
agent. Runs on an :class:`~alphamind.analysis.adaptive_research.models.AdaptiveBrief`
already parsed by story 03b and reports structural/referential failures.

The Layer-3 referential resolution against three upstream brief contexts —
``sector_briefs``, ``qualitative_brief``, ``correlation_regime_brief`` — is the
structural check unique to this agent. Every Strengthens / Weakens reference
in every SIGNAL thread must resolve to an ID present in one of the upstream
briefs, drawn from six prefix families:

* ``SA-{SECTOR}-N``       — ``SectorBrief.findings[*].finding_id``
* ``SA-{SECTOR}-ANOM-N``  — ``SectorBrief.anomalies[*].anomaly_id``
* ``SA-{SECTOR}-TC-N``    — ``SectorBrief.thesis_candidates[*].thesis_candidate_id``
* ``QR-N``                — ``QualitativeBrief.threads[*].thread_id``
* ``QR-CW-N``             — ``QualitativeBrief.catalyst_watches[*].catalyst_id``
* ``CR-N``                — ``CorrelationRegimeBrief.reference_index`` keys

The validator returns the *full* error list so the diagnostic record receives
the complete inventory; the harness (story 05) surfaces only the first error
in the corrective-retry message.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import BaseModel

from alphamind.analysis.adaptive_research.models import AdaptiveBrief, Assessment
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief

__all__ = [
    "ValidationError",
    "ValidationResult",
    "validate_adaptive_brief",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ValidationError(BaseModel, frozen=True):
    """A single structural violation found in an :class:`AdaptiveBrief`.

    Mirrors the error-record shape produced by the JSON Schema validator so the
    harness can build corrective-retry messages from a uniform type regardless
    of whether the underlying validator is formal-schema or hand-written.
    """

    field_path: str
    rule: str
    message: str


class ValidationResult(BaseModel, frozen=True):
    """Aggregate outcome of running :func:`validate_adaptive_brief`."""

    is_valid: bool
    errors: tuple[ValidationError, ...]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _last_int(ref_id: str) -> int | None:
    """Extract the trailing integer from a reference ID (the section index N)."""
    try:
        return int(ref_id.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None


def _check_sequential_indexing(
    ids: list[str],
    field_path_template: str,
    rule: str,
    extract_index: Callable[[str], int | None],
) -> Iterable[ValidationError]:
    """Generic sequential-index checker.

    Verifies that *ids* form the contiguous sequence 1, 2, …, N with no gaps
    and no duplicates. Mirrors the qualitative-research helper of the same
    name; copying is intentional per story scope (no shared utility module).
    """
    if not ids:
        return

    seen: set[int] = set()
    for raw_id in ids:
        idx = extract_index(raw_id)
        if idx is None:
            continue
        if idx in seen:
            yield ValidationError(
                field_path=field_path_template,
                rule=rule,
                message=f"Duplicate index {idx} found in {rule}",
            )
        seen.add(idx)

    if not seen:
        return

    sorted_indexes = sorted(seen)
    expected = list(range(1, len(sorted_indexes) + 1))
    if sorted_indexes != expected:
        yield ValidationError(
            field_path=field_path_template,
            rule=rule,
            message=(
                f"Indexes must be sequential starting at 1 with no gaps; got {sorted_indexes!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Layer-2 checks
# ---------------------------------------------------------------------------


def _check_invocation_id_not_empty(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    if not brief.invocation_id:
        yield ValidationError(
            field_path="invocation_id",
            rule="invocation_id_not_empty",
            message="invocation_id must be a non-empty string",
        )


def _check_threads_sequential_indexing(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[t.thread_id for t in brief.threads],
        field_path_template="threads[*].thread_id",
        rule="threads_sequential_indexing",
        extract_index=_last_int,
    )


def _is_ar_id(value: str) -> bool:
    """True iff *value* matches ``^AR-\\d+$``."""
    suffix = value.removeprefix("AR-")
    return suffix != value and suffix.isdigit()


def _check_thread_id_prefix(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    """Belt-and-suspenders: every thread_id matches ``^AR-\\d+$``.

    Pydantic's ``Field(pattern=...)`` already enforces this at construction;
    this check guards parser-bypass paths.
    """
    for i, thread in enumerate(brief.threads):
        if not _is_ar_id(thread.thread_id):
            yield ValidationError(
                field_path=f"threads[{i}].thread_id",
                rule="thread_id_prefix",
                message=(
                    f"thread_id {thread.thread_id!r} does not match the required 'AR-{{N}}' format"
                ),
            )


# The next two checks duplicate `AdaptiveBrief._brief_invariants` (model-level).
# Kept here for parser-bypass paths (programmatic AdaptiveBrief construction or
# direct mutation) that skip the model validator.
def _check_threads_count_matches_header(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    if brief.threads_investigated_count != len(brief.threads):
        yield ValidationError(
            field_path="threads_investigated_count",
            rule="threads_count_matches_header",
            message=(
                f"threads_investigated_count ({brief.threads_investigated_count}) "
                f"must equal len(threads) ({len(brief.threads)})"
            ),
        )


def _check_anomalies_triaged_at_least_investigated(
    brief: AdaptiveBrief,
) -> Iterable[ValidationError]:
    if brief.anomalies_triaged_count < brief.threads_investigated_count:
        yield ValidationError(
            field_path="anomalies_triaged_count",
            rule="anomalies_triaged_at_least_investigated",
            message=(
                f"anomalies_triaged_count ({brief.anomalies_triaged_count}) "
                f"must be >= threads_investigated_count ({brief.threads_investigated_count})"
            ),
        )


def _check_thread_field_distinct(
    brief: AdaptiveBrief, field_name: str, rule: str
) -> Iterable[ValidationError]:
    """For each thread, fire ``rule`` on duplicate entries in the named tuple field."""
    for i, thread in enumerate(brief.threads):
        seen: set[str] = set()
        for j, entry in enumerate(getattr(thread, field_name)):
            if entry in seen:
                yield ValidationError(
                    field_path=f"threads[{i}].{field_name}[{j}]",
                    rule=rule,
                    message=(
                        f"threads[{i}].{field_name}[{j}] is byte-identical to an earlier"
                        f" {field_name} entry in the same thread"
                    ),
                )
            seen.add(entry)


def _check_findings_distinct_within_thread(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    yield from _check_thread_field_distinct(brief, "findings", "findings_distinct_within_thread")


def _check_tools_used_distinct_within_thread(brief: AdaptiveBrief) -> Iterable[ValidationError]:
    yield from _check_thread_field_distinct(
        brief, "tools_used", "tools_used_distinct_within_thread"
    )


def _check_tickers_in_universe(
    brief: AdaptiveBrief, universe: frozenset[str]
) -> Iterable[ValidationError]:
    for i, thread in enumerate(brief.threads):
        for j, ticker in enumerate(thread.tickers):
            if ticker not in universe:
                yield ValidationError(
                    field_path=f"threads[{i}].tickers[{j}]",
                    rule="tickers_in_universe",
                    message=f"ticker {ticker!r} is not in the asset universe",
                )


# ---------------------------------------------------------------------------
# Layer-3 reference resolution
# ---------------------------------------------------------------------------


def _build_reference_universe(
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> frozenset[str]:
    """Walk every upstream brief once and return the set of valid reference IDs.

    Includes:

    * ``SA-{SECTOR}-N`` from each ``SectorBrief.findings[*].finding_id``
    * ``SA-{SECTOR}-ANOM-N`` from each ``SectorBrief.anomalies[*].anomaly_id``
    * ``SA-{SECTOR}-TC-N`` from each ``SectorBrief.thesis_candidates[*].thesis_candidate_id``
    * ``QR-N`` from each ``QualitativeBrief.threads[*].thread_id``
    * ``QR-CW-N`` from each ``QualitativeBrief.catalyst_watches[*].catalyst_id``
    * ``CR-N`` from ``CorrelationRegimeBrief.reference_index`` keys
    """
    valid: set[str] = set()
    for sector_brief in sector_briefs:
        valid.update(f.finding_id for f in sector_brief.findings)
        valid.update(a.anomaly_id for a in sector_brief.anomalies)
        valid.update(tc.thesis_candidate_id for tc in sector_brief.thesis_candidates)
    valid.update(t.thread_id for t in qualitative_brief.threads)
    valid.update(cw.catalyst_id for cw in qualitative_brief.catalyst_watches)
    valid.update(correlation_regime_brief.reference_index.keys())
    return frozenset(valid)


def _check_strengthens_weakens_resolve(
    brief: AdaptiveBrief,
    valid_ids: frozenset[str],
) -> Iterable[ValidationError]:
    """For each SIGNAL thread, every Strengthens/Weakens ref must be in ``valid_ids``.

    NOISE and INCONCLUSIVE threads have ``None`` for these fields and are
    skipped. An empty tuple ``()`` (the explicit ``none`` wire literal on a
    SIGNAL thread) iterates zero times and produces no errors.
    """
    for i, thread in enumerate(brief.threads):
        if thread.assessment is not Assessment.SIGNAL:
            continue
        # SIGNAL invariant: strengthens/weakens are non-None tuples (model-enforced).
        for field_name in ("strengthens", "weakens"):
            for j, ref in enumerate(getattr(thread, field_name) or ()):
                if ref not in valid_ids:
                    yield ValidationError(
                        field_path=f"threads[{i}].{field_name}[{j}]",
                        rule="referential_integrity",
                        message=(
                            f"reference {ref!r} does not resolve to any upstream brief ID "
                            "in this invocation"
                        ),
                    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# `anomalies_deferred` carries free-text descriptions of triage skips per the
# adaptive-research output contract (e.g. "Distillation: gold-yield correlation
# flip"); no Layer-3 referential check applies — they are not citations.
_LAYER_2_CHECKS: list[Callable[[AdaptiveBrief], Iterable[ValidationError]]] = [
    _check_invocation_id_not_empty,
    _check_threads_sequential_indexing,
    _check_thread_id_prefix,
    _check_threads_count_matches_header,
    _check_anomalies_triaged_at_least_investigated,
    _check_findings_distinct_within_thread,
    _check_tools_used_distinct_within_thread,
]


def validate_adaptive_brief(
    brief: AdaptiveBrief,
    *,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universe: frozenset[str] | None = None,
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on *brief* and aggregate the error list.

    Layer-3 referential resolution: every Strengthens/Weakens reference in every
    SIGNAL thread must resolve to an ID present in one of the three upstream briefs.

    Parameters
    ----------
    brief:
        The parsed :class:`AdaptiveBrief` to validate (already parsed by story 03b).
    sector_briefs:
        The three domain-researcher :class:`SectorBrief` instances from the same
        invocation. Sources for ``SA-{SECTOR}-N`` (findings),
        ``SA-{SECTOR}-ANOM-N`` (anomalies), ``SA-{SECTOR}-TC-N`` (thesis candidates).
    qualitative_brief:
        The baseline :class:`QualitativeBrief` from the same invocation. Sources
        for ``QR-N`` (narrative threads) and ``QR-CW-N`` (catalyst watches).
    correlation_regime_brief:
        The distillation-layer :class:`CorrelationRegimeBrief`. Sources for
        ``CR-N`` (the keys of ``reference_index``).
    universe:
        Optional pre-loaded set of asset-universe tickers. When supplied, every
        ``thread.tickers`` member is verified to be in the universe. Pass
        ``None`` (the default) to skip the membership check.

    Returns
    -------
    ValidationResult
        ``is_valid=True`` and ``errors=()`` when all checks pass; otherwise
        ``is_valid=False`` with the full error inventory.
    """
    errors: list[ValidationError] = []
    for check in _LAYER_2_CHECKS:
        errors.extend(check(brief))
    if universe is not None:
        errors.extend(_check_tickers_in_universe(brief, universe))
    valid_ids = _build_reference_universe(
        sector_briefs, qualitative_brief, correlation_regime_brief
    )
    errors.extend(_check_strengthens_weakens_resolve(brief, valid_ids))
    return ValidationResult(is_valid=not errors, errors=tuple(errors))
