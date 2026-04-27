---
status: in_progress
completed_date:
commit_id:
---

# 05k — FINRA vendor adapter

## Goal

Implement `src/alphamind/data_sources/finra/` covering Q4:4a (short interest) and Q4:4c (daily short volume). Both feeds come from FINRA's public CDN; no API key, only a `User-Agent` header.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `short_interest_snapshots`, `short_volume_daily`
- `docs/design/01-data-layer/api-key-checklist.md § FINRA` — concrete CDN URL patterns and the FINRA file-layout reference
- `docs/design/01-data-layer/api-failure-handling.md` — Q4 is `optional`
- `docs/design/01-data-layer/schema/short_selling.py` — entity definitions for `ShortInterestSnapshot` and `ShortVolumeDaily`
- `docs/design/01-data-layer/external/quantitative.md § Q4` — signal context

## Depends on

- 04 (shared library `_common.py`)
- 03b (persistence — table DDL applied)

## Scope

In scope: under `src/alphamind/data_sources/finra/` —

- `client.py` — HTTP wrapper around FINRA's CDN. Sends `User-Agent: AlphaMind <ops_email>` on every request (read from `data_sources.yaml.providers.finra.user_agent` or shared default). Integrates `with_retries(important)`, `RateLimiter`, `verify_connectivity()` (HEAD against a recent known-good URL). Follows redirects.
- `short_volume.py`:
  - `collect_short_volume(since=None)` — pulls Reg SHO daily files for the consolidated `cnms` market. URL: `https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`. Pipe-delimited; columns per FINRA's file layout PDF: `Date | Symbol | ShortVolume | ShortExemptVolume | TotalVolume | Market`. Walks trading days from the resolved `since` to today (skip weekends + US market holidays). Filters to `asset_universe.is_active=1`. Writes `short_volume_daily` with `market='cnms'` and `source='finra'`. Idempotent upsert on `(trade_date, ticker, market)`.
  - `bootstrap_short_volume(days=60)` — pulls trailing 60 trading days. Idempotent against partial prior bootstraps.
- `short_interest.py`:
  - `collect_short_interest()` — checks `https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv` for any settlement-date file newer than the latest stored `settlement_date`. CSV columns include `settlementDate`, `symbol`, `currentShortPositionQuantity`, `previousShortPositionQuantity`, `averageDailyShareVolumeQuantity`, `daysToCoverQuantity`, `changePercent`. Filters to universe tickers. Writes `short_interest_snapshots` with `source='finra'`. Idempotent on `(settlement_date, ticker)`. Returns silently if no new file is published.
  - `bootstrap_short_interest(months=6)` — walks the FINRA bi-weekly publication schedule for the trailing 6 months (24 settlement dates) and ingests each.
- Register both collectors in `src/alphamind/collector/scheduler.py:COLLECTORS`:
  - `"finra.short_volume": finra.short_volume.collect_short_volume`
  - `"finra.short_interest": finra.short_interest.collect_short_interest`
- Extend `src/alphamind/collector/bootstrap.py:run_all()` to invoke `finra.short_volume.bootstrap_short_volume()` and `finra.short_interest.bootstrap_short_interest()` after step 1 (Reference) — they have no dependency on Polygon equity bootstrap and can run in parallel-eligible position 4.
- Unit tests with mocked HTTP responses covering: successful pull, missing file (404 → no rows, no error), partial universe coverage (rows for non-universe tickers ignored), idempotency (re-run produces no duplicates), `track_run` failure path.

Out of scope:
- Q2c dark pool / ATS transparency — separate Q2 storage shape, deferred.
- Per-facility short volume files (`fnra`, `fnyx`, `fnqc`, `forf`, `adf`) — `cnms` consolidated covers the operational need; per-facility expansion lands when a venue-mix question arises.

## Notes

FINRA publishes the daily Reg SHO file by 6pm ET on the trade date — the cron at 7pm ET in `collector_schedule.yaml` (`0 19 * * mon-fri`) provides margin for late publication.

Bi-weekly short-interest files are published ~7–10 days after the settlement date, typically on Tuesdays. The cron polls weekly (`0 10 * * tue`) and exits cleanly when no new file exists.

`collection_runs.error_summary` should distinguish "file not yet published" (404 on a future-dated URL — expected, no error logged) from "URL pattern broken" (404 on a date that should exist — failure path).

Universe filter happens at parse time: read each file row, drop rows whose ticker is not in `asset_universe` or is `is_active=0`. The full FINRA file is ~10K tickers; our universe is ~78. Filtering at parse keeps the storage table small.

## Acceptance criteria

- [ ] `client.py` sends a `User-Agent` header on every request.
- [ ] `client.py` has `verify_connectivity()` that exits 0 on a successful FINRA CDN reach.
- [ ] `client.py` integrates `with_retries(important)`, `RateLimiter`, and `track_run`.
- [ ] `short_volume.collect_short_volume()` writes `short_volume_daily` rows with `market='cnms'` and `source='finra'`.
- [ ] `short_volume.bootstrap_short_volume()` covers trailing 60 trading days.
- [ ] `short_interest.collect_short_interest()` detects new bi-weekly files since the latest stored `settlement_date` and writes `short_interest_snapshots`.
- [ ] `short_interest.bootstrap_short_interest()` covers trailing 6 months (24 settlement dates).
- [ ] Universe filter: rows for tickers not in `asset_universe` (or `is_active=0`) are not written.
- [ ] Re-running any function on the same window produces no duplicate rows.
- [ ] 404 on a future-dated URL is treated as "not yet published" (no rows, no failure log).
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] `finra.short_volume` and `finra.short_interest` registered in `scheduler.py:COLLECTORS`.
- [ ] `bootstrap.run_all()` invokes `finra.short_volume.bootstrap_short_volume()` and `finra.short_interest.bootstrap_short_interest()`.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
