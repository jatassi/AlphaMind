"""
Verify that a bootstrap-completed AlphaMind SQLite database has all required
tables and row counts within the tolerances documented in storage.md S Storage
volume.

Exit codes:
  0 -- all tables present and all row counts within tolerance
  1 -- one or more checks failed (table missing or row count out of tolerance)

Usage:
  DATABASE_PATH=/path/to/alphamind.db uv run python scripts/verify_bootstrap.py

The DATABASE_PATH env var, or the session.py resolution chain, is used to locate
the database.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

# ---------------------------------------------------------------------------
# Tolerance spec -- mirrors storage.md S Storage volume exactly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    """Expected row count range for a single table after bootstrap."""

    table_name: str
    expected: int
    tolerance_pct: float  # e.g. 0.20 -> +-20%

    @property
    def lower(self) -> int:
        return max(0, int(self.expected * (1 - self.tolerance_pct)))

    @property
    def upper(self) -> int:
        return int(self.expected * (1 + self.tolerance_pct))


# Reference tables: ~100 rows total split across 4 tables; +-50%
_REFERENCE_TOTAL_EXPECTED = 100
_REFERENCE_TOTAL_TOLERANCE = 0.50

# Per-table specs for the time-series / event tables
TABLE_SPECS: list[TableSpec] = [
    TableSpec("ohlcv_bars", 600_000, 0.20),
    TableSpec("corporate_actions", 3_000, 0.50),
    TableSpec("macro_observations", 50_000, 0.20),
    TableSpec("treasury_auctions", 50, 0.50),
    TableSpec("event_calendar", 500, 0.50),
]

# All tables that must exist (from storage.md S Tables)
ALL_TABLES: list[str] = [
    "asset_universe",
    "sector_classification",
    "etf_membership",
    "ticker_change_history",
    "ohlcv_bars",
    "corporate_actions",
    "options_contracts",
    "options_contract_snapshots",
    "macro_observations",
    "treasury_auctions",
    "event_calendar",
    "earnings_event_details",
    "news_articles",
    "news_article_tickers",
    "prediction_market_contracts",
    "prediction_market_snapshots",
    "collection_runs",
]

_REFERENCE_TABLES = [
    "asset_universe",
    "sector_classification",
    "etf_membership",
    "ticker_change_history",
]


def _row_count(conn: Any, table: str) -> int:
    row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()  # nosec
    return int(row[0]) if row else 0


def _reference_row_total(conn: Any) -> int:
    """Sum rows across the four reference tables."""
    return sum(_row_count(conn, t) for t in _REFERENCE_TABLES)


# ---------------------------------------------------------------------------
# Per-section check helpers
# ---------------------------------------------------------------------------


def _check_table_existence(
    existing_tables: set[str],
) -> tuple[list[str], bool]:
    """Print and return (missing_tables, all_present)."""
    print("\n[ Table existence ]")
    missing: list[str] = []
    for tname in ALL_TABLES:
        present = tname in existing_tables
        status = "OK" if present else "MISSING"
        print(f"  {tname:<45} {status}")
        if not present:
            missing.append(tname)
    if missing:
        print(f"\n  FAIL: {len(missing)} table(s) missing: {', '.join(missing)}")
    else:
        print("\n  All tables present.")
    return missing, not missing


def _check_timeseries_counts(conn: Any, existing_tables: set[str]) -> bool:
    """Print per-table row count vs tolerance; return True iff all pass."""
    print("\n[ Row counts -- time-series / event tables ]")
    all_pass = True
    for spec in TABLE_SPECS:
        if spec.table_name not in existing_tables:
            print(f"  {spec.table_name:<30} SKIP (table missing)")
            all_pass = False
            continue
        count = _row_count(conn, spec.table_name)
        in_range = spec.lower <= count <= spec.upper
        status = "OK" if in_range else "FAIL"
        if not in_range:
            all_pass = False
        print(
            f"  {spec.table_name:<30} {count:>8} rows; "
            f"expected ~{spec.expected:,} "
            f"[{spec.lower:,}-{spec.upper:,}]  {status}"
        )
    return all_pass


def _check_reference_counts(conn: Any, existing_tables: set[str]) -> bool:
    """Print aggregate reference-table row count; return True iff within tolerance."""
    print("\n[ Row counts -- reference tables (aggregate) ]")
    if not all(t in existing_tables for t in _REFERENCE_TABLES):
        print("  SKIP (one or more reference tables missing)")
        return False
    ref_total = _reference_row_total(conn)
    lower_ref = max(0, int(_REFERENCE_TOTAL_EXPECTED * (1 - _REFERENCE_TOTAL_TOLERANCE)))
    upper_ref = int(_REFERENCE_TOTAL_EXPECTED * (1 + _REFERENCE_TOTAL_TOLERANCE))
    in_range = lower_ref <= ref_total <= upper_ref
    status = "OK" if in_range else "FAIL"
    print(
        f"  Reference tables (4)         {ref_total:>8} rows; "
        f"expected ~{_REFERENCE_TOTAL_EXPECTED:,} "
        f"[{lower_ref:,}-{upper_ref:,}]  {status}"
    )
    return in_range


def _check_collection_runs(conn: Any, existing_tables: set[str]) -> None:
    """Print collection_runs sanity counts (informational only; does not affect pass/fail)."""
    print("\n[ collection_runs sanity ]")
    if "collection_runs" not in existing_tables:
        print("  SKIP (table missing)")
        return
    rows = conn.execute(
        text("SELECT status, COUNT(*) FROM collection_runs GROUP BY status")
    ).fetchall()
    counts: dict[str, int] = {status: int(n) for status, n in rows}
    total = sum(counts.values())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0)
    print(f"  total={total}  success={success}  failed={failed}")


def run_verification() -> bool:
    """
    Connect to the database, run all checks, print a report, and return True
    iff all checks pass.
    """
    engine = make_engine()

    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        print("=" * 70)
        print("AlphaMind Bootstrap Verification")
        print("=" * 70)

        _, tables_ok = _check_table_existence(existing_tables)
        ts_ok = _check_timeseries_counts(conn, existing_tables)
        ref_ok = _check_reference_counts(conn, existing_tables)
        _check_collection_runs(conn, existing_tables)

        all_pass = tables_ok and ts_ok and ref_ok

        print("\n" + "=" * 70)
        if all_pass:
            print("RESULT: PASS -- all checks within tolerance.")
        else:
            print("RESULT: FAIL -- one or more checks out of tolerance (see above).")
        print("=" * 70)

    return all_pass


def main() -> None:
    passed = run_verification()
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
