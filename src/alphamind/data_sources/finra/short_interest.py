"""
FINRA consolidated short interest collector.

Downloads bi-weekly CSV files from:
    https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv

CSV columns (relevant subset):
    settlementDate, symbol, currentShortPositionQuantity,
    previousShortPositionQuantity, averageDailyShareVolumeQuantity,
    daysToCoverQuantity, changePercent

Publication schedule:
    FINRA publishes bi-weekly on approximately the 15th and last business day
    of each month, with a ~7-10 day lag from settlement.  The scheduled cron
    (``0 10 * * tue``) polls weekly and exits cleanly when no new file exists.

404 handling:
    A 404 means the file is not yet published.  Silently skipped — the cron
    will retry next week.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
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
from alphamind.persistence.models import ShortInterestSnapshot

logger = logging.getLogger(__name__)

_SOURCE = "finra"


def _cdn_path(settlement_date_iso: str) -> str:
    """Convert ISO date string to the FINRA CDN path."""
    yyyymmdd = settlement_date_iso.replace("-", "")
    return f"/equity/otcmarket/biweekly/shrt{yyyymmdd}.csv"


def _biweekly_settlement_dates(months: int) -> list[str]:
    """
    Generate approximate FINRA bi-weekly settlement dates for the trailing
    *months* calendar months.

    FINRA publishes on approximately the 15th and the last business day of each
    month.  This function generates those candidate dates; the caller's 404
    handling drops dates that have not been published yet.
    """
    today = datetime.now(UTC).date()
    dates = []
    current = today.replace(day=1)
    for _ in range(months * 2):
        # Mid-month candidate: 15th
        mid = current.replace(day=15)
        if mid <= today:
            dates.append(mid.isoformat())
        # End-of-month candidate: last day
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1, day=1)
        else:
            next_month = current.replace(month=current.month + 1, day=1)
        end_of_month = next_month - timedelta(days=1)
        # Roll back to Friday if on a weekend
        while end_of_month.weekday() >= 5:
            end_of_month -= timedelta(days=1)
        if end_of_month <= today:
            dates.append(end_of_month.isoformat())

        # Move back one month
        if current.month == 1:
            current = current.replace(year=current.year - 1, month=12, day=1)
        else:
            current = current.replace(month=current.month - 1, day=1)

    return sorted(set(dates))


def _parse_csv(text: str, universe: set[str], ingested_at: str) -> list[dict[str, Any]]:
    """Parse a FINRA short interest CSV and return insertable dicts."""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for rec in reader:
        symbol = (rec.get("symbol") or rec.get("Symbol") or "").strip().upper()
        if symbol not in universe:
            continue
        try:
            settlement_raw = (rec.get("settlementDate") or rec.get("SettlementDate") or "").strip()
            current_si = int(rec.get("currentShortPositionQuantity") or 0)
            prev_si_raw = rec.get("previousShortPositionQuantity") or None
            adv_raw = rec.get("averageDailyShareVolumeQuantity") or None
            dtc_raw = rec.get("daysToCoverQuantity") or None
            chg_raw = rec.get("changePercent") or None

            rows.append(
                {
                    "settlement_date": settlement_raw,
                    "ticker": symbol,
                    "current_short_shares": current_si,
                    "previous_short_shares": int(prev_si_raw) if prev_si_raw else None,
                    "avg_daily_volume_shares": int(adv_raw) if adv_raw else None,
                    "days_to_cover": float(dtc_raw) if dtc_raw else None,
                    "change_pct": float(chg_raw) if chg_raw else None,
                    "source": _SOURCE,
                    "ingested_at": ingested_at,
                }
            )
        except (ValueError, KeyError):
            logger.warning("FINRA short interest: unparseable row %r — skipped.", rec)
    return rows


def collect_short_interest(
    settlement_dates: list[str] | None = None,
    *,
    client: FinraAPI | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Collect FINRA short interest for the provided *settlement_dates*.

    For each date, attempts to download the corresponding CDN file.  A 404 is
    treated as "not yet published" and silently skipped.  Successfully parsed
    rows are UPSERTed into ``short_interest_snapshots``.

    Parameters
    ----------
    settlement_dates:
        List of ISO date strings (``YYYY-MM-DD``).  When *None*, defaults to
        all dates generated by :func:`_biweekly_settlement_dates` for 6 months.
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
    if settlement_dates is None:
        settlement_dates = _biweekly_settlement_dates(months=6)

    universe = set(
        active_universe_tickers(session_factory=session_factory, include_benchmarks=False)
    )
    ingested_at = datetime.now(UTC).isoformat()

    with track_run("finra.short_interest", _repo=_repo) as run:
        rows_written = 0

        for sd in settlement_dates:
            path = _cdn_path(sd)
            try:
                text = client.get(path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.debug(
                        "FINRA short interest: no file for %s (404 — not yet published).", sd
                    )
                    continue
                raise

            parsed = _parse_csv(text, universe, ingested_at)
            if not parsed:
                continue

            with session_factory() as sess:
                for row in parsed:
                    stmt = sqlite_insert(ShortInterestSnapshot).values(**row)
                    stmt = stmt.on_conflict_do_nothing(index_elements=["settlement_date", "ticker"])
                    result = sess.execute(stmt)
                    if result.rowcount > 0:
                        rows_written += 1
                sess.commit()

        run.rows_written = rows_written


def bootstrap_short_interest(
    months: int = 6,
    *,
    client: FinraAPI | None = None,
    session_factory: Any = None,
    _repo: Any = None,
) -> None:
    """
    Bootstrap trailing *months* of short interest data.

    Generates bi-weekly settlement dates for the window, then calls
    :func:`collect_short_interest`.  Idempotent.

    Parameters
    ----------
    months:
        Number of trailing months to cover.  Defaults to 6 (≈ 12 settlement dates).
    client:
        :class:`FinraClient` — injectable for testing.
    session_factory:
        SQLAlchemy session factory.  Defaults to the production DB.
    _repo:
        ``track_run`` repository override for testing.
    """
    dates = _biweekly_settlement_dates(months=months)
    collect_short_interest(
        settlement_dates=dates,
        client=client,
        session_factory=session_factory,
        _repo=_repo,
    )
