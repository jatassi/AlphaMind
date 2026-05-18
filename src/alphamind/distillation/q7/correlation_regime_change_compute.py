"""Pure compute for Q7 correlation regime change detection (ALP-486).

Splits the correlation-regime-change compute path along the compute/load
boundary. This module is ORM-free; the session-bound read surface (in
particular the ``news_articles`` scan that resolves the
``qualifying_news_present`` flag) lives in
:mod:`.correlation_regime_change_loaders`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
    _pearson_correlation,
)

# Min observations the Fisher-z null variance is defined for: ``1/(N-3)``.
# Below 4 the variance term is undefined or negative; the block declines to
# emit rather than report a magnitude on a degenerate denominator.
_FISHER_Z_MIN_SAMPLES: int = 4

# Degrees-of-freedom correction in the Fisher z-transform null variance
# (``Var(z) = 1/(N-3)``). A property of the atanh-stabilized correlation
# distribution — not a Class A threshold.
_FISHER_Z_DF_CORRECTION: int = 3

# Saturating clip applied before ``atanh`` so a perfect ±1 correlation
# (e.g., a contrived fixture) doesn't blow up the transform. ``atanh(0.9999)``
# evaluates to ~4.95, which keeps real signal well-separated from the cap.
_ATANH_CLIP: float = 0.9999


@dataclass(frozen=True)
class CorrelationRegimeChangeParameters:
    """Threshold bundle for :func:`compute_correlation_regime_change_pure`.

    Packs the per-window and per-detection thresholds the orchestrator pulls
    out of :class:`DistillationDomainConfig` into a single immutable record
    so the pure compute keeps a tight signature.

    ``correlation_breakdown_sigma`` gates the Fisher-z breakdown test
    (multiple-comparison-aware default in ``config/distillation.yaml``);
    ``dispersion_sigma`` gates the cross-stock dispersion z-test against
    the trailing 20-day distribution.

    ALP-541 added two preconditions that suppress phantom breakdowns on
    sparse or noise-floor pairs:

    - ``correlation_min_overlap_fraction`` — each window (short, prior,
      long) must have at least this fraction of its nominal length in
      overlapping observations between the two tickers. Below the floor
      the sigma-test denominator is misspecified and the math inflates;
      pairs failing the guard are filtered before flag emission.
    - ``correlation_noise_floor`` — the long-window correlation
      magnitude must clear this absolute value before a sigma-test is run.
      When ``|long_corr|`` is near zero the short-window deviation is
      dominated by noise rather than a genuine shift in joint dynamics.
    """

    short_window_days: int
    long_window_days: int
    correlation_breakdown_sigma: float
    correlation_min_overlap_fraction: float
    correlation_noise_floor: float
    dispersion_window_days: int
    dispersion_sigma: float
    media_silence_hours: int


def _fisher_z(correlation: float) -> float:
    """Fisher z-transform with a saturating ±1 clip.

    ``atanh`` is undefined at ±1; clipping keeps the transform well-defined
    on contrived fixtures (perfect correlation or anti-correlation) while
    leaving real signal far from the cap.
    """
    clipped = max(-_ATANH_CLIP, min(_ATANH_CLIP, correlation))
    return math.atanh(clipped)


def _correlation_breakdown_blocks(
    *,
    long_returns: Mapping[str, Sequence[float]],
    short_returns: Mapping[str, Sequence[float]],
    correlation_breakdown_sigma: float,
    correlation_min_overlap_fraction: float,
    correlation_noise_floor: float,
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Emit one block per pair whose recent correlation broke from the prior baseline.

    ALP-541 added two preconditions that filter phantom breakdowns:

    - **Observation count guard** — short, prior, and long windows must each
      carry at least ``ceil(window_days * correlation_min_overlap_fraction)``
      overlapping observations between the two tickers. Sparse pairs (one
      ticker brand-new in the universe, holiday-misaligned series) misspecify
      the sigma-test denominator; the math inflates and the pair becomes a false
      positive.
    - **Magnitude noise-floor guard** — the long-window correlation
      magnitude must clear ``correlation_noise_floor`` before the sigma-test
      runs. When ``|long_corr|`` is near zero the short-window deviation
      is dominated by noise rather than a genuine shift in joint
      dynamics.

    Pairs failing either guard are dropped before any flag is emitted, so the
    downstream synthesizer brief never sees them.
    """
    prior_window_days = long_window_days - short_window_days
    if prior_window_days < _FISHER_Z_MIN_SAMPLES:
        return []
    null_stdev = math.sqrt(
        1.0 / (short_window_days - _FISHER_Z_DF_CORRECTION)
        + 1.0 / (prior_window_days - _FISHER_Z_DF_CORRECTION)
    )

    short_overlap_min = math.ceil(short_window_days * correlation_min_overlap_fraction)
    prior_overlap_min = math.ceil(prior_window_days * correlation_min_overlap_fraction)
    long_overlap_min = math.ceil(long_window_days * correlation_min_overlap_fraction)

    short_matrix = _correlation_matrix(short_returns)
    prior_returns: dict[str, list[float]] = {
        ticker: list(returns)[:-short_window_days] for ticker, returns in long_returns.items()
    }
    prior_matrix = _correlation_matrix(prior_returns)

    tickers = sorted(short_matrix)
    blocks: list[OutputBlock] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_overlap = min(
                len(short_returns.get(row, ())),
                len(short_returns.get(col, ())),
            )
            prior_overlap = min(
                len(prior_returns.get(row, [])),
                len(prior_returns.get(col, [])),
            )
            long_overlap = min(
                len(long_returns.get(row, ())),
                len(long_returns.get(col, ())),
            )
            if min(short_overlap, prior_overlap) < _FISHER_Z_MIN_SAMPLES:
                continue
            if (
                short_overlap < short_overlap_min
                or prior_overlap < prior_overlap_min
                or long_overlap < long_overlap_min
            ):
                continue
            long_corr = _pearson_correlation(long_returns[row], long_returns[col])
            if abs(long_corr) < correlation_noise_floor:
                continue
            recent_corr = short_matrix[row][col]
            prior_corr = prior_matrix[row][col]
            magnitude = abs(_fisher_z(recent_corr) - _fisher_z(prior_corr)) / null_stdev
            if magnitude < correlation_breakdown_sigma:
                continue
            state, reason = _calibration_for_window(
                n_observations=min(short_overlap, long_overlap),
                required=short_window_days,
                input_name="correlation_breakdown_observations",
            )
            blocks.append(
                OutputBlock(
                    block_id=f"{_BLOCK_NAMESPACE}.correlation_breakdown.{row}_{col}",
                    audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
                    freshness_ts=as_of,
                    calibration_state=state,
                    bootstrap_reason=reason,
                    payload={
                        "pair": [row, col],
                        "short_correlation": recent_corr,
                        "long_correlation": long_corr,
                        "deviation_sigma": magnitude,
                        "short_window_days": short_window_days,
                        "long_window_days": long_window_days,
                        "n_overlapping_observations": long_overlap,
                    },
                    anomaly_flags=(
                        AnomalyFlag(
                            name=f"correlation_breakdown_flag:{row}:{col}",
                            magnitude=magnitude,
                            severity="investigate_now",
                        ),
                    ),
                    regime_context=None,
                )
            )
    return blocks


def _per_day_universe_stdevs(
    *,
    universe_tickers: Sequence[str],
    short_returns: Mapping[str, Sequence[float]],
) -> list[float]:
    """Walk per-day cross-ticker stdevs across the universe's return matrix."""
    n_days = max(
        (len(short_returns.get(t, ())) for t in universe_tickers),
        default=0,
    )
    daily_stdevs: list[float] = []
    for day_index in range(n_days):
        day_returns: list[float] = []
        for ticker in universe_tickers:
            rs = short_returns.get(ticker, ())
            if day_index < len(rs):
                day_returns.append(rs[day_index])
        # ``pstdev`` is undefined on a single observation; pairwise iter is
        # the canonical idiom for "have at least one neighbouring value".
        if any(True for _ in pairwise(day_returns)):
            daily_stdevs.append(statistics.pstdev(day_returns))
    return daily_stdevs


def _dispersion_shift_block(
    *,
    universe_tickers: Sequence[str],
    short_returns: Mapping[str, Sequence[float]],
    dispersion_window_days: int,
    dispersion_sigma: float,
    as_of: datetime,
) -> OutputBlock | None:
    """Emit the dispersion-shift block if today's cross-stock dispersion spiked."""
    if not universe_tickers:
        return None
    daily_stdevs = _per_day_universe_stdevs(
        universe_tickers=universe_tickers, short_returns=short_returns
    )
    if not daily_stdevs:
        return None
    today_dispersion = daily_stdevs[-1]
    trailing = daily_stdevs[:-1]
    if not trailing:
        return None
    mean_trailing = statistics.fmean(trailing)
    sd_trailing = statistics.pstdev(trailing)
    if sd_trailing == 0.0:
        return None
    z = (today_dispersion - mean_trailing) / sd_trailing
    flags: tuple[AnomalyFlag, ...] = ()
    if z >= dispersion_sigma:
        flags = (
            AnomalyFlag(
                name="dispersion_shift_flag",
                magnitude=z,
                severity="investigate_now",
            ),
        )
    state, reason = _calibration_for_window(
        n_observations=len(daily_stdevs),
        required=dispersion_window_days,
        input_name="dispersion_window_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.correlation_breakdown.dispersion_shift",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "today_dispersion": today_dispersion,
            "mean_trailing_dispersion": mean_trailing,
            "stdev_trailing_dispersion": sd_trailing,
            "zscore": z,
            "dispersion_window_days": dispersion_window_days,
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def _narrative_lag_block(
    *,
    breakdown_blocks: Sequence[OutputBlock],
    qualifying_news: bool,
    as_of: datetime,
    media_silence_hours: int,
) -> OutputBlock | None:
    """Emit the narrative-lag block when correlation broke and media is silent."""
    if not breakdown_blocks:
        return None
    flags: tuple[AnomalyFlag, ...] = ()
    if not qualifying_news:
        # Strongest breakdown magnitude carries the narrative-lag magnitude.
        magnitude = max(
            (flag.magnitude for block in breakdown_blocks for flag in block.anomaly_flags),
            default=0.0,
        )
        flags = (
            AnomalyFlag(
                name="narrative_lag_flag",
                magnitude=magnitude,
                severity="investigate_now",
            ),
        )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.narrative_lag",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "media_silence_hours": media_silence_hours,
            "qualifying_news_present": qualifying_news,
            "n_breakdowns": len(breakdown_blocks),
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def compute_correlation_regime_change_pure(
    *,
    universe_tickers: Sequence[str],
    long_returns_by_ticker: Mapping[str, Sequence[float]],
    qualifying_news_present: bool,
    params: CorrelationRegimeChangeParameters,
    as_of: datetime,
) -> list[OutputBlock]:
    """Pure compute of correlation-breakdown / dispersion / narrative-lag blocks.

    Operates entirely over pre-loaded long-window log-return series. The
    short-window slice is the tail of the long-window series.

    The narrative-lag flag relies on the loader-resolved
    ``qualifying_news_present`` flag — the loader does the
    ``news_articles`` scan and threads the result through here.
    """
    short_returns: dict[str, list[float]] = {
        ticker: list(returns)[-params.short_window_days :]
        for ticker, returns in long_returns_by_ticker.items()
    }
    breakdown_blocks = _correlation_breakdown_blocks(
        short_returns=short_returns,
        long_returns=long_returns_by_ticker,
        correlation_breakdown_sigma=params.correlation_breakdown_sigma,
        correlation_min_overlap_fraction=params.correlation_min_overlap_fraction,
        correlation_noise_floor=params.correlation_noise_floor,
        as_of=as_of,
        short_window_days=params.short_window_days,
        long_window_days=params.long_window_days,
    )
    dispersion_block = _dispersion_shift_block(
        universe_tickers=universe_tickers,
        short_returns=short_returns,
        dispersion_window_days=params.dispersion_window_days,
        dispersion_sigma=params.dispersion_sigma,
        as_of=as_of,
    )
    narrative_block = _narrative_lag_block(
        breakdown_blocks=breakdown_blocks,
        qualifying_news=qualifying_news_present,
        as_of=as_of,
        media_silence_hours=params.media_silence_hours,
    )

    blocks: list[OutputBlock] = list(breakdown_blocks)
    if dispersion_block is not None:
        blocks.append(dispersion_block)
    if narrative_block is not None:
        blocks.append(narrative_block)
    return blocks


__all__ = [
    "CorrelationRegimeChangeParameters",
    "compute_correlation_regime_change_pure",
]
