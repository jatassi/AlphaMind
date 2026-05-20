"""
iBorrowDesk borrow cost collector.

Two public entry points:

``collect_borrow_cost(ticker_scope=None)``
    Daily universe sweep.  For each active ticker, fetches via the rate-limited
    client, then UPSERTs ``borrow_cost_daily`` (from ``daily[]``) and
    ``borrow_cost_intraday`` (from ``real_time[]``).

    - ``IBorrowDeskCoverageError`` (404 not_found) → warn-log, continue.
    - ``IBorrowDeskBlockedError`` (444 / TCP empty-reply) → halt sweep,
      persist a resume cursor at the blocked ticker, and complete as a
      partial success.  No further requests are issued within the same run;
      the next run resumes at the cursor so coverage rotates across the full
      universe over consecutive runs (``borrow_cost_sweep_cursor``).

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
from alphamind.persistence.models import (
    BorrowCostDaily,
    BorrowCostIntraday,
    BorrowCostSweepCursor,
)

log = logging.getLogger(__name__)

_SOURCE = "iborrowdesk"

# Collector name — used both as the ``collection_runs`` label and as the
# primary key of the ``borrow_cost_sweep_cursor`` resume-cursor row.
_SWEEP_COLLECTOR = "iborrowdesk.borrow_cost"

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
    """Return the sorted ticker list to sweep. Replaced by patch in tests.

    The list is sorted so the rotating resume cursor (``_resume_order``)
    addresses a stable, deterministic sweep order run-to-run — the
    ``asset_universe`` query carries no ``ORDER BY`` of its own.
    """
    if ticker_scope is not None:
        return sorted(ticker_scope)
    return sorted(active_universe_tickers(include_benchmarks=False))


def _now() -> datetime:
    """Return current UTC time. Replaced by patch in tests."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Resume cursor — rotates the sweep across the universe over several runs
# ---------------------------------------------------------------------------


def _resume_order(sess: Any, tickers: list[str]) -> list[str]:
    """Rotate *tickers* so the sweep resumes at the persisted cursor.

    The previous run records the ticker its 444 block tripped on; resuming
    there rotates coverage across the universe rather than restarting at the
    same head every run. Returns *tickers* unchanged when no cursor is set,
    when a completed sweep cleared it, or when the cursor ticker has dropped
    out of the active universe.
    """
    cursor = sess.get(BorrowCostSweepCursor, _SWEEP_COLLECTOR)
    if cursor is None or cursor.next_ticker is None:
        return list(tickers)
    try:
        idx = tickers.index(cursor.next_ticker)
    except ValueError:
        return list(tickers)
    return tickers[idx:] + tickers[:idx]


def _save_cursor(sess: Any, next_ticker: str | None, updated_at: str) -> None:
    """Persist where the next sweep should resume.

    *next_ticker* is the ticker a 444 block halted on, or *None* when the
    sweep completed without a block (resetting the cursor to the head).
    """
    cursor = sess.get(BorrowCostSweepCursor, _SWEEP_COLLECTOR)
    if cursor is None:
        cursor = BorrowCostSweepCursor(collector=_SWEEP_COLLECTOR)
        sess.add(cursor)
    cursor.next_ticker = next_ticker
    cursor.updated_at = updated_at


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

    iBorrowDesk 444-blocks the egress IP after a fixed number of requests per
    run, so one run only reaches part of the universe. On a block the sweep
    halts (the client contract requires it — sustained requests deepen the
    block), records the blocked ticker as a resume cursor, and completes as a
    partial success rather than ``failed``. The next run resumes at that
    ticker so coverage rotates across the whole universe over consecutive
    runs.

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

    with track_run(_SWEEP_COLLECTOR, _repo=_repo) as run:
        ingested_at = _now().isoformat()
        total_rows = 0
        swept = 0
        blocked_at: str | None = None

        with _session_factory() as sess:
            sweep = _resume_order(sess, tickers)
            for ticker in sweep:
                try:
                    payload = _fetch_ticker(ticker)
                except IBorrowDeskCoverageError:
                    log.warning(
                        "iborrowdesk: ticker %r not in coverage — skipping",
                        ticker,
                    )
                    swept += 1
                    continue
                except IBorrowDeskBlockedError:
                    # 444 block — iBorrowDesk's per-run request quota for this
                    # egress IP is spent. Halt the sweep and record this
                    # ticker so the next run resumes here, rotating coverage
                    # across the universe instead of stranding the tail.
                    blocked_at = ticker
                    log.warning(
                        "iborrowdesk: 444 block at %r after %d/%d tickers — "
                        "halting sweep; next run resumes here",
                        ticker,
                        swept,
                        len(sweep),
                    )
                    break

                total_rows += _process_response(sess, ticker, payload, ingested_at)
                swept += 1

            _save_cursor(sess, blocked_at, ingested_at)
            sess.commit()

        run.rows_written = total_rows
        if blocked_at is not None:
            run.error_summary = (
                f"iBorrowDesk 444 block at {blocked_at} — swept "
                f"{swept}/{len(sweep)} tickers; next run resumes at {blocked_at}"
            )


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
        latest: str | None = real_time[-1].get("datetime")
        return latest
    return None
