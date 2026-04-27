"""
Verify that all collectors are producing fresh data within their expected
cadence windows.

For each collector / table pair the script finds the most recent ``ingested_at``
timestamp and checks whether it is within ``2x cadence_minutes`` of the
current UTC time.  For market-hours collectors (polygon.equity,
polygon.options) the check is skipped when the NYSE is currently closed, using
``exchange_calendars`` to determine market status.

Exit codes:
  0 — all checked collectors are within their freshness window
  1 — one or more collectors have stale data

Usage:
  DATABASE_PATH=/path/to/alphamind.db uv run python scripts/verify_ongoing_collection.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import exchange_calendars as xcals
from sqlalchemy import text

from alphamind.persistence.session import make_engine

# ---------------------------------------------------------------------------
# Collector freshness specs
# Cadences come from config/collector_schedule.yaml.
# max_age = 2x cadence_minutes per the story spec.
# market_hours_only = True means skip check when NYSE is closed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorSpec:
    """Freshness expectation for one collector."""

    collector_id: str
    table: str
    cadence_minutes: int
    market_hours_only: bool = False


COLLECTOR_SPECS: list[CollectorSpec] = [
    # polygon.equity fires every 15 min during market hours
    CollectorSpec("polygon.equity", "ohlcv_bars", 15, market_hours_only=True),
    # polygon.options fires every 30 min during market hours
    CollectorSpec("polygon.options", "options_contract_snapshots", 30, market_hours_only=True),
    # polygon.corporate_actions fires once daily at 06:00 ET on weekdays
    CollectorSpec(
        "polygon.corporate_actions", "corporate_actions", 60 * 24, market_hours_only=True
    ),
    # polygon.reference fires weekly
    CollectorSpec("polygon.reference", "asset_universe", 60 * 24 * 7),
    # fred.macro fires every 4h
    CollectorSpec("fred.macro", "macro_observations", 60 * 4),
    # eia.energy fires every 4h
    CollectorSpec("eia.energy", "macro_observations", 60 * 4),
    # bls.macro fires daily at 09:00 ET
    CollectorSpec("bls.macro", "macro_observations", 60 * 24),
    # treasury.auctions fires daily at 17:00 ET on weekdays
    CollectorSpec("treasury.auctions", "treasury_auctions", 60 * 24, market_hours_only=True),
    # finnhub.news fires every 30 min
    CollectorSpec("finnhub.news", "news_articles", 30),
    # finnhub.calendar fires daily
    CollectorSpec("finnhub.calendar", "event_calendar", 60 * 24),
    # marketaux.news fires every 30 min
    CollectorSpec("marketaux.news", "news_articles", 30),
    # sec_edgar.rss fires every 15 min
    CollectorSpec("sec_edgar.rss", "news_articles", 15),
    # polymarket fires every 30 min
    CollectorSpec("polymarket", "prediction_market_snapshots", 30),
    # kalshi fires every 30 min
    CollectorSpec("kalshi", "prediction_market_snapshots", 30),
]


# ---------------------------------------------------------------------------
# Market-hours helper
# ---------------------------------------------------------------------------


def _nyse_is_open(now_utc: datetime) -> bool:
    """Return True if the NYSE regular session is currently open."""
    try:
        nyse = xcals.get_calendar("XNYS")
        date_str = now_utc.strftime("%Y-%m-%d")
        if not nyse.is_session(date_str):
            return False
        session_open = nyse.session_open(date_str)
        session_close = nyse.session_close(date_str)
        # exchange_calendars returns tz-aware Timestamps; convert to UTC for comparison
        open_utc: datetime = session_open.to_pydatetime()
        close_utc: datetime = session_close.to_pydatetime()
    except Exception:
        # If calendar lookup fails, be conservative and do not skip
        return True
    else:
        return bool(open_utc <= now_utc <= close_utc)


# ---------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------


def _latest_ingested(conn: Any, table: str, collector_id: str) -> datetime | None:
    """
    Return the most recent ``ingested_at`` for the given collector (matched via
    ``collection_runs``) or fall back to the most recent ``ingested_at`` in the
    data table itself.
    """
    # First try collection_runs for a precise per-collector timestamp
    try:
        row = conn.execute(
            text(
                "SELECT MAX(completed_at) FROM collection_runs "
                "WHERE collector = :cid AND status = 'success'"
            ),
            {"cid": collector_id},
        ).fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0].rstrip("Z")).replace(tzinfo=UTC)
    except Exception:
        pass

    # Fall back to the data table's ingested_at
    try:
        row = conn.execute(text(f"SELECT MAX(ingested_at) FROM {table}")).fetchone()
        if row and row[0]:
            ts_str: str = row[0]
            return datetime.fromisoformat(ts_str.rstrip("Z")).replace(tzinfo=UTC)
    except Exception:
        pass

    return None


def run_verification() -> bool:
    """Connect, check freshness for each collector, print report, return pass/fail."""
    engine = make_engine()
    now_utc = datetime.now(tz=UTC)
    nyse_open = _nyse_is_open(now_utc)
    all_pass = True

    print("=" * 70)
    print("AlphaMind Ongoing-Collection Freshness Verification")
    print(f"Now (UTC): {now_utc.isoformat()}")
    print(f"NYSE currently open: {nyse_open}")
    print("=" * 70)
    print(f"\n  {'Collector':<35} {'Table':<35} {'Last seen':<30} {'Age':>10}  {'Status'}")
    print("  " + "-" * 120)

    with engine.connect() as conn:
        for spec in COLLECTOR_SPECS:
            max_age = timedelta(minutes=spec.cadence_minutes * 2)

            # Skip market-hours collectors when the market is closed
            if spec.market_hours_only and not nyse_open:
                print(
                    f"  {spec.collector_id:<35} {spec.table:<35} "
                    f"{'(market closed)':<30} {'':>10}  SKIP"
                )
                continue

            latest = _latest_ingested(conn, spec.table, spec.collector_id)

            if latest is None:
                print(
                    f"  {spec.collector_id:<35} {spec.table:<35} "
                    f"{'no data':<30} {'':>10}  FAIL (no rows)"
                )
                all_pass = False
                continue

            age = now_utc - latest
            age_str = str(age).split(".")[0]  # drop microseconds
            threshold_str = str(max_age).split(".")[0]
            ok = age <= max_age
            status = "OK" if ok else f"STALE (max {threshold_str})"
            if not ok:
                all_pass = False

            print(
                f"  {spec.collector_id:<35} {spec.table:<35} "
                f"{latest.isoformat():<30} {age_str:>10}  {status}"
            )

    print("\n" + "=" * 70)
    if all_pass:
        print("RESULT: PASS — all active collectors within freshness window.")
    else:
        print("RESULT: FAIL — one or more collectors have stale or absent data.")
    print("=" * 70)

    return all_pass


def main() -> None:
    passed = run_verification()
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
