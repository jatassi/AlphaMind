"""
Revoked-key drill for the AlphaMind collector.

Purpose
-------
Confirm that when one vendor's API key is revoked (or pointed at a bad value),
the collector:
  1. Records a ``failed`` row in ``collection_runs`` for that vendor.
  2. Continues recording ``success`` rows for all other vendors.
  3. Does not crash the collector process.

This script uses the operator-driven approach (default per lifecycle.md § Operator
workflow):

  1. Operator revokes the real key in the vendor's web console.
  2. Operator runs this script with ``--vendor <vendor_id>``.
  3. Script prints a checklist, waits for at least one scheduled cycle of that
     vendor to fire (polling ``collection_runs``), then queries and reports the
     expected ``failed`` / ``success`` mix.
  4. Operator restores the real key.
  5. Script confirms that subsequent runs succeed.

The script never modifies ``.env`` or any credential file.

Usage:
  DATABASE_PATH=/path/to/alphamind.db uv run python scripts/ops/run_revoked_key_drill.py \\
      --vendor polygon [--timeout 900]

Arguments:
  --vendor   Vendor prefix matching ``collection_runs.collector``.
             Must be one of: polygon, fred, eia, bls, treasury, finnhub,
             marketaux, sec_edgar, polymarket, kalshi.
  --timeout  Maximum seconds to wait for a failure to appear (default: 900 = 15 min).

Exit codes:
  0 — drill passed (failed row found for target vendor; other vendors still succeeding)
  1 — drill failed or timed out
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from alphamind.persistence.session import make_engine
from alphamind.scripts._stdio import configure_utf8_stdio

VALID_VENDORS = [
    "polygon",
    "fred",
    "eia",
    "bls",
    "treasury",
    "finnhub",
    "marketaux",
    "sec_edgar",
    "polymarket",
    "kalshi",
]

# Approximate cadence for each vendor (seconds between scheduled fires)
# Used to estimate how long to wait before the next cycle.
VENDOR_CADENCE_SECONDS: dict[str, int] = {
    "polygon": 15 * 60,  # polygon.equity fires every 15 min
    "fred": 4 * 3600,  # every 4h
    "eia": 4 * 3600,
    "bls": 24 * 3600,
    "treasury": 24 * 3600,
    "finnhub": 30 * 60,
    "marketaux": 30 * 60,
    "sec_edgar": 15 * 60,
    "polymarket": 30 * 60,
    "kalshi": 30 * 60,
}


def _print_checklist(vendor: str) -> None:
    """Print the operator checklist before the wait loop."""
    cadence = VENDOR_CADENCE_SECONDS.get(vendor, 30 * 60)
    print("=" * 70)
    print(f"Revoked-key drill: vendor = {vendor}")
    print("=" * 70)
    print()
    print("PRE-DRILL CHECKLIST (complete before pressing Enter):")
    print()
    print(f"  [ ] Log in to the {vendor} web console (or API key management page).")
    print(f"  [ ] Revoke or rotate the API key for {vendor}.")
    print("  [ ] Confirm the collector service (alphamind-collector) is running.")
    print(f"  [ ] Note: the next {vendor} cycle fires in ≤{cadence // 60} min.")
    print()
    print("  When you have revoked the key, press Enter to begin monitoring ...")
    input()


def _query_runs_since(conn: Any, vendor: str, since: datetime) -> dict[str, int]:
    """Return counts of failed / success / other runs for vendor since a timestamp."""
    rows = conn.execute(
        text(
            "SELECT status, COUNT(*) "
            "FROM collection_runs "
            "WHERE collector LIKE :prefix AND started_at >= :since "
            "GROUP BY status"
        ),
        {"prefix": f"{vendor}.%", "since": since.isoformat()},
    ).fetchall()
    counts: dict[str, int] = {}
    for status, cnt in rows:
        counts[status] = int(cnt)
    return counts


def _query_other_vendor_successes(conn: Any, vendor: str, since: datetime) -> int:
    """Count success rows for all vendors *other than* the target since a timestamp."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM collection_runs "
            "WHERE collector NOT LIKE :prefix "
            "AND status = 'success' "
            "AND started_at >= :since"
        ),
        {"prefix": f"{vendor}.%", "since": since.isoformat()},
    ).fetchone()
    return int(row[0]) if row else 0


def _print_manual_queries(vendor: str, drill_start: datetime) -> None:
    """Print SQL snippets the operator can run for manual inspection."""
    print()
    print("Manual verification queries (run in sqlite3 or DBeaver):")
    print()
    print(
        f"  -- All runs for {vendor} since drill start:\n"
        f"  SELECT * FROM collection_runs\n"
        f"  WHERE collector LIKE '{vendor}.%'\n"
        f"  AND started_at >= '{drill_start.isoformat()}'\n"
        f"  ORDER BY started_at DESC;\n"
    )
    print(
        "  -- Other vendor successes since drill start:\n"
        "  SELECT collector, COUNT(*) AS n_success\n"
        "  FROM collection_runs\n"
        f"  WHERE collector NOT LIKE '{vendor}.%'\n"
        f"  AND status = 'success' AND started_at >= '{drill_start.isoformat()}'\n"
        "  GROUP BY collector\n"
        "  ORDER BY collector;\n"
    )


def _print_result(vendor: str, found_failure: bool, other_successes: int) -> bool:
    """Print pass/fail summary and post-drill checklist; return pass bool."""
    passed = found_failure and other_successes > 0
    print("=" * 70)
    if passed:
        print("RESULT: PASS")
        print(f"  - {vendor}: at least one failed run confirmed.")
        print(f"  - Other vendors: {other_successes} success row(s) observed -- system alive.")
    else:
        print("RESULT: FAIL")
        if not found_failure:
            print(f"  - No failed run observed for {vendor}.")
        if other_successes == 0:
            print("  - No success rows from other vendors -- system may be unhealthy.")
    print("=" * 70)
    print()
    print("POST-DRILL:")
    print(f"  [ ] Restore the real API key for {vendor} in the web console.")
    print("  [ ] Wait for the next scheduled cycle to confirm success rows resume.")
    print()
    return passed


def _poll_for_failure(conn: Any, vendor: str, drill_start: datetime, deadline: datetime) -> bool:
    """Poll collection_runs every 30 s until a failure appears or deadline passes."""
    poll_interval = 30
    while (now := datetime.now(tz=UTC)) < deadline:
        counts = _query_runs_since(conn, vendor, drill_start)
        failed = counts.get("failed", 0)
        success = counts.get("success", 0)
        partial = counts.get("partial_failure", 0)
        now_str = now.strftime("%H:%M:%S")
        print(
            f"  [{now_str}] {vendor}: failed={failed}  success={success}  partial_failure={partial}"
        )
        if failed > 0:
            print(f"\n  Failure detected for {vendor}.")
            return True
        time.sleep(poll_interval)
    return False


def run_drill(vendor: str, timeout_seconds: int) -> bool:
    """
    Wait for at least one ``failed`` run for *vendor*, then verify other vendors
    are still succeeding.  Returns True iff the drill passes.
    """
    engine = make_engine()
    drill_start = datetime.now(tz=UTC)
    deadline = drill_start + timedelta(seconds=timeout_seconds)

    print(f"\nMonitoring collection_runs for vendor={vendor} ...")
    print(f"Drill started at {drill_start.isoformat()}")
    print(f"Timeout: {timeout_seconds}s (until {deadline.isoformat()})")
    print()

    with engine.connect() as conn:
        found_failure = _poll_for_failure(conn, vendor, drill_start, deadline)

        if not found_failure:
            print(f"\n  TIMEOUT: No failed run observed for {vendor} within {timeout_seconds}s.")
            print("  Possible causes:")
            print("    - The vendor's scheduled cycle has not fired yet.")
            print("    - The key was not actually revoked / the error is silently swallowed.")
            print("    - The collector is not running.")
            return False

        other_successes = _query_other_vendor_successes(conn, vendor, drill_start)
        print(f"  Other-vendor success rows since drill start: {other_successes}")
        _print_manual_queries(vendor, drill_start)

    return _print_result(vendor, found_failure, other_successes)


def main() -> None:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Revoked-key drill for AlphaMind collector.")
    parser.add_argument(
        "--vendor",
        required=True,
        choices=VALID_VENDORS,
        help="Vendor to drill (must match a prefix in collection_runs.collector).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        metavar="SECONDS",
        help="Maximum seconds to wait for a failed run to appear (default: 900).",
    )
    args = parser.parse_args()

    _print_checklist(args.vendor)
    passed = run_drill(args.vendor, args.timeout)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
