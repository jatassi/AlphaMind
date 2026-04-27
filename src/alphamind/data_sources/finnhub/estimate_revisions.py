"""
Finnhub estimate-revisions collector — Q5.

Polls Finnhub's earnings/revenue estimate endpoints daily, diffs the observed
consensus against the latest stored row per ``(ticker, fiscal_year,
fiscal_period, metric)``, and inserts a new row only when the consensus has
changed.

Fiscal-period mapping
---------------------
Finnhub returns the fiscal-period end date (ISO string).  Mapping to
``fiscal_year`` + ``fiscal_period`` (``Q1``/``Q2``/``Q3``/``Q4``/``FY``):

1. Look up ``earnings_event_details`` for the ticker: the most recent row
   whose fiscal-calendar end date matches (within ± 30 days) provides the
   authoritative mapping for that period.
2. Fall back to calendar-quarter mapping: month 1-3 -> Q1, 4-6 -> Q2,
   7-9 -> Q3, 10-12 -> Q4.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import finnhub

from alphamind.data_sources._common import (
    RetryShape,
    active_universe_tickers,
    track_run,
    with_retries,
)
from alphamind.persistence.models import (
    Base,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
)
from alphamind.persistence.session import make_engine, make_session_factory

_PERIOD_MATCH_DAYS = 30


def _get_api_key() -> str:
    return os.environ.get("FINNHUB_API_KEY", "")


# ---------------------------------------------------------------------------
# Fiscal-period helpers
# ---------------------------------------------------------------------------


def _calendar_quarter(period_date: str) -> tuple[int, str]:
    """Map a period-end ISO date to (fiscal_year, fiscal_period) via calendar quarter."""
    dt = datetime.fromisoformat(period_date)
    month = dt.month
    if month <= 3:
        period = "Q1"
    elif month <= 6:
        period = "Q2"
    elif month <= 9:
        period = "Q3"
    else:
        period = "Q4"
    return dt.year, period


def _resolve_fiscal_period(
    sess: Any,
    ticker: str,
    period_date: str,
) -> tuple[int, str]:
    """
    Return ``(fiscal_year, fiscal_period)`` for the given period-end date.

    Searches ``earnings_event_details`` for a row whose scheduled earnings date
    is within ``_PERIOD_MATCH_DAYS`` of ``period_date``.  Falls back to
    calendar-quarter mapping when no precedent exists.
    """
    try:
        target_dt = datetime.fromisoformat(period_date)
    except ValueError:
        return _calendar_quarter(period_date)

    window_start = (target_dt - timedelta(days=_PERIOD_MATCH_DAYS)).isoformat()
    window_end = (target_dt + timedelta(days=_PERIOD_MATCH_DAYS)).isoformat()

    row = (
        sess.query(EarningsEventDetails)
        .join(EventCalendar, EarningsEventDetails.event_id == EventCalendar.event_id)
        .filter(
            EarningsEventDetails.ticker == ticker,
            EventCalendar.scheduled_at >= window_start,
            EventCalendar.scheduled_at <= window_end,
        )
        .order_by(EventCalendar.scheduled_at.desc())
        .first()
    )

    if row is not None:
        return row.fiscal_year, row.fiscal_period

    return _calendar_quarter(period_date)


# ---------------------------------------------------------------------------
# Latest-stored lookup
# ---------------------------------------------------------------------------


def _latest_stored(
    sess: Any,
    ticker: str,
    fiscal_year: int,
    fiscal_period: str,
    metric: str,
) -> EarningsEstimateRevisions | None:
    """Return the most-recent stored row for the given composite key, or None."""
    row: EarningsEstimateRevisions | None = (
        sess.query(EarningsEstimateRevisions)
        .filter_by(
            ticker=ticker, fiscal_year=fiscal_year, fiscal_period=fiscal_period, metric=metric
        )
        .order_by(EarningsEstimateRevisions.revised_at.desc())
        .first()
    )
    return row


# ---------------------------------------------------------------------------
# Fetch helpers (with retry)
# ---------------------------------------------------------------------------


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_eps_estimates(sdk: Any, symbol: str) -> list[dict[str, Any]]:
    result = sdk.earnings_estimate(symbol) or {}
    return result.get("data") or []


@with_retries(RetryShape.important, _sleep=lambda _: None)
def _fetch_revenue_estimates(sdk: Any, symbol: str) -> list[dict[str, Any]]:
    result = sdk.revenue_estimate(symbol) or {}
    return result.get("data") or []


# ---------------------------------------------------------------------------
# Core ingest
# ---------------------------------------------------------------------------


def _ingest_entries(
    sess: Any,
    ticker: str,
    entries: list[dict[str, Any]],
    metric: str,
    value_key: str,
    *,
    bootstrap: bool,
) -> int:
    """Persist one metric's entries for a ticker; return count of rows written."""
    rows_written = 0
    for entry in entries:
        period_date: str = entry.get("period") or ""
        if not period_date:
            continue

        raw_value = entry.get(value_key)
        if raw_value is None:
            continue
        consensus_value = float(raw_value)

        num_analysts_raw = entry.get("numberAnalysts")
        num_analysts = int(num_analysts_raw) if num_analysts_raw is not None else None

        fiscal_year, fiscal_period = _resolve_fiscal_period(sess, ticker, period_date)

        latest = _latest_stored(sess, ticker, fiscal_year, fiscal_period, metric)
        if bootstrap:
            if latest is not None:
                continue
            prior_value = None
        else:
            if latest is not None and latest.consensus_value == consensus_value:
                continue
            prior_value = latest.consensus_value if latest is not None else None

        now_iso = datetime.now(UTC).isoformat()
        sess.add(
            EarningsEstimateRevisions(
                revised_at=now_iso,
                ticker=ticker,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                metric=metric,
                prior_consensus_value=prior_value,
                consensus_value=consensus_value,
                num_analysts=num_analysts,
                source="finnhub",
                ingested_at=now_iso,
            )
        )
        rows_written += 1
    return rows_written


def _ingest_ticker(
    sess: Any,
    sdk: Any,
    ticker: str,
    *,
    bootstrap: bool = False,
) -> int:
    """Fetch and persist estimate revisions for one ticker; return rows written."""
    eps_data = _fetch_eps_estimates(sdk, ticker)
    rev_data = _fetch_revenue_estimates(sdk, ticker)
    return _ingest_entries(
        sess, ticker, eps_data, "eps", "epsAvg", bootstrap=bootstrap
    ) + _ingest_entries(sess, ticker, rev_data, "revenue", "revenueAvg", bootstrap=bootstrap)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_estimate_revisions(
    ticker_scope: list[str] | None = None,
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Poll Finnhub estimate endpoints for each active-universe ticker and persist
    new rows to ``earnings_estimate_revisions`` only when the consensus has changed.

    Parameters
    ----------
    ticker_scope:
        Explicit list of tickers to process.  When ``None``, the active
        universe (``asset_universe.is_active=1``) is used.
    """
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())

    if ticker_scope is None:
        ticker_scope = active_universe_tickers(include_benchmarks=False, session_factory=sf)

    with track_run("finnhub.estimate_revisions", _repo=_repo) as run:
        total = 0
        with sf() as sess:
            for ticker in ticker_scope:
                total += _ingest_ticker(sess, sdk, ticker)
            sess.commit()
        run.rows_written = total


def bootstrap_estimate_revisions(
    *,
    _engine: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Seed ``earnings_estimate_revisions`` with the initial snapshot for every
    active-universe ticker.

    Each ``(ticker, fiscal_year, fiscal_period, metric)`` tuple that has no
    prior row is inserted with ``prior_consensus_value = NULL``.  Already-seeded
    tuples are skipped (idempotent).
    """
    engine = _engine or make_engine()
    sf = _session_factory or make_session_factory(engine)
    Base.metadata.create_all(engine)

    sdk = finnhub.Client(api_key=_get_api_key())
    tickers = active_universe_tickers(include_benchmarks=False, session_factory=sf)

    with track_run("finnhub.bootstrap_estimate_revisions", _repo=_repo) as run:
        total = 0
        with sf() as sess:
            for ticker in tickers:
                total += _ingest_ticker(sess, sdk, ticker, bootstrap=True)
            sess.commit()
        run.rows_written = total
