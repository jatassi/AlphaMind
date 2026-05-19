"""Q6 macro-release surprise anomaly detector — pure compute (ALP-485).

Extracted from the legacy ``q6_macro.py`` so the orchestrator's Phase 2 can
run q6 in parallel under ``asyncio.TaskGroup`` + ``asyncio.to_thread``.

The session-bound reads (``event_calendar`` for completed release dates,
``macro_observations`` for the differenced-actual series) live in
:mod:`alphamind.distillation.q6._loaders`. The pure-compute
:func:`detect_macro_surprise_anomaly` is the function the orchestrator's
thread-bound dispatch calls; it imports no ORM types so the
``distillation-compute-no-sqlalchemy`` import-linter contract pins it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alphamind.distillation.normalization import macro_surprise_zscore, percentile_rank
from alphamind.distillation.output import AnomalyFlag


def detect_macro_surprise_anomaly(
    *,
    actual: float,
    consensus: float,
    trailing_surprises: Sequence[float],
    alert_percentile: float,
) -> AnomalyFlag | None:
    """Flag a macro release surprise that lands in the top 10% of trailing magnitudes.

    Per ``external.md`` § 3 Anomaly detection (Macro):

    1. Compute the surprise as ``actual - consensus`` per story 06's
       :func:`macro_surprise` framing.
    2. Rank ``|surprise|`` against the trailing distribution of past
       ``|surprise|``. Default trailing window is 24 months of releases.
    3. Fire when the rank reaches ``alert_percentile`` (default 90 = top 10%).
       Magnitude is the z-score from story 06's
       :func:`macro_surprise_zscore` — the same primitive every macro
       analyst-facing layer uses for surprise scaling.
    4. Severity is ``investigate_now`` per
       ``external.md`` § 3 Anomaly detection — surprise spikes are
       triggers for the adaptive research layer.

    Indicator name is *not* an argument — the caller pairs the returned
    flag with the indicator at the assembly site
    (:func:`assemble_q6_blocks` keys block_ids by indicator). This keeps
    the detector pure with respect to the surprise math.

    Returns ``None`` when the surprise is below the threshold or when the
    absolute trailing distribution provides no calibrated signal (empty or
    zero-variance — :func:`percentile_rank` returns ``None``).
    """
    surprise = actual - consensus
    abs_history = [abs(x) for x in trailing_surprises]
    rank = percentile_rank(abs_history, abs(surprise))
    if rank is None or rank < alert_percentile:
        return None
    z_score = macro_surprise_zscore(surprise, trailing_surprises)
    return AnomalyFlag(
        name="macro_surprise_anomaly",
        magnitude=float(z_score),
        severity="investigate_now",
    )


__all__ = [
    "detect_macro_surprise_anomaly",
]
