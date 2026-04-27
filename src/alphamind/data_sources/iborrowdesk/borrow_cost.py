"""
iBorrowDesk borrow cost collector.

Two public entry points:

``collect_borrow_cost(ticker_scope=None)``
    Daily universe sweep.  For each active ticker, fetches via the rate-limited
    client, then UPSERTs ``borrow_cost_daily`` (from ``daily[]``) and
    ``borrow_cost_intraday`` (from ``real_time[]``).

    - ``IBorrowDeskCoverageError`` (404 not_found) → warn-log, continue.
    - ``IBorrowDeskBlockedError`` (444 / TCP empty-reply) → halt sweep,
      mark ``collection_runs`` failed, re-raise.  No further requests are
      issued within the same run.

``refresh_ticker(ticker)``
    On-demand single-ticker refresh.  Same parse + upsert path; respects the
    global ``RateLimiter``.  Returns the most-recent ``real_time.datetime``
    from the response (or ``None`` when the ``real_time`` array is empty).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alphamind.data_sources._common import (
    RateLimiter,
    active_universe_tickers,
    default_session_factory,
    track_run,
)
from alphamind.data_sources.iborrowdesk.client import (
    IBorrowDeskBlockedError,
    IBorrowDeskClient,
    IBorrowDeskCoverageError,
)
from alphamind.persistence.models import BorrowCostDaily, BorrowCostIntraday

log = logging.getLogger(__name__)

_SOURCE = "iborrowdesk"

# Module-level client — reused across the full universe sweep so a single
# httpx.Client connection pool serves all tickers in one run.
_client = IBorrowDeskClient()


# ---------------------------------------------------------------------------
# Injectable helpers (replaced by patch in tests)
# ---------------------------------------------------------------------------


def _fetch_ticker(ticker: str) -> dict[str, Any]:
    """Fetch one ticker from iBorrowDesk. Replaced by patch in tests."""
    return _client.fetch_ticker(ticker)


def _get_tickers(ticker_scope: list[str] | None) -> list[str]:
    """Return the ticker list to sweep. Replaced by patch in tests."""
    if ticker_scope is not None:
        return ticker_scope
    return active_universe_tickers(include_benchmarks=False)


def _now() -> datetime:
    """Return current UTC time. Replaced by patch in tests."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Parse + upsert helpers
# ---------------------------------------------------------------------------


def _upsert_daily(sess: Any, ticker: str, entry: dict[str, Any], ingested_at: str) -> int:
    """Upsert one ``borrow_cost_daily`` row. Returns 1 if row written/updated."""
    observation_date: str = entry["date"]
    existing = sess.get(BorrowCostDaily, (observation_date, ticker))
    if existing is None:
        sess.add(
            BorrowCostDaily(
                observation_date=observation_date,
                ticker=ticker,
                fee_pct=entry.get("fee"),
                rebate_pct=entry.get("rebate"),
                available_shares=entry.get("available"),
                intraday_high_fee_pct=entry.get("high_fee"),
                intraday_low_fee_pct=entry.get("low_fee"),
                intraday_high_available_shares=entry.get("high_available"),
                intraday_low_available_shares=entry.get("low_available"),
                source=_SOURCE,
                ingested_at=ingested_at,
            )
        )
    else:
        existing.fee_pct = entry.get("fee")
        existing.rebate_pct = entry.get("rebate")
        existing.available_shares = entry.get("available")
        existing.intraday_high_fee_pct = entry.get("high_fee")
        existing.intraday_low_fee_pct = entry.get("low_fee")
        existing.intraday_high_available_shares = entry.get("high_available")
        existing.intraday_low_available_shares = entry.get("low_available")
        existing.ingested_at = ingested_at
    return 1


def _upsert_intraday(sess: Any, ticker: str, entry: dict[str, Any], ingested_at: str) -> int:
    """Upsert one ``borrow_cost_intraday`` row. Returns 1 if row written/updated."""
    snapshot_at: str = entry["datetime"]
    existing = sess.get(BorrowCostIntraday, (snapshot_at, ticker))
    if existing is None:
        sess.add(
            BorrowCostIntraday(
                snapshot_at=snapshot_at,
                ticker=ticker,
                fee_pct=entry.get("fee"),
                available_shares=entry.get("available"),
                source=_SOURCE,
                ingested_at=ingested_at,
            )
        )
    else:
        existing.fee_pct = entry.get("fee")
        existing.available_shares = entry.get("available")
        existing.ingested_at = ingested_at
    return 1


def _process_response(
    sess: Any,
    ticker: str,
    payload: dict[str, Any],
    ingested_at: str,
) -> int:
    """Parse the iBorrowDesk payload and upsert rows. Returns total rows written."""
    rows = 0
    for entry in payload.get("daily") or []:
        rows += _upsert_daily(sess, ticker, entry, ingested_at)
    for entry in payload.get("real_time") or []:
        rows += _upsert_intraday(sess, ticker, entry, ingested_at)
    return rows


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def collect_borrow_cost(
    ticker_scope: list[str] | None = None,
    *,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Daily universe sweep: fetch borrow cost for every active ticker.

    Parameters
    ----------
    ticker_scope:
        Explicit list of tickers to process. Defaults to ``asset_universe``
        active rows (``is_active=1``).
    _session_factory:
        SQLAlchemy session factory override (for tests).
    _repo:
        ``track_run`` repo override (for tests).
    """
    if _session_factory is None:
        _session_factory = default_session_factory()

    tickers = _get_tickers(ticker_scope)

    with track_run("iborrowdesk.borrow_cost", _repo=_repo) as run:
        ingested_at = _now().isoformat()
        total_rows = 0

        with _session_factory() as sess:
            for ticker in tickers:
                try:
                    payload = _fetch_ticker(ticker)
                except IBorrowDeskCoverageError:
                    log.warning(
                        "iborrowdesk: ticker %r not in coverage — skipping",
                        ticker,
                    )
                    continue
                except IBorrowDeskBlockedError:
                    # Commit whatever we have, then propagate to abort the sweep.
                    sess.commit()
                    raise

                total_rows += _process_response(sess, ticker, payload, ingested_at)

            sess.commit()

        run.rows_written = total_rows


def refresh_ticker(
    ticker: str,
    *,
    _session_factory: Any = None,
    _repo: Any = None,
    _rate_limiter: RateLimiter | None = None,
) -> str | None:
    """
    On-demand single-ticker borrow cost refresh.

    Parameters
    ----------
    ticker:
        Ticker symbol to refresh.
    _session_factory:
        SQLAlchemy session factory override (for tests).
    _repo:
        ``track_run`` repo override (for tests).
    _rate_limiter:
        ``RateLimiter`` override (for tests).  When *None*, a fresh
        ``IBorrowDeskClient`` with its default limiter is used via
        ``_fetch_ticker``.

    Returns
    -------
    str | None
        ISO datetime string of the most-recent ``real_time`` entry, or *None*
        when the response contains no intraday snapshots.
    """
    if _session_factory is None:
        _session_factory = default_session_factory()

    if _rate_limiter is not None:
        # Use an explicit client so the injected limiter is honoured.
        client = IBorrowDeskClient(rate_limiter=_rate_limiter)
        payload = client.fetch_ticker(ticker)
    else:
        payload = _fetch_ticker(ticker)

    ingested_at = _now().isoformat()

    with track_run("iborrowdesk.refresh_ticker", _repo=_repo) as run:
        with _session_factory() as sess:
            rows = _process_response(sess, ticker, payload, ingested_at)
            sess.commit()
        run.rows_written = rows

    real_time = payload.get("real_time") or []
    if real_time:
        return real_time[-1].get("datetime")
    return None
