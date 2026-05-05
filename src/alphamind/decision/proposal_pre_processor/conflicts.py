"""Same-underlying conflict detection — ALP-316.

Pure function over typed analyst recommendations and strategist records.
Produces three lookup maps (analyst-side, strategist-position-side,
strategist-pending-side) keyed by record id, each value a tuple of conflict
entries. Mirror-symmetry — every analyst-side entry has a strategist-side
counterpart on the cross-referenced record — is the central correctness
property.

See ``docs/design/04-decision-layer/proposal-pre-processor.md``
§ Wrap pattern and conflicts annotation for the conflict_type enum table.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from alphamind.decision.analyst.models import (
    InstrumentEquity,
    InstrumentOption,
    Recommendation,
)
from alphamind.decision.proposal_pre_processor.models import (
    AnalystSideConflict,
    ConflictType,
    StrategistSideConflict,
)
from alphamind.decision.strategist.models import (
    PendingOrderAssessment,
    PositionAssessment,
)


@dataclass(frozen=True, slots=True)
class ConflictDetectionResult:
    """Mirror-symmetric conflict matrix produced by :func:`detect_conflicts`."""

    analyst_conflicts_by_rec_id: Mapping[str, tuple[AnalystSideConflict, ...]]
    strategist_position_conflicts_by_assessment_id: Mapping[str, tuple[StrategistSideConflict, ...]]
    strategist_pending_conflicts_by_assessment_id: Mapping[str, tuple[StrategistSideConflict, ...]]


def detect_conflicts(
    *,
    recommendations: Sequence[Recommendation],
    position_assessments: Sequence[PositionAssessment],
    pending_order_assessments: Sequence[PendingOrderAssessment],
    held_direction_resolver: Callable[[str], Literal["long", "short"]],
) -> ConflictDetectionResult:
    """Build the same-underlying conflict matrix.

    See module docstring for semantics. ``held_direction_resolver`` is consulted
    only when classifying analyst entries against strategist ``hold`` position
    assessments — the caller seeds it from the snapshot's existing-positions map.
    """
    analyst_buckets: dict[str, list[AnalystSideConflict]] = {
        r.recommendation_id: [] for r in recommendations
    }
    position_buckets: dict[str, list[StrategistSideConflict]] = {
        pa.assessment_id: [] for pa in position_assessments
    }
    pending_buckets: dict[str, list[StrategistSideConflict]] = {
        po.pending_order_assessment_id: [] for po in pending_order_assessments
    }
    underlying_by_assessment_id = {pa.assessment_id: pa.underlying for pa in position_assessments}
    # Pre-compute per-pending-order classification and resolved underlying so
    # the inner rec-loop is O(N*M) appends rather than O(N*M) re-resolutions.
    eligible_pending: list[tuple[PendingOrderAssessment, ConflictType, str]] = []
    for pending in pending_order_assessments:
        classification = _classify_pending(pending)
        if classification is None:
            continue
        pending_underlying = _resolve_pending_underlying(pending, underlying_by_assessment_id)
        if pending_underlying is None:
            continue
        eligible_pending.append((pending, classification, pending_underlying))

    for rec in recommendations:
        for assessment in position_assessments:
            if assessment.underlying != rec.underlying:
                continue
            classification = _classify_position(rec, assessment, held_direction_resolver)
            if classification is None:
                continue
            analyst_buckets[rec.recommendation_id].append(
                AnalystSideConflict(
                    with_assessment_id=assessment.assessment_id,
                    underlying=rec.underlying,
                    conflict_type=classification,
                )
            )
            position_buckets[assessment.assessment_id].append(
                StrategistSideConflict(
                    with_recommendation_id=rec.recommendation_id,
                    underlying=rec.underlying,
                    conflict_type=classification,
                )
            )

        for pending, pending_classification, pending_underlying in eligible_pending:
            if pending_underlying != rec.underlying:
                continue
            analyst_buckets[rec.recommendation_id].append(
                AnalystSideConflict(
                    with_pending_order_assessment_id=pending.pending_order_assessment_id,
                    underlying=rec.underlying,
                    conflict_type=pending_classification,
                )
            )
            pending_buckets[pending.pending_order_assessment_id].append(
                StrategistSideConflict(
                    with_recommendation_id=rec.recommendation_id,
                    underlying=rec.underlying,
                    conflict_type=pending_classification,
                )
            )

    return ConflictDetectionResult(
        analyst_conflicts_by_rec_id={k: tuple(v) for k, v in analyst_buckets.items()},
        strategist_position_conflicts_by_assessment_id={
            k: tuple(v) for k, v in position_buckets.items()
        },
        strategist_pending_conflicts_by_assessment_id={
            k: tuple(v) for k, v in pending_buckets.items()
        },
    )


def _classify_position(
    rec: Recommendation,
    assessment: PositionAssessment,
    held_direction_resolver: Callable[[str], Literal["long", "short"]],
) -> ConflictType | None:
    """Classify a position-assessment interaction with an analyst entry.

    Returns ``None`` when the action produces no conflict (``adjust-bracket``)
    or when the analyst's instrument lacks a single-direction reading needed
    for hold-vs-direction classification (multi-leg strategies).
    """
    if assessment.recommended_action in ("close", "reduce"):
        return ConflictType.entry_vs_close
    if assessment.recommended_action == "add":
        return ConflictType.entry_vs_add
    if assessment.recommended_action == "hold":
        rec_direction = _direction_of(rec)
        if rec_direction is None:
            return None
        held_direction = held_direction_resolver(assessment.position_id)
        if rec_direction == held_direction:
            return ConflictType.entry_vs_hold
        return ConflictType.entry_direction_conflict
    return None


_PENDING_ENTRY_ORDER_TYPES = frozenset({"entry_limit", "entry_stop_limit"})

_PENDING_CLASSIFICATION: dict[str, ConflictType] = {
    "maintain": ConflictType.entry_vs_pending_maintain,
    "modify": ConflictType.entry_vs_pending_modify,
    "cancel": ConflictType.entry_vs_pending_cancel,
}


def _classify_pending(pending: PendingOrderAssessment) -> ConflictType | None:
    """Classify a pending-order interaction with an analyst entry.

    Bracket legs (``order_type ∈ {bracket_*}``) carry no conflicts — their
    conflicts are captured by the parent position's assessment.
    """
    if pending.order_type not in _PENDING_ENTRY_ORDER_TYPES:
        return None
    return _PENDING_CLASSIFICATION.get(pending.recommended_action)


def _resolve_pending_underlying(
    pending: PendingOrderAssessment,
    underlying_by_assessment_id: dict[str, str],
) -> str | None:
    """Resolve a pending entry order's underlying via its linked assessment.

    Returns ``None`` if the pending order has no ``linked_position_assessment_id``
    or the linked id is not present in the supplied position-assessment list.
    """
    if pending.linked_position_assessment_id is None:
        return None
    return underlying_by_assessment_id.get(pending.linked_position_assessment_id)


def _direction_of(rec: Recommendation) -> Literal["long", "short"] | None:
    """Resolve the recommendation's directional bet to long/short, or None.

    Multi-leg strategies don't carry a single direction; the design doc's
    hold-vs-direction enum entries don't cover them, so they're skipped.
    """
    if isinstance(rec.instrument, InstrumentEquity | InstrumentOption):
        return rec.instrument.direction
    return None
