---
status: not_started
completed_date:
commit_id:
---

# 08 — End-to-end verification

## Goal

Produce verification scripts and a documented procedure that prove the collector works end-to-end: bootstrap completes, ongoing collection runs, failure handling behaves as specified.

## Reading

- `docs/design/01-data-layer/SESSION-BRIEF.md` § Verification — the criteria
- `docs/design/01-data-layer/collector/lifecycle.md` § Operator workflow — drill procedure
- `docs/design/01-data-layer/collector/storage.md` § Storage volume — expected post-bootstrap row counts

## Depends on

- 06a (bootstrap orchestrator)
- 06b (runner & scheduler)
- 07 (NSSM service)

## Scope

In scope:
- `scripts/verify_bootstrap.py`:
  - Connects to the SQLite database.
  - Asserts every table from `storage.md` § Tables exists.
  - Reports row counts per table, comparing against `storage.md` § Storage volume expectations:
    - `ohlcv_bars`: ~600K (allow ±20% tolerance).
    - `corporate_actions`: ~3K (±50%).
    - `macro_observations`: ~50K (±20%).
    - `treasury_auctions`: ~50 (±50%).
    - `event_calendar`: ~500 (±50%).
    - Reference tables: ~100 (±50%).
  - Exits 0 if all tables exist and row counts are within tolerance; exits 1 with a per-table report otherwise.
- `scripts/verify_ongoing_collection.py`:
  - Connects to the SQLite database.
  - For each table, finds the most recent `ingested_at` and asserts it's within the last 2× cadence period for that collector (e.g., `ohlcv_bars` rows from `polygon.equity` should have `ingested_at` within the last 30 minutes during market hours).
  - Reports per-collector freshness; exits 0 when all are within tolerance.
- `scripts/run_revoked_key_drill.py`:
  - Reads a target vendor from `--vendor` argument.
  - Temporarily writes a fake API key to a copy of `.env` (or instructs the operator to revoke the real key for N minutes), waits for the next scheduled fire of that vendor, then verifies:
    - `collection_runs` has a `failed` row for that vendor.
    - Other vendors' collectors continue producing `success` rows.
    - The collector process is still running.
  - Restores the real key. Exits 0 on a clean drill.
- `docs/implementation/01-data-layer/collector/VERIFICATION.md` — operator runbook with:
  - Pre-conditions (bootstrap completed, service running).
  - Sequence of verification steps using the three scripts above.
  - Expected outputs for each.
  - The "mental check" from `SESSION-BRIEF.md`: example queries against the schema (e.g., "compute a 20-day volume baseline for AAPL" → SQL query) demonstrating distillation can do its work.

Out of scope:
- Distillation logic itself.
- Long-term operational monitoring (Phase 4 concern).
- Performance benchmarking under load.

## Notes

Tolerance values on `verify_bootstrap.py` row counts come from real-world variability: the universe might be 60–80 tickers (not exactly 65); some tickers may have shorter trading history; some macro series have gaps. The ±20–50% bands are specified above per table.

`verify_ongoing_collection.py` should respect market hours when checking freshness — `polygon.equity` won't have new rows on a Sunday afternoon, so the freshness check needs to factor in `exchange_calendars` to skip non-trading windows.

The revoked-key drill is the trickiest verification. Two approaches:
- Operator-driven: revoke the key in the vendor's web console for N minutes, run the drill, restore. The script provides a checklist and verification queries.
- Automated: rewrite `.env` to point to a fake key, restart the service, wait for the failure to be observed, restore the real key, restart again.

Default to the operator-driven approach for safety; the script just provides verification queries and a wait loop.

The "mental check" example queries should be drawn from the distillation layer's expected reads. From `external.md § 2`:
- "Last 20 days of daily bars for AAPL" → `SELECT * FROM ohlcv_bars WHERE ticker='AAPL' AND timeframe='1d' ORDER BY period_start DESC LIMIT 20;`
- "60-day correlation window for tech sector" → similar pattern joining sector_classification.
- "Latest options chain snapshot for NVDA" → `options_contract_snapshots` join.

## Acceptance criteria

- [ ] `scripts/verify_bootstrap.py` connects to the database, asserts every table from `storage.md` exists, reports row counts, exits 0 when all are within tolerance.
- [ ] `scripts/verify_ongoing_collection.py` reports per-collector freshness against expected cadence, factoring in market hours.
- [ ] `scripts/run_revoked_key_drill.py` exists and provides a documented procedure (operator-driven or automated).
- [ ] `VERIFICATION.md` exists and walks through bootstrap verification, ongoing-collection verification, the revoked-key drill, and the mental-check example queries.
- [ ] After running `python -m alphamind.collector bootstrap` against real APIs and then `verify_bootstrap.py`, the script exits 0.
- [ ] After running the service for one full trading day and then `verify_ongoing_collection.py`, the script exits 0.
- [ ] After running the revoked-key drill on at least one vendor, the script exits 0 and the runbook's verification queries return the expected `failed` / `success` mix.
- [ ] The mental-check queries in `VERIFICATION.md` execute against the populated database without error and return non-empty results for the documented cases.
