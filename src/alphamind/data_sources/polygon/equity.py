"""
Polygon equity bars collection.

Exports
-------
collect_universe_bars(ticker_scope, timeframes, since, ...)
bootstrap_universe_bars(...)
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta
from typing import Any

from alphamind.data_sources._common import (
    RetryShape,
    active_universe_tickers,
    default_session_factory,
    resume_since,
    track_run,
    with_retries,
)
from alphamind.data_sources.polygon.client import PolygonClient
from alphamind.persistence.models import OhlcvBars

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIMEFRAMES = ["15min", "1h", "4h", "1d", "1w"]
_BOOTSTRAP_DAYS = 252

# Map AlphaMind timeframe strings to (multiplier, timespan) for Polygon API
_TIMEFRAME_TO_POLYGON: dict[str, tuple[int, str]] = {
    "15min": (15, "minute"),
    "1h": (1, "hour"),
    "4h": (4, "hour"),
    "1d": (1, "day"),
    "1w": (1, "week"),
}

_ET = zoneinfo.ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Session classification
# ---------------------------------------------------------------------------


def _classify_session(ts_ms: int, timeframe: str) -> str:
    """
    Return the NYSE trading session label for the bar starting at *ts_ms*
    (milliseconds since epoch, UTC).

    Daily and weekly bars aggregate the regular trading session by definition,
    so the timestamp's time-of-day (often midnight ET) is not meaningful.

    Intraday session boundaries (ET):
      04:00-09:30  pre_market
      09:30-16:00  regular
      16:00-20:00  after_hours
      otherwise    overnight
    """
    if timeframe in ("1d", "1w"):
        return "regular"
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    dt_et = dt_utc.astimezone(_ET)
    total_minutes = dt_et.hour * 60 + dt_et.minute

    if 4 * 60 <= total_minutes < 9 * 60 + 30:
        return "pre_market"
    if 9 * 60 + 30 <= total_minutes < 16 * 60:
        return "regular"
    if 16 * 60 <= total_minutes < 20 * 60:
        return "after_hours"
    return "overnight"


# ---------------------------------------------------------------------------
# Core collection
# ---------------------------------------------------------------------------


def collect_universe_bars(
    ticker_scope: list[str] | None = None,
    timeframes: list[str] | None = None,
    since: datetime | None = None,
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Fetch OHLCV bars for every (ticker, timeframe) pair and UPSERT into
    ``ohlcv_bars``.

    Two API calls per pair: ``adjusted=True`` then ``adjusted=False``.
    Merges the two responses into a single row with ``adj_*`` and ``unadj_*``
    columns.

    Parameters
    ----------
    ticker_scope:
        List of tickers to collect.  Defaults to all active universe +
        benchmark tickers from ``asset_universe``.
    timeframes:
        Subset of ``["15min","1h","4h","1d","1w"]``.  Defaults to all five.
    since:
        Lower bound (inclusive) for ``period_start``.  Defaults to
        ``resume_since`` over ``ohlcv_bars`` with a 1-day lookback.
    """
    if _client is None:
        _client = PolygonClient()
    _session_factory = _session_factory or default_session_factory()
    if ticker_scope is None:
        ticker_scope = active_universe_tickers(
            include_benchmarks=True, session_factory=_session_factory
        )
    tfs = timeframes if timeframes is not None else _TIMEFRAMES
    now_utc = datetime.now(UTC)
    since_dt = (
        since
        if since is not None
        else resume_since(
            column=OhlcvBars.period_start,
            filters=(OhlcvBars.source == "polygon",),
            default_lookback=timedelta(days=1),
            overlap=timedelta(hours=1),
            session_factory=_session_factory,
        )
    )
    from_str = since_dt.strftime("%Y-%m-%d")
    to_str = now_utc.strftime("%Y-%m-%d")

    @with_retries(RetryShape.critical, _sleep=lambda _: None)
    def _fetch_aggs(ticker: str, mult: int, span: str, adj: bool) -> list[Any]:
        return _client.get_aggs(  # type: ignore[no-any-return]
            ticker=ticker,
            multiplier=mult,
            timespan=span,
            from_=from_str,
            to=to_str,
            adjusted=adj,
        )

    with track_run("polygon.equity", _repo=_repo) as run:
        ingested_at = datetime.now(UTC).isoformat()
        rows_written = 0
        single = len(ticker_scope) == 1 and len(tfs) == 1
        for tf in tfs:
            mult, span = _TIMEFRAME_TO_POLYGON[tf]
            for ticker in ticker_scope:
                try:
                    adj_bars = _fetch_aggs(ticker, mult, span, True)
                    unadj_bars = _fetch_aggs(ticker, mult, span, False)
                except Exception:
                    if single:
                        raise
                    continue

                # Index unadjusted bars by timestamp for O(1) lookup
                unadj_by_ts: dict[int, Any] = {b.timestamp: b for b in unadj_bars}

                rows_to_write: list[OhlcvBars] = []
                for ab in adj_bars:
                    ub = unadj_by_ts.get(ab.timestamp)
                    if ub is None:
                        continue
                    bar_start = datetime.fromtimestamp(ab.timestamp / 1000, tz=UTC).isoformat()
                    rows_to_write.append(
                        OhlcvBars(
                            ticker=ticker,
                            timeframe=tf,
                            period_start=bar_start,
                            period_end=bar_start,
                            session=_classify_session(ab.timestamp, tf),
                            adj_open=ab.open,
                            adj_high=ab.high,
                            adj_low=ab.low,
                            adj_close=ab.close,
                            adj_volume=int(ab.volume),
                            adj_vwap=ab.vwap,
                            unadj_open=ub.open,
                            unadj_high=ub.high,
                            unadj_low=ub.low,
                            unadj_close=ub.close,
                            unadj_volume=int(ub.volume),
                            unadj_vwap=ub.vwap,
                            trade_count=ab.transactions,
                            source="polygon",
                            ingested_at=ingested_at,
                        )
                    )

                _upsert_bars(_session_factory, rows_to_write)
                rows_written += len(rows_to_write)

        run.rows_written = rows_written


def _upsert_bars(session_factory: Any, rows: list[OhlcvBars]) -> None:
    """INSERT OR REPLACE rows into ``ohlcv_bars``."""
    if not rows:
        return
    with session_factory() as sess:
        for row in rows:
            sess.merge(row)
        sess.commit()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def bootstrap_universe_bars(
    *,
    _client: Any = None,
    _session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Bootstrap 252 trading days x 5 timeframes x universe + benchmarks.

    Reads active ticker scope from the ``asset_universe`` table.
    """
    sf = _session_factory or default_session_factory()
    tickers = active_universe_tickers(include_benchmarks=True, session_factory=sf)
    since = datetime.now(UTC) - timedelta(days=_BOOTSTRAP_DAYS)

    collect_universe_bars(
        ticker_scope=tickers,
        timeframes=_TIMEFRAMES,
        since=since,
        _client=_client,
        _session_factory=sf,
        _repo=_repo,
    )
