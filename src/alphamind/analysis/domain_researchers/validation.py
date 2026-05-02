"""Structural validator for domain-researcher output — story 05 (ALP-196).

Implements Layer-2 + Layer-3 producer-side checks from
`docs/design/testing/llm-output-validation.md` for domain researcher agents.
Runs on a :class:`~alphamind.analysis.domain_researchers.models.SectorBrief`
already parsed by story 04 and reports structural/referential failures.

The validator returns the *full* error list so the diagnostic record receives
the complete inventory, even though the harness (story 07) surfaces only the
first error in the corrective-retry message per the fail-fast, fail-once policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import BaseModel

from alphamind.analysis.domain_researchers.models import SECTOR_PREFIX, SectorBrief

__all__ = [
    "ValidationError",
    "ValidationResult",
    "validate_brief",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ValidationError(BaseModel, frozen=True):
    """A single structural violation found in a :class:`SectorBrief`.

    Mirrors the error-record shape produced by the JSON Schema validator so
    the harness (story 07) can build corrective-retry messages from a uniform
    type regardless of whether the underlying validator is formal-schema or
    hand-written.
    """

    field_path: str
    rule: str
    message: str


class ValidationResult(BaseModel, frozen=True):
    """Aggregate outcome of running :func:`validate_brief`."""

    is_valid: bool
    errors: tuple[ValidationError, ...]


# ---------------------------------------------------------------------------
# Private checks
# ---------------------------------------------------------------------------


def _check_invocation_id_not_empty(brief: SectorBrief) -> Iterable[ValidationError]:
    if not brief.invocation_id:
        yield ValidationError(
            field_path="invocation_id",
            rule="invocation_id_not_empty",
            message="invocation_id must be a non-empty string",
        )


def _check_reference_prefix_consistency(brief: SectorBrief) -> Iterable[ValidationError]:
    expected_prefix = SECTOR_PREFIX[brief.sector]

    for i, finding in enumerate(brief.findings):
        parts = finding.finding_id.rsplit("-", 1)
        actual_prefix = parts[0] if len(parts) == 2 else finding.finding_id
        if actual_prefix != expected_prefix:
            yield ValidationError(
                field_path=f"findings[{i}].finding_id",
                rule="reference_prefix_consistency",
                message=(
                    f"finding_id {finding.finding_id!r} has prefix {actual_prefix!r};"
                    f" expected {expected_prefix!r} for sector {brief.sector!r}"
                ),
            )

    for i, anomaly in enumerate(brief.anomalies):
        parts = anomaly.anomaly_id.split("-ANOM-", 1)
        actual_prefix = parts[0] if len(parts) == 2 else anomaly.anomaly_id
        if actual_prefix != expected_prefix:
            yield ValidationError(
                field_path=f"anomalies[{i}].anomaly_id",
                rule="reference_prefix_consistency",
                message=(
                    f"anomaly_id {anomaly.anomaly_id!r} has prefix {actual_prefix!r};"
                    f" expected {expected_prefix!r} for sector {brief.sector!r}"
                ),
            )

    for i, tc in enumerate(brief.thesis_candidates):
        parts = tc.thesis_candidate_id.split("-TC-", 1)
        actual_prefix = parts[0] if len(parts) == 2 else tc.thesis_candidate_id
        if actual_prefix != expected_prefix:
            yield ValidationError(
                field_path=f"thesis_candidates[{i}].thesis_candidate_id",
                rule="reference_prefix_consistency",
                message=(
                    f"thesis_candidate_id {tc.thesis_candidate_id!r} has prefix {actual_prefix!r};"
                    f" expected {expected_prefix!r} for sector {brief.sector!r}"
                ),
            )


def _check_sequential_indexing(
    ids: list[str],
    field_path_template: str,
    rule: str,
    extract_index: Callable[[str], int | None],
) -> Iterable[ValidationError]:
    """Generic sequential-index checker.

    Verifies that *ids* form the contiguous sequence 1, 2, …, N with no gaps
    and no duplicates.
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


def _last_int(ref_id: str) -> int | None:
    """Extract the trailing integer from a reference ID (the section index N)."""
    try:
        return int(ref_id.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None


def _check_findings_sequential_indexing(brief: SectorBrief) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[f.finding_id for f in brief.findings],
        field_path_template="findings[*].finding_id",
        rule="findings_sequential_indexing",
        extract_index=_last_int,
    )


def _check_anomalies_sequential_indexing(brief: SectorBrief) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[a.anomaly_id for a in brief.anomalies],
        field_path_template="anomalies[*].anomaly_id",
        rule="anomalies_sequential_indexing",
        extract_index=_last_int,
    )


def _check_thesis_candidates_sequential_indexing(brief: SectorBrief) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[tc.thesis_candidate_id for tc in brief.thesis_candidates],
        field_path_template="thesis_candidates[*].thesis_candidate_id",
        rule="thesis_candidates_sequential_indexing",
        extract_index=_last_int,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_CHECKS = [
    _check_invocation_id_not_empty,
    _check_reference_prefix_consistency,
    _check_findings_sequential_indexing,
    _check_anomalies_sequential_indexing,
    _check_thesis_candidates_sequential_indexing,
]


def validate_brief(brief: SectorBrief) -> ValidationResult:
    """Run all structural checks against *brief* and return the aggregate result.

    Per the fail-fast, fail-once policy in ``llm-output-validation.md``, the
    harness only surfaces the *first* error for the corrective-retry message.
    This function returns the full list so the diagnostic record has the
    complete inventory.
    """
    errors: list[ValidationError] = []
    for check in _CHECKS:
        errors.extend(check(brief))
    return ValidationResult(is_valid=not errors, errors=tuple(errors))
