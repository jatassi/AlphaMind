"""In-context input loaders for the qualitative researcher — ALP-246.

Implements the four loaders that produce the typed value objects the input
bundle assembler (story 05) renders into the qualitative researcher's user
message:

- :func:`load_sentiment_aggregates` — per-ticker sentiment percentile records
  from the distillation calibration-state snapshot.
- :func:`load_prediction_market_snapshot` — current probability + deltas from
  ``prediction_market_contracts`` / ``prediction_market_snapshots`` /
  ``distillation_contract_history``.
- :func:`load_calendar_events_72h` — events in [as_of, as_of + 72h) from
  ``event_calendar`` joined to ``earnings_event_details``.
- :func:`load_active_thesis_summaries` — per-position thesis summaries for
  every ACTIVE thesis backed by a PENDING or OPEN position.
- :func:`load_qualitative_inputs` — aggregator assembling the
  :class:`QualitativeInputs` container.

All records are immutable ``@dataclass(frozen=True, slots=True)``; no
rendering happens here.

Public names
------------
- :class:`SentimentAggregate`
- :class:`PredictionMarketSnapshot`
- :class:`CalendarEvent`
- :class:`ActiveThesis`
- :class:`QualitativeInputs`
- :func:`load_sentiment_aggregates`
- :func:`load_prediction_market_snapshot`
- :func:`load_calendar_events_72h`
- :func:`load_active_thesis_summaries`
- :func:`load_qualitative_inputs`
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CalibrationState
from alphamind.analysis._shared import Sector
from alphamind.persistence.models import (
    DistillationContractHistory,
    DistillationTickerBaseline,
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.portfolio_state.records.positions import (
    PositionStatus,
    resolve_ticker,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import details_from_json
from alphamind.state.tables.theses import ThesisRow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SQRT_TWO: float = math.sqrt(1.0 + 1.0)
_72_HOURS: timedelta = timedelta(hours=72)

# Threshold above which |delta_pp| triggers meets_threshold_flag.
# Mirrors the design's delta_pp_threshold concept; a 10pp move is material.
_DEFAULT_DELTA_THRESHOLD_PP: float = 10.0

# Low-liquidity volume cutoff in USD — below this 24h volume is low liquidity.
_DEFAULT_LOW_LIQUIDITY_USD: float = 10_000.0

# Minimum trailing observations for per-ticker calibration.
_DEFAULT_SENTIMENT_MIN_OBSERVATIONS: int = 30

# News-vs-price divergence window — lookback in calendar days for the price
# return that is compared against sentiment direction.
_DEFAULT_DIVERGENCE_PRICE_LOOKBACK_DAYS: int = 5

# Both sentiment direction and price return must clear this absolute magnitude
# before a divergence is asserted. Suppresses noise from near-zero sentiment
# or sideways tape.
_DEFAULT_DIVERGENCE_MIN_ABS: float = 0.02


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentimentAggregate:
    """Per-ticker sentiment aggregate, expressed as a percentile vs. history.

    ``percentile_vs_self`` is in ``[0.0, 1.0]`` (unit interval, not 0-100).
    ``data_freshness`` is the timestamp of the underlying baseline row.

    Every numeric field may be ``None``: tickers whose latest baseline row is
    :attr:`CalibrationState.UNAVAILABLE` (zero observations — collector down,
    vendor outage, or genuinely no headlines yet) emit an all-``None`` record
    rather than pool-fallback numbers that look like neutral signal (ALP-538).
    ``rate_of_change``, ``volume``, and ``divergence_flag`` also fall back to
    ``None`` for calibrated tickers when their own per-window source data is
    missing (single baseline row, no daily-bar history). The renderer surfaces
    ``None`` as ``pending`` so the LLM reads "data not available yet", not
    "no signal".
    """

    ticker: str
    directional_score: float | None
    magnitude: float | None
    rate_of_change: float | None
    volume: int | None
    divergence_flag: bool | None
    percentile_vs_self: float | None
    data_freshness: datetime

    def __post_init__(self) -> None:
        if self.directional_score is not None and not -1.0 <= self.directional_score <= 1.0:
            raise ValueError(f"directional_score must be in [-1.0, 1.0]: {self.directional_score}")
        if self.magnitude is not None and not 0.0 <= self.magnitude <= 1.0:
            raise ValueError(f"magnitude must be in [0.0, 1.0]: {self.magnitude}")
        if self.volume is not None and self.volume < 0:
            raise ValueError(f"volume must be >= 0 when set: {self.volume}")
        if self.percentile_vs_self is not None and not 0.0 <= self.percentile_vs_self <= 1.0:
            raise ValueError(f"percentile_vs_self must be in [0.0, 1.0]: {self.percentile_vs_self}")


@dataclass(frozen=True, slots=True)
class PredictionMarketSnapshot:
    """Snapshot of one prediction-market contract with delta fields.

    ``delta_since_last_invocation_pp`` is ``0.0`` when only one history row
    exists (no prior to compute against).

    ``delta_since_prior_pp`` is the change versus the second-latest history
    row at or before ``as_of``. The "since prior" naming is precise: the
    underlying snapshot history is irregular, so the prior row could be any
    timestamp ago.
    """

    contract_id: str
    description: str
    platform: str
    category: str
    current_probability: float
    delta_since_last_invocation_pp: float
    delta_since_prior_pp: float
    volume_24h_usd: float | None
    expiration: str | None
    is_low_liquidity: bool
    meets_threshold_flag: bool
    data_freshness: datetime


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One event in the 72-hour forward calendar.

    ``sectors`` is a ``frozenset[Sector]`` (empty means cross-sector / macro).
    ``consensus`` is only populated for ``event_type == "earnings"``.
    """

    event_id: str
    event_name: str
    event_time: datetime
    event_type: str
    tickers: tuple[str, ...]
    sectors: frozenset[Sector]
    consensus: str | None


@dataclass(frozen=True, slots=True)
class ActiveThesis:
    """Summary record for one active investment thesis.

    Surfaces the per-thesis fields the qualitative researcher's bundle renders
    in the ``ACTIVE THESIS SUMMARIES`` section. One record per ACTIVE thesis
    backed by a PENDING or OPEN position.
    """

    thesis_id: str
    ticker: str
    summary: str
    key_catalyst: str
    time_expectation_hours: int

    def __post_init__(self) -> None:
        if self.time_expectation_hours < 0:
            raise ValueError(f"time_expectation_hours must be >= 0: {self.time_expectation_hours}")


@dataclass(frozen=True, slots=True)
class QualitativeInputs:
    """Top-level container assembled by :func:`load_qualitative_inputs`.

    ``data_freshness`` is the minimum (most stale) of the four sub-loaders'
    freshness timestamps so the renderer can flag when any upstream is stale.
    """

    sentiment_aggregates: tuple[SentimentAggregate, ...]
    prediction_markets: tuple[PredictionMarketSnapshot, ...]
    events: tuple[CalendarEvent, ...]
    theses: tuple[ActiveThesis, ...]
    data_freshness: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC datetime."""
    text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile_from_normal(*, value: float, mean: float, stdev: float) -> float:
    """Return the Normal CDF percentile of ``value`` against ``(mean, stdev)``.

    Returns ``0.5`` when ``stdev <= 0`` (degenerate / no information).
    Result is in ``[0.0, 1.0]``.
    """
    if stdev <= 0:
        return 0.5
    z = (value - mean) / stdev
    cdf = 0.5 * (1.0 + math.erf(z / _SQRT_TWO))
    return max(0.0, min(1.0, cdf))


def _parse_event_sectors(raw: str | None) -> frozenset[Sector]:
    """Parse the comma-separated sectors column into a ``frozenset[Sector]``."""
    if not raw:
        return frozenset()
    out: set[Sector] = set()
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            out.add(Sector(cleaned))
        except ValueError:
            continue
    return frozenset(out)


def _render_consensus(eps: float | None, revenue: float | None) -> str | None:
    parts: list[str] = []
    if eps is not None:
        parts.append(f"EPS ${eps:.2f}")
    if revenue is not None:
        parts.append(f"revenue ${revenue / 1e9:.1f}B")
    return ", ".join(parts) if parts else None


@dataclass
class _EventBucket:
    """Mutable accumulator while grouping event_calendar rows by event_id."""

    event_type: str
    event_name: str
    event_time: datetime
    sectors: frozenset[Sector]
    tickers: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sentiment aggregates
# ---------------------------------------------------------------------------


def _latest_close_at_or_before(
    session: Session,
    *,
    tickers: Sequence[str],
    anchor: str,
) -> dict[str, tuple[str, float]]:
    """Return ``ticker → (period_start, adj_close)`` for each ticker's most
    recent daily ``OhlcvBars`` row whose ``period_start`` is ``<= anchor``.
    """
    max_subq = (
        select(
            OhlcvBars.ticker,
            func.max(OhlcvBars.period_start).label("ms"),
        )
        .where(
            OhlcvBars.ticker.in_(tuple(tickers)),
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start <= anchor,
        )
        .group_by(OhlcvBars.ticker)
        .subquery()
    )
    rows = session.execute(
        select(OhlcvBars.ticker, OhlcvBars.period_start, OhlcvBars.adj_close)
        .join(
            max_subq,
            (OhlcvBars.ticker == max_subq.c.ticker) & (OhlcvBars.period_start == max_subq.c.ms),
        )
        .where(OhlcvBars.timeframe == "1d")
    ).all()
    return {row[0]: (row[1], float(row[2])) for row in rows}


def _load_price_returns_by_ticker(
    session: Session,
    *,
    tickers: Sequence[str],
    as_of: datetime,
    lookback_days: int,
) -> dict[str, float]:
    """Return ``ticker → (latest_close - prior_close) / prior_close`` over a
    ``lookback_days`` calendar-day window ending at ``as_of``.

    ``prior_close`` is the most recent daily bar at or before
    ``as_of - lookback_days``; ``latest_close`` is the most recent at or
    before ``as_of``. Tickers without bars on both sides of the window (or
    where the same bar satisfies both anchors) are absent from the mapping.
    """
    if not tickers:
        return {}
    prior_anchor = (as_of - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    latest = _latest_close_at_or_before(session, tickers=tickers, anchor=_format_iso_utc(as_of))
    prior = _latest_close_at_or_before(session, tickers=tickers, anchor=prior_anchor)

    returns: dict[str, float] = {}
    for ticker, (latest_start, latest_close) in latest.items():
        prior_pair = prior.get(ticker)
        if prior_pair is None:
            continue
        prior_start, prior_close = prior_pair
        if latest_start == prior_start or prior_close == 0.0:
            continue
        returns[ticker] = (latest_close - prior_close) / prior_close
    return returns


def _is_divergent(directional_score: float, price_return: float, *, threshold: float) -> bool:
    """True iff sentiment and price disagree on direction and both
    ``abs(directional_score)`` and ``abs(price_return)`` clear ``threshold``.
    """
    if abs(directional_score) < threshold or abs(price_return) < threshold:
        return False
    return (directional_score > 0) != (price_return > 0)


def _load_article_volume_by_ticker(
    session: Session,
    *,
    tickers: Sequence[str],
    window_start_str: str,
    window_end_str: str,
) -> dict[str, int]:
    """Count news_article_tickers rows per ticker whose article was published
    in ``(window_start_str, window_end_str]``.

    The interval is half-open at the lower edge so the prior-baseline timestamp
    is excluded (it already contributed to the prior period's sentiment) and
    inclusive at the upper edge so the latest baseline's own day is counted.
    """
    if not tickers:
        return {}
    rows = session.execute(
        select(NewsArticleTickers.ticker, func.count())
        .join(NewsArticles, NewsArticleTickers.article_id == NewsArticles.article_id)
        .where(
            NewsArticleTickers.ticker.in_(tuple(tickers)),
            NewsArticles.published_at > window_start_str,
            NewsArticles.published_at <= window_end_str,
        )
        .group_by(NewsArticleTickers.ticker)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _load_recent_sentiment_baselines(
    session: Session,
    *,
    as_of_str: str,
    ticker_scope: Sequence[str] | None,
) -> dict[str, list[DistillationTickerBaseline]]:
    """Return the two most-recent sentiment baseline rows per ticker, latest first.

    The latest row drives ``SentimentAggregate``'s point-in-time fields; the
    prior row supplies the diff for ``rate_of_change``. Tickers with no
    baseline rows at or before ``as_of`` are absent from the mapping.
    """
    stmt = (
        select(DistillationTickerBaseline)
        .where(
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.as_of <= as_of_str,
        )
        .order_by(
            DistillationTickerBaseline.ticker,
            DistillationTickerBaseline.as_of.desc(),
        )
    )
    if ticker_scope is not None:
        stmt = stmt.where(DistillationTickerBaseline.ticker.in_(tuple(ticker_scope)))

    buckets: dict[str, list[DistillationTickerBaseline]] = {}
    for row in session.execute(stmt).scalars():
        bucket = buckets.setdefault(row.ticker, [])
        if len(bucket) < 2:
            bucket.append(row)
    return buckets


def _universe_pooled_sentiment(session: Session, *, as_of_str: str) -> tuple[float, float] | None:
    """Return universe-pooled ``(mean, stdev)`` from calibrated baselines."""
    rows = session.execute(
        select(DistillationTickerBaseline.mean, DistillationTickerBaseline.stdev).where(
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.as_of <= as_of_str,
            DistillationTickerBaseline.calibration_state == "calibrated",
        )
    ).all()
    if not rows:
        return None
    means = [float(r[0]) for r in rows]
    stdevs = [float(r[1]) for r in rows]
    return statistics.fmean(means), statistics.fmean(stdevs)


def load_sentiment_aggregates(
    session: Session,
    *,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
    sentiment_min_observations: int = _DEFAULT_SENTIMENT_MIN_OBSERVATIONS,
    divergence_price_lookback_days: int = _DEFAULT_DIVERGENCE_PRICE_LOOKBACK_DAYS,
    divergence_min_abs: float = _DEFAULT_DIVERGENCE_MIN_ABS,
) -> tuple[SentimentAggregate, ...]:
    """Return per-ticker :class:`SentimentAggregate` records.

    Reads the latest ``distillation_ticker_baseline`` rows for
    ``baseline_kind = 'sentiment'`` at or before ``as_of``.

    ``percentile_vs_self`` is computed via the Normal-CDF approximation using
    the stored ``(mean, stdev)`` from the rolling calibration-state snapshot.
    Tickers below ``sentiment_min_observations`` fall back to the
    universe-pooled distribution; the record still emits with
    ``data_freshness`` surfacing the bootstrap state.

    ``rate_of_change`` is the diff between the latest baseline mean and the
    prior-period mean; ``None`` when only one row exists.

    ``volume`` is the count of ``news_article_tickers`` rows whose article
    ``published_at`` falls in the rate-of-change window; ``None`` when there
    is no prior baseline to anchor the window.

    ``divergence_flag`` is ``True`` when sentiment direction disagrees with
    the ``divergence_price_lookback_days`` trailing price return and both
    magnitudes clear ``divergence_min_abs``; ``None`` when price history is
    insufficient.

    When ``ticker_scope`` is ``None`` all tickers with a sentiment baseline
    are returned.
    """
    as_of_str = _format_iso_utc(as_of)
    baselines = _load_recent_sentiment_baselines(
        session, as_of_str=as_of_str, ticker_scope=ticker_scope
    )
    if not baselines:
        return ()

    # UNAVAILABLE rows skip the per-window article / price-return queries —
    # the downstream branch emits an all-None record and never reads either
    # mapping, so feeding their tickers into the IN-list would be dead work.
    available_tickers = [
        t
        for t, r in baselines.items()
        if CalibrationState(r[0].calibration_state) is not CalibrationState.UNAVAILABLE
    ]

    # Article counts use each ticker's own rate-of-change window. In the
    # steady state Class B refresh writes one row per ticker per refresh, so
    # nearly every ticker shares the same ``(prior, latest)`` pair and this
    # collapses to one query; backfills or repair refreshes that desync the
    # panel naturally fan out into per-window queries.
    window_buckets: dict[tuple[str, str], list[str]] = {}
    for ticker in available_tickers:
        recent = baselines[ticker]
        if len(recent) == 2:
            window_buckets.setdefault((recent[1].as_of, recent[0].as_of), []).append(ticker)
    volume_by_ticker: dict[str, int] = {}
    for (prior_as_of, latest_as_of), bucket_tickers in window_buckets.items():
        volume_by_ticker.update(
            _load_article_volume_by_ticker(
                session,
                tickers=bucket_tickers,
                window_start_str=prior_as_of,
                window_end_str=latest_as_of,
            )
        )

    price_returns = _load_price_returns_by_ticker(
        session,
        tickers=available_tickers,
        as_of=as_of,
        lookback_days=divergence_price_lookback_days,
    )

    pool: tuple[float, float] | None = None  # lazy-loaded

    results: list[SentimentAggregate] = []
    for ticker, recent in baselines.items():
        row = recent[0]
        # UNAVAILABLE rows (zero sentiment observations — collector down,
        # vendor outage, benchmark ticker without headline coverage) emit an
        # all-None record. The pool-fallback path below would otherwise
        # broadcast bit-identical numeric placeholders to every such ticker,
        # masking the missing-data state as "neutral signal".
        if CalibrationState(row.calibration_state) is CalibrationState.UNAVAILABLE:
            results.append(
                SentimentAggregate(
                    ticker=ticker,
                    directional_score=None,
                    magnitude=None,
                    rate_of_change=None,
                    volume=None,
                    divergence_flag=None,
                    percentile_vs_self=None,
                    data_freshness=_parse_iso_utc(row.as_of),
                )
            )
            continue
        if row.n_observations >= sentiment_min_observations:
            mean = float(row.mean)
            stdev = float(row.stdev)
        else:
            if pool is None:
                pool = _universe_pooled_sentiment(session, as_of_str=as_of_str)
            if pool is None:
                # Pool also empty (pre-bootstrap): skip this ticker.
                continue
            mean, stdev = pool

        percentile = _percentile_from_normal(value=float(row.mean), mean=mean, stdev=stdev)

        # directional_score: sign of mean relative to the baseline mean,
        # clamped to [-1, 1].
        directional_score = max(-1.0, min(1.0, float(row.mean)))
        # magnitude: absolute deviation normalised by stdev (capped at 1.0).
        magnitude = min(1.0, abs(float(row.mean) - mean) / stdev) if stdev > 0 else 0.0
        has_window = len(recent) == 2
        rate_of_change = float(row.mean) - float(recent[1].mean) if has_window else None
        volume = volume_by_ticker.get(ticker, 0) if has_window else None
        price_return = price_returns.get(ticker)
        divergence_flag = (
            _is_divergent(directional_score, price_return, threshold=divergence_min_abs)
            if price_return is not None
            else None
        )

        results.append(
            SentimentAggregate(
                ticker=ticker,
                directional_score=directional_score,
                magnitude=magnitude,
                rate_of_change=rate_of_change,
                volume=volume,
                divergence_flag=divergence_flag,
                percentile_vs_self=percentile,
                data_freshness=_parse_iso_utc(row.as_of),
            )
        )

    return tuple(results)


# ---------------------------------------------------------------------------
# Prediction market snapshot
# ---------------------------------------------------------------------------


def load_prediction_market_snapshot(
    session: Session,
    *,
    as_of: datetime,
    delta_pp_threshold: float = _DEFAULT_DELTA_THRESHOLD_PP,
    low_liquidity_volume_min_usd: float = _DEFAULT_LOW_LIQUIDITY_USD,
) -> tuple[PredictionMarketSnapshot, ...]:
    """Return per-contract :class:`PredictionMarketSnapshot` records.

    For each contract that has at least one
    ``distillation_contract_history`` row at or before ``as_of``:

    * ``delta_since_last_invocation_pp`` comes from the latest history row's
      ``delta_pp_since_prior`` column (written by story 07's
      ``refresh_contract_history``).
    * ``delta_since_prior_pp`` is the difference between the latest history
      row's ``yes_probability`` and the second-latest row's; when only one
      row exists it is ``0.0``. The two rows can be any timestamps apart —
      snapshot history is irregular.
    * ``meets_threshold_flag`` is ``True`` when
      ``|delta_since_last_invocation_pp| >= delta_pp_threshold``.
    * ``is_low_liquidity`` is ``True`` when 24h volume ≤
      ``low_liquidity_volume_min_usd``.
    """
    as_of_str = _format_iso_utc(as_of)

    # Batch-load the latest two history rows per contract in one query
    # via ROW_NUMBER() — avoids the N+1 of running one limit-2 query per
    # tracked contract.
    rn = (
        func.row_number()
        .over(
            partition_by=DistillationContractHistory.contract_id,
            order_by=DistillationContractHistory.snapshot_ts.desc(),
        )
        .label("rn")
    )
    ranked_subq = (
        select(
            DistillationContractHistory.contract_id,
            DistillationContractHistory.yes_probability,
            DistillationContractHistory.delta_pp_since_prior,
            DistillationContractHistory.snapshot_ts,
            rn,
        )
        .where(DistillationContractHistory.snapshot_ts <= as_of_str)
        .subquery()
    )
    ranked_rows = session.execute(
        select(
            ranked_subq.c.contract_id,
            ranked_subq.c.yes_probability,
            ranked_subq.c.delta_pp_since_prior,
            ranked_subq.c.snapshot_ts,
        )
        .where(ranked_subq.c.rn <= 2)
        .order_by(ranked_subq.c.contract_id, ranked_subq.c.snapshot_ts.desc())
    ).all()

    if not ranked_rows:
        return ()

    # Each value is up to 2 rows ordered latest-first:
    # (yes_probability, delta_pp_since_prior, snapshot_ts).
    history_by_id: dict[str, list[tuple[float, float, str]]] = {}
    for row in ranked_rows:
        history_by_id.setdefault(row[0], []).append((float(row[1]), float(row[2]), row[3]))

    contract_ids = list(history_by_id)

    # Batch-load contract metadata in one query.
    meta_rows = session.execute(
        select(
            PredictionMarketContracts.contract_id,
            PredictionMarketContracts.platform,
            PredictionMarketContracts.description,
            PredictionMarketContracts.category,
            PredictionMarketContracts.resolution_date,
        ).where(PredictionMarketContracts.contract_id.in_(contract_ids))
    ).all()
    meta_by_id: dict[str, tuple[str, str, str, str | None]] = {
        r[0]: (r[1] or "", r[2] or "", r[3] or "", r[4]) for r in meta_rows
    }

    # Batch-load latest snapshot volume per contract using a subquery for
    # max snapshot_ts per contract at or before as_of.
    latest_snap_ts_subq = (
        select(
            PredictionMarketSnapshots.contract_id,
            func.max(PredictionMarketSnapshots.snapshot_ts).label("max_ts"),
        )
        .where(
            PredictionMarketSnapshots.contract_id.in_(contract_ids),
            PredictionMarketSnapshots.snapshot_ts <= as_of_str,
        )
        .group_by(PredictionMarketSnapshots.contract_id)
        .subquery()
    )
    snap_rows = session.execute(
        select(
            PredictionMarketSnapshots.contract_id,
            PredictionMarketSnapshots.volume_24h_usd,
        ).join(
            latest_snap_ts_subq,
            (PredictionMarketSnapshots.contract_id == latest_snap_ts_subq.c.contract_id)
            & (PredictionMarketSnapshots.snapshot_ts == latest_snap_ts_subq.c.max_ts),
        )
    ).all()
    volume_by_id: dict[str, float | None] = {
        r[0]: (float(r[1]) if r[1] is not None else None) for r in snap_rows
    }

    results: list[PredictionMarketSnapshot] = []

    for contract_id in contract_ids:
        history_rows = history_by_id[contract_id]
        latest_yes, delta_since_last, snapshot_ts_str = history_rows[0]
        # Subtract the second-latest history row's yes_probability — the gap
        # between the two rows is irregular (anywhere from minutes to days).
        delta_since_prior = latest_yes - history_rows[1][0] if len(history_rows) == 2 else 0.0

        platform, description, category, resolution_date = meta_by_id.get(
            contract_id, ("", "", "", None)
        )
        volume_24h = volume_by_id.get(contract_id)
        low_liquidity = (volume_24h is None) or (volume_24h <= low_liquidity_volume_min_usd)

        results.append(
            PredictionMarketSnapshot(
                contract_id=contract_id,
                description=description,
                platform=platform,
                category=category,
                current_probability=latest_yes,
                delta_since_last_invocation_pp=delta_since_last,
                delta_since_prior_pp=delta_since_prior,
                volume_24h_usd=volume_24h,
                expiration=resolution_date,
                is_low_liquidity=low_liquidity,
                meets_threshold_flag=abs(delta_since_last) >= delta_pp_threshold,
                data_freshness=_parse_iso_utc(snapshot_ts_str),
            )
        )

    return tuple(results)


# ---------------------------------------------------------------------------
# Calendar events (72-hour window)
# ---------------------------------------------------------------------------


def load_calendar_events_72h(
    session: Session,
    *,
    as_of: datetime,
) -> tuple[CalendarEvent, ...]:
    """Return :class:`CalendarEvent` records for the next 72 hours.

    Reads ``event_calendar`` rows where ``event_time`` is in
    ``[as_of, as_of + 72h)``. Joins ``earnings_event_details`` for earnings
    consensus. Returns events sorted by ``event_time`` ascending.
    """
    window_start = _format_iso_utc(as_of)
    window_end = _format_iso_utc(as_of + _72_HOURS)

    rows = session.execute(
        select(
            EventCalendar.event_id,
            EventCalendar.event_type,
            EventCalendar.ticker,
            EventCalendar.scheduled_at,
            EventCalendar.description,
            EventCalendar.sectors,
        ).where(
            EventCalendar.scheduled_at >= window_start,
            EventCalendar.scheduled_at < window_end,
        )
    ).all()

    if not rows:
        return ()

    # Group rows by event_id (multiple tickers share one event).
    grouped: dict[str, _EventBucket] = {}
    for row in rows:
        eid = row.event_id
        if eid not in grouped:
            grouped[eid] = _EventBucket(
                event_type=row.event_type,
                event_name=row.description or "",
                event_time=_parse_iso_utc(row.scheduled_at),
                sectors=_parse_event_sectors(row.sectors),
            )
        if row.ticker and row.ticker not in grouped[eid].tickers:
            grouped[eid].tickers.append(row.ticker)

    # Earnings consensus.
    earnings_ids = [eid for eid, b in grouped.items() if b.event_type == "earnings"]
    consensus_map: dict[str, str | None] = {}
    if earnings_ids:
        consensus_rows = session.execute(
            select(
                EarningsEventDetails.event_id,
                EarningsEventDetails.eps_consensus,
                EarningsEventDetails.revenue_consensus_usd,
            ).where(EarningsEventDetails.event_id.in_(earnings_ids))
        ).all()
        for cr in consensus_rows:
            consensus_map[cr[0]] = _render_consensus(cr[1], cr[2])

    entries = [
        CalendarEvent(
            event_id=eid,
            event_name=bucket.event_name,
            event_time=bucket.event_time,
            event_type=bucket.event_type,
            tickers=tuple(bucket.tickers),
            sectors=bucket.sectors,
            consensus=consensus_map.get(eid) if bucket.event_type == "earnings" else None,
        )
        for eid, bucket in grouped.items()
        if not (bucket.event_type == "other" and not bucket.tickers)
    ]
    entries.sort(key=lambda e: e.event_time)
    return tuple(entries)


# ---------------------------------------------------------------------------
# Active thesis summaries
# ---------------------------------------------------------------------------


def load_active_thesis_summaries(
    session: Session,
    *,
    as_of: datetime,
) -> tuple[ActiveThesis, ...]:
    """Return :class:`ActiveThesis` summaries for every ACTIVE thesis backed by
    a PENDING or OPEN position generated at or before ``as_of``.

    Joins ``theses`` to ``positions`` so the ticker can be lifted from the
    position's ``details_json``. ``summary`` reads from ``ThesisRow.summary``;
    ``key_catalyst`` reads from the ``narrative_json`` payload written by
    :func:`alphamind.state.tables.theses_codec.record_to_rows`. Results are
    sorted by ``thesis_id`` so renderings are deterministic.
    """
    as_of_str = _format_iso_utc(as_of)
    active_position_statuses = (PositionStatus.OPEN.value, PositionStatus.PENDING.value)
    rows = session.execute(
        select(ThesisRow, PositionRow.details_json)
        .join(PositionRow, ThesisRow.position_id == PositionRow.position_id)
        .where(
            ThesisRow.status == ThesisRecordStatus.ACTIVE.value,
            ThesisRow.generation_timestamp <= as_of_str,
            PositionRow.status.in_(active_position_statuses),
        )
        .order_by(ThesisRow.thesis_id.asc())
    ).all()

    results: list[ActiveThesis] = []
    for thesis_row, details_json in rows:
        ticker = resolve_ticker(details_from_json(details_json))
        if ticker is None:
            # Only ``StrategyPositionDetails`` with no legs can produce ``None``
            # — nothing to attribute the thesis to in the per-ticker view.
            continue
        narrative = json.loads(thesis_row.narrative_json)
        time_hours = (
            round(thesis_row.time_expectation_hours)
            if thesis_row.time_expectation_hours is not None
            else 0
        )
        results.append(
            ActiveThesis(
                thesis_id=thesis_row.thesis_id,
                ticker=ticker,
                summary=thesis_row.summary,
                key_catalyst=narrative.get("key_catalyst", ""),
                time_expectation_hours=time_hours,
            )
        )

    return tuple(results)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def load_qualitative_inputs(
    session: Session,
    *,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
) -> QualitativeInputs:
    """Assemble and return the full :class:`QualitativeInputs` container.

    Calls all four loaders and sets ``data_freshness`` to the minimum (most
    stale) of the four sub-loaders' freshness timestamps so the agent can
    set ``signal_quality = DEGRADED`` when any upstream is stale.

    :class:`ActiveThesis` carries no ``data_freshness`` field, so thesis
    records are excluded from the minimum-freshness calculation.
    """
    sentiment = load_sentiment_aggregates(session, as_of=as_of, ticker_scope=ticker_scope)
    prediction = load_prediction_market_snapshot(session, as_of=as_of)
    events = load_calendar_events_72h(session, as_of=as_of)
    theses = load_active_thesis_summaries(session, as_of=as_of)

    # Collect freshness candidates from sub-loaders that have data.
    freshness_candidates: list[datetime] = []
    for agg in sentiment:
        freshness_candidates.append(agg.data_freshness)
    for snap in prediction:
        freshness_candidates.append(snap.data_freshness)
    # CalendarEvent has no data_freshness field (events are always "fresh"
    # relative to the calendar); use as_of as the floor.

    data_freshness = min(freshness_candidates) if freshness_candidates else as_of

    return QualitativeInputs(
        sentiment_aggregates=sentiment,
        prediction_markets=prediction,
        events=events,
        theses=theses,
        data_freshness=data_freshness,
    )


__all__ = [
    "ActiveThesis",
    "CalendarEvent",
    "PredictionMarketSnapshot",
    "QualitativeInputs",
    "SentimentAggregate",
    "load_active_thesis_summaries",
    "load_calendar_events_72h",
    "load_prediction_market_snapshot",
    "load_qualitative_inputs",
    "load_sentiment_aggregates",
]
