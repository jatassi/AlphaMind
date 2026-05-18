"""Shared pure math primitives for the Q7 cross-asset sub-package.

ALP-486 retired the session-bound ``_select_close_series`` helper that
previously lived here. It now lives in :mod:`alphamind.distillation.q7._loaders`
alongside the rest of the loader machinery, so this module is ORM-free and
safe to call from the parallel pure compute path.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from alphamind.distillation._calibration_core import CalibrationState, decide_calibration_state

# ---------------------------------------------------------------------------
# Block-id namespace
# ---------------------------------------------------------------------------

_BLOCK_NAMESPACE = "q7"
"""Pinned namespace for every Q7 block id per the dispatch convention."""


# ---------------------------------------------------------------------------
# Time arithmetic
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
    """Render a tz-aware datetime as ISO-8601 UTC with a ``Z`` suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_bounds(*, as_of: datetime, window_days: int) -> tuple[str, str]:
    """Return ``(range_start, range_end)`` ISO strings spanning ``window_days``.

    Inclusive on both ends; the same range used by every per-window query in
    this module.
    """
    end = _format_iso_utc(as_of)
    start = _format_iso_utc(as_of - timedelta(days=window_days))
    return start, end


# ---------------------------------------------------------------------------
# Correlation primitives
# ---------------------------------------------------------------------------


def _pearson_correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation between two equal-length sequences.

    Returns ``0.0`` when either sequence has zero variance — the caller
    interprets a flat series as carrying no relationship rather than as a
    pathological signal.
    """
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    dot = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b, strict=True):
        da = x - mean_a
        db = y - mean_b
        dot += da * db
        var_a += da * da
        var_b += db * db
    if var_a == 0.0 or var_b == 0.0:
        return 0.0
    return float(dot / math.sqrt(var_a * var_b))


def _log_returns_from_closes(closes: Sequence[float]) -> list[float]:
    """Day-over-day log returns from an ascending close series."""
    out: list[float] = []
    for prior, latest in pairwise(closes):
        if prior <= 0.0 or latest <= 0.0:
            out.append(0.0)
        else:
            out.append(math.log(latest / prior))
    return out


def _correlation_matrix(
    returns_by_ticker: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, float]]:
    """Pairwise Pearson correlation matrix across the given return series.

    Both axes iterate sorted ticker order so the rendered envelope is
    byte-deterministic per the story-05 contract.
    """
    tickers = sorted(returns_by_ticker)
    matrix: dict[str, dict[str, float]] = {}
    for row_ticker in tickers:
        row: dict[str, float] = {}
        for col_ticker in tickers:
            if row_ticker == col_ticker:
                row[col_ticker] = 1.0
                continue
            row[col_ticker] = _pearson_correlation(
                returns_by_ticker[row_ticker],
                returns_by_ticker[col_ticker],
            )
        matrix[row_ticker] = row
    return matrix


def _calibration_for_window(
    *,
    n_observations: int,
    required: int,
    input_name: str,
) -> tuple[CalibrationState, str | None]:
    """Decide ``(state, bootstrap_reason)`` for a per-window correlation block.

    Delegates the three-state decision to :func:`decide_calibration_state`
    (ALP-540 vocabulary) and synthesizes the operator-readable reason
    string. The matrix is still computed below the threshold for visibility
    so domain researchers can weight the percentile read.
    """
    state = decide_calibration_state(observed_n=n_observations, required_n=required)
    if state is CalibrationState.CALIBRATED:
        return state, None
    if state is CalibrationState.UNAVAILABLE:
        return state, f"{input_name}: 0 observations"
    return state, f"{input_name}: {n_observations} < {required}"


def _zscore(value: float, distribution: Sequence[float]) -> float:
    """Population z-score of ``value`` against ``distribution``.

    Returns ``0.0`` when the distribution is empty or has zero variance — the
    caller treats no-information as no-magnitude rather than as a blow-up.
    """
    if not distribution:
        return 0.0
    mean = statistics.fmean(distribution)
    sd = statistics.pstdev(distribution)
    if sd == 0.0:
        return 0.0
    return float((value - mean) / sd)


def _max_lagged_correlation(
    *,
    leading_returns: Sequence[float],
    following_returns: Sequence[float],
    max_lag_days: int,
) -> float:
    """Maximum lagged Pearson correlation across ``d`` in ``1..max_lag_days``.

    Returns ``corr(leading[:n-d], following[d:])`` maximized over candidate
    lags. A high value means the leading series predicts the following
    series after a lag of ``d`` days. Returns ``0.0`` when the windows are
    too short to align even the smallest candidate lag.
    """
    n = len(following_returns)
    if n < 2 or max_lag_days < 1:
        return 0.0
    best = 0.0
    for d in range(1, min(max_lag_days, n - 1) + 1):
        leading_window = leading_returns[: n - d]
        following_window = following_returns[d:]
        if len(leading_window) < 2:
            continue
        corr = _pearson_correlation(leading_window, following_window)
        if corr > best:
            best = corr
    return best
