"""Structural validator for qualitative-researcher output — ALP-244.

Implements Layer-2 + Layer-3 producer-side checks from
`docs/design/testing/llm-output-validation.md` for the qualitative-researcher
agent. Runs on a :class:`~alphamind.analysis.qualitative_research.models.QualitativeBrief`
already parsed by story 03a and reports structural/referential failures.

The validator returns the *full* error list so the diagnostic record receives
the complete inventory, even though the harness (story 04b) surfaces only the
first error in the corrective-retry message.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import BaseModel

from alphamind.analysis.qualitative_research.models import QualitativeBrief

__all__ = [
    "ValidationError",
    "ValidationResult",
    "validate_qualitative_brief",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ValidationError(BaseModel, frozen=True):
    """A single structural violation found in a :class:`QualitativeBrief`.

    Mirrors the error-record shape produced by the JSON Schema validator so
    the harness can build corrective-retry messages from a uniform type.
    """

    field_path: str
    rule: str
    message: str


class ValidationResult(BaseModel, frozen=True):
    """Aggregate outcome of running :func:`validate_qualitative_brief`."""

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


# ---------------------------------------------------------------------------
# Private checks
# ---------------------------------------------------------------------------


def _check_invocation_id_not_empty(brief: QualitativeBrief) -> Iterable[ValidationError]:
    if not brief.invocation_id:
        yield ValidationError(
            field_path="invocation_id",
            rule="invocation_id_not_empty",
            message="invocation_id must be a non-empty string",
        )


def _check_threads_sequential_indexing(brief: QualitativeBrief) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[t.thread_id for t in brief.threads],
        field_path_template="threads[*].thread_id",
        rule="threads_sequential_indexing",
        extract_index=_last_int,
    )


def _check_catalyst_watches_sequential_indexing(
    brief: QualitativeBrief,
) -> Iterable[ValidationError]:
    yield from _check_sequential_indexing(
        ids=[cw.catalyst_id for cw in brief.catalyst_watches],
        field_path_template="catalyst_watches[*].catalyst_id",
        rule="catalyst_watches_sequential_indexing",
        extract_index=_last_int,
    )


def _check_reference_prefix_consistency(brief: QualitativeBrief) -> Iterable[ValidationError]:
    for i, thread in enumerate(brief.threads):
        if "-CW-" in thread.thread_id:
            yield ValidationError(
                field_path=f"threads[{i}].thread_id",
                rule="reference_prefix_consistency",
                message=(
                    f"thread_id {thread.thread_id!r} has the catalyst-watch infix '-CW-';"
                    " thread IDs must follow the format 'QR-{{N}}'"
                ),
            )

    for i, cw in enumerate(brief.catalyst_watches):
        if not cw.catalyst_id.startswith("QR-CW-"):
            yield ValidationError(
                field_path=f"catalyst_watches[{i}].catalyst_id",
                rule="reference_prefix_consistency",
                message=(
                    f"catalyst_id {cw.catalyst_id!r} does not have the required 'QR-CW-' prefix"
                ),
            )


def _check_ticker_in_universe(
    brief: QualitativeBrief, universe: frozenset[str]
) -> Iterable[ValidationError]:
    for i, cw in enumerate(brief.catalyst_watches):
        if cw.ticker not in universe:
            yield ValidationError(
                field_path=f"catalyst_watches[{i}].ticker",
                rule="ticker_in_universe",
                message=f"ticker {cw.ticker!r} is not in the asset universe",
            )


def _check_evidence_lines_distinct_within_thread(
    brief: QualitativeBrief,
) -> Iterable[ValidationError]:
    for i, thread in enumerate(brief.threads):
        seen_lines: set[object] = set()
        for j, line in enumerate(thread.evidence):
            if line in seen_lines:
                yield ValidationError(
                    field_path=f"threads[{i}].evidence[{j}].citation",
                    rule="evidence_lines_distinct_within_thread",
                    message=(
                        f"threads[{i}].evidence[{j}] is byte-identical to an earlier"
                        " evidence line in the same thread"
                    ),
                )
            seen_lines.add(line)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_CHECKS: list[Callable[[QualitativeBrief], Iterable[ValidationError]]] = [
    _check_invocation_id_not_empty,
    _check_threads_sequential_indexing,
    _check_catalyst_watches_sequential_indexing,
    _check_reference_prefix_consistency,
    _check_evidence_lines_distinct_within_thread,
]


def validate_qualitative_brief(
    brief: QualitativeBrief, *, universe: frozenset[str] | None = None
) -> ValidationResult:
    """Run all structural checks against *brief* and return the aggregate result.

    Parameters
    ----------
    brief:
        The :class:`QualitativeBrief` to validate (already parsed by story 03a).
    universe:
        Optional pre-loaded set of asset-universe tickers.  When supplied,
        every ``CatalystWatch.ticker`` is verified to be a member.  Pass
        ``None`` (the default) to skip the membership check.

    Returns
    -------
    ValidationResult
        ``is_valid=True`` and ``errors=()`` when all checks pass; otherwise
        ``is_valid=False`` with the full error inventory.
    """
    errors: list[ValidationError] = []
    for check in _CHECKS:
        errors.extend(check(brief))
    if universe is not None:
        errors.extend(_check_ticker_in_universe(brief, universe))
    return ValidationResult(is_valid=not errors, errors=tuple(errors))
