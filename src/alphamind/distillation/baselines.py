"""Rolling baseline refresh primitive — story 02-distillation-layer/07.

Class B refresh: read current state, append the newest underlying observation,
incrementally update the rolling mean / stdev / observation count via
Welford's algorithm, write the new row, return a :class:`CalibratedValue`.

Five entry points, one per state-table family in
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Where each threshold lives:

- :func:`refresh_ticker_baselines` for ``distillation_ticker_baseline``
- :func:`refresh_pair_lag` for ``distillation_pair_lag``
- :func:`refresh_contract_history` for ``distillation_contract_history``
- :func:`refresh_event_history` for ``distillation_event_history``
- :func:`refresh_composite_state` for ``distillation_composite_state``

Each entry point wraps its work in a single SQLAlchemy transaction. Any
uncaught exception triggers a rollback and propagates per the fail-closed
policy in ``docs/design/01-data-layer/api-failure-handling.md`` and the
no-checkpoint, no-resume policy in
``docs/design/mid-pipeline-failure-handling.md``.

Refresh functions return plain :class:`CalibratedValue` instances (or dicts
of them keyed by ticker / pair / contract). SQLAlchemy ORM objects are not
leaked across the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import (
    CalibratedValue,
    CalibrationState,
)
from alphamind.persistence.models import (
    DistillationCompositeState,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationPairLag,
    DistillationTickerBaseline,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    PredictionMarketSnapshots,
)

# ---------------------------------------------------------------------------
# Configuration-passing convention
# ---------------------------------------------------------------------------
#
# Per ``threshold-calibration.md`` § Where each threshold lives, every Class A
# value (``*_baseline_days``, ``*_min_observations``, ``*_max_days``) lives in
# ``config/distillation.yaml`` and reaches the refresh primitive through the
# orchestrator (story 12), which loads the YAML once per invocation and
# passes the resolved integers as keyword arguments. The refresh module
# carries no default values — that would invite a Class A bypass and trip
# the no-magic-numbers audit (tests/distillation/test_no_magic_numbers.py).


# ---------------------------------------------------------------------------
# Transaction wrapper
# ---------------------------------------------------------------------------


@contextmanager
def _refresh_transaction(session: Session) -> Iterator[None]:
    """Wrap one refresh's writes in a single atomic transaction.

    Commits on success; rolls back and re-raises on any exception. This is
    the fail-closed seam that ``api-failure-handling.md`` and
    ``mid-pipeline-failure-handling.md`` mandate — partial state never
    persists.
    """
    try:
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise


# ---------------------------------------------------------------------------
# Time arithmetic
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 ``Z``-suffixed UTC timestamp into a tz-aware datetime."""
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
    return datetime.fromisoformat(ts)


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_start(*, as_of: str, window_days: int) -> str:
    """Return ``as_of - window_days`` as an ISO 8601 UTC string."""
    end = _parse_iso_utc(as_of)
    start = end - timedelta(days=window_days)
    return _format_iso_utc(start)


# ---------------------------------------------------------------------------
# Welford's-algorithm running statistics
# ---------------------------------------------------------------------------


def _welford_extend(
    *,
    prior_n: int,
    prior_mean: float,
    prior_m2: float,
    new_values: Sequence[float],
) -> tuple[int, float, float]:
    """Extend a running ``(n, mean, M2)`` triple with ``new_values``.

    Implements the standard Welford update so the per-call cost is O(len
    new_values) regardless of ``prior_n``. Returns the new ``(n, mean, M2)``.
    Caller computes the population stdev as ``sqrt(M2 / n)`` once finished.
    """
    n = prior_n
    mean = prior_mean
    m2 = prior_m2
    for value in new_values:
        n += 1
        delta = value - mean
        mean += delta / n
        delta2 = value - mean
        m2 += delta * delta2
    return n, mean, m2


def _welford_evict(
    *,
    prior_n: int,
    prior_mean: float,
    prior_m2: float,
    evicted_values: Sequence[float],
) -> tuple[int, float, float]:
    """Reverse-update a running ``(n, mean, M2)`` triple by removing observations.

    Inverse of :func:`_welford_extend`. Drops each value in ``evicted_values``
    from the running statistics by inverting the Welford recurrence:

        mean_{k-1} = (k * mean_k - x) / (k - 1)
        M2_{k-1}   = M2_k - (x - mean_{k-1}) * (x - mean_k)

    Returns ``(0, 0.0, 0.0)`` if the eviction empties the running state. Used
    by the rolling-baseline refresh to drop observations that have fallen
    outside the new window — the design contract is "append the newest point,
    drop the oldest" (threshold-calibration.md § Update process), so the
    incremental Welford state must shrink as well as grow.
    """
    n = prior_n
    mean = prior_mean
    m2 = prior_m2
    for value in evicted_values:
        if n <= 1:
            return 0, 0.0, 0.0
        new_n = n - 1
        new_mean = (n * mean - value) / new_n
        m2 -= (value - new_mean) * (value - mean)
        n = new_n
        mean = new_mean
    return n, mean, m2


def _stdev_from_m2(*, n: int, m2: float) -> float:
    """Population standard deviation derived from Welford's M2 accumulator."""
    if n <= 0:
        return 0.0
    return float((m2 / n) ** 0.5)


# ---------------------------------------------------------------------------
# refresh_ticker_baselines
# ---------------------------------------------------------------------------


def _select_ohlcv_observations(
    session: Session,
    *,
    kind: str,
    ticker: str,
    range_start: str,
    range_end: str,
    inclusive_start: bool,
    inclusive_end: bool = True,
) -> list[float]:
    """Return per-day observations of ``kind`` inside the time range ascending.

    ``kind`` selects the source projection:

    - ``volume`` → ``adj_volume``.
    - ``atr`` → ``adj_high - adj_low`` (true range from daily bars; the
      14-period ATR is the rolling mean of this series).
    - ``spread`` → ``(adj_high - adj_low) / adj_close`` (intraday range as
      a fraction of close — a liquidity proxy on daily resolution).

    ``inclusive_start = True`` is the full-window recompute path; ``False``
    is the Welford incremental path where ``range_start`` is the prior
    refresh's ``as_of`` and only strictly-newer bars are fetched. The
    eviction path uses ``inclusive_end = False`` to scan ``[old_window_start,
    new_window_start)`` — bars that were inside the prior window but fall
    outside the new one.
    """
    start_clause = (
        OhlcvBars.period_start >= range_start
        if inclusive_start
        else OhlcvBars.period_start > range_start
    )
    end_clause = (
        OhlcvBars.period_start <= range_end if inclusive_end else OhlcvBars.period_start < range_end
    )
    if kind == "volume":
        volume_stmt = (
            select(OhlcvBars.adj_volume)
            .where(
                OhlcvBars.ticker == ticker,
                OhlcvBars.timeframe == "1d",
                start_clause,
                end_clause,
            )
            .order_by(OhlcvBars.period_start)
        )
        return [float(v) for v in session.execute(volume_stmt).scalars().all()]
    # ATR and spread both need high/low/close; reuse one query.
    range_stmt = (
        select(OhlcvBars.adj_high, OhlcvBars.adj_low, OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            start_clause,
            end_clause,
        )
        .order_by(OhlcvBars.period_start)
    )
    rows = session.execute(range_stmt).all()
    if kind == "atr":
        return [float(high) - float(low) for high, low, _close in rows]
    if kind == "spread":
        out: list[float] = []
        for high, low, close in rows:
            close_f = float(close)
            out.append((float(high) - float(low)) / close_f if close_f else 0.0)
        return out
    raise ValueError(f"unknown ticker-baseline kind: {kind!r}")


def _select_sentiment_observations(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
    inclusive_start: bool,
    inclusive_end: bool = True,
) -> list[float]:
    """Return per-article vendor sentiment scores for ``ticker`` ascending.

    Joins ``news_article_tickers`` to ``news_articles`` on the article id so
    the date filter operates against ``published_at`` (the article-time
    convention used by the qualitative pipeline). Articles whose vendor
    sentiment is NULL are skipped — they carry no observation.
    """
    start_clause = (
        NewsArticles.published_at >= range_start
        if inclusive_start
        else NewsArticles.published_at > range_start
    )
    end_clause = (
        NewsArticles.published_at <= range_end
        if inclusive_end
        else NewsArticles.published_at < range_end
    )
    stmt = (
        select(NewsArticleTickers.vendor_sentiment_score)
        .join(
            NewsArticles,
            NewsArticles.article_id == NewsArticleTickers.article_id,
        )
        .where(
            NewsArticleTickers.ticker == ticker,
            NewsArticleTickers.vendor_sentiment_score.isnot(None),
            start_clause,
            end_clause,
        )
        .order_by(NewsArticles.published_at)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _select_kind_observations(
    session: Session,
    *,
    kind: str,
    ticker: str,
    range_start: str,
    range_end: str,
    inclusive_start: bool,
    inclusive_end: bool = True,
) -> list[float]:
    """Dispatch to the source-table reader matching ``kind``."""
    if kind == "sentiment":
        return _select_sentiment_observations(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=range_end,
            inclusive_start=inclusive_start,
            inclusive_end=inclusive_end,
        )
    return _select_ohlcv_observations(
        session,
        kind=kind,
        ticker=ticker,
        range_start=range_start,
        range_end=range_end,
        inclusive_start=inclusive_start,
        inclusive_end=inclusive_end,
    )


def _load_prior_baseline(
    session: Session,
    *,
    ticker: str,
    kind: str,
    as_of: str,
) -> DistillationTickerBaseline | None:
    """Return the most recent baseline row strictly before ``as_of``.

    Used to seed Welford's running statistics so the per-call cost depends
    only on the bars added since the prior refresh, not on the full window.
    """
    stmt = (
        select(DistillationTickerBaseline)
        .where(
            DistillationTickerBaseline.ticker == ticker,
            DistillationTickerBaseline.baseline_kind == kind,
            DistillationTickerBaseline.as_of < as_of,
        )
        .order_by(DistillationTickerBaseline.as_of.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class _TickerBaselineRow:
    """Value object capturing one ``distillation_ticker_baseline`` payload."""

    ticker: str
    kind: str
    as_of: str
    mean: float
    stdev: float
    n_observations: int
    window_days: int
    state: CalibrationState


def _upsert_ticker_baseline(session: Session, row: _TickerBaselineRow) -> None:
    """Insert or update one ``distillation_ticker_baseline`` row."""
    existing = session.execute(
        select(DistillationTickerBaseline).where(
            DistillationTickerBaseline.ticker == row.ticker,
            DistillationTickerBaseline.baseline_kind == row.kind,
            DistillationTickerBaseline.as_of == row.as_of,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DistillationTickerBaseline(
                ticker=row.ticker,
                baseline_kind=row.kind,
                as_of=row.as_of,
                mean=row.mean,
                stdev=row.stdev,
                n_observations=row.n_observations,
                window_days=row.window_days,
                calibration_state=row.state.value,
                ingested_at=row.as_of,
            )
        )
    else:
        existing.mean = row.mean
        existing.stdev = row.stdev
        existing.n_observations = row.n_observations
        existing.window_days = row.window_days
        existing.calibration_state = row.state.value
        existing.ingested_at = row.as_of


def _full_recompute_required(
    *,
    prior: DistillationTickerBaseline,
    as_of_dt: datetime,
    window_days: int,
) -> bool:
    """Return ``True`` when the incremental Welford path cannot be reused.

    Two conditions force a full-window recompute over the new window:

    1. The configured ``window_days`` differs from the prior row's
       ``window_days`` (operator changed the Class A window).
    2. The refresh gap meets or exceeds the window — every observation in
       the prior window has fallen out, so there is nothing to extend from
       and scanning the eviction range would cost more than a fresh scan.
    """
    if prior.window_days != window_days:
        return True
    prior_as_of_dt = _parse_iso_utc(prior.as_of)
    return as_of_dt - prior_as_of_dt >= timedelta(days=window_days)


def refresh_ticker_baselines(
    session: Session,
    *,
    kind: str,
    ticker_scope: Sequence[str],
    as_of: str,
    window_days: int,
    min_observations: int,
) -> dict[str, CalibratedValue]:
    """Refresh per-ticker ``volume`` / ``atr`` / ``spread`` / ``sentiment`` baselines.

    For each ticker in ``ticker_scope``:

    1. Look up the prior baseline row for ``(ticker, kind)``.
    2. On the incremental path, append observations newer than the prior
       refresh and evict observations that have fallen outside the new
       window — the design contract is "append the newest point, drop the
       oldest" (threshold-calibration.md § Update process), so the running
       Welford state always reflects ``[as_of - window_days, as_of]``.
       On the full-recompute path (no prior row, window changed, or refresh
       gap ≥ window), scan the entire new window from cold state.
    3. Compute the calibration state from ``min_observations``.
    4. UPSERT a new row keyed ``(ticker, kind, as_of)``.
    5. Return a dict of :class:`CalibratedValue` keyed by ticker.

    Per-call cost is O(|inflow| + |outflow|) on the incremental path — for a
    daily refresh cadence that's two observations regardless of window size.

    The whole loop runs inside a single transaction. An uncaught exception
    rolls back any partial writes and propagates.
    """
    out: dict[str, CalibratedValue] = {}
    as_of_dt = _parse_iso_utc(as_of)
    with _refresh_transaction(session):
        for ticker in ticker_scope:
            prior = _load_prior_baseline(session, ticker=ticker, kind=kind, as_of=as_of)
            if prior is None or _full_recompute_required(
                prior=prior, as_of_dt=as_of_dt, window_days=window_days
            ):
                # Full-window recompute: first deployment, window changed,
                # or the prior window has rolled entirely off.
                range_start = _window_start(as_of=as_of, window_days=window_days)
                window_values = _select_kind_observations(
                    session,
                    kind=kind,
                    ticker=ticker,
                    range_start=range_start,
                    range_end=as_of,
                    inclusive_start=True,
                )
                n, mean, m2 = _welford_extend(
                    prior_n=0,
                    prior_mean=0.0,
                    prior_m2=0.0,
                    new_values=window_values,
                )
            else:
                # Welford incremental path: extend with inflow, evict outflow.
                inflow_values = _select_kind_observations(
                    session,
                    kind=kind,
                    ticker=ticker,
                    range_start=prior.as_of,
                    range_end=as_of,
                    inclusive_start=False,
                )
                prior_window_start = _window_start(as_of=prior.as_of, window_days=prior.window_days)
                new_window_start = _window_start(as_of=as_of, window_days=window_days)
                outflow_values = _select_kind_observations(
                    session,
                    kind=kind,
                    ticker=ticker,
                    range_start=prior_window_start,
                    range_end=new_window_start,
                    inclusive_start=True,
                    inclusive_end=False,
                )
                prior_m2 = prior.stdev * prior.stdev * prior.n_observations
                n, mean, m2 = _welford_extend(
                    prior_n=prior.n_observations,
                    prior_mean=prior.mean,
                    prior_m2=prior_m2,
                    new_values=inflow_values,
                )
                n, mean, m2 = _welford_evict(
                    prior_n=n,
                    prior_mean=mean,
                    prior_m2=m2,
                    evicted_values=outflow_values,
                )
            stdev = _stdev_from_m2(n=n, m2=m2)
            state = (
                CalibrationState.CALIBRATED if n >= min_observations else CalibrationState.BOOTSTRAP
            )
            _upsert_ticker_baseline(
                session,
                _TickerBaselineRow(
                    ticker=ticker,
                    kind=kind,
                    as_of=as_of,
                    mean=mean,
                    stdev=stdev,
                    n_observations=n,
                    window_days=window_days,
                    state=state,
                ),
            )
            payload: dict[str, Any] = {
                "mean": mean,
                "stdev": stdev,
                "n_observations": n,
                "window_days": window_days,
            }
            reason = (
                None
                if state is CalibrationState.CALIBRATED
                else f"{kind}_min_observations: {n} < {min_observations}"
            )
            out[ticker] = CalibratedValue(
                value=payload,
                state=state,
                bootstrap_reason=reason,
            )
    return out


# ---------------------------------------------------------------------------
# refresh_pair_lag
# ---------------------------------------------------------------------------


def _select_daily_closes(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> list[tuple[str, float]]:
    """Return (period_start, adj_close) tuples for ``ticker`` ascending."""
    stmt = (
        select(OhlcvBars.period_start, OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    return [(row[0], float(row[1])) for row in session.execute(stmt).all()]


def _pct_changes(closes: list[tuple[str, float]]) -> list[float]:
    """Day-over-day percentage changes given an ascending close series."""
    out: list[float] = []
    for prior, latest in pairwise(closes):
        prior_close = prior[1]
        if prior_close == 0.0:
            out.append(0.0)
        else:
            out.append((latest[1] - prior_close) / prior_close)
    return out


def _correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation between two equal-length series.

    Returns ``0.0`` when either series has zero variance — the lag with the
    least pathological alignment then wins by tie-break order.
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
    return float(dot / (var_a * var_b) ** 0.5)


def _estimate_lag_days(
    *,
    lead_returns: list[float],
    lag_returns: list[float],
    max_lag_days: int,
) -> tuple[float, int]:
    """Return ``(lag_days, n_pair_events)`` maximizing lead/lag correlation.

    For each candidate lag ``d`` in ``1..max_lag_days``, line up
    ``lead_returns[i]`` with ``lag_returns[i + d]``. The candidate with the
    highest correlation wins; ties go to the smallest lag. The reported
    ``n_pair_events`` is the aligned-pair count for the *winning* lag —
    short lags yield more pairs but only the winning lag's count is
    persisted, so consumers see the true sample size behind the estimate.
    """
    best_lag = 1
    best_corr: float | None = None
    best_n_aligned = 0
    for d in range(1, max_lag_days + 1):
        lead_window = lead_returns[: len(lag_returns) - d]
        lag_window = lag_returns[d:]
        if not lead_window:
            continue
        corr = _correlation(lead_window, lag_window)
        if best_corr is None or corr > best_corr:
            best_corr = corr
            best_lag = d
            best_n_aligned = len(lead_window)
    return float(best_lag), best_n_aligned


def _upsert_pair_lag(
    session: Session,
    *,
    lead: str,
    lag: str,
    as_of: str,
    estimate: float,
    n_events: int,
    state: CalibrationState,
) -> None:
    existing = session.execute(
        select(DistillationPairLag).where(
            DistillationPairLag.lead_ticker == lead,
            DistillationPairLag.lag_ticker == lag,
            DistillationPairLag.as_of == as_of,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DistillationPairLag(
                lead_ticker=lead,
                lag_ticker=lag,
                as_of=as_of,
                lead_lag_days_estimate=estimate,
                n_pair_events=n_events,
                last_overdue_flag=0,
                calibration_state=state.value,
                ingested_at=as_of,
            )
        )
    else:
        existing.lead_lag_days_estimate = estimate
        existing.n_pair_events = n_events
        existing.calibration_state = state.value
        existing.ingested_at = as_of


def refresh_pair_lag(
    session: Session,
    *,
    pair_scope: Sequence[tuple[str, str]],
    as_of: str,
    window_days: int,
    min_events: int,
    max_lag_days: int,
) -> dict[tuple[str, str], CalibratedValue]:
    """Refresh per-pair lead-lag timing estimates.

    For each ``(lead_ticker, lag_ticker)`` in ``pair_scope``:

    1. Read the lead and lag close series over ``[as_of - window_days, as_of]``.
    2. Compute day-over-day returns and find the integer lag in
       ``1..max_lag_days`` maximizing the lead-vs-lag-shifted correlation.
    3. Tag ``calibrated`` when the aligned-bar count meets ``min_events``;
       otherwise ``bootstrap``.
    4. UPSERT a row keyed ``(lead, lag, as_of)``.

    The whole loop runs inside a single SQLAlchemy transaction.
    """
    range_start = _window_start(as_of=as_of, window_days=window_days)
    out: dict[tuple[str, str], CalibratedValue] = {}
    with _refresh_transaction(session):
        for lead, lag in pair_scope:
            lead_series = _select_daily_closes(
                session, ticker=lead, range_start=range_start, range_end=as_of
            )
            lag_series = _select_daily_closes(
                session, ticker=lag, range_start=range_start, range_end=as_of
            )
            lead_returns = _pct_changes(lead_series)
            lag_returns = _pct_changes(lag_series)
            estimate, n_events = _estimate_lag_days(
                lead_returns=lead_returns,
                lag_returns=lag_returns,
                max_lag_days=max_lag_days,
            )
            state = (
                CalibrationState.CALIBRATED
                if n_events >= min_events
                else CalibrationState.BOOTSTRAP
            )
            _upsert_pair_lag(
                session,
                lead=lead,
                lag=lag,
                as_of=as_of,
                estimate=estimate,
                n_events=n_events,
                state=state,
            )
            payload: dict[str, Any] = {
                "lead_lag_days_estimate": estimate,
                "n_pair_events": n_events,
            }
            reason = (
                None
                if state is CalibrationState.CALIBRATED
                else f"pair_lag_min_events: {n_events} < {min_events}"
            )
            out[(lead, lag)] = CalibratedValue(
                value=payload,
                state=state,
                bootstrap_reason=reason,
            )
    return out


# ---------------------------------------------------------------------------
# refresh_contract_history
# ---------------------------------------------------------------------------


def _select_latest_snapshot(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
) -> tuple[float, float | None] | None:
    """Return ``(yes_probability, liquidity_usd)`` of the newest snapshot at or before ``as_of``."""
    stmt = (
        select(
            PredictionMarketSnapshots.yes_probability,
            PredictionMarketSnapshots.liquidity_usd,
        )
        .where(
            PredictionMarketSnapshots.contract_id == contract_id,
            PredictionMarketSnapshots.snapshot_ts <= as_of,
        )
        .order_by(PredictionMarketSnapshots.snapshot_ts.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    yes = float(row[0])
    liquidity = float(row[1]) if row[1] is not None else None
    return yes, liquidity


def _select_prior_history_yes(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
) -> float | None:
    """Return the ``yes_probability`` of the most recent history row strictly before ``as_of``."""
    stmt = (
        select(DistillationContractHistory.yes_probability)
        .where(
            DistillationContractHistory.contract_id == contract_id,
            DistillationContractHistory.snapshot_ts < as_of,
        )
        .order_by(DistillationContractHistory.snapshot_ts.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar_one_or_none()
    return float(value) if value is not None else None


def _upsert_contract_history(
    session: Session,
    *,
    contract_id: str,
    snapshot_ts: str,
    yes_probability: float,
    delta_pp: float,
    liquidity_usd: float,
    state: CalibrationState,
) -> None:
    existing = session.execute(
        select(DistillationContractHistory).where(
            DistillationContractHistory.contract_id == contract_id,
            DistillationContractHistory.snapshot_ts == snapshot_ts,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DistillationContractHistory(
                contract_id=contract_id,
                snapshot_ts=snapshot_ts,
                yes_probability=yes_probability,
                delta_pp_since_prior=delta_pp,
                liquidity_usd=liquidity_usd,
                calibration_state=state.value,
                ingested_at=snapshot_ts,
            )
        )
    else:
        existing.yes_probability = yes_probability
        existing.delta_pp_since_prior = delta_pp
        existing.liquidity_usd = liquidity_usd
        existing.calibration_state = state.value
        existing.ingested_at = snapshot_ts


def refresh_contract_history(
    session: Session,
    *,
    contract_scope: Sequence[str],
    as_of: str,
    min_observations: int,
) -> dict[str, CalibratedValue]:
    """Refresh per-contract prediction-market trailing probability history.

    For each contract in ``contract_scope``:

    1. Read the newest snapshot at or before ``as_of`` from
       ``prediction_market_snapshots``.
    2. Read the immediately preceding ``distillation_contract_history`` row
       to compute ``delta_pp_since_prior`` in percentage points.
    3. UPSERT a row keyed ``(contract_id, snapshot_ts == as_of)``.
    4. When no snapshot exists yet, write a bootstrap row at zero so the
       orchestrator sees a tagged-but-unknown reading rather than a missing
       block.
    """
    out: dict[str, CalibratedValue] = {}
    with _refresh_transaction(session):
        for contract_id in contract_scope:
            snapshot = _select_latest_snapshot(session, contract_id=contract_id, as_of=as_of)
            if snapshot is None:
                state = CalibrationState.BOOTSTRAP
                yes_probability = 0.0
                liquidity_usd = 0.0
                delta_pp = 0.0
                reason: str | None = f"contract_history_min_observations: 0 < {min_observations}"
            else:
                yes_probability, liquidity_raw = snapshot
                liquidity_usd = liquidity_raw if liquidity_raw is not None else 0.0
                prior_yes = _select_prior_history_yes(session, contract_id=contract_id, as_of=as_of)
                # delta_pp_since_prior is in percentage points: probabilities
                # are 0..1 so subtract and scale by 100. First refresh has no
                # prior history row → delta is zero.
                delta_pp = 0.0 if prior_yes is None else (yes_probability - prior_yes) * 100.0
                state = CalibrationState.CALIBRATED
                reason = None
            _upsert_contract_history(
                session,
                contract_id=contract_id,
                snapshot_ts=as_of,
                yes_probability=yes_probability,
                delta_pp=delta_pp,
                liquidity_usd=liquidity_usd,
                state=state,
            )
            payload: dict[str, Any] = {
                "yes_probability": yes_probability,
                "delta_pp_since_prior": delta_pp,
                "liquidity_usd": liquidity_usd,
            }
            out[contract_id] = CalibratedValue(
                value=payload,
                state=state,
                bootstrap_reason=reason,
            )
    return out


# ---------------------------------------------------------------------------
# refresh_event_history
# ---------------------------------------------------------------------------
#
# Two distinct paths inside a single entry point per story 07's "KEEP IN
# SEPARATE CODE PATHS" guidance:
#
# - Event detection: scan recent OHLCV bars for new gap / extended-hours
#   moves and append rows with ``outcome = "pending"`` (the column is NOT
#   NULL so the schema-friendly sentinel string stands in for NULL).
# - Outcome resolution: re-evaluate prior pending rows once enough sessions
#   have passed; set ``outcome`` to ``filled`` / ``unfilled`` /
#   ``confirmed`` / ``reversed`` and record ``outcome_observed_at``.

PENDING_OUTCOME = "pending"
GAP_FILLED_OUTCOME = "filled"
GAP_UNFILLED_OUTCOME = "unfilled"
EXTENDED_HOURS_CONFIRMED_OUTCOME = "confirmed"
EXTENDED_HOURS_REVERSED_OUTCOME = "reversed"


def _select_recent_bars(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> list[OhlcvBars]:
    """Return daily bars in ``[range_start, range_end]`` ascending."""
    stmt = (
        select(OhlcvBars)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    return list(session.execute(stmt).scalars().all())


def _detect_gap_event(
    *,
    prior_bar: OhlcvBars,
    current_bar: OhlcvBars,
    detection_atr_multiple: float,
) -> tuple[str, float] | None:
    """Return ``(direction, magnitude_atr_multiple)`` if the gap clears the threshold.

    Uses the prior bar's daily range as a poor-man's ATR proxy — story 07
    leaves the precise ATR formula to the per-category indicator stories
    (08*); the refresh primitive only needs to detect anything event-like.
    """
    prior_range = max(prior_bar.adj_high - prior_bar.adj_low, 1e-9)
    gap = current_bar.adj_open - prior_bar.adj_close
    magnitude = abs(gap) / prior_range
    if magnitude < detection_atr_multiple:
        return None
    direction = "up" if gap > 0 else "down"
    return direction, magnitude


def _resolve_gap_outcome(
    *,
    event_row: DistillationEventHistory,
    bars_after: list[OhlcvBars],
    gap_origin_close: float,
) -> tuple[str, str] | None:
    """Return ``(outcome, observed_at)`` if the gap has resolved.

    A gap fills when any subsequent bar's intraday range covers the prior
    close. Returns ``None`` if no resolving bar has been observed yet.
    """
    for bar in bars_after:
        if event_row.direction == "up":
            if bar.adj_low <= gap_origin_close:
                return GAP_FILLED_OUTCOME, bar.period_start
        else:
            if bar.adj_high >= gap_origin_close:
                return GAP_FILLED_OUTCOME, bar.period_start
    if bars_after:
        # Outcome window has closed without a fill.
        return GAP_UNFILLED_OUTCOME, bars_after[-1].period_start
    return None


def _resolve_extended_hours_outcome(
    *,
    event_row: DistillationEventHistory,
    bars_after: list[OhlcvBars],
) -> tuple[str, str] | None:
    """Return ``(outcome, observed_at)`` for an extended-hours direction call.

    Confirmed when the next regular-session bar opens in the same direction
    as the extended-hours move; reversed otherwise.
    """
    if not bars_after:
        return None
    next_bar = bars_after[0]
    if event_row.direction == "up":
        outcome = (
            EXTENDED_HOURS_CONFIRMED_OUTCOME
            if next_bar.adj_close >= next_bar.adj_open
            else EXTENDED_HOURS_REVERSED_OUTCOME
        )
    else:
        outcome = (
            EXTENDED_HOURS_CONFIRMED_OUTCOME
            if next_bar.adj_close <= next_bar.adj_open
            else EXTENDED_HOURS_REVERSED_OUTCOME
        )
    return outcome, next_bar.period_start


def _select_pending_events(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    as_of: str,
) -> list[DistillationEventHistory]:
    stmt = select(DistillationEventHistory).where(
        DistillationEventHistory.ticker == ticker,
        DistillationEventHistory.event_kind == event_kind,
        DistillationEventHistory.outcome == PENDING_OUTCOME,
        DistillationEventHistory.event_ts <= as_of,
    )
    return list(session.execute(stmt).scalars().all())


def _select_event_at(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    event_ts: str,
) -> DistillationEventHistory | None:
    return session.execute(
        select(DistillationEventHistory).where(
            DistillationEventHistory.ticker == ticker,
            DistillationEventHistory.event_kind == event_kind,
            DistillationEventHistory.event_ts == event_ts,
        )
    ).scalar_one_or_none()


def _select_post_event_bars(
    session: Session,
    *,
    ticker: str,
    event_ts: str,
    as_of: str,
) -> list[OhlcvBars]:
    """Bars at and after ``event_ts`` up to ``as_of``.

    The event-day bar is included because intraday fill on the same session
    is the most common gap-fill outcome — the gap is detected at the open
    and often closes during regular trading.
    """
    stmt = (
        select(OhlcvBars)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= event_ts,
            OhlcvBars.period_start <= as_of,
        )
        .order_by(OhlcvBars.period_start)
    )
    return list(session.execute(stmt).scalars().all())


def _count_events(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    as_of: str,
    only_resolved: bool,
) -> int:
    """Count event rows for ``ticker`` / ``event_kind`` at or before ``as_of``.

    ``only_resolved=True`` excludes pending rows so the caller sees the
    calibration-eligible event count.
    """
    stmt = (
        select(func.count())
        .select_from(DistillationEventHistory)
        .where(
            DistillationEventHistory.ticker == ticker,
            DistillationEventHistory.event_kind == event_kind,
            DistillationEventHistory.event_ts <= as_of,
        )
    )
    if only_resolved:
        stmt = stmt.where(DistillationEventHistory.outcome != PENDING_OUTCOME)
    return int(session.execute(stmt).scalar_one())


def _select_immediate_prior_close(
    session: Session,
    *,
    ticker: str,
    event_ts: str,
) -> float | None:
    """Return the close of the most recent bar strictly before ``event_ts``.

    The gap-fill resolution logic needs the gap-origin close to know what a
    fill must touch.
    """
    stmt = (
        select(OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start < event_ts,
        )
        .order_by(OhlcvBars.period_start.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar_one_or_none()
    return float(value) if value is not None else None


def _detect_new_events_for_ticker(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    detection_window_start: str,
    as_of: str,
    detection_atr_multiple: float,
) -> None:
    """Pass 1 — append pending rows for newly-detected events.

    The detection window covers ``[as_of - detection_window_days, as_of]``.
    Events already persisted under ``(ticker, kind, event_ts)`` are not
    duplicated; the UPSERT semantics live in the prior-row lookup.
    """
    recent_bars = _select_recent_bars(
        session,
        ticker=ticker,
        range_start=detection_window_start,
        range_end=as_of,
    )
    for prior_bar, current_bar in pairwise(recent_bars):
        detection = _detect_gap_event(
            prior_bar=prior_bar,
            current_bar=current_bar,
            detection_atr_multiple=detection_atr_multiple,
        )
        if detection is None:
            continue
        direction, magnitude = detection
        event_ts = current_bar.period_start
        existing = _select_event_at(
            session,
            ticker=ticker,
            event_kind=event_kind,
            event_ts=event_ts,
        )
        if existing is None:
            session.add(
                DistillationEventHistory(
                    ticker=ticker,
                    event_kind=event_kind,
                    event_ts=event_ts,
                    direction=direction,
                    magnitude_atr_multiple=magnitude,
                    outcome=PENDING_OUTCOME,
                    outcome_observed_at=None,
                    ingested_at=as_of,
                )
            )


def _resolve_pending_outcomes_for_ticker(
    session: Session,
    *,
    ticker: str,
    event_kind: str,
    as_of: str,
    outcome_resolution_days: int,
) -> None:
    """Pass 2 — update prior pending rows whose outcome window has elapsed.

    Pending rows still inside the resolution window are left untouched.
    """
    pending = _select_pending_events(session, ticker=ticker, event_kind=event_kind, as_of=as_of)
    as_of_dt = _parse_iso_utc(as_of)
    for event_row in pending:
        event_ts_dt = _parse_iso_utc(event_row.event_ts)
        if as_of_dt < event_ts_dt + timedelta(days=outcome_resolution_days):
            continue
        bars_after = _select_post_event_bars(
            session,
            ticker=ticker,
            event_ts=event_row.event_ts,
            as_of=as_of,
        )
        if event_kind == "gap":
            prior_close = _select_immediate_prior_close(
                session, ticker=ticker, event_ts=event_row.event_ts
            )
            if prior_close is None:
                continue
            resolution = _resolve_gap_outcome(
                event_row=event_row,
                bars_after=bars_after,
                gap_origin_close=prior_close,
            )
        else:
            resolution = _resolve_extended_hours_outcome(event_row=event_row, bars_after=bars_after)
        if resolution is None:
            continue
        outcome, observed_at = resolution
        event_row.outcome = outcome
        event_row.outcome_observed_at = observed_at


def refresh_event_history(
    session: Session,
    *,
    event_kind: str,
    ticker_scope: Sequence[str],
    as_of: str,
    min_events: int,
    detection_atr_multiple: float,
    outcome_resolution_days: int,
    detection_window_days: int,
) -> dict[str, CalibratedValue]:
    """Refresh per-ticker gap / extended-hours event history.

    Two passes per ticker, each in a separate code path:

    1. **Event detection.** Scan bars in
       ``[as_of - detection_window_days, as_of]`` for new events. For
       ``event_kind == "gap"`` a gap is detected when the open vs. prior
       close exceeds ``detection_atr_multiple`` times the prior bar's daily
       range. For ``event_kind == "extended_hours"`` the same threshold
       gates the magnitude. Detected events INSERT with
       ``outcome = "pending"`` and ``outcome_observed_at = NULL``.
    2. **Outcome resolution.** Walk every prior pending row whose event-ts
       is at least ``outcome_resolution_days`` before ``as_of``. Pull the
       bars that landed after the event and update ``outcome`` /
       ``outcome_observed_at``. The pending row is UPDATED in place; no
       new row is created.

    The whole loop runs inside a single SQLAlchemy transaction.
    """
    out: dict[str, CalibratedValue] = {}
    detection_window_start = _window_start(as_of=as_of, window_days=detection_window_days)
    with _refresh_transaction(session):
        for ticker in ticker_scope:
            _detect_new_events_for_ticker(
                session,
                ticker=ticker,
                event_kind=event_kind,
                detection_window_start=detection_window_start,
                as_of=as_of,
                detection_atr_multiple=detection_atr_multiple,
            )
            # Flush so the resolution pass below sees newly-inserted
            # pending rows within the same transaction.
            session.flush()
            _resolve_pending_outcomes_for_ticker(
                session,
                ticker=ticker,
                event_kind=event_kind,
                as_of=as_of,
                outcome_resolution_days=outcome_resolution_days,
            )
            session.flush()

            n_events = _count_events(
                session,
                ticker=ticker,
                event_kind=event_kind,
                as_of=as_of,
                only_resolved=False,
            )
            n_resolved = _count_events(
                session,
                ticker=ticker,
                event_kind=event_kind,
                as_of=as_of,
                only_resolved=True,
            )
            state = (
                CalibrationState.CALIBRATED
                if n_resolved >= min_events
                else CalibrationState.BOOTSTRAP
            )
            reason: str | None = (
                None
                if state is CalibrationState.CALIBRATED
                else f"{event_kind}_min_events: {n_resolved} < {min_events}"
            )
            out[ticker] = CalibratedValue(
                value={"n_events": n_events, "n_resolved": n_resolved},
                state=state,
                bootstrap_reason=reason,
            )
    return out


# ---------------------------------------------------------------------------
# refresh_composite_state
# ---------------------------------------------------------------------------


AlertDirection = Literal["upper", "lower"]


def _select_composite_history(
    session: Session,
    *,
    composite_kind: str,
    as_of: str,
) -> list[float]:
    """Return composite values strictly before ``as_of`` ascending."""
    stmt = (
        select(DistillationCompositeState.composite_value)
        .where(
            DistillationCompositeState.composite_kind == composite_kind,
            DistillationCompositeState.as_of < as_of,
        )
        .order_by(DistillationCompositeState.as_of)
    )
    return [float(v) for v in session.execute(stmt).scalars().all()]


def _percentile_rank(history: Sequence[float], value: float) -> float:
    """Return the percentile of ``value`` against ``history`` in 0..100.

    Uses the "<= value" convention: percentile = (count <= value) / n * 100.
    Returns 0.0 against an empty history.
    """
    if not history:
        return 0.0
    le = sum(1 for x in history if x <= value)
    return float(le) / float(len(history)) * 100.0


def _alert_active(
    *,
    percentile: float,
    alert_percentile: float,
    direction: AlertDirection,
) -> bool:
    if direction == "upper":
        return percentile >= alert_percentile
    return percentile <= alert_percentile


def _upsert_composite_state(
    session: Session,
    *,
    composite_kind: str,
    as_of: str,
    composite_value: float,
    component_breakdown_json: str,
    percentile_60d: float,
    alert_active: int,
    state: CalibrationState,
) -> None:
    existing = session.execute(
        select(DistillationCompositeState).where(
            DistillationCompositeState.composite_kind == composite_kind,
            DistillationCompositeState.as_of == as_of,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DistillationCompositeState(
                composite_kind=composite_kind,
                as_of=as_of,
                composite_value=composite_value,
                component_breakdown_json=component_breakdown_json,
                percentile_60d=percentile_60d,
                alert_active=alert_active,
                calibration_state=state.value,
                ingested_at=as_of,
            )
        )
    else:
        existing.composite_value = composite_value
        existing.component_breakdown_json = component_breakdown_json
        existing.percentile_60d = percentile_60d
        existing.alert_active = alert_active
        existing.calibration_state = state.value
        existing.ingested_at = as_of


def refresh_composite_state(
    session: Session,
    *,
    composite_kind: str,
    components: Mapping[str, float],
    as_of: str,
    min_observations: int,
    alert_percentile: float,
    alert_direction: AlertDirection,
) -> CalibratedValue:
    """Refresh a ``funding_stress`` or ``market_liquidity`` composite row.

    The caller passes the precomputed component values; the refresh primitive
    sums them into a composite, ranks it against the trailing distribution
    in ``distillation_composite_state``, derives the ``alert_active`` flag
    via ``alert_direction``, and UPSERTs a row keyed
    ``(composite_kind, as_of)``.

    The whole refresh runs inside a single SQLAlchemy transaction.
    """
    with _refresh_transaction(session):
        composite_value = float(sum(components.values()))
        history = _select_composite_history(session, composite_kind=composite_kind, as_of=as_of)
        percentile_60d = _percentile_rank(history, composite_value)
        alert = _alert_active(
            percentile=percentile_60d,
            alert_percentile=alert_percentile,
            direction=alert_direction,
        )
        n_history = len(history)
        state = (
            CalibrationState.CALIBRATED
            if n_history >= min_observations
            else CalibrationState.BOOTSTRAP
        )
        reason: str | None = (
            None
            if state is CalibrationState.CALIBRATED
            else f"{composite_kind}_min_observations: {n_history} < {min_observations}"
        )
        components_json = json.dumps(dict(components))
        _upsert_composite_state(
            session,
            composite_kind=composite_kind,
            as_of=as_of,
            composite_value=composite_value,
            component_breakdown_json=components_json,
            percentile_60d=percentile_60d,
            alert_active=int(alert),
            state=state,
        )
    return CalibratedValue(
        value={
            "composite_value": composite_value,
            "components": dict(components),
            "percentile_60d": percentile_60d,
            "alert_active": alert,
            "n_history": n_history,
        },
        state=state,
        bootstrap_reason=reason,
    )
