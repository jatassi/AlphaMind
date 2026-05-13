"""Gap analysis — thin orchestration shim (story ALP-467).

The pure compute lives in :mod:`.gap_compute`; the IO shell (DB reads via
the repository, DB writes through ``Session``) lives in :mod:`.gap_loaders`.
This module re-exports the public surface unchanged so existing call sites
continue to work — and wraps the new repository-based
``resolve_gap_fill_probability`` with a ``Session``-accepting shim so
``q1/assemble.py`` (and existing tests) keep their signature.

After the ALP-467 pilot lands, the natural next step is to push the
``Session`` parameter out of every q1 entry point and have the orchestrator
construct one :class:`SqlDistillationRepository` at the composition root.
This shim keeps the migration incremental.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.calibration import CalibratedValue
from alphamind.distillation.q1.gap_compute import (
    GAP_KIND_FULL,
    GAP_KIND_PARTIAL,
    GAP_TREND_COUNTER,
    GAP_TREND_WITH,
    DetectedGap,
    GapAnalysisResult,
    GapDirection,
    GapFillEventHistory,
    TrendDirection,
    analyze_gap,
    compute_gap_fill_probability,
    detect_session_gap,
)
from alphamind.distillation.q1.gap_loaders import (
    record_pending_gap_event,
)
from alphamind.distillation.q1.gap_loaders import (
    resolve_gap_fill_probability as _resolve_gap_fill_probability,
)


def resolve_gap_fill_probability(
    session: Session,
    *,
    ticker: str,
    sector: str,
    as_of: str,
    min_events: int,
) -> CalibratedValue:
    """Session-accepting shim that constructs a repository and delegates.

    Existing call sites pass a ``Session`` directly; the shim wraps it in a
    :class:`SqlDistillationRepository` so the underlying pure compute sees
    only the repository surface.
    """
    repository = SqlDistillationRepository(session)
    return _resolve_gap_fill_probability(
        repository,
        ticker=ticker,
        sector=sector,
        as_of=as_of,
        min_events=min_events,
    )


__all__ = [
    "GAP_KIND_FULL",
    "GAP_KIND_PARTIAL",
    "GAP_TREND_COUNTER",
    "GAP_TREND_WITH",
    "DetectedGap",
    "GapAnalysisResult",
    "GapDirection",
    "GapFillEventHistory",
    "TrendDirection",
    "analyze_gap",
    "compute_gap_fill_probability",
    "detect_session_gap",
    "record_pending_gap_event",
    "resolve_gap_fill_probability",
]
