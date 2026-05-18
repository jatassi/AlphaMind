"""Pure-compute core for the q3 ATM-IV trailing baseline (ALP-484).

The baseline computation operates over a per-ticker pre-loaded ATM-IV
history (``Sequence[float]``) and produces both the calibrated rank value
the q3 IV-rank block consumes and the upsert payload the loader applies
back to ``distillation_ticker_baseline``. Splitting compute from upsert
lets the orchestrator's Phase 2 run the pure compute under
``asyncio.to_thread`` without touching the shared SQLAlchemy session.

The constant :data:`ATM_IV_BASELINE_KIND` lives here so callers (compute,
loader, shim) all import from the canonical home.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from alphamind.distillation._calibration_core import (
    CalibratedValue,
    CalibrationState,
    decide_calibration_state,
)

ATM_IV_BASELINE_KIND = "atm_iv"
"""Baseline kind tag for the ATM-IV trailing baseline rows."""


@dataclass(frozen=True, slots=True)
class AtmIvBaselineUpsertPayload:
    """Per-ticker upsert payload the loader writes back to the baseline table.

    The loader applies one ``UPSERT`` against ``distillation_ticker_baseline``
    per non-empty ticker history; tickers whose history is empty produce no
    payload (the rank is ``UNAVAILABLE`` and there is nothing to write).
    """

    mean: float
    stdev: float
    n_observations: int
    window_days: int
    state: CalibrationState


@dataclass(frozen=True, slots=True)
class AtmIvBaselineResult:
    """Pure-compute output for a single ticker.

    ``rank`` is the calibrated value the ``q3.iv_rank`` block reads;
    ``upsert`` carries the values the loader needs to refresh the baseline
    state row, or ``None`` when the history is empty (no observation, no
    write).
    """

    rank: CalibratedValue
    upsert: AtmIvBaselineUpsertPayload | None


def _percentile_rank(values: Sequence[float], target: float) -> float:
    """Percentile of ``target`` against ``values`` using the ``<= target`` convention.

    Mirrors :func:`alphamind.distillation.baselines._percentile_rank` so the
    IV-rank read is consistent with the composite-state IV-rank read.
    """
    if not values:
        return 0.0
    le = sum(1 for x in values if x <= target)
    return float(le) / float(len(values)) * 100.0


def compute_atm_iv_baseline(
    history: Sequence[float],
    *,
    window_days: int,
    min_observations: int,
) -> AtmIvBaselineResult:
    """Compute the per-ticker ATM-IV baseline + IV-rank from a history series.

    Pure function: takes the chronologically-ordered IV observations and
    returns both the calibrated rank value (consumed by the q3 IV-rank
    block) and the upsert payload (applied by the loader). Empty history
    produces an ``UNAVAILABLE`` rank and no upsert payload.
    """
    n = len(history)
    if n == 0:
        return AtmIvBaselineResult(
            rank=CalibratedValue(
                value=None,
                state=CalibrationState.UNAVAILABLE,
                bootstrap_reason=f"atm_iv: 0 < {min_observations} (no observations in window)",
            ),
            upsert=None,
        )
    mean = statistics.fmean(history)
    stdev = statistics.pstdev(history) if n > 1 else 0.0
    latest = history[-1]
    percentile = _percentile_rank(history, latest)
    state = decide_calibration_state(observed_n=n, required_n=min_observations)
    reason = (
        None
        if state is CalibrationState.CALIBRATED
        else f"atm_iv_min_observations: {n} < {min_observations}"
    )
    return AtmIvBaselineResult(
        rank=CalibratedValue(
            value={
                "mean": mean,
                "stdev": stdev,
                "n_observations": n,
                "window_days": window_days,
                "latest_iv": latest,
                "iv_rank_percentile": percentile,
            },
            state=state,
            bootstrap_reason=reason,
        ),
        upsert=AtmIvBaselineUpsertPayload(
            mean=mean,
            stdev=stdev,
            n_observations=n,
            window_days=window_days,
            state=state,
        ),
    )


def compute_atm_iv_baselines(
    history_by_ticker: Mapping[str, Sequence[float]],
    *,
    window_days: int,
    min_observations: int,
) -> dict[str, AtmIvBaselineResult]:
    """Apply :func:`compute_atm_iv_baseline` per ticker. Pure compute."""
    return {
        ticker: compute_atm_iv_baseline(
            history,
            window_days=window_days,
            min_observations=min_observations,
        )
        for ticker, history in history_by_ticker.items()
    }


__all__ = [
    "ATM_IV_BASELINE_KIND",
    "AtmIvBaselineResult",
    "AtmIvBaselineUpsertPayload",
    "compute_atm_iv_baseline",
    "compute_atm_iv_baselines",
]
