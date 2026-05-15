"""Correlation regime change detection: breakdown, dispersion shift, narrative-lag.

Implements the correlation breakdown, dispersion shift, and narrative-lag
detection blocks.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.data_sources.news.types import HeadlineType
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
    _format_iso_utc,
    _log_returns_from_closes,
    _pearson_correlation,
    _select_close_series,
    _window_bounds,
)
from alphamind.persistence.models import NewsArticles, NewsArticleTickers

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

# Topic-tag set defining "regime-relevant" media coverage. The narrative-lag
# indicator is gated on news articles whose ``topic_tags`` overlap with this
# set so routine company news doesn't drown out the silence signal — see the
# story 08d notes on filtering ``news_articles`` for narrative pickup.
NARRATIVE_LAG_REGIME_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.MACRO_DATA,
        HeadlineType.REGULATORY,
        HeadlineType.GEOPOLITICAL,
        HeadlineType.SECTOR_ROTATION,
    }
)


@dataclass(frozen=True)
class CorrelationRegimeChangeConfig:
    """Threshold bundle for :func:`compute_correlation_regime_change`.

    Packs the per-window and per-detection thresholds that the orchestrator
    pulls out of :class:`DistillationConfig` into a single immutable record
    so :func:`compute_correlation_regime_change` keeps a tight signature.

    ``correlation_breakdown_sigma`` gates the Fisher-z breakdown test
    (multiple-comparison-aware default in ``config/distillation.yaml``);
    ``dispersion_sigma`` gates the cross-stock dispersion z-test against
    the trailing 20-day distribution.
    """

    short_window_days: int
    long_window_days: int
    correlation_breakdown_sigma: float
    dispersion_window_days: int
    dispersion_sigma: float
    media_silence_hours: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _select_universe_returns(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    window_days: int,
) -> dict[str, list[float]]:
    """Return per-ticker daily returns over ``window_days`` ascending."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    out: dict[str, list[float]] = {}
    for ticker in universe_tickers:
        closes = _select_close_series(
            session, ticker=ticker, range_start=range_start, range_end=range_end
        )
        out[ticker] = _log_returns_from_closes(closes)
    return out


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
    long_returns: dict[str, list[float]],
    short_returns: dict[str, list[float]],
    correlation_breakdown_sigma: float,
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Emit one block per pair whose recent correlation broke from the prior baseline.

    The test is the standard two-sample Fisher z-transform comparing the
    recent short-window correlation against the *non-overlapping* prior
    segment of the long window. Magnitude:

    ``|atanh(r_recent) - atanh(r_prior)| / sqrt(1/(N_recent-3) + 1/(N_prior-3))``

    The denominator is the Fisher-information null variance for the
    difference of two correlation estimates from independent samples — it
    depends only on the sample sizes. Independence is guaranteed by
    excluding the short tail from the prior segment.
    """
    prior_window_days = long_window_days - short_window_days
    if prior_window_days < _FISHER_Z_MIN_SAMPLES:
        return []
    null_stdev = math.sqrt(
        1.0 / (short_window_days - _FISHER_Z_DF_CORRECTION)
        + 1.0 / (prior_window_days - _FISHER_Z_DF_CORRECTION)
    )

    short_matrix = _correlation_matrix(short_returns)
    prior_returns: dict[str, list[float]] = {
        ticker: returns[:-short_window_days] for ticker, returns in long_returns.items()
    }
    prior_matrix = _correlation_matrix(prior_returns)

    tickers = sorted(short_matrix)
    blocks: list[OutputBlock] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            if (
                min(
                    len(short_returns.get(row, [])),
                    len(short_returns.get(col, [])),
                    len(prior_returns.get(row, [])),
                    len(prior_returns.get(col, [])),
                )
                < _FISHER_Z_MIN_SAMPLES
            ):
                continue
            recent_corr = short_matrix[row][col]
            prior_corr = prior_matrix[row][col]
            magnitude = abs(_fisher_z(recent_corr) - _fisher_z(prior_corr)) / null_stdev
            if magnitude < correlation_breakdown_sigma:
                continue
            state, reason = _calibration_for_window(
                n_observations=min(
                    len(short_returns.get(row, [])),
                    len(short_returns.get(col, [])),
                    len(long_returns.get(row, [])),
                    len(long_returns.get(col, [])),
                ),
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
                        "long_correlation": _pearson_correlation(
                            long_returns[row], long_returns[col]
                        ),
                        "deviation_sigma": magnitude,
                        "short_window_days": short_window_days,
                        "long_window_days": long_window_days,
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
    short_returns: dict[str, list[float]],
) -> list[float]:
    """Walk per-day cross-ticker stdevs across the universe's return matrix.

    Each output element is the cross-ticker stdev of the universe's daily
    returns at one day index, dropped when fewer than two tickers reported.
    """
    n_days = max(
        (len(short_returns.get(t, [])) for t in universe_tickers),
        default=0,
    )
    daily_stdevs: list[float] = []
    for day_index in range(n_days):
        day_returns: list[float] = []
        for ticker in universe_tickers:
            rs = short_returns.get(ticker, [])
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
    short_returns: dict[str, list[float]],
    dispersion_window_days: int,
    dispersion_sigma: float,
    as_of: datetime,
) -> OutputBlock | None:
    """Emit the dispersion-shift block if today's cross-stock dispersion spiked.

    Today's dispersion is the population stdev of the universe's most recent
    daily returns. The trailing distribution is the per-day dispersion over
    ``dispersion_window_days`` of prior days. Fires when today is at or above
    ``dispersion_sigma`` multiples of the trailing distribution's mean.
    """
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


def _qualifying_articles_present(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    media_silence_hours: int,
) -> bool:
    """Return ``True`` when at least one regime-relevant article qualifies.

    Qualifying article: published in the trailing ``media_silence_hours``,
    mentions a ticker in ``universe_tickers``, and carries at least one
    topic tag in :data:`NARRATIVE_LAG_REGIME_TAGS`.
    """
    if not universe_tickers:
        return False
    silence_start = as_of - timedelta(hours=media_silence_hours)
    silence_start_iso = _format_iso_utc(silence_start)
    as_of_iso = _format_iso_utc(as_of)
    stmt = (
        select(NewsArticles.topic_tags)
        .join(
            NewsArticleTickers,
            NewsArticles.article_id == NewsArticleTickers.article_id,
        )
        .where(
            NewsArticleTickers.ticker.in_(list(universe_tickers)),
            NewsArticles.published_at >= silence_start_iso,
            NewsArticles.published_at <= as_of_iso,
            NewsArticles.topic_tags.isnot(None),
        )
    )
    rows = session.execute(stmt).scalars().all()
    for raw_tags in rows:
        if raw_tags is None:
            continue
        # ``topic_tags`` is JSON-encoded list of canonical HeadlineType values
        # (per ``config/headline_tag_mapping.yaml`` normalization at the
        # collector boundary). Historical rows may carry vendor-raw text that
        # isn't valid JSON — treat as empty rather than crash.
        try:
            parsed = json.loads(raw_tags)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        article_tags: set[HeadlineType] = set()
        for tag in parsed:
            if not isinstance(tag, str):
                continue
            try:
                article_tags.add(HeadlineType(tag))
            except ValueError:
                continue
        if article_tags & NARRATIVE_LAG_REGIME_TAGS:
            return True
    return False


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


# ---------------------------------------------------------------------------
# Public compute function
# ---------------------------------------------------------------------------


def compute_correlation_regime_change(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    config: CorrelationRegimeChangeConfig,
) -> list[OutputBlock]:
    """Compute correlation-breakdown / dispersion / narrative-lag blocks.

    Returns:

    - One :class:`OutputBlock` per pair whose correlation broke out
      (``q7.correlation_breakdown.<lead>_<lag>``).
    - One ``q7.correlation_breakdown.dispersion_shift`` block.
    - One ``q7.narrative_lag`` block when at least one breakdown fired —
      its anomaly flag fires only when no qualifying news was published
      in the trailing ``media_silence_hours`` window.

    All blocks address :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    """
    # Read once over the long window; slice the short window from each
    # ticker's tail so we don't requery the overlapping range.
    long_returns = _select_universe_returns(
        session,
        universe_tickers=universe_tickers,
        as_of=as_of,
        window_days=config.long_window_days,
    )
    short_returns: dict[str, list[float]] = {
        ticker: list(returns)[-config.short_window_days :]
        for ticker, returns in long_returns.items()
    }
    breakdown_blocks = _correlation_breakdown_blocks(
        short_returns=short_returns,
        long_returns=long_returns,
        correlation_breakdown_sigma=config.correlation_breakdown_sigma,
        as_of=as_of,
        short_window_days=config.short_window_days,
        long_window_days=config.long_window_days,
    )
    dispersion_block = _dispersion_shift_block(
        universe_tickers=universe_tickers,
        short_returns=short_returns,
        dispersion_window_days=config.dispersion_window_days,
        dispersion_sigma=config.dispersion_sigma,
        as_of=as_of,
    )
    qualifying_news = _qualifying_articles_present(
        session,
        universe_tickers=universe_tickers,
        as_of=as_of,
        media_silence_hours=config.media_silence_hours,
    )
    narrative_block = _narrative_lag_block(
        breakdown_blocks=breakdown_blocks,
        qualifying_news=qualifying_news,
        as_of=as_of,
        media_silence_hours=config.media_silence_hours,
    )

    blocks: list[OutputBlock] = list(breakdown_blocks)
    if dispersion_block is not None:
        blocks.append(dispersion_block)
    if narrative_block is not None:
        blocks.append(narrative_block)
    return blocks
