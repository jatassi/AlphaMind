"""Pure compute for Q7 intra-sector correlation matrices (ALP-486).

Splits the intra-sector-correlation compute path along the compute/load
boundary. This module is ORM-free; the session-bound read + write surface
lives in :mod:`.intra_sector_correlation_loaders` — that loader is
responsible for persisting one ``correlation_divergence`` event per pair
flag the pure compute emits.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime

from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
)


def _detect_pair_divergence(
    *,
    short_matrix: Mapping[str, Mapping[str, float]],
    long_matrix: Mapping[str, Mapping[str, float]],
    divergence_sigma: float,
) -> list[AnomalyFlag]:
    """Emit one :class:`AnomalyFlag` per sector pair whose correlation diverged.

    The baseline distribution is the population of off-diagonal long-window
    correlations within the sector; a pair fires when its short-window
    correlation deviates from the long-window value by at least
    ``divergence_sigma`` multiples of that distribution's standard deviation.
    """
    tickers = sorted(short_matrix)
    long_values: list[float] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            long_values.append(long_matrix[row][col])
    # Pairs require at least one off-diagonal entry (i.e., a non-singleton
    # ticker set); pstdev needs at least one observation to be defined.
    if not long_values:
        return []
    sigma = statistics.pstdev(long_values)
    if sigma == 0.0:
        return []
    flags: list[AnomalyFlag] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_corr = short_matrix[row][col]
            long_corr = long_matrix[row][col]
            deviation = abs(short_corr - long_corr)
            magnitude = deviation / sigma
            if magnitude >= divergence_sigma:
                flags.append(
                    AnomalyFlag(
                        name=f"intra_sector_correlation_divergence:{row}:{col}",
                        magnitude=magnitude,
                        severity="investigate_if_persists",
                    )
                )
    return flags


def compute_intra_sector_correlation_pure(
    *,
    sector: str,
    sector_tickers: Sequence[str],
    long_returns_by_ticker: Mapping[str, Sequence[float]],
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
    as_of: datetime,
) -> OutputBlock:
    """Pure compute of the per-sector intra-sector correlation block.

    Operates entirely over the pre-loaded long-window log-return series
    for the sector's tickers. The short window is sliced from the tail of
    the long-window series.

    The pair-divergence detector attaches :class:`AnomalyFlag` instances
    to the block; the loader is responsible for writing the corresponding
    ``correlation_divergence`` event rows synchronously under the shared
    session before the parallel compute fires.
    """
    del sector_tickers  # informational; long_returns_by_ticker enumerates the sector

    short_returns: dict[str, Sequence[float]] = {}
    short_n_min: float = math.inf
    long_n_min: float = math.inf
    for ticker, returns in long_returns_by_ticker.items():
        short_returns[ticker] = list(returns)[-short_window_days:]
        short_n_min = min(short_n_min, len(short_returns[ticker]))
        long_n_min = min(long_n_min, len(returns))

    short_matrix = _correlation_matrix(short_returns)
    long_matrix = _correlation_matrix(long_returns_by_ticker)

    short_n = 0 if short_n_min is math.inf else int(short_n_min)
    long_n = 0 if long_n_min is math.inf else int(long_n_min)
    state, reason = _calibration_for_window(
        n_observations=min(short_n, long_n),
        required=short_window_days,
        input_name="correlation_short_days",
    )

    flags = _detect_pair_divergence(
        short_matrix=short_matrix,
        long_matrix=long_matrix,
        divergence_sigma=divergence_sigma,
    )

    payload = {
        "short_window": {
            "correlation_matrix": short_matrix,
            "window_days": short_window_days,
            "n_observations": short_n,
        },
        "long_window": {
            "correlation_matrix": long_matrix,
            "window_days": long_window_days,
            "n_observations": long_n,
        },
        "sector": sector,
    }
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intra_sector_correlation.{sector}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


__all__ = [
    "compute_intra_sector_correlation_pure",
]
