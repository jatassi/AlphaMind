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
    A 404 is interpreted against the publication SLA:

    * **Pre-SLA** — the settlement date is within the last 10 calendar days.
      Treated as "file not yet published" and silently skipped; the weekly
      cron retries next week.
    * **Post-SLA** — the settlement date is more than 10 calendar days in the
      past.  The file *should* be available; a 404 indicates a CDN outage or
      a URL-pattern break.  The skipped date is recorded in
      ``collection_runs.error_summary`` so freshness checks can distinguish
      this from the normal pre-publication poll.  The run still completes
      ``success`` so a single missing back-date does not mark the whole
      window failed.
"""

from __future__ import annotations

import csv
import io
import logging
import zoneinfo
from datetime import UTC, date, datetime, timedelta
from typing import Any

import exchange_calendars
import httpx
import pandas as pd
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
_ET = zoneinfo.ZoneInfo("America/New_York")
_XNYS = exchange_calendars.get_calendar("XNYS")
_PUBLICATION_LAG_DAYS = 10  # FINRA publishes ~7-10 days after settlement.


def _persist_rows(rows: list[dict[str, Any]], session_factory: Any) -> int:
    """UPSERT *rows* into ``short_interest_snapshots``. Returns the new-row count."""
    new_rows = 0
    with session_factory() as sess:
        for row in rows:
            stmt = sqlite_insert(ShortInterestSnapshot).values(**row)
            stmt = stmt.on_conflict_do_nothing(index_elements=["settlement_date", "ticker"])
            result = sess.execute(stmt)
            if result.rowcount > 0:
                new_rows += 1
        sess.commit()
    return new_rows


def _classify_404(settlement_iso: str, now_utc: datetime) -> bool:
    """Return ``True`` when a 404 for *settlement_iso* is a post-SLA outage.

    Side effect: emits a WARNING or DEBUG log line describing the
    classification, so callers don't duplicate the logging.
    """
    settlement = date.fromisoformat(settlement_iso)
    if _post_sla(settlement, now_utc):
        logger.warning(
            "FINRA short interest: 404 past SLA for %s - CDN outage suspected.",
            settlement_iso,
        )
        return True
    logger.debug(
        "FINRA short interest: no file for %s (404 - pre-SLA).",
        settlement_iso,
    )
    return False


def _post_sla(settlement_date: date, now_utc: datetime) -> bool:
    """Return ``True`` when FINRA's publication SLA for *settlement_date* has passed.

    Past-SLA means: more than 10 calendar days have elapsed since settlement,
    so the file should already be available.  A 404 represents a CDN outage
    rather than a normal pre-publication poll.

    US market holidays return ``False`` — FINRA does not produce a snapshot
    for a holiday-dated settlement date, so the 404 is correct silent
    behavior regardless of how far in the past it is.
    """
    if not _XNYS.is_session(pd.Timestamp(settlement_date)):
        return False
    today_et = now_utc.astimezone(_ET).date()
    return settlement_date <= today_et - timedelta(days=_PUBLICATION_LAG_DAYS)


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
    _now: datetime | None = None,
) -> None:
    """
    Collect FINRA short interest for the provided *settlement_dates*.

    For each date, attempts to download the corresponding CDN file and UPSERT
    parsed rows into ``short_interest_snapshots``.

    A 404 is branched against FINRA's ~7-10 day publication lag:

    * Pre-SLA (settlement within last 10 calendar days) — silent skip.
    * Post-SLA (settlement more than 10 calendar days ago) — date recorded on
      ``run.error_summary`` so freshness checks see the gap.  The run still
      completes ``success``.

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
    _now:
        Override for "now" used to evaluate the publication SLA.  Test-only.
        (Unlike short_volume, the settlement-date window comes from the caller
        — ``_now`` only affects SLA classification here.)
    """
    if client is None:
        client = FinraClient()
    if session_factory is None:
        session_factory = default_session_factory()
    if settlement_dates is None:
        settlement_dates = _biweekly_settlement_dates(months=6)

    now_utc = _now if _now is not None else datetime.now(UTC)

    universe = set(
        active_universe_tickers(session_factory=session_factory, include_benchmarks=False)
    )
    ingested_at = datetime.now(UTC).isoformat()

    with track_run("finra.short_interest", _repo=_repo) as run:
        rows_written = 0
        post_sla_misses: list[str] = []

        for sd in settlement_dates:
            path = _cdn_path(sd)
            try:
                text = client.get(path)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    if _classify_404(sd, now_utc):
                        post_sla_misses.append(sd)
                    continue
                raise

            parsed = _parse_csv(text, universe, ingested_at)
            if parsed:
                rows_written += _persist_rows(parsed, session_factory)

        run.rows_written = rows_written
        if post_sla_misses:
            run.error_summary = (
                f"finra cdn 404 past sla for {len(post_sla_misses)} date(s): "
                f"{','.join(post_sla_misses)}"
            )


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
