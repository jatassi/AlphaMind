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
- :func:`load_active_thesis_summaries` — stub returning ``()`` until the
  execution-layer thesis model lands (see ALP-111 § Sequencing context
  § Position and thesis model).
- :func:`load_qualitative_inputs` — aggregator assembling the
  :class:`QualitativeInputs` container.

All records are immutable ``BaseModel(frozen=True)``; no rendering happens
here.

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

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.analysis._shared import Sector
from alphamind.persistence.models import (
    DistillationContractHistory,
    DistillationTickerBaseline,
    EarningsEventDetails,
    EventCalendar,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)

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


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class SentimentAggregate(BaseModel, frozen=True):
    """Per-ticker sentiment aggregate, expressed as a percentile vs. history.

    ``percentile_vs_self`` is in ``[0.0, 1.0]`` (unit interval, not 0-100).
    ``data_freshness`` is the timestamp of the underlying baseline row.
    """

    ticker: str
    directional_score: float = Field(ge=-1.0, le=1.0)
    magnitude: float = Field(ge=0.0, le=1.0)
    rate_of_change: float
    volume: int = Field(ge=0)
    divergence_flag: bool
    percentile_vs_self: float = Field(ge=0.0, le=1.0)
    data_freshness: datetime


class PredictionMarketSnapshot(BaseModel, frozen=True):
    """Snapshot of one prediction-market contract with delta fields.

    ``delta_since_last_invocation_pp`` is ``0.0`` when only one history row
    exists (no prior to compute against).
    """

    contract_id: str
    description: str
    platform: str
    category: str
    current_probability: float
    delta_since_last_invocation_pp: float
    delta_24h_pp: float
    volume_24h_usd: float | None
    expiration: str | None
    is_low_liquidity: bool
    meets_threshold_flag: bool
    data_freshness: datetime


class CalendarEvent(BaseModel, frozen=True):
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


class ActiveThesis(BaseModel, frozen=True):
    """Summary record for one active investment thesis.

    Stub until the execution-layer thesis model lands (ALP-111 § Sequencing
    context § Position and thesis model).
    """

    thesis_id: str
    ticker: str
    summary: str
    key_catalyst: str
    time_expectation_hours: int = Field(ge=0)


class QualitativeInputs(BaseModel, frozen=True):
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


def _load_all_sentiment_baselines(
    session: Session,
    *,
    as_of_str: str,
    ticker_scope: Sequence[str] | None,
) -> list[DistillationTickerBaseline]:
    """Return the most-recent sentiment baseline row per ticker at or before as_of."""
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

    # Deduplicate to latest-as_of per ticker.
    rows = session.execute(stmt).scalars().all()
    seen: set[str] = set()
    latest: list[DistillationTickerBaseline] = []
    for row in rows:
        if row.ticker not in seen:
            seen.add(row.ticker)
            latest.append(row)
    return latest


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
) -> tuple[SentimentAggregate, ...]:
    """Return per-ticker :class:`SentimentAggregate` records.

    Reads the latest ``distillation_ticker_baseline`` rows for
    ``baseline_kind = 'sentiment'`` at or before ``as_of``.

    ``percentile_vs_self`` is computed via the Normal-CDF approximation using
    the stored ``(mean, stdev)`` from the rolling calibration-state snapshot.
    Tickers below ``sentiment_min_observations`` fall back to the
    universe-pooled distribution; the record still emits with
    ``data_freshness`` surfacing the bootstrap state.

    When ``ticker_scope`` is ``None`` all tickers with a sentiment baseline are
    returned.
    """
    as_of_str = _format_iso_utc(as_of)
    baselines = _load_all_sentiment_baselines(
        session, as_of_str=as_of_str, ticker_scope=ticker_scope
    )
    if not baselines:
        return ()

    pool: tuple[float, float] | None = None  # lazy-loaded

    results: list[SentimentAggregate] = []
    for row in baselines:
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

        results.append(
            SentimentAggregate(
                ticker=row.ticker,
                directional_score=directional_score,
                magnitude=magnitude,
                rate_of_change=0.0,  # Requires two baseline rows; v1 stub.
                volume=0,  # Not stored in baseline; v1 stub.
                divergence_flag=False,  # Populated by news-price divergence loader.
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
    * ``delta_24h_pp`` is computed from the two most-recent history rows; when
      only one row exists it is ``0.0``.
    * ``meets_threshold_flag`` is ``True`` when
      ``|delta_since_last_invocation_pp| >= delta_pp_threshold``.
    * ``is_low_liquidity`` is ``True`` when 24h volume ≤
      ``low_liquidity_volume_min_usd``.
    """
    as_of_str = _format_iso_utc(as_of)

    # All distinct contract IDs that have history rows.
    contract_ids_row = (
        session.execute(
            select(DistillationContractHistory.contract_id)
            .where(DistillationContractHistory.snapshot_ts <= as_of_str)
            .distinct()
        )
        .scalars()
        .all()
    )

    if not contract_ids_row:
        return ()

    contract_ids = list(contract_ids_row)

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
        # Latest two history rows for delta fields.
        history_rows = session.execute(
            select(
                DistillationContractHistory.yes_probability,
                DistillationContractHistory.delta_pp_since_prior,
                DistillationContractHistory.snapshot_ts,
            )
            .where(
                DistillationContractHistory.contract_id == contract_id,
                DistillationContractHistory.snapshot_ts <= as_of_str,
            )
            .order_by(DistillationContractHistory.snapshot_ts.desc())
            .limit(2)
        ).all()

        if not history_rows:
            continue

        latest = history_rows[0]
        yes_probability = float(latest[0])
        delta_since_last = float(latest[1])
        snapshot_ts_str = latest[2]
        delta_24h = yes_probability - float(history_rows[1][0]) if len(history_rows) == 2 else 0.0

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
                current_probability=yes_probability,
                delta_since_last_invocation_pp=delta_since_last,
                delta_24h_pp=delta_24h,
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
    ]
    entries.sort(key=lambda e: e.event_time)
    return tuple(entries)


# ---------------------------------------------------------------------------
# Active thesis summaries — stub
# ---------------------------------------------------------------------------


def load_active_thesis_summaries(
    session: Session,  # noqa: ARG001
    *,
    as_of: datetime,  # noqa: ARG001
) -> tuple[ActiveThesis, ...]:
    """Return active thesis summaries. Stub — returns ``()`` unconditionally.

    ALP-111 § Sequencing context § Position and thesis model: the thesis model
    lives in the execution layer and has not yet landed. When it does, the
    function body is replaced with a real query against the thesis-model table;
    nothing else in this module changes.
    """
    return ()


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

    ``load_active_thesis_summaries`` returns ``()`` and carries no freshness
    timestamp; it is excluded from the minimum calculation.
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
