"""IO shell for q1 gap analysis (ALP-467).

This module owns every DB read and write the gap pipeline needs. The pure
classification + probability resolution lives in :mod:`.gap_compute`. The
thin orchestrator at :mod:`.gap` composes the two.

Two responsibilities live here that the pure compute can't carry:

1. ``resolve_gap_fill_probability`` — wraps
   :func:`alphamind.distillation.q1.gap_compute.compute_gap_fill_probability`
   with the repository read needed to populate
   :class:`alphamind.distillation.q1.gap_compute.GapFillEventHistory`.
2. ``record_pending_gap_event`` — writes a fresh row to
   ``distillation_event_history`` (the only DB write in the gap pipeline).

The write helper is intentionally a thin wrapper over the ORM rather than
a repository method — adding a write method to the repository would
expand the pilot's surface beyond the read-only seam the audit calls out.
The write path is exercised by story 07's refresh entry point, not by the
per-category indicator compute path the parallelization unlocks.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation._repository import DistillationRepository
from alphamind.distillation.baselines import PENDING_OUTCOME
from alphamind.distillation.calibration import CalibratedValue
from alphamind.distillation.q1.gap_compute import (
    DetectedGap,
    GapFillEventHistory,
    compute_gap_fill_probability,
)
from alphamind.persistence.models import DistillationEventHistory


def resolve_gap_fill_probability(
    repository: DistillationRepository,
    *,
    ticker: str,
    sector: str,
    as_of: str,
    min_events: int,
) -> CalibratedValue:
    """Load the gap-fill event counts and resolve the probability.

    The repository reads come first (per-ticker counts always; sector pool
    only when the per-ticker resolved count is below ``min_events``); the
    pure compute then folds the counts into the three-state calibration
    framework.
    """
    ticker_counts = repository.load_gap_fill_event_counts(ticker=ticker, as_of=as_of)
    if ticker_counts.resolved >= min_events:
        # Calibrated branch — skip the sector pool read.
        history = GapFillEventHistory(
            ticker_resolved=ticker_counts.resolved,
            ticker_filled=ticker_counts.filled,
            ticker_pending=ticker_counts.pending,
            sector_resolved=0,
            sector_filled=0,
        )
    else:
        sector_counts = repository.load_sector_pooled_gap_fill_counts(sector=sector, as_of=as_of)
        history = GapFillEventHistory(
            ticker_resolved=ticker_counts.resolved,
            ticker_filled=ticker_counts.filled,
            ticker_pending=ticker_counts.pending,
            sector_resolved=sector_counts.resolved,
            sector_filled=sector_counts.filled,
        )
    return compute_gap_fill_probability(history=history, min_events=min_events)


def record_pending_gap_event(
    session: Session,
    detected: DetectedGap,
    *,
    ingested_at: str,
) -> None:
    """Append a pending ``distillation_event_history`` row, idempotent on rerun.

    Unchanged behavior from the pre-split implementation; the write is kept
    on a raw ``Session`` rather than the repository Protocol because the
    Protocol surface is read-only by design (pilot scope).
    """
    existing = session.execute(
        select(DistillationEventHistory).where(
            DistillationEventHistory.ticker == detected.ticker,
            DistillationEventHistory.event_kind == "gap",
            DistillationEventHistory.event_ts == detected.event_ts,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        DistillationEventHistory(
            ticker=detected.ticker,
            event_kind="gap",
            event_ts=detected.event_ts,
            direction=detected.direction,
            magnitude_atr_multiple=detected.magnitude_atr_multiple,
            outcome=PENDING_OUTCOME,
            outcome_observed_at=None,
            ingested_at=ingested_at,
        )
    )


__all__ = [
    "record_pending_gap_event",
    "resolve_gap_fill_probability",
]
