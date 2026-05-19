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
from collections import Counter
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

# Denominator in the standard-normal tail probability ``erfc(|z| / sqrt(2))``.
_SQRT_TWO: float = math.sqrt(2.0)


@dataclass(frozen=True)
class CorrelationRegimeChangeParameters:
    """Threshold bundle for :func:`compute_correlation_regime_change_pure`.

    Packs the per-window and per-detection thresholds the orchestrator pulls
    out of :class:`DistillationDomainConfig` into a single immutable record
    so the pure compute keeps a tight signature.

    ``correlation_breakdown_sigma`` is the operator-set raw-magnitude floor
    on the Fisher-z breakdown test; the multiple-comparison correction is
    handled by ``correlation_breakdown_fdr_q``. ``dispersion_sigma`` gates
    the cross-stock dispersion z-test against the trailing 20-day
    distribution.

    Two preconditions suppress phantom breakdowns on sparse or noise-floor
    pairs:

    - ``correlation_min_overlap_fraction`` — each window (short, prior,
      long) must have at least this fraction of its nominal length in
      overlapping observations between the two tickers. Below the floor
      the sigma-test denominator is misspecified and the math inflates;
      pairs failing the guard are filtered before flag emission.
    - ``correlation_noise_floor`` — the long-window correlation
      magnitude must clear this absolute value before a sigma-test is run.
      When ``|long_corr|`` is near zero the short-window deviation is
      dominated by noise rather than a genuine shift in joint dynamics.

    ``correlation_breakdown_fdr_q`` is the Benjamini-Hochberg false-discovery
    rate target. The pair-wise sigma-test runs N*(N-1)/2 hypotheses per
    invocation, so the bare sigma threshold (single-test) produces a phantom
    flag count that grows with the universe size. The sigma for each
    guard-surviving pair is mapped to a two-tailed p-value and BH is
    applied across the candidate pool; only pairs whose BH-adjusted
    q-value clears the target are published. The raw sigma floor still
    applies as an emit gate so operator-set magnitude requirements
    survive.

    ``correlation_locus_pair_count_threshold`` aggregates published
    per-pair breakdowns into per-ticker locus flags. Pair-wise
    correlations are not independent when a single ticker's return path
    shifts — one shock to META can flip its correlation with N partners
    in lock-step, producing N independent-looking flags that all express
    one underlying signal. After BH-FDR + sigma-gate filtering, ticker
    occurrence counts are taken across the surviving pairs; any ticker
    appearing in at least the configured pair count is emitted as a
    single ``q7.correlation_locus.<ticker>`` block and its contributing
    per-pair blocks are suppressed from the output stream. Pairs not
    involving any locus ticker continue to publish as per-pair flags.
    """

    short_window_days: int
    long_window_days: int
    correlation_breakdown_sigma: float
    correlation_min_overlap_fraction: float
    correlation_noise_floor: float
    correlation_breakdown_fdr_q: float
    dispersion_window_days: int
    dispersion_sigma: float
    media_silence_hours: int
    correlation_locus_pair_count_threshold: int


def _fisher_z(correlation: float) -> float:
    """Fisher z-transform with a saturating ±1 clip.

    ``atanh`` is undefined at ±1; clipping keeps the transform well-defined
    on contrived fixtures (perfect correlation or anti-correlation) while
    leaving real signal far from the cap.
    """
    clipped = max(-_ATANH_CLIP, min(_ATANH_CLIP, correlation))
    return math.atanh(clipped)


def _two_tailed_p_value(z: float) -> float:
    """P(|Z| ≥ |z|) for a standard normal Z.

    Computed via the complementary error function so the tail probability
    stays well-conditioned at large |z| (where ``1 - Phi(z)`` underflows
    to zero in float64).
    """
    return math.erfc(abs(z) / _SQRT_TWO)


def _benjamini_hochberg_q_values(p_values: Sequence[float]) -> tuple[float, ...]:
    """BH-adjusted q-values preserving input order.

    Standard step-up Benjamini-Hochberg with the monotonicity-enforcing
    cumulative minimum: ``q_(i) = min_{j ≥ i} p_(j) * m / j`` for the
    sorted p-values, clamped to ``[0, 1]``. The result reindexed to the
    input order so callers can pair each p-value with its q-value
    without a separate bookkeeping table.
    """
    m = len(p_values)
    if m == 0:
        return ()
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [p * m / (rank + 1) for rank, (_, p) in enumerate(indexed)]
    for i in reversed(range(len(adjusted) - 1)):
        adjusted[i] = min(adjusted[i], adjusted[i + 1])
    q_values = [0.0] * m
    for sorted_rank, (orig_idx, _) in enumerate(indexed):
        q_values[orig_idx] = min(adjusted[sorted_rank], 1.0)
    return tuple(q_values)


@dataclass(frozen=True, slots=True)
class _BreakdownCandidate:
    """One pair that passed the data-alignment guards.

    Carries the inputs the publish layer needs to construct an
    :class:`OutputBlock` once BH-FDR has decided which candidates clear
    the publish gate.
    """

    row: str
    col: str
    short_corr: float
    long_corr: float
    magnitude: float
    long_overlap: int
    short_overlap: int


@dataclass(frozen=True, slots=True)
class _PublishedPair:
    """A guard- and BH-surviving pair before the locus-aggregation pass.

    Held in intermediate form so the locus pass can decide whether to
    suppress the pair in favour of a locus rollup before the
    :class:`OutputBlock` is constructed.
    """

    candidate: _BreakdownCandidate
    q_value: float


def _published_pairs(
    *,
    long_returns: Mapping[str, Sequence[float]],
    short_returns: Mapping[str, Sequence[float]],
    params: CorrelationRegimeChangeParameters,
) -> list[_PublishedPair]:
    """Return the pairs that survive guards + BH-FDR + sigma-gate filtering.

    Suppresses the two phantom-breakdown patterns documented on
    :class:`CorrelationRegimeChangeParameters`: sparse-overlap pairs whose
    sigma-test denominator is misspecified, and near-zero-baseline pairs
    where the short-window deviation is dominated by noise. A pair is
    published only when its BH-adjusted q-value clears
    ``correlation_breakdown_fdr_q`` *and* its raw deviation sigma clears
    ``correlation_breakdown_sigma``.
    """
    short_window_days = params.short_window_days
    long_window_days = params.long_window_days
    prior_window_days = long_window_days - short_window_days
    if prior_window_days < _FISHER_Z_MIN_SAMPLES:
        return []
    null_stdev = math.sqrt(
        1.0 / (short_window_days - _FISHER_Z_DF_CORRECTION)
        + 1.0 / (prior_window_days - _FISHER_Z_DF_CORRECTION)
    )

    short_overlap_min = math.ceil(short_window_days * params.correlation_min_overlap_fraction)
    prior_overlap_min = math.ceil(prior_window_days * params.correlation_min_overlap_fraction)

    short_matrix = _correlation_matrix(short_returns)
    prior_returns: dict[str, list[float]] = {
        ticker: list(returns)[:-short_window_days] for ticker, returns in long_returns.items()
    }
    prior_matrix = _correlation_matrix(prior_returns)

    # Per-ticker length lookups so the inner loop doesn't re-len() the
    # same series 2N times across the pair iteration.
    short_lens = {ticker: len(series) for ticker, series in short_returns.items()}
    prior_lens = {ticker: len(series) for ticker, series in prior_returns.items()}
    long_lens = {ticker: len(series) for ticker, series in long_returns.items()}

    tickers = sorted(short_matrix)
    candidates: list[_BreakdownCandidate] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_overlap = min(short_lens.get(row, 0), short_lens.get(col, 0))
            prior_overlap = min(prior_lens.get(row, 0), prior_lens.get(col, 0))
            long_overlap = min(long_lens.get(row, 0), long_lens.get(col, 0))
            # Hard floor — below 4 observations the Fisher-z null variance
            # (``1/(N-3)``) is undefined; the overlap-fraction guard is the
            # tighter check at production defaults but the floor stays as
            # the math invariant for any configuration.
            if min(short_overlap, prior_overlap) < _FISHER_Z_MIN_SAMPLES:
                continue
            if short_overlap < short_overlap_min or prior_overlap < prior_overlap_min:
                continue
            long_corr = _pearson_correlation(long_returns[row], long_returns[col])
            if abs(long_corr) < params.correlation_noise_floor:
                continue
            recent_corr = short_matrix[row][col]
            prior_corr = prior_matrix[row][col]
            magnitude = abs(_fisher_z(recent_corr) - _fisher_z(prior_corr)) / null_stdev
            candidates.append(
                _BreakdownCandidate(
                    row=row,
                    col=col,
                    short_corr=recent_corr,
                    long_corr=long_corr,
                    magnitude=magnitude,
                    long_overlap=long_overlap,
                    short_overlap=short_overlap,
                )
            )

    # BH-FDR operates on the full guard-surviving candidate pool — the
    # sigma floor is intentionally applied *after* BH so the multiplicity
    # correction sees the full population of tested hypotheses, not just
    # those that happened to clear the magnitude gate.
    p_values = tuple(_two_tailed_p_value(c.magnitude) for c in candidates)
    q_values = _benjamini_hochberg_q_values(p_values)

    published: list[_PublishedPair] = []
    for candidate, q_value in zip(candidates, q_values, strict=True):
        if q_value > params.correlation_breakdown_fdr_q:
            continue
        if candidate.magnitude < params.correlation_breakdown_sigma:
            continue
        published.append(_PublishedPair(candidate=candidate, q_value=q_value))
    return published


def _pair_block_id(candidate: _BreakdownCandidate) -> str:
    return f"{_BLOCK_NAMESPACE}.correlation_breakdown.{candidate.row}_{candidate.col}"


def _build_pair_block(
    pair: _PublishedPair,
    *,
    params: CorrelationRegimeChangeParameters,
    as_of: datetime,
) -> OutputBlock:
    """Construct the :class:`OutputBlock` for a published per-pair breakdown."""
    candidate = pair.candidate
    state, reason = _calibration_for_window(
        n_observations=min(candidate.short_overlap, candidate.long_overlap),
        required=params.short_window_days,
        input_name="correlation_breakdown_observations",
    )
    return OutputBlock(
        block_id=_pair_block_id(candidate),
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "pair": [candidate.row, candidate.col],
            "short_correlation": candidate.short_corr,
            "long_correlation": candidate.long_corr,
            "deviation_sigma": candidate.magnitude,
            "q_value": pair.q_value,
            "short_window_days": params.short_window_days,
            "long_window_days": params.long_window_days,
            "n_overlapping_observations": candidate.long_overlap,
        },
        anomaly_flags=(
            AnomalyFlag(
                name=f"correlation_breakdown_flag:{candidate.row}:{candidate.col}",
                magnitude=candidate.magnitude,
                severity="investigate_now",
            ),
        ),
        regime_context=None,
    )


def _locus_block(
    *,
    locus_ticker: str,
    contributing: Sequence[_PublishedPair],
    sector_by_ticker: Mapping[str, str] | None,
    as_of: datetime,
) -> OutputBlock:
    """Build a single locus block summarising one ticker's contributing pairs."""
    partners = sorted(
        {
            pair.candidate.col if pair.candidate.row == locus_ticker else pair.candidate.row
            for pair in contributing
        }
    )
    max_sigma = max(pair.candidate.magnitude for pair in contributing)
    supporting_pair_ids = tuple(sorted(_pair_block_id(pair.candidate) for pair in contributing))
    partners_by_sector: dict[str, list[str]] | None = None
    cross_sector_spread: str | None = None
    if sector_by_ticker is not None:
        partners_by_sector = {}
        for partner in partners:
            sector = sector_by_ticker.get(partner, "unknown")
            partners_by_sector.setdefault(sector, []).append(partner)
        if len(partners_by_sector) == 1:
            (only_sector,) = partners_by_sector.keys()
            cross_sector_spread = f"{only_sector}-only"
        else:
            cross_sector_spread = f"{len(partners_by_sector)} sectors"
    payload: dict[str, object] = {
        "locus_ticker": locus_ticker,
        "pair_count": len(contributing),
        "max_deviation_sigma": max_sigma,
        "partner_tickers": partners,
        "supporting_pairs": supporting_pair_ids,
        "partners_by_sector": partners_by_sector,
        "cross_sector_spread": cross_sector_spread,
    }
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.correlation_locus.{locus_ticker}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload,
        anomaly_flags=(
            AnomalyFlag(
                name=f"correlation_locus_flag:{locus_ticker}",
                magnitude=max_sigma,
                severity="investigate_now",
            ),
        ),
        regime_context=None,
    )


def _correlation_breakdown_blocks(
    *,
    long_returns: Mapping[str, Sequence[float]],
    short_returns: Mapping[str, Sequence[float]],
    params: CorrelationRegimeChangeParameters,
    sector_by_ticker: Mapping[str, str] | None,
    as_of: datetime,
) -> list[OutputBlock]:
    """Emit per-pair breakdown blocks plus per-ticker locus rollups.

    Tickers whose occurrence count across published pairs reaches
    ``correlation_locus_pair_count_threshold`` are rolled up into one
    ``q7.correlation_locus.<ticker>`` block and their contributing
    per-pair blocks are suppressed; pairs not involving any locus ticker
    continue to publish individually.
    """
    published = _published_pairs(
        long_returns=long_returns,
        short_returns=short_returns,
        params=params,
    )
    if not published:
        return []

    occurrences: Counter[str] = Counter(
        ticker for pair in published for ticker in (pair.candidate.row, pair.candidate.col)
    )
    locus_tickers: frozenset[str] = frozenset(
        ticker
        for ticker, count in occurrences.items()
        if count >= params.correlation_locus_pair_count_threshold
    )

    blocks: list[OutputBlock] = []
    if locus_tickers:
        contributing_by_locus: dict[str, list[_PublishedPair]] = {
            ticker: [] for ticker in locus_tickers
        }
        for pair in published:
            for ticker in (pair.candidate.row, pair.candidate.col):
                if ticker in locus_tickers:
                    contributing_by_locus[ticker].append(pair)
        for locus_ticker in sorted(locus_tickers):
            blocks.append(
                _locus_block(
                    locus_ticker=locus_ticker,
                    contributing=contributing_by_locus[locus_ticker],
                    sector_by_ticker=sector_by_ticker,
                    as_of=as_of,
                )
            )

    for pair in published:
        if pair.candidate.row in locus_tickers or pair.candidate.col in locus_tickers:
            continue
        blocks.append(_build_pair_block(pair, params=params, as_of=as_of))
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
    sector_by_ticker: Mapping[str, str] | None = None,
) -> list[OutputBlock]:
    """Pure compute of correlation-breakdown / dispersion / narrative-lag blocks.

    Operates entirely over pre-loaded long-window log-return series. The
    short-window slice is the tail of the long-window series.

    The narrative-lag flag relies on the loader-resolved
    ``qualifying_news_present`` flag — the loader does the
    ``news_articles`` scan and threads the result through here.

    ``sector_by_ticker`` is consumed only by the locus-aggregation pass.
    When provided, each locus block's payload groups its partner tickers
    by sector and surfaces a ``cross_sector_spread`` summary; when
    :data:`None`, the locus payload carries the flat partner list only.
    """
    short_returns: dict[str, list[float]] = {
        ticker: list(returns)[-params.short_window_days :]
        for ticker, returns in long_returns_by_ticker.items()
    }
    breakdown_blocks = _correlation_breakdown_blocks(
        short_returns=short_returns,
        long_returns=long_returns_by_ticker,
        params=params,
        sector_by_ticker=sector_by_ticker,
        as_of=as_of,
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
