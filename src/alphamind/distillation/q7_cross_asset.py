"""Q7 cross-asset and correlation computations — story 02-distillation-layer/08d.

Implements the deterministic cross-asset and correlation computations from
``docs/design/02-distillation-layer/external.md`` § 2 From cross-asset and
correlation (quant 7):

- intra-sector pairwise correlation matrices and divergence detection,
- cross-sector rotation classification with narrative tagging,
- breadth and market internals,
- intermarket regime signals,
- lead-lag relationships with overdue and inversion flags,
- correlation regime change detection with the narrative-lag indicator.

Q7 has no raw data of its own — every signal is derived from Q1-Q6 and Q8
inputs persisted by the collector. The functions below read from
``ohlcv_bars``, ``macro_observations``, ``news_articles``,
``distillation_pair_lag``, and ``distillation_event_history``; they emit
:class:`OutputBlock` envelopes per story 05.

Reference docs:

- ``docs/design/02-distillation-layer/external.md`` § 2 quant 7 — authoritative
  scope for every computation here.
- ``docs/design/02-distillation-layer/threshold-calibration.md``
  § Lead-lag and narrative-lag — the configurable sigma multiples.
- ``docs/design/02-distillation-layer/threshold-calibration.md``
  § Persistence and percentile windows — ``correlation_short_days`` and
  ``correlation_long_days``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationEventHistory,
    DistillationPairLag,
    MacroObservations,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# Block-id namespace and audience helpers
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
# Correlation primitive
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


def _select_close_series(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> list[float]:
    """Return ascending daily-bar adj_close values for ``ticker`` in the window."""
    stmt = (
        select(OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    return [float(v) for v in session.execute(stmt).scalars().all()]


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


# ---------------------------------------------------------------------------
# Intra-sector correlation
# ---------------------------------------------------------------------------


def _calibration_for_window(
    *,
    n_observations: int,
    required: int,
    input_name: str,
) -> tuple[CalibrationState, str | None]:
    """Decide ``(state, bootstrap_reason)`` for a per-window correlation block.

    A correlation matrix's calibration is gated on having at least
    ``required`` daily returns inside the window. Below that, the matrix is
    still computed for visibility but tagged ``bootstrap`` so domain
    researchers weight the percentile read accordingly.
    """
    if n_observations >= required:
        return CalibrationState.CALIBRATED, None
    return (
        CalibrationState.BOOTSTRAP,
        f"{input_name}: {n_observations} < {required}",
    )


def _persist_correlation_divergence_events(
    session: Session,
    *,
    flags: Sequence[AnomalyFlag],
    as_of: datetime,
) -> None:
    """Persist one ``correlation_divergence`` event row per pair-divergence flag.

    The event row's ``ticker`` carries the lead leg of the pair and
    ``direction`` carries the lag leg, so the (lead, kind, ts) primary key
    remains unique. ``magnitude_atr_multiple`` carries the divergence
    z-score (re-using the column for visibility — the column is generic
    enough that the q7 reader treats it as the divergence magnitude).

    Insertion is idempotent: rows already present at the same
    ``(ticker, event_kind, event_ts)`` triple are not duplicated.
    """
    as_of_iso = _format_iso_utc(as_of)
    for flag in flags:
        # Flag name shape: ``intra_sector_correlation_divergence:<lead>:<lag>``.
        try:
            _prefix, lead, lag = flag.name.split(":")
        except ValueError:
            continue
        existing = session.execute(
            select(DistillationEventHistory).where(
                DistillationEventHistory.ticker == lead,
                DistillationEventHistory.event_kind == "correlation_divergence",
                DistillationEventHistory.event_ts == as_of_iso,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            DistillationEventHistory(
                ticker=lead,
                event_kind="correlation_divergence",
                event_ts=as_of_iso,
                direction=lag,
                magnitude_atr_multiple=float(flag.magnitude),
                outcome="pending",
                outcome_observed_at=None,
                ingested_at=as_of_iso,
            )
        )


def compute_intra_sector_correlation(
    session: Session,
    *,
    sector: str,
    sector_tickers: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
) -> list[OutputBlock]:
    """Compute the per-sector intra-sector correlation block.

    For ``sector`` with ``sector_tickers`` returns one
    :class:`OutputBlock` whose payload carries:

    - ``short_window.correlation_matrix`` — pairwise short-window matrix.
    - ``long_window.correlation_matrix`` — pairwise long-window matrix.
    - ``short_window.window_days`` / ``long_window.window_days`` —
      window sizes for the audit trail.

    Pair-level divergence detection is attached as
    :class:`AnomalyFlag` instances on the block: a pair fires when the
    short-window correlation deviates from the long-window correlation by
    at least ``divergence_sigma`` multiples of the standard deviation of
    the long-window correlations. Each fired flag also writes a
    ``correlation_divergence`` event row to ``distillation_event_history``
    for resolution lookup on subsequent invocations.
    """
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)

    short_returns: dict[str, Sequence[float]] = {}
    long_returns: dict[str, Sequence[float]] = {}
    short_n_min = math.inf
    long_n_min = math.inf
    for ticker in sector_tickers:
        # One query per ticker: read the full long-window close series and
        # slice the short window from its tail. Avoids N redundant queries
        # for an otherwise overlapping range.
        long_closes = _select_close_series(
            session,
            ticker=ticker,
            range_start=long_start,
            range_end=range_end,
        )
        long_returns[ticker] = _log_returns_from_closes(long_closes)
        short_returns[ticker] = list(long_returns[ticker])[-short_window_days:]
        short_n_min = min(short_n_min, len(short_returns[ticker]))
        long_n_min = min(long_n_min, len(long_returns[ticker]))

    short_matrix = _correlation_matrix(short_returns)
    long_matrix = _correlation_matrix(long_returns)

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

    _persist_correlation_divergence_events(session, flags=flags, as_of=as_of)
    session.flush()

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
    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intra_sector_correlation.{sector}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=tuple(flags),
        regime_context=None,
    )
    return [block]


def _detect_pair_divergence(
    *,
    short_matrix: dict[str, dict[str, float]],
    long_matrix: dict[str, dict[str, float]],
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


# ---------------------------------------------------------------------------
# Cross-sector rotation
# ---------------------------------------------------------------------------


VELOCITY_SLOW = "slow_regime_shift"
"""Rotation classified as gradual cross-sector ranking change over multiple days."""

VELOCITY_SHARP = "sharp_intraday_event_driven"
"""Rotation classified as a single-session event moving multiple sectors past each other."""

ROTATION_NARRATIVE_RATE_DRIVEN = "rate_driven"
"""Financials vs. tech is the dominant cross-sector relative move."""

ROTATION_NARRATIVE_GROWTH_DRIVEN = "growth_driven"
"""Cyclicals vs. defensives (energy vs. tech) is the dominant relative move."""

ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN = "risk_appetite_driven"
"""High-beta vs. low-beta (small-cap vs. broad market) is the dominant relative move."""

# Single-session magnitude (in returns terms) above which a cross-sector move
# is treated as a sharp intraday event rather than a slow regime shift. Story
# 08d describes this discriminant qualitatively; numerically we treat any
# last-day move whose magnitude exceeds 4% as sharp. The constant is named so
# the rule is searchable and not a magic number scattered across the file.
_SHARP_INTRADAY_RETURN_THRESHOLD: float = 0.04


def _ratio_change(numer: list[float], denom: list[float]) -> float:
    """Return the percentage change of ``numer / denom`` between window edges.

    Window edges are the first and last entries of each series. Returns
    ``0.0`` when either edge is non-positive (a degenerate path the caller
    treats as no relative movement).
    """
    if not numer or not denom:
        return 0.0
    if numer[0] <= 0.0 or denom[0] <= 0.0:
        return 0.0
    if numer[-1] <= 0.0 or denom[-1] <= 0.0:
        return 0.0
    start = numer[0] / denom[0]
    end = numer[-1] / denom[-1]
    if start == 0.0:
        return 0.0
    return (end - start) / start


def _classify_velocity(
    *,
    closes_by_etf: dict[str, list[float]],
) -> str:
    """Classify rotation velocity as slow regime shift vs. sharp intraday event.

    Looks at the last single-day return for each ETF. If at least two ETFs
    move more than :data:`_SHARP_INTRADAY_RETURN_THRESHOLD` in opposite
    directions on the final session, the rotation is sharp; otherwise slow.
    """
    last_returns: list[float] = []
    for closes in closes_by_etf.values():
        # ``pairwise`` yields nothing when ``closes`` has fewer than two
        # elements; the last pair (if any) gives the latest day-over-day
        # return.
        last_pair: tuple[float, float] | None = None
        for prev, curr in pairwise(closes):
            last_pair = (prev, curr)
        if last_pair is None:
            continue
        prev, curr = last_pair
        if prev <= 0.0:
            continue
        last_returns.append((curr - prev) / prev)
    if not last_returns:
        return VELOCITY_SLOW
    big_up = any(r > _SHARP_INTRADAY_RETURN_THRESHOLD for r in last_returns)
    big_down = any(r < -_SHARP_INTRADAY_RETURN_THRESHOLD for r in last_returns)
    if big_up and big_down:
        return VELOCITY_SHARP
    return VELOCITY_SLOW


# Narrative-classification proxy pairs. Each entry maps a narrative label to
# a ``(numerator_etf, denominator_etf)`` tuple whose relative-strength move
# over the trailing short window represents the strength of that narrative.
# Per story 08d:
#   - rate_driven: financials (XLF) vs. tech (XLK)
#   - growth_driven: energy (XLE) vs. tech (XLK)  [cyclicals vs. defensives proxy]
#   - risk_appetite_driven: small-cap (IWM) vs. broad market (SPY)
_NARRATIVE_PAIRS: tuple[tuple[str, tuple[str, str]], ...] = (
    (ROTATION_NARRATIVE_RATE_DRIVEN, ("XLF", "XLK")),
    (ROTATION_NARRATIVE_GROWTH_DRIVEN, ("XLE", "XLK")),
    (ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN, ("IWM", "SPY")),
)


def _classify_narrative(
    *,
    closes_by_etf: dict[str, list[float]],
) -> str:
    """Tag the dominant rotation narrative.

    Returns the narrative whose proxy-pair relative-strength move (absolute
    magnitude) is largest over the available window. A pair whose tickers are
    missing from ``closes_by_etf`` contributes magnitude ``0`` (i.e. cannot
    win); when no pair has data, returns the first label so the output block
    still carries a deterministic value.
    """
    best_label = _NARRATIVE_PAIRS[0][0]
    best_magnitude = -1.0
    for label, (numer_ticker, denom_ticker) in _NARRATIVE_PAIRS:
        numer = closes_by_etf.get(numer_ticker, [])
        denom = closes_by_etf.get(denom_ticker, [])
        change = abs(_ratio_change(numer, denom))
        if change > best_magnitude:
            best_magnitude = change
            best_label = label
    return best_label


def _compute_relative_strength_table(
    closes_by_etf: dict[str, list[float]],
) -> dict[str, dict[str, float]]:
    """Per-ETF percentage move over the loaded window.

    Result keys are sorted ETF tickers; each value is ``{"return": <pct>}``.
    """
    table: dict[str, dict[str, float]] = {}
    for ticker in sorted(closes_by_etf):
        closes = closes_by_etf[ticker]
        # ``pairwise`` is empty when ``closes`` has fewer than two entries;
        # treat that as zero relative move so the block still emits.
        if not list(pairwise(closes)) or closes[0] <= 0.0:
            table[ticker] = {"return": 0.0}
            continue
        ret = (closes[-1] - closes[0]) / closes[0]
        table[ticker] = {"return": ret}
    return table


def compute_cross_sector_rotation(
    session: Session,
    *,
    sector_etfs: Sequence[str],
    risk_proxies: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Compute the cross-sector rotation block.

    For ``sector_etfs`` plus ``risk_proxies`` returns one
    :class:`OutputBlock` whose payload carries:

    - ``short_window.relative_performance`` / ``long_window.relative_performance``
      — per-ETF percentage move over each window (sorted by ticker).
    - ``velocity_label`` — :data:`VELOCITY_SLOW` or :data:`VELOCITY_SHARP`.
    - ``narrative_label`` — :data:`ROTATION_NARRATIVE_RATE_DRIVEN`,
      :data:`ROTATION_NARRATIVE_GROWTH_DRIVEN`, or
      :data:`ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN`.

    The block routes to :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    """
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)

    all_etfs = tuple(sector_etfs) + tuple(risk_proxies)
    short_closes: dict[str, list[float]] = {}
    long_closes: dict[str, list[float]] = {}
    for etf in all_etfs:
        # One query per ETF: read the long-window series and slice the
        # short window from its tail. Avoids redundant range queries.
        long_closes[etf] = _select_close_series(
            session, ticker=etf, range_start=long_start, range_end=range_end
        )
        short_closes[etf] = long_closes[etf][-short_window_days:]

    velocity = _classify_velocity(closes_by_etf=short_closes)
    narrative = _classify_narrative(closes_by_etf=short_closes)

    short_table = _compute_relative_strength_table(short_closes)
    long_table = _compute_relative_strength_table(long_closes)

    n_observed = min(
        (len(closes) for closes in short_closes.values() if closes),
        default=0,
    )
    state, reason = _calibration_for_window(
        n_observations=n_observed,
        required=short_window_days,
        input_name="cross_sector_short_window_observations",
    )

    payload = {
        "short_window": {
            "relative_performance": short_table,
            "window_days": short_window_days,
        },
        "long_window": {
            "relative_performance": long_table,
            "window_days": long_window_days,
        },
        "velocity_label": velocity,
        "narrative_label": narrative,
    }
    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.cross_sector_rotation",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )
    return [block]


# ---------------------------------------------------------------------------
# Breadth and market internals
# ---------------------------------------------------------------------------


# Standard EMA windows for market-internals breadth checks. Story 08d names
# 20 / 50 / 200 day EMAs; the trio is a structural convention rather than a
# tunable threshold so the values are encoded as named constants in the
# code rather than as configuration.
EMA_WINDOWS_DAYS: tuple[int, ...] = (20, 50, 200)


def _ema(closes: Sequence[float], window: int) -> float:
    """Exponential moving average over ``closes`` with span ``window``.

    Uses the standard EMA recurrence ``ema_t = alpha * close_t + (1 - alpha) * ema_{t-1}``
    seeded with the simple mean of the first ``window`` closes. When the
    series is shorter than ``window``, falls back to the simple mean of
    available closes (so the EMA value is always defined while the caller
    can flag the calibration state separately).
    """
    if not closes:
        return 0.0
    if len(closes) <= window:
        return float(sum(closes)) / float(len(closes))
    seed = sum(closes[:window]) / window
    alpha = 2.0 / (window + 1.0)
    ema_value = seed
    for close in closes[window:]:
        ema_value = alpha * close + (1.0 - alpha) * ema_value
    return float(ema_value)


def _pct_above_ema(closes_by_ticker: dict[str, list[float]], window: int) -> float:
    """Fraction of tickers whose latest close is above their ``window``-day EMA.

    Returns 0.0 when no ticker has any closes (the empty universe edge
    case); the caller should still emit the block so consumers see the
    calibration tag.
    """
    above = 0
    total = 0
    for closes in closes_by_ticker.values():
        if not closes:
            continue
        total += 1
        ema_value = _ema(closes, window)
        if closes[-1] > ema_value:
            above += 1
    if total == 0:
        return 0.0
    return float(above) / float(total)


def _last_two_closes(closes: list[float]) -> tuple[float, float] | None:
    """Return the final ``(prev, curr)`` close pair, or ``None`` if unavailable."""
    last_pair: tuple[float, float] | None = None
    for prev, curr in pairwise(closes):
        last_pair = (prev, curr)
    return last_pair


def _last_day_return(closes: list[float]) -> float | None:
    """Day-over-day return on the most recent bar, or ``None`` when unavailable."""
    pair = _last_two_closes(closes)
    if pair is None:
        return None
    prev, curr = pair
    if prev <= 0.0:
        return None
    return (curr - prev) / prev


def _advance_decline_per_sector(
    closes_by_ticker: dict[str, list[float]],
    sector_members: dict[str, Sequence[str]],
) -> dict[str, dict[str, int]]:
    """Per-sector ``{advances, declines}`` dict, sorted by sector key."""
    out: dict[str, dict[str, int]] = {}
    for sector in sorted(sector_members):
        advances = 0
        declines = 0
        for ticker in sector_members[sector]:
            ret = _last_day_return(closes_by_ticker.get(ticker, []))
            if ret is None:
                continue
            if ret > 0.0:
                advances += 1
            elif ret < 0.0:
                declines += 1
        out[sector] = {"advances": advances, "declines": declines}
    return out


def _equal_vs_cap_weight(
    closes_by_ticker: dict[str, list[float]],
    universe_tickers: Sequence[str],
    broad_market_closes: list[float],
) -> dict[str, float]:
    """Equal-weight portfolio return vs. broad-market ETF return on the day."""
    universe_returns: list[float] = []
    for ticker in universe_tickers:
        ret = _last_day_return(closes_by_ticker.get(ticker, []))
        if ret is None:
            continue
        universe_returns.append(ret)
    equal_weight_return = sum(universe_returns) / len(universe_returns) if universe_returns else 0.0
    cap_weight_return = _last_day_return(broad_market_closes) or 0.0
    return {
        "equal_weight_return": equal_weight_return,
        "cap_weight_return": cap_weight_return,
        "spread": equal_weight_return - cap_weight_return,
    }


def compute_breadth_internals(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    sectors: Sequence[str],
    sector_members: dict[str, Sequence[str]],
    broad_market_etf: str,
    as_of: datetime,
) -> list[OutputBlock]:
    """Compute the breadth-and-internals block.

    Payload carries:

    - ``pct_above_<n>d_ema`` for each window in :data:`EMA_WINDOWS_DAYS`.
    - ``advance_decline_per_sector`` — ``{sector: {advances, declines}}``.
    - ``equal_vs_cap_weight`` — equal-weight portfolio return vs. the
      ``broad_market_etf`` return on the latest day.

    The block carries both :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`
    and :attr:`OutputAudience.UNIVERSAL_BROADCAST` per the story scope —
    breadth is a universal cross-cutting indicator.
    """
    long_window = max(EMA_WINDOWS_DAYS)
    range_start, range_end = _window_bounds(as_of=as_of, window_days=long_window)

    closes_by_ticker: dict[str, list[float]] = {}
    for ticker in universe_tickers:
        closes_by_ticker[ticker] = _select_close_series(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=range_end,
        )
    broad_market_closes = _select_close_series(
        session,
        ticker=broad_market_etf,
        range_start=range_start,
        range_end=range_end,
    )

    payload: dict[str, object] = {}
    for window in EMA_WINDOWS_DAYS:
        payload[f"pct_above_{window}d_ema"] = _pct_above_ema(closes_by_ticker, window)
    # ``sectors`` is informational; ``sector_members`` drives the actual
    # advance/decline counts and is what the breadth payload exposes.
    _ = sectors
    payload["advance_decline_per_sector"] = _advance_decline_per_sector(
        closes_by_ticker, dict(sector_members)
    )
    payload["equal_vs_cap_weight"] = _equal_vs_cap_weight(
        closes_by_ticker, universe_tickers, broad_market_closes
    )

    n_observed = min(
        (len(closes) for closes in closes_by_ticker.values() if closes),
        default=0,
    )
    state, reason = _calibration_for_window(
        n_observations=n_observed,
        required=long_window,
        input_name="breadth_long_ema_observations",
    )

    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.breadth_internals",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )
    return [block]


# ---------------------------------------------------------------------------
# Intermarket regime signals
# ---------------------------------------------------------------------------


# Default series identifiers for the four intermarket relationships. Each
# constant names the canonical source/series tuple per the data-layer
# mappings (FRED, EIA). The names are encoded here rather than passed by the
# caller because every Q7 invocation reads the same series; story 12's
# orchestrator wiring needs no knob for this.
SPY_TICKER = "SPY"
TLT_TICKER = "TLT"
GLD_TICKER = "GLD"
XLE_TICKER = "XLE"

REAL_YIELD_SOURCE = "FRED"
REAL_YIELD_SERIES = "DFII10"
"""10-year TIPS yield from FRED — the real-yield series quant 7d cites."""

VIX_SOURCE = "FRED"
VIX_SERIES = "VIXCLS"
"""VIX close from FRED — the volatility-index series intermarket monitors."""

OIL_SOURCE = "EIA"
OIL_SERIES = "DCOILWTICO"
"""WTI crude price series from EIA — used as the oil leg of oil-vs-XLE beta."""

# Beta-drift threshold in absolute units (story 08d): "flag when beta moves
# > 0.5 from 60-day mean". Encoded as a named constant so the rule is
# discoverable; not a tunable Class A threshold.
_OIL_XLE_BETA_DRIFT_THRESHOLD: float = 0.5

# Threshold (in correlation deviation units) above which the gold/real-yield
# correlation is considered to diverge from the textbook negative-correlation
# regime. The textbook expectation is ``rho ≈ -1``; once the realized
# correlation rises above ``-rho_drift_threshold`` (i.e., loses its negative
# sign) the divergence fires. Encoded as a named constant rather than a
# magic literal in the detection code.
_GOLD_REAL_YIELDS_DIVERGENCE_THRESHOLD: float = 0.0
"""Realized GLD vs. DFII10 correlation above this threshold fires divergence.

The textbook intermarket relationship is negatively correlated; a realized
correlation that has flipped non-negative is a divergence regardless of its
absolute magnitude.
"""


def _select_macro_series(
    session: Session,
    *,
    source: str,
    series_id: str,
    range_start_date: str,
    range_end_date: str,
) -> list[float]:
    """Return ascending non-null macro values in a date window."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.source == source,
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date >= range_start_date,
            MacroObservations.observation_date <= range_end_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _spy_tlt_regime_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    window_days: int,
) -> OutputBlock:
    """Stocks-vs-bonds regime: positive (inflation) vs. negative (growth)."""
    spy_closes = _select_close_series(
        session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
    )
    tlt_closes = _select_close_series(
        session, ticker=TLT_TICKER, range_start=range_start, range_end=range_end
    )
    spy_returns = _log_returns_from_closes(spy_closes)
    tlt_returns = _log_returns_from_closes(tlt_closes)
    correlation = _pearson_correlation(spy_returns, tlt_returns)
    regime_label = "inflation_environment" if correlation > 0.0 else "growth_environment"
    state, reason = _calibration_for_window(
        n_observations=min(len(spy_returns), len(tlt_returns)),
        required=window_days,
        input_name="spy_tlt_correlation_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.spy_tlt",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "regime_label": regime_label,
            "window_days": window_days,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _gld_real_yields_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
) -> OutputBlock:
    """Gold vs. real-yields divergence detection."""
    gld_closes = _select_close_series(
        session, ticker=GLD_TICKER, range_start=range_start, range_end=range_end
    )
    real_yields = _select_macro_series(
        session,
        source=REAL_YIELD_SOURCE,
        series_id=REAL_YIELD_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    gld_returns = _log_returns_from_closes(gld_closes)
    # Real-yield deltas (level changes in basis points) drive the
    # correlation; convert to a per-day delta sequence aligned in length to
    # ``gld_returns``.
    yield_deltas: list[float] = [b - a for a, b in pairwise(real_yields)]
    n = min(len(gld_returns), len(yield_deltas))
    correlation = _pearson_correlation(gld_returns[-n:], yield_deltas[-n:])

    flags: list[AnomalyFlag] = []
    # Textbook regime is negative correlation; a realized correlation above
    # the threshold has flipped sign and counts as divergence.
    if correlation > _GOLD_REAL_YIELDS_DIVERGENCE_THRESHOLD:
        flags.append(
            AnomalyFlag(
                name="gold_real_yields_divergence",
                magnitude=abs(correlation),
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="gld_real_yields_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.gld_real_yields",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def _rolling_beta(
    *,
    underlying_returns: Sequence[float],
    factor_returns: Sequence[float],
) -> float:
    """OLS slope of ``underlying`` regressed on ``factor`` (single-factor beta).

    Returns ``0.0`` when the factor has zero variance.
    """
    n = min(len(underlying_returns), len(factor_returns))
    if n == 0:
        return 0.0
    u = list(underlying_returns)[-n:]
    f = list(factor_returns)[-n:]
    mean_u = sum(u) / n
    mean_f = sum(f) / n
    cov = sum((ui - mean_u) * (fi - mean_f) for ui, fi in zip(u, f, strict=True))
    var = sum((fi - mean_f) * (fi - mean_f) for fi in f)
    if var == 0.0:
        return 0.0
    return float(cov / var)


def _oil_xle_beta_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
    short_window_days: int,
) -> OutputBlock:
    """Oil vs. XLE beta stability: fire when beta drifts >0.5 from baseline."""
    xle_closes = _select_close_series(
        session, ticker=XLE_TICKER, range_start=range_start, range_end=range_end
    )
    oil_values = _select_macro_series(
        session,
        source=OIL_SOURCE,
        series_id=OIL_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    xle_returns = _log_returns_from_closes(xle_closes)
    oil_returns = _log_returns_from_closes(oil_values)
    long_beta = _rolling_beta(underlying_returns=xle_returns, factor_returns=oil_returns)
    # Recent beta — the trailing ``short_window_days`` returns. The split
    # makes a regime flip detectable distinctly from the long baseline.
    short_n = min(short_window_days, len(xle_returns))
    short_beta = _rolling_beta(
        underlying_returns=xle_returns[-short_n:],
        factor_returns=oil_returns[-short_n:],
    )
    drift = abs(short_beta - long_beta)

    flags: list[AnomalyFlag] = []
    if drift > _OIL_XLE_BETA_DRIFT_THRESHOLD:
        flags.append(
            AnomalyFlag(
                name="oil_xle_beta_drift",
                magnitude=drift,
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=min(len(xle_returns), len(oil_returns)),
        required=window_days,
        input_name="oil_xle_beta_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.oil_xle_beta",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "long_beta": long_beta,
            "short_beta": short_beta,
            "beta_drift": drift,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def _vix_spy_block(
    session: Session,
    *,
    as_of: datetime,
    range_start: str,
    range_end: str,
    range_start_date: str,
    range_end_date: str,
    window_days: int,
) -> OutputBlock:
    """VIX vs. SPY divergence: VIX rising on flat or rising market."""
    spy_closes = _select_close_series(
        session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
    )
    vix_values = _select_macro_series(
        session,
        source=VIX_SOURCE,
        series_id=VIX_SERIES,
        range_start_date=range_start_date,
        range_end_date=range_end_date,
    )
    spy_returns = _log_returns_from_closes(spy_closes)
    # VIX is a level series; compute day-over-day deltas as the "return".
    vix_deltas = [b - a for a, b in pairwise(vix_values)]
    n = min(len(spy_returns), len(vix_deltas))
    correlation = _pearson_correlation(spy_returns[-n:], vix_deltas[-n:])

    flags: list[AnomalyFlag] = []
    # Standard regime: VIX falls when SPY rises (negative correlation). When
    # the realized correlation flips non-negative, divergence fires.
    if correlation > 0.0:
        flags.append(
            AnomalyFlag(
                name="vix_spy_divergence",
                magnitude=abs(correlation),
                severity="investigate_if_persists",
            )
        )

    state, reason = _calibration_for_window(
        n_observations=n,
        required=window_days,
        input_name="vix_spy_observations",
    )
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intermarket_regime.vix_spy",
        audience=frozenset(
            {
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload={
            "correlation": correlation,
            "window_days": window_days,
        },
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def compute_intermarket_regime(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
    short_window_days: int,
) -> list[OutputBlock]:
    """Compute the four intermarket regime blocks.

    Returns one :class:`OutputBlock` per relationship:

    - ``q7.intermarket_regime.spy_tlt``
    - ``q7.intermarket_regime.gld_real_yields``
    - ``q7.intermarket_regime.oil_xle_beta``
    - ``q7.intermarket_regime.vix_spy``

    Each block carries both
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF` and
    :attr:`OutputAudience.UNIVERSAL_BROADCAST`.
    """
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    range_start_date = (as_of - timedelta(days=window_days)).strftime("%Y-%m-%d")
    range_end_date = as_of.strftime("%Y-%m-%d")
    return [
        _gld_real_yields_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
        ),
        _oil_xle_beta_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
            short_window_days=short_window_days,
        ),
        _spy_tlt_regime_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            window_days=window_days,
        ),
        _vix_spy_block(
            session,
            as_of=as_of,
            range_start=range_start,
            range_end=range_end,
            range_start_date=range_start_date,
            range_end_date=range_end_date,
            window_days=window_days,
        ),
    ]


# ---------------------------------------------------------------------------
# Lead-lag relationships
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadLagPair:
    """Configuration for one lead-lag pair the layer monitors.

    The five named pairs from
    ``docs/design/02-distillation-layer/threshold-calibration.md`` § Lead-lag
    each map to a :class:`LeadLagPair`. ``pair_key`` is the named identifier
    (``"semis_to_tech"`` etc.), ``lead_ticker`` and ``lag_ticker`` are the
    OHLCV-bar tickers driving each leg, and ``max_days`` is the per-pair
    ``_max_days`` bound from the configuration.
    """

    pair_key: str
    lead_ticker: str
    lag_ticker: str
    max_days: int


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


def _select_recent_returns_window(
    session: Session,
    *,
    ticker: str,
    as_of: datetime,
    window_days: int,
) -> list[float]:
    """Return ``window_days`` ascending day-over-day percentage returns up to ``as_of``."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    closes = _select_close_series(
        session, ticker=ticker, range_start=range_start, range_end=range_end
    )
    return [(later - earlier) / earlier for earlier, later in pairwise(closes) if earlier > 0.0]


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


def _detect_overdue_and_inversion(
    *,
    pair: LeadLagPair,
    lead_returns: list[float],
    lag_returns: list[float],
    overdue_lead_sigma: float,
) -> list[AnomalyFlag]:
    """Emit overdue and inversion flags for one lead-lag pair.

    Per ``external.md`` § quant 7f and story 08d:

    - **Overdue lag**: the most recent lead return has z-score (against its
      trailing distribution) at or above ``overdue_lead_sigma`` AND the
      lag has not tracked within the pair's ``max_days`` window.
    - **Inversion (regime-shift)**: the named "lag" asset moves first —
      the normal leader/follower has flipped. Detected by comparing
      forward-direction (lead→lag) and reverse-direction (lag→lead)
      lagged correlation across the recent window; the structural
      comparison uses every observation rather than a single argmax bar
      so transient noise does not flip the verdict. Magnitude gates on
      both legs require both sides to clear ``overdue_lead_sigma`` so
      the inversion is meaningful, not noise.
    """
    flags: list[AnomalyFlag] = []
    if not lead_returns or not lag_returns:
        return flags

    # The lead's "today" move is the most recent return.
    lead_latest = lead_returns[-1]
    lead_zscore = abs(_zscore(lead_latest, lead_returns[:-1]))

    # Tracking window: lag's most recent ``max_days`` returns.
    lag_window = lag_returns[-pair.max_days :]
    lag_max_magnitude = max((abs(r) for r in lag_window), default=0.0)
    # A lag has "tracked" when its magnitude in the window is comparable to
    # the lead's. The simple comparison: lag's max abs return is at least
    # half the lead's. Encoded as a named ratio rather than a magic literal.
    tracked = lag_max_magnitude >= _LAG_TRACKING_RATIO * abs(lead_latest)

    if lead_zscore >= overdue_lead_sigma and not tracked:
        flags.append(
            AnomalyFlag(
                name=f"overdue_lag_flag:{pair.pair_key}",
                magnitude=lead_zscore,
                severity="investigate_now",
            )
        )

    # Inversion: forward (lead→lag) vs. reverse (lag→lead) lagged
    # correlation across the recent window. When the reverse direction
    # strictly dominates, the named "lag" leads the named "lead". The
    # magnitude gate on both legs keeps single-bar coincidences from
    # firing the regime-shift signal.
    n = min(len(lead_returns), len(lag_returns), pair.max_days + 1)
    if n >= 2:
        lead_recent = lead_returns[-n:]
        lag_recent = lag_returns[-n:]
        forward_corr = _max_lagged_correlation(
            leading_returns=lead_recent,
            following_returns=lag_recent,
            max_lag_days=pair.max_days,
        )
        reverse_corr = _max_lagged_correlation(
            leading_returns=lag_recent,
            following_returns=lead_recent,
            max_lag_days=pair.max_days,
        )
        lag_max = max(lag_recent, key=lambda r: abs(r))
        lag_zscore = abs(_zscore(lag_max, lag_returns[:-1]))
        if (
            reverse_corr > forward_corr
            and lag_zscore >= overdue_lead_sigma
            and lead_zscore >= overdue_lead_sigma
        ):
            flags.append(
                AnomalyFlag(
                    name=f"lead_lag_inversion_flag:{pair.pair_key}",
                    magnitude=lag_zscore,
                    severity="investigate_now",
                )
            )

    return flags


# Ratio at which a lag asset is considered to have "tracked" the lead. A
# tracking lag's max-magnitude move within the pair window is at least this
# fraction of the lead's most recent return (in absolute units). 0.5 is a
# common "half-tracked" rule of thumb; not a tunable Class A threshold.
_LAG_TRACKING_RATIO: float = 0.5


# Default trailing window over which the per-pair z-score distribution for
# lead-lag overdue / inversion detection is computed. Story 08d does not
# specify a calibrated value; a quarter (~120 calendar days) is conventional
# for short-term lead-lag monitoring. Caller may override via
# ``lookback_window_days``.
LEAD_LAG_LOOKBACK_DEFAULT_DAYS: int = 120


def _select_pair_lag_estimate(
    session: Session,
    *,
    lead: str,
    lag: str,
    as_of: datetime,
) -> tuple[float, int, str] | None:
    """Return the most recent persisted ``(lag_days, n_events, state)`` triple."""
    as_of_str = _format_iso_utc(as_of)
    stmt = (
        select(
            DistillationPairLag.lead_lag_days_estimate,
            DistillationPairLag.n_pair_events,
            DistillationPairLag.calibration_state,
        )
        .where(
            DistillationPairLag.lead_ticker == lead,
            DistillationPairLag.lag_ticker == lag,
            DistillationPairLag.as_of <= as_of_str,
        )
        .order_by(DistillationPairLag.as_of.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    estimate, n_events, state = row
    return float(estimate), int(n_events), str(state)


def compute_lead_lag(
    session: Session,
    *,
    pairs: Sequence[LeadLagPair],
    as_of: datetime,
    overdue_lead_sigma: float,
    lookback_window_days: int = LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
) -> list[OutputBlock]:
    """Compute per-pair lead-lag overdue and inversion flags.

    For each :class:`LeadLagPair` in ``pairs``:

    1. Read the persisted ``distillation_pair_lag`` row to surface the
       calibrated lead-lag estimate.
    2. Read the recent return windows for the lead and lag tickers.
    3. Emit the ``overdue_lag_flag`` and ``lead_lag_inversion_flag`` flags
       when the documented conditions are met.

    ``lookback_window_days`` is the trailing window over which the per-pair
    z-score distribution is computed; pairs with a longer ``max_days`` need
    proportionally more lookback to keep the distribution stable.

    Returns one :class:`OutputBlock` per pair, addressed to
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    """
    blocks: list[OutputBlock] = []
    for pair in pairs:
        # Read returns over the long-window cutoff so the trailing
        # distribution carries enough mass for a stable z-score.
        recent_lead = _select_recent_returns_window(
            session,
            ticker=pair.lead_ticker,
            as_of=as_of,
            window_days=max(lookback_window_days, pair.max_days + 1),
        )
        recent_lag = _select_recent_returns_window(
            session,
            ticker=pair.lag_ticker,
            as_of=as_of,
            window_days=max(lookback_window_days, pair.max_days + 1),
        )
        flags = _detect_overdue_and_inversion(
            pair=pair,
            lead_returns=recent_lead,
            lag_returns=recent_lag,
            overdue_lead_sigma=overdue_lead_sigma,
        )

        persisted = _select_pair_lag_estimate(
            session, lead=pair.lead_ticker, lag=pair.lag_ticker, as_of=as_of
        )
        if persisted is None:
            estimate_days = float(pair.max_days)
            n_pair_events = 0
            state = CalibrationState.BOOTSTRAP
            reason: str | None = f"pair_lag_history_missing:{pair.pair_key}: 0 < 1"
        else:
            estimate_days, n_pair_events, state_str = persisted
            state = CalibrationState(state_str)
            reason = (
                None
                if state is CalibrationState.CALIBRATED
                else f"pair_lag_calibration:{pair.pair_key}: state={state_str}"
            )

        payload = {
            "pair_lag": {
                pair.pair_key: {
                    "lead_ticker": pair.lead_ticker,
                    "lag_ticker": pair.lag_ticker,
                    "lead_lag_days_estimate": estimate_days,
                    "n_pair_events": n_pair_events,
                    "max_days": pair.max_days,
                }
            },
        }

        blocks.append(
            OutputBlock(
                block_id=f"{_BLOCK_NAMESPACE}.lead_lag.{pair.pair_key}",
                audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
                freshness_ts=as_of,
                calibration_state=state,
                bootstrap_reason=reason,
                payload=payload,
                anomaly_flags=tuple(flags),
                regime_context=None,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Correlation regime change: breakdown / dispersion / narrative-lag
# ---------------------------------------------------------------------------


# Topic-tag set defining "regime-relevant" media coverage. The narrative-lag
# indicator is gated on news articles whose ``topic_tags`` overlap with this
# set so routine company news doesn't drown out the silence signal — see the
# story 08d notes on filtering ``news_articles`` for narrative pickup.
NARRATIVE_LAG_REGIME_TAGS: frozenset[str] = frozenset(
    {"macro_data", "regulatory", "geopolitical", "sector_rotation"}
)


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


def _rolling_pair_correlations(
    *,
    a_returns: Sequence[float],
    b_returns: Sequence[float],
    window: int,
) -> list[float]:
    """Sliding-window pair correlations across a returns history.

    The output has length ``len(a_returns) - window + 1``, one correlation
    per window position. Returns ``[]`` when the input is shorter than
    ``window``.
    """
    n = min(len(a_returns), len(b_returns))
    if n < window:
        return []
    out: list[float] = []
    for start in range(n - window + 1):
        out.append(
            _pearson_correlation(
                list(a_returns)[start : start + window],
                list(b_returns)[start : start + window],
            )
        )
    return out


def _correlation_breakdown_blocks(
    *,
    short_returns: dict[str, list[float]],
    long_returns: dict[str, list[float]],
    correlation_shift_sigma: float,
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Emit one block per pair whose short-window correlation broke out.

    The "broke out" predicate: the deviation between the short and long
    correlations exceeds ``correlation_shift_sigma`` multiples of the
    trailing per-pair correlation variance computed via a rolling
    short-window correlation series over the long-window history.
    """
    short_matrix = _correlation_matrix(short_returns)
    long_matrix = _correlation_matrix(long_returns)
    tickers = sorted(short_matrix)
    blocks: list[OutputBlock] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_corr = short_matrix[row][col]
            long_corr = long_matrix[row][col]
            # Trailing rolling correlations EXCLUDING the most recent
            # short window — the trailing distribution is the historical
            # baseline, not the active window we're testing.
            history_a = list(long_returns.get(row, []))[:-short_window_days]
            history_b = list(long_returns.get(col, []))[:-short_window_days]
            rolling = _rolling_pair_correlations(
                a_returns=history_a,
                b_returns=history_b,
                window=short_window_days,
            )
            if not rolling:
                continue
            sigma = statistics.pstdev(rolling)
            if sigma == 0.0:
                continue
            magnitude = abs(short_corr - long_corr) / sigma
            if magnitude < correlation_shift_sigma:
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
                        "short_correlation": short_corr,
                        "long_correlation": long_corr,
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
        # ``topic_tags`` is stored as a comma-separated string; intersect
        # with the regime-relevant set.
        article_tags = {tag.strip() for tag in raw_tags.split(",") if tag.strip()}
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


@dataclass(frozen=True)
class CorrelationRegimeChangeConfig:
    """Threshold bundle for :func:`compute_correlation_regime_change`.

    Packs the per-window and per-detection thresholds that the orchestrator
    pulls out of :class:`DistillationConfig` into a single immutable record
    so :func:`compute_correlation_regime_change` keeps a tight signature.
    """

    short_window_days: int
    long_window_days: int
    correlation_shift_sigma: float
    dispersion_window_days: int
    dispersion_sigma: float
    media_silence_hours: int


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
        correlation_shift_sigma=config.correlation_shift_sigma,
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


# ---------------------------------------------------------------------------
# Top-level entry point — assemble_q7_blocks
# ---------------------------------------------------------------------------
#
# The orchestrator drops its ``_placeholder_blocks("q7")`` stub by calling
# :func:`assemble_q7_blocks`. The function resolves the ticker scope and
# sector roster from the database, runs the six independent compute
# functions, and packs every result into one ``OutputBlock`` list keyed by
# ``q7.*`` block_ids per story 08d.

# The four cross-sector ETFs the cross-sector rotation block reads. Pinned
# here per story 08d's named ETF roster (XLK / SMH / XLF / XLE); the
# constants live alongside ``SPY_TICKER`` etc. so the q7 module is the
# single source of truth for which tickers it pulls.
_CROSS_SECTOR_ETFS: tuple[str, ...] = ("XLK", "SMH", "XLF", "XLE")
"""ETF tickers driving the cross-sector rotation block."""

_RISK_PROXY_ETFS: tuple[str, ...] = ("IWM", "SPY")
"""Risk-appetite proxies (small-cap vs. broad market)."""

# Lead-lag pair scope per the orchestrator's ``_LEAD_LAG_PAIR_SCOPE``;
# pinned here so the q7 entry point owns the (lead, lag) pair-key wiring
# rather than relying on the orchestrator to thread it in. Each tuple is
# ``(pair_key, lead_ticker, lag_ticker)``; the per-pair ``_max_days``
# bound is read out of the loaded :class:`DistillationConfig`.
_LEAD_LAG_PAIR_SCOPE: tuple[tuple[str, str, str], ...] = (
    ("credit_to_equity", "HYG", "SPY"),
    ("semis_to_tech", "SOXX", "QQQ"),
    ("financials_to_market", "XLF", "SPY"),
    ("commodity_to_energy_equity", "USO", "XLE"),
)


def _resolve_universe_tickers(session: Session) -> list[str]:
    """Return every ticker in ``asset_universe`` ascending.

    The cross-asset compute functions need broad coverage (sector-pair
    correlations, breadth) so the default scope is the full universe.
    """
    stmt = select(AssetUniverse.ticker).order_by(AssetUniverse.ticker)
    return [str(t) for t in session.execute(stmt).scalars().all()]


def _resolve_sector_roster(
    session: Session,
    *,
    ticker_scope: Sequence[str],
) -> dict[str, list[str]]:
    """Return ``{alphamind_sector: [tickers...]}`` restricted to ``ticker_scope``.

    Sector membership comes from ``sector_classification.alphamind_sector``;
    tickers in ``ticker_scope`` without a classification row are skipped.
    Result keys iterate sorted for deterministic downstream block_ids.
    """
    if not ticker_scope:
        return {}
    stmt = (
        select(SectorClassification.ticker, SectorClassification.alphamind_sector)
        .where(SectorClassification.ticker.in_(list(ticker_scope)))
        .order_by(SectorClassification.alphamind_sector, SectorClassification.ticker)
    )
    out: dict[str, list[str]] = {}
    for ticker, sector in session.execute(stmt).all():
        out.setdefault(str(sector), []).append(str(ticker))
    return out


def compute_pair_correlations(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: datetime,
    window_days: int,
) -> dict[tuple[str, str], float]:
    """Pairwise correlation dict for ``ticker_scope`` over the trailing window.

    The returned mapping is the canonical input the q3 pair-trade-signature
    detector reads (see :func:`alphamind.distillation.q3_options.detect_pair_trade_signatures`).
    Both ``(a, b)`` and ``(b, a)`` orderings are populated so the q3 scan
    sees every directional pairing once.

    Self-pairs (``a == b``) are omitted — q3's call/put leg-role assignment
    has no meaning for a single ticker.
    """
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    returns_by_ticker: dict[str, list[float]] = {}
    for ticker in ticker_scope:
        closes = _select_close_series(
            session, ticker=ticker, range_start=range_start, range_end=range_end
        )
        returns_by_ticker[ticker] = _log_returns_from_closes(closes)
    matrix = _correlation_matrix(returns_by_ticker)
    out: dict[tuple[str, str], float] = {}
    for row, columns in matrix.items():
        for col, value in columns.items():
            if row == col:
                continue
            out[(row, col)] = value
    return out


def assemble_q7_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
) -> list[OutputBlock]:
    """Top-level q7 entry point — six compute functions packaged as blocks.

    Resolves ``ticker_scope`` (defaults to every ticker in
    ``asset_universe`` when ``None``) and the sector roster, then dispatches
    the six independent compute helpers and concatenates their blocks.

    Audience routing follows story 08d:

    - ``q7.intra_sector_correlation.<sector>``,
      ``q7.cross_sector_rotation``, ``q7.lead_lag.<pair>``,
      ``q7.correlation_breakdown.<pair>``,
      ``q7.correlation_breakdown.dispersion_shift``,
      ``q7.narrative_lag`` — :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    - ``q7.breadth_internals``, ``q7.intermarket_regime.*`` — both
      :attr:`OutputAudience.CORRELATION_REGIME_BRIEF` and
      :attr:`OutputAudience.UNIVERSAL_BROADCAST`.

    An empty ``ticker_scope`` returns ``[]`` — the q7 layer has no
    standalone signal absent universe coverage.
    """
    if ticker_scope is None:
        scope = tuple(_resolve_universe_tickers(session))
    else:
        scope = tuple(ticker_scope)
    if not scope:
        return []

    pw = config.persistence_windows
    sector_roster = _resolve_sector_roster(session, ticker_scope=scope)

    blocks: list[OutputBlock] = []

    # Intra-sector correlation: one block per classified sector.
    # ``_resolve_sector_roster`` only registers a sector key when at least
    # one ticker maps to it, so no empty-sector guard is needed here.
    for sector in sorted(sector_roster):
        blocks.extend(
            compute_intra_sector_correlation(
                session,
                sector=sector,
                sector_tickers=tuple(sector_roster[sector]),
                as_of=as_of,
                short_window_days=pw.correlation_short_days,
                long_window_days=pw.correlation_long_days,
                divergence_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
            )
        )

    # Cross-sector rotation: one block over the four sector ETFs plus risk
    # proxies.
    blocks.extend(
        compute_cross_sector_rotation(
            session,
            sector_etfs=_CROSS_SECTOR_ETFS,
            risk_proxies=_RISK_PROXY_ETFS,
            as_of=as_of,
            short_window_days=pw.correlation_short_days,
            long_window_days=pw.correlation_long_days,
        )
    )

    # Breadth and market internals: read across the full scope, partitioned
    # by alphamind_sector for the advance/decline counts.
    blocks.extend(
        compute_breadth_internals(
            session,
            universe_tickers=scope,
            sectors=tuple(sorted(sector_roster)),
            sector_members={k: tuple(v) for k, v in sector_roster.items()},
            broad_market_etf=SPY_TICKER,
            as_of=as_of,
        )
    )

    # Intermarket regime — fixed series IDs encoded inside the helper.
    blocks.extend(
        compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=pw.correlation_long_days,
            short_window_days=pw.correlation_short_days,
        )
    )

    # Lead-lag — per-pair ``_max_days`` from the loaded config.
    pair_max_days: Mapping[str, int] = {
        "credit_to_equity": config.lead_lag.lead_lag_credit_to_equity_max_days,
        "semis_to_tech": config.lead_lag.lead_lag_semis_to_tech_max_days,
        "financials_to_market": config.lead_lag.lead_lag_financials_to_market_max_days,
        "commodity_to_energy_equity": config.lead_lag.lead_lag_commodity_to_energy_equity_max_days,
    }
    pairs = tuple(
        LeadLagPair(
            pair_key=pair_key,
            lead_ticker=lead,
            lag_ticker=lag,
            max_days=pair_max_days[pair_key],
        )
        for pair_key, lead, lag in _LEAD_LAG_PAIR_SCOPE
    )
    blocks.extend(
        compute_lead_lag(
            session,
            pairs=pairs,
            as_of=as_of,
            overdue_lead_sigma=config.lead_lag.lead_lag_overdue_lead_sigma,
        )
    )

    # Correlation regime change — breakdown / dispersion / narrative-lag.
    blocks.extend(
        compute_correlation_regime_change(
            session,
            universe_tickers=scope,
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=pw.correlation_short_days,
                long_window_days=pw.correlation_long_days,
                correlation_shift_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
                dispersion_window_days=pw.correlation_short_days,
                dispersion_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
                media_silence_hours=config.narrative_lag.narrative_lag_media_silence_hours,
            ),
        )
    )

    return blocks


__all__ = [
    "EMA_WINDOWS_DAYS",
    "GLD_TICKER",
    "NARRATIVE_LAG_REGIME_TAGS",
    "OIL_SERIES",
    "OIL_SOURCE",
    "REAL_YIELD_SERIES",
    "REAL_YIELD_SOURCE",
    "ROTATION_NARRATIVE_GROWTH_DRIVEN",
    "ROTATION_NARRATIVE_RATE_DRIVEN",
    "ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN",
    "SPY_TICKER",
    "TLT_TICKER",
    "VELOCITY_SHARP",
    "VELOCITY_SLOW",
    "VIX_SERIES",
    "VIX_SOURCE",
    "XLE_TICKER",
    "CorrelationRegimeChangeConfig",
    "LeadLagPair",
    "assemble_q7_blocks",
    "compute_breadth_internals",
    "compute_correlation_regime_change",
    "compute_cross_sector_rotation",
    "compute_intermarket_regime",
    "compute_intra_sector_correlation",
    "compute_lead_lag",
    "compute_pair_correlations",
]
