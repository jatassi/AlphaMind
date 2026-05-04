"""
Verify that all collectors are producing fresh data within their expected
cadence windows.

For each spec the script reads the most recent ``completed_at`` timestamp from
``collection_runs`` (matching any of the spec's ``track_run_names``, status
``success``) and checks whether it is within ``2x cadence_minutes`` of the
current UTC time. For market-hours collectors (polygon.equity, polygon.options)
the check is skipped when the NYSE is currently closed.

The ``track_run_names`` indirection exists because some scheduler IDs fan out
to several ``track_run`` calls (e.g. ``finnhub.calendar`` fans out to
``finnhub.{earnings,economic,ipo,fda}_calendar``), and a few historical
collector IDs differ from the names actually written to ``collection_runs``.
Using row-freshness on the data table is unreliable for sparse upserts that
no-op when content is unchanged, so ``collection_runs`` is the only source of
truth here.

Exit codes:
  0 — all checked collectors are within their freshness window
  1 — one or more collectors have stale data

Usage:
  uv run python scripts/verify_ongoing_collection.py [--db-path PATH]

``--db-path`` (when supplied) takes precedence over the ``DATABASE_PATH`` env
var and the ``session.py`` resolution chain.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    """Freshness expectation for one collector.

    ``track_run_names`` lists the actual ``track_run(name=...)`` strings the
    collector writes to ``collection_runs``. Defaults to ``(collector_id,)``
    when omitted; supply explicit names when the scheduler ID fans out (e.g.
    ``finnhub.calendar``) or when the historical track_run name diverges from
    the scheduler ID (e.g. ``sec_edgar.rss`` → ``sec_edgar.8k_rss``).
    """

    collector_id: str
    cadence_minutes: int
    market_hours_only: bool = False
    track_run_names: tuple[str, ...] = field(default=())

    @property
    def effective_run_names(self) -> tuple[str, ...]:
        return self.track_run_names or (self.collector_id,)


COLLECTOR_SPECS: list[CollectorSpec] = [
    # polygon.equity fires every 15 min during market hours
    CollectorSpec("polygon.equity", 15, market_hours_only=True),
    # polygon.options fires every 30 min during market hours
    CollectorSpec("polygon.options", 30, market_hours_only=True),
    # polygon.corporate_actions fires once daily at 06:00 ET on weekdays
    CollectorSpec("polygon.corporate_actions", 60 * 24, market_hours_only=True),
    # polygon.reference fires weekly (Saturday 06:00 ET)
    CollectorSpec("polygon.reference", 60 * 24 * 7),
    # fred.macro fires every 4h
    CollectorSpec("fred.macro", 60 * 4),
    # eia.energy fires every 4h
    CollectorSpec("eia.energy", 60 * 4),
    # bls.macro fires daily at 09:00 ET
    CollectorSpec("bls.macro", 60 * 24),
    # treasury.auctions fires Mon-Fri 17:00 ET (after market close), so the
    # Fri → Mon gap is ~72h. Cadence widened to 3 days so the 2x max_age
    # (6 days) accommodates the legitimate weekend gap without false STALE.
    CollectorSpec("treasury.auctions", 60 * 24 * 3, market_hours_only=True),
    # finnhub.news fires every 30 min
    CollectorSpec("finnhub.news", 30),
    # finnhub.calendar fires daily at 06:00 ET; the scheduler entry fans out
    # to four sub-collectors, each writing its own collection_runs row.
    CollectorSpec(
        "finnhub.calendar",
        60 * 24,
        track_run_names=(
            "finnhub.earnings_calendar",
            "finnhub.economic_calendar",
            "finnhub.ipo_calendar",
            "finnhub.fda_calendar",
        ),
    ),
    # marketaux.news fires every 4h (sized for the 100-req/day free-tier quota)
    CollectorSpec("marketaux.news", 60 * 4),
    # sec_edgar.rss fires every 15 min; track_run name is "sec_edgar.8k_rss"
    CollectorSpec("sec_edgar.rss", 15, track_run_names=("sec_edgar.8k_rss",)),
    # polymarket fires every 30 min; track_run name is "polymarket.contracts"
    CollectorSpec("polymarket", 30, track_run_names=("polymarket.contracts",)),
    # kalshi fires every 30 min; track_run name is "kalshi.contracts"
    CollectorSpec("kalshi", 30, track_run_names=("kalshi.contracts",)),
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


def _latest_run_completion(conn: Any, run_names: tuple[str, ...]) -> datetime | None:
    """Return the most recent ``completed_at`` across collection_runs matching any of ``run_names``.

    ``status = 'success'`` is required so an in-flight or failed run does not
    mask a true freshness gap. ``run_names`` may carry one entry (the common
    case) or several (fan-out collectors like ``finnhub.calendar``); the most
    recent successful completion across the set wins.
    """
    placeholders = ", ".join(f":n{i}" for i in range(len(run_names)))
    params: dict[str, str] = {f"n{i}": name for i, name in enumerate(run_names)}
    row = conn.execute(
        text(
            f"SELECT MAX(completed_at) FROM collection_runs "
            f"WHERE collector IN ({placeholders}) AND status = 'success'"
        ),
        params,
    ).fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0].rstrip("Z")).replace(tzinfo=UTC)
    return None


def run_verification(db_path: str | None = None) -> bool:
    """Connect, check freshness for each collector, print report, return pass/fail."""
    engine = make_engine(db_path)
    now_utc = datetime.now(tz=UTC)
    nyse_open = _nyse_is_open(now_utc)
    all_pass = True

    print("=" * 70)
    print("AlphaMind Ongoing-Collection Freshness Verification")
    print(f"Now (UTC): {now_utc.isoformat()}")
    print(f"NYSE currently open: {nyse_open}")
    print("=" * 70)
    print(f"\n  {'Collector':<35} {'Last seen':<30} {'Age':>10}  {'Status'}")
    print("  " + "-" * 95)

    with engine.connect() as conn:
        for spec in COLLECTOR_SPECS:
            max_age = timedelta(minutes=spec.cadence_minutes * 2)

            # Skip market-hours collectors when the market is closed
            if spec.market_hours_only and not nyse_open:
                print(f"  {spec.collector_id:<35} {'(market closed)':<30} {'':>10}  SKIP")
                continue

            latest = _latest_run_completion(conn, spec.effective_run_names)

            if latest is None:
                print(
                    f"  {spec.collector_id:<35} {'no successful run':<30} {'':>10}  FAIL (no rows)"
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

            print(f"  {spec.collector_id:<35} {latest.isoformat():<30} {age_str:>10}  {status}")

    print("\n" + "=" * 70)
    if all_pass:
        print("RESULT: PASS — all active collectors within freshness window.")
    else:
        print("RESULT: FAIL — one or more collectors have stale or absent data.")
    print("=" * 70)

    return all_pass


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to the AlphaMind SQLite database (overrides DATABASE_PATH and main.yaml).",
    )
    args = parser.parse_args(argv)
    passed = run_verification(args.db_path)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
