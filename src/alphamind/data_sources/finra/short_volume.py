"""
FINRA Reg SHO daily short volume collector.

Downloads consolidated market (``cnms``) pipe-delimited files from:
    https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt

File format (pipe-delimited):
    Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market

Publication schedule:
    FINRA publishes the file by 6 PM ET on the trade date.  The scheduled
    cron (``0 19 * * mon-fri``) fires at 7 PM ET to allow for late publication.

404 handling:
    A 404 on a *future-dated* URL (file not yet published) is expected and
    silently swallowed.  A 404 on a date that should already be published is
    also swallowed here — the caller (runner) logs the gap separately.
    Any other HTTP error propagates.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alphamind.data_sources._common import (
    active_universe_tickers,
    default_session_factory,
    track_run,
)
from alphamind.data_sources.finra._protocol import FinraAPI
from alphamind.data_sources.finra.client import FinraClient
from alphamind.persistence.models import ShortVolumeDaily

logger = logging.getLogger(__name__)

_MARKET = "cnms"
_SOURCE = "finra"


def _cdn_path(trade_date: date) -> str:
    return f"/equity/regsho/daily/CNMSshvol{trade_date.strftime('%Y%m%d')}.txt"


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def _trading_days(start: date, end: date) -> list[date]:
    """Return weekdays between *start* and *end* inclusive.

    A simple weekday filter — FINRA does not publish on US market holidays but
    also does not publish on weekends.  For holiday handling, the 404 on the
    file is silently swallowed.
    """
    result = []
    current = start
    while current <= end:
        if not _is_weekend(current):
            result.append(current)
        current += timedelta(days=1)
    return result


def _parse_file(text: str, universe: set[str], ingested_at: str) -> list[dict[str, Any]]:
    """Parse a FINRA short volume pipe-delimited text and return insertable dicts."""
    rows = []
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for rec in reader:
        ticker = (rec.get("Symbol") or "").strip().upper()
        if ticker not in universe:
            continue
        try:
            trade_date_raw = (rec.get("Date") or "").strip()
            # FINRA format: YYYYMMDD → ISO date YYYY-MM-DD
            trade_date_iso = f"{trade_date_raw[:4]}-{trade_date_raw[4:6]}-{trade_date_raw[6:8]}"
            rows.append(
                {
                    "trade_date": trade_date_iso,
                    "ticker": ticker,
                    "market": _MARKET,
                    "short_volume": int(rec.get("ShortVolume") or 0),
                    "short_exempt_volume": int(rec.get("ShortExemptVolume") or 0),
                    "total_volume": int(rec.get("TotalVolume") or 0),
                    "source": _SOURCE,
                    "ingested_at": ingested_at,
                }
            )
        except (ValueError, KeyError):
            logger.warning("FINRA short volume: unparseable row %r — skipped.", rec)
    return rows


def collect_short_volume(
    since: date | None = None,
    until: date | None = None,
    *,
    client: FinraAPI | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Collect FINRA daily short volume for trading days between *since* and *until*.

    Walks each trading day in the window, downloads the ``cnms`` file, filters
    to universe tickers, and UPSERTs ``short_volume_daily``.  A 404 on any
    single day is treated as "file not yet published" and silently skipped.

    Parameters
    ----------
    since:
        First trade date to collect (inclusive).  Defaults to yesterday.
    until:
        Last trade date to collect (inclusive).  Defaults to today.
    client:
        :class:`FinraClient` — injectable for testing.
    session_factory:
        SQLAlchemy session factory.  Defaults to the production DB.
    _repo:
        ``track_run`` repository override for testing.
    """
    if client is None:
        client = FinraClient()
    if session_factory is None:
        session_factory = default_session_factory()

    today = datetime.now(UTC).date()
    start = since if since is not None else today - timedelta(days=1)
    end = until if until is not None else today

    universe = set(
        active_universe_tickers(session_factory=session_factory, include_benchmarks=False)
    )
    ingested_at = datetime.now(UTC).isoformat()

    with track_run("finra.short_volume", _repo=_repo) as run:
        rows_written = 0

        for trade_date in _trading_days(start, end):
            path = _cdn_path(trade_date)
            try:
                text = client.get(path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.debug(
                        "FINRA short volume: no file for %s (404 — not yet published).",
                        trade_date,
                    )
                    continue
                raise

            parsed = _parse_file(text, universe, ingested_at)
            if not parsed:
                continue

            with session_factory() as sess:
                for row in parsed:
                    stmt = sqlite_insert(ShortVolumeDaily).values(**row)
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["trade_date", "ticker", "market"]
                    )
                    result = sess.execute(stmt)
                    if result.rowcount > 0:
                        rows_written += 1
                sess.commit()

        run.rows_written = rows_written


def bootstrap_short_volume(
    days: int = 60,
    *,
    client: FinraAPI | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Bootstrap trailing *days* trading days of short volume.

    Idempotent — re-running against a partially bootstrapped DB skips already
    stored rows via UPSERT conflict handling.

    Parameters
    ----------
    days:
        Number of calendar days to look back.  Defaults to 60.
    client:
        :class:`FinraClient` — injectable for testing.
    session_factory:
        SQLAlchemy session factory.  Defaults to the production DB.
    _repo:
        ``track_run`` repository override for testing.
    """
    today = datetime.now(UTC).date()
    since = today - timedelta(days=days)
    collect_short_volume(
        since=since,
        until=today,
        client=client,
        session_factory=session_factory,
        _repo=_repo,
    )
