# Collector Verification Runbook

Authoritative operator procedure for verifying the AlphaMind data-layer
collector end-to-end: bootstrap completion, ongoing-collection freshness, and
the revoked-key failure drill.  Cross-referenced by story
`08-end-to-end-verification.md`.

---

## Pre-conditions

Complete all of the following before running any verification step:

- [ ] Bootstrap has been run to completion:
  `DATABASE_PATH=<path> python -m alphamind.collector bootstrap`
- [ ] The collector service is running:
  Windows: `nssm status alphamind-collector` → `SERVICE_RUNNING`
  Dev machine: `python -m alphamind.collector run` in a terminal window
- [ ] At least one full trading day of ongoing collection has elapsed since
  bootstrap (needed for `verify_ongoing_collection.py` to pass).
- [ ] The `DATABASE_PATH` environment variable points to the AlphaMind SQLite
  database, or the database exists at the platform default:
  `%USERPROFILE%\AlphaMind\data\alphamind.db`

---

## Step 1 — Bootstrap verification

**Script:** `scripts/verify_bootstrap.py`

**What it checks:**
- Every table from `storage.md § Tables` exists in the database.
- Row counts for time-series and event tables are within tolerance:

  | Table                | Expected   | Tolerance |
  |----------------------|------------|-----------|
  | `ohlcv_bars`         | ~600K rows | ±20%      |
  | `corporate_actions`  | ~3K rows   | ±50%      |
  | `macro_observations` | ~50K rows  | ±20%      |
  | `treasury_auctions`  | ~50 rows   | ±50%      |
  | `event_calendar`     | ~500 rows  | ±50%      |
  | Reference tables     | ~100 rows  | ±50%      |

**Run:**
```
DATABASE_PATH=%USERPROFILE%\AlphaMind\data\alphamind.db \
  uv run python scripts/verify_bootstrap.py
```

**Expected output (passing):**
```
======================================================================
AlphaMind Bootstrap Verification
======================================================================

[ Table existence ]
  asset_universe                                OK
  sector_classification                         OK
  ... (all 17 tables)
  All tables present.

[ Row counts — time-series / event tables ]
  ohlcv_bars               612345 rows; expected ~600,000 [480,000–720,000]  OK
  corporate_actions          3150 rows; expected ~3,000 [1,500–4,500]         OK
  macro_observations        51200 rows; expected ~50,000 [40,000–60,000]      OK
  treasury_auctions            48 rows; expected ~50 [25–75]                  OK
  event_calendar              512 rows; expected ~500 [250–750]               OK

[ Row counts — reference tables (aggregate) ]
  Reference tables (4)        82 rows; expected ~100 [50–150]                 OK

[ collection_runs sanity ]
  total=2405  success=2390  failed=15

======================================================================
RESULT: PASS — all checks within tolerance.
======================================================================
```

**Failure interpretation:**
- `MISSING` table → bootstrap did not complete; re-run `bootstrap`.
- Row count `FAIL` → check `collection_runs` for errors on the relevant vendor.
  A count below the lower bound suggests the bootstrap run was interrupted or
  the universe was smaller than expected; re-run `bootstrap --only <vendor>`.

---

## Step 2 — Ongoing-collection freshness verification

**Script:** `scripts/verify_ongoing_collection.py`

**What it checks:**
For each collector, the most recent `ingested_at` timestamp (resolved via
`collection_runs.completed_at` where available, else the data table's
`ingested_at`) must be within `2 × cadence_minutes` of the current UTC time.

Market-hours collectors (`polygon.equity`, `polygon.options`,
`polygon.corporate_actions`, `treasury.auctions`) are skipped automatically
when the NYSE is closed, using `exchange_calendars`.

**Run:**
```
DATABASE_PATH=%USERPROFILE%\AlphaMind\data\alphamind.db \
  uv run python scripts/verify_ongoing_collection.py
```

**Expected output (passing, during market hours):**
```
======================================================================
AlphaMind Ongoing-Collection Freshness Verification
Now (UTC): 2026-04-26T19:45:00+00:00
NYSE currently open: True
======================================================================

  Collector                           Table                               Last seen                       Age  Status
  ------------------------------------------------------------------------------------------------------------------------
  polygon.equity                      ohlcv_bars                          2026-04-26T19:30:01+00:00     0:14:59  OK
  polygon.options                     options_contract_snapshots          2026-04-26T19:30:02+00:00     0:14:58  OK
  ...

======================================================================
RESULT: PASS — all active collectors within freshness window.
======================================================================
```

**Expected output (outside market hours):**
Market-hours collectors show `SKIP` instead of a row count.

**Failure interpretation:**
- `FAIL (no rows)` → the collector has never written to that table; check that
  bootstrap ran and the service is active.
- `STALE (max N:MM:SS)` → the collector fired but has not produced new data
  within `2 × cadence`.  Inspect `collection_runs` for the relevant collector:
  ```sql
  SELECT * FROM collection_runs
  WHERE collector LIKE 'polygon.%'
  ORDER BY started_at DESC
  LIMIT 10;
  ```

---

## Step 3 — Revoked-key drill

**Script:** `scripts/run_revoked_key_drill.py`

**Purpose:** Confirm that revoking one vendor's API key produces a `failed` row
in `collection_runs` for that vendor while all other vendors continue to record
`success` rows — and that the collector process itself does not crash.

**Approach:** Operator-driven (default per `lifecycle.md § Operator workflow`).
The script provides a checklist and verification queries; the operator revokes
and restores the key manually in the vendor's web console.

**Run:**
```
DATABASE_PATH=%USERPROFILE%\AlphaMind\data\alphamind.db \
  uv run python scripts/run_revoked_key_drill.py --vendor polygon [--timeout 900]
```

**Procedure:**

1. Start the script.  It prints a pre-drill checklist and waits for the
   operator to press Enter.
2. Log in to the vendor (e.g., Polygon) web console and revoke or rotate the
   API key.
3. Press Enter.  The script polls `collection_runs` every 30 seconds.
4. When the vendor's next scheduled cycle fires, the collector will fail and
   write a `failed` row.  The script detects this and reports the result.
5. Restore the real API key in the vendor web console.
6. Wait for the next successful cycle to confirm the collector recovers
   automatically (no restart needed — the scheduler retries on next fire).

**Expected output (passing):**
```
Pre-drill checklist printed...
[press Enter after revoking key]

Monitoring collection_runs for vendor=polygon ...
  [19:30:00] polygon: failed=0  success=0  partial_failure=0
  [19:30:30] polygon: failed=0  success=0  partial_failure=0
  [19:31:15] polygon: failed=1  success=0  partial_failure=0

  Failure detected for polygon.
  Other-vendor success rows since drill start: 4

======================================================================
RESULT: PASS
  - polygon: at least one failed run confirmed.
  - Other vendors: 4 success row(s) observed — system alive.
======================================================================

POST-DRILL:
  [ ] Restore the real API key for polygon in the web console.
  [ ] Wait for the next scheduled cycle to confirm success rows resume.
```

**Manual verification queries (for inspection in sqlite3 or DBeaver):**
```sql
-- Failed runs for the target vendor
SELECT collector, started_at, status, error_summary
FROM collection_runs
WHERE collector LIKE 'polygon.%'
  AND status = 'failed'
ORDER BY started_at DESC
LIMIT 10;

-- Other vendor health during the drill window
SELECT collector, COUNT(*) AS n_success
FROM collection_runs
WHERE collector NOT LIKE 'polygon.%'
  AND status = 'success'
  AND started_at >= '<drill_start_ts>'
GROUP BY collector
ORDER BY collector;
```

**Supported vendors:** `polygon`, `fred`, `eia`, `bls`, `treasury`, `finnhub`,
`marketaux`, `sec_edgar`, `polymarket`, `kalshi`.

---

## Step 4 — Mental-check queries (distillation readiness)

These queries confirm the schema supports the read patterns the distillation
layer requires.  Run them against the populated database to verify they return
non-empty results.

### 4a. Last 20 days of daily bars for AAPL

```sql
SELECT ticker, timeframe, period_start, adj_close, adj_volume
FROM ohlcv_bars
WHERE ticker = 'AAPL'
  AND timeframe = '1d'
ORDER BY period_start DESC
LIMIT 20;
```

Expected: 20 rows with dates spaced ~1 trading day apart.

### 4b. 20-day volume baseline for AAPL (avg daily volume)

```sql
SELECT
  ticker,
  COUNT(*)         AS trading_days,
  AVG(adj_volume)  AS avg_daily_vol,
  MIN(adj_volume)  AS min_daily_vol,
  MAX(adj_volume)  AS max_daily_vol
FROM ohlcv_bars
WHERE ticker = 'AAPL'
  AND timeframe = '1d'
  AND period_start >= datetime('now', '-28 days')
GROUP BY ticker;
```

Expected: 1 row for AAPL with ~20 trading days in a 28-calendar-day window.

### 4c. 60-day correlation window across tech-sector tickers

```sql
SELECT ob.ticker, ob.period_start, ob.adj_close
FROM ohlcv_bars AS ob
JOIN sector_classification AS sc ON ob.ticker = sc.ticker
WHERE sc.alphamind_sector = 'tech'
  AND ob.timeframe = '1d'
  AND ob.period_start >= datetime('now', '-62 days')
ORDER BY ob.ticker, ob.period_start;
```

Expected: Multiple rows per ticker covering ~60 trading days.

### 4d. Latest options chain snapshot for NVDA

```sql
SELECT
  ocs.underlying_ticker,
  oc.expiration_date,
  oc.strike_price,
  oc.contract_type,
  ocs.implied_volatility,
  ocs.delta,
  ocs.bid,
  ocs.ask,
  ocs.snapshot_ts
FROM options_contract_snapshots AS ocs
JOIN options_contracts AS oc ON ocs.contract_ticker = oc.contract_ticker
WHERE ocs.underlying_ticker = 'NVDA'
  AND ocs.snapshot_ts = (
    SELECT MAX(snapshot_ts)
    FROM options_contract_snapshots
    WHERE underlying_ticker = 'NVDA'
  )
ORDER BY oc.expiration_date, oc.strike_price, oc.contract_type;
```

Expected: The most recent full options chain for NVDA across all strikes and
expiries at the latest snapshot timestamp.

### 4e. DGS10 yield series — last 90 days

```sql
SELECT observation_date, value, units
FROM macro_observations
WHERE source = 'fred'
  AND series_id = 'DGS10'
  AND observation_date >= date('now', '-90 days')
ORDER BY observation_date DESC;
```

Expected: ~60–90 rows (FRED DGS10 is daily; weekends and holidays excluded).

### 4f. Upcoming earnings in the next 14 days

```sql
SELECT
  ec.scheduled_at,
  ec.ticker,
  eed.fiscal_period,
  eed.fiscal_year,
  eed.expected_call_time,
  eed.eps_consensus,
  eed.revenue_consensus_usd
FROM event_calendar AS ec
JOIN earnings_event_details AS eed ON ec.event_id = eed.event_id
WHERE ec.event_type = 'earnings'
  AND ec.scheduled_at BETWEEN datetime('now') AND datetime('now', '+14 days')
ORDER BY ec.scheduled_at;
```

Expected: Rows for all universe tickers reporting in the next two weeks.

---

## Acceptance criteria attestation

| Criterion | Script / Section |
|-----------|-----------------|
| `verify_bootstrap.py` connects, asserts tables, reports row counts, exits 0 when within tolerance | `scripts/verify_bootstrap.py` |
| `verify_ongoing_collection.py` reports per-collector freshness factoring market hours | `scripts/verify_ongoing_collection.py` |
| `run_revoked_key_drill.py` provides operator-driven documented procedure with `--vendor` | `scripts/run_revoked_key_drill.py` |
| Runbook covers bootstrap, ongoing, drill, mental-check queries | This file, Steps 1–4 |
| Post-bootstrap `verify_bootstrap.py` exits 0 | Operator-execution criterion — documented in Step 1 |
| After one full trading day `verify_ongoing_collection.py` exits 0 | Operator-execution criterion — documented in Step 2 |
| Drill on one vendor exits 0, `failed`/`success` mix confirmed | Operator-execution criterion — documented in Step 3 |
| Mental-check queries return non-empty results on populated database | Operator-execution criterion — documented in Step 4 |
