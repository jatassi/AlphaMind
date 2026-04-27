---
status: in_progress
completed_date:
commit_id:
---

# 05m — Finnhub estimate revisions

## Goal

Extend the existing Finnhub vendor adapter (story 05f) with `estimate_revisions.py` covering Q5 analyst-estimate revision tracking. The collector polls Finnhub's earnings/revenue estimate endpoints daily, diffs the observed consensus against the latest stored row per `(ticker, fiscal_year, fiscal_period, metric)`, and inserts a new row only when the consensus has changed.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `earnings_estimate_revisions`
- `docs/design/01-data-layer/api-key-checklist.md § Finnhub` — endpoint inventory (the existing entry covers the analyst-estimates endpoints)
- `docs/design/01-data-layer/api-failure-handling.md` — Q5 is `important`
- `docs/design/01-data-layer/schema/fundamentals.py` — `EarningsEstimateDynamics` entity
- `docs/design/01-data-layer/external/quantitative.md § Q5` — revision-dynamics signal context
- `docs/implementation/01-data-layer/collector/05f-finnhub-vendor-adapter.md` — existing Finnhub `client.py` is reused, not replaced

## Depends on

- 04 (shared library `_common.py`)
- 05f (Finnhub vendor adapter — provides `client.py` with retry, rate limiting, and `verify_connectivity()`)
- 03b (persistence — table DDL applied)

## Scope

In scope: under `src/alphamind/data_sources/finnhub/` —

- `estimate_revisions.py`:
  - `collect_estimate_revisions(ticker_scope=None)` — for each ticker in `ticker_scope` (default: `asset_universe.is_active=1`), fetch:
    - EPS estimates via the `finnhub-python` SDK's earnings-estimate method (returns one entry per upcoming fiscal period with `period`, `epsAvg`, `numberAnalysts`).
    - Revenue estimates via the SDK's revenue-estimate method (returns `period`, `revenueAvg`, `numberAnalysts`).
  - For each `(ticker, fiscal_year, fiscal_period, metric)` tuple in the response:
    - Map Finnhub's `period` (an ISO date) to `fiscal_year` + `fiscal_period` (`Q1`/`Q2`/`Q3`/`Q4`/`FY`) using the company's reported fiscal calendar from `earnings_event_details`. Fall back to calendar-year mapping (period date → calendar quarter) when no fiscal-calendar entry exists for the ticker.
    - Look up the latest stored row in `earnings_estimate_revisions` by `(ticker, fiscal_year, fiscal_period, metric)` ordered by `revised_at` desc.
    - If `consensus_value` differs (or no prior row exists), insert a new row with `revised_at = now (UTC)`, `prior_consensus_value = stored value or NULL`, `consensus_value = observed value`, `num_analysts = response.numberAnalysts`.
    - If unchanged, no write.
  - `bootstrap_estimate_revisions()` — initial seed: insert one row per `(ticker, fiscal_year, fiscal_period, metric)` for every active universe ticker, with `prior_consensus_value = NULL`. Idempotent: skips tuples already present.
- Register the collector in `src/alphamind/collector/scheduler.py:COLLECTORS`:
  - `"finnhub.estimate_revisions": finnhub.estimate_revisions.collect_estimate_revisions`
- Extend `src/alphamind/collector/bootstrap.py:run_all()` step 6 (Calendar) to also invoke `finnhub.estimate_revisions.bootstrap_estimate_revisions()` after the existing calendar bootstraps.
- Unit tests with mocked SDK responses covering: first-time seed (all rows have `prior_consensus_value = NULL`), changed consensus (new row written with prior value populated), unchanged consensus (no write), missing `numberAnalysts` field (null stored), fiscal-period mapping fallback when `earnings_event_details` has no row for the ticker, idempotent re-run.

Out of scope:
- Other Q5 entities (`AnalystRatings`, `InsiderInstitutionalOwnership`, `RevenueGrowthTrajectory`, `MarginProfitabilityShift`) — separate stories when their consumers materialize.
- Earnings actuals — already captured in `earnings_event_details` via 05f's `calendar.py`.
- A dedicated `client.py` — the existing 05f Finnhub client is reused.
- EBITDA estimates — Finnhub's free tier doesn't expose EBITDA estimates uniformly; the schema accommodates it (NULL allowed) but no collector path is required for v1.

## Notes

The data source contract: Finnhub returns the *current* consensus snapshot, not a revision history. Our `earnings_estimate_revisions` table reconstructs the revision history by diffing daily observations. The first observation for any tuple seeds with `prior_consensus_value = NULL`; subsequent revisions chain via `prior_consensus_value`.

Diff at the SQL level — read the latest stored row inside the same transaction as the insert to avoid races. Use `SELECT ... ORDER BY revised_at DESC LIMIT 1` keyed on the composite `(ticker, fiscal_year, fiscal_period, metric)`.

`metric` values per row: `'eps'` for EPS estimates, `'revenue'` for revenue estimates. One Finnhub response per ticker yields up to ~8 rows (4 fiscal periods × 2 metrics) of which only changed tuples write a new row.

`fiscal_period` mapping: Finnhub's `period` field is the fiscal-period end date (ISO string). For most tickers, calendar-quarter mapping is correct. Where a ticker's fiscal calendar offsets from calendar quarters (e.g., AAPL's fiscal year ending September), the `earnings_event_details.fiscal_period` history provides the authoritative mapping for past releases — use that as the precedent for future periods on the same ticker.

`num_analysts` is in `numberAnalysts` on Finnhub's response. Some responses omit it — store `NULL`.

Free-tier rate limit is shared with 05f (60 req/min). One pass over 78 universe tickers × 2 endpoint families = 156 calls — fits comfortably under the per-minute budget when paced by the existing `RateLimiter`.

## Acceptance criteria

- [ ] `estimate_revisions.collect_estimate_revisions()` reuses 05f's `client.py` (no new client file).
- [ ] A new `earnings_estimate_revisions` row is written only when the observed consensus differs from the most-recent stored row for the same `(ticker, fiscal_year, fiscal_period, metric)`.
- [ ] `prior_consensus_value` is populated from the stored row being superseded; `NULL` on first observation.
- [ ] `num_analysts` populated when present in the Finnhub response; `NULL` when omitted.
- [ ] Universe filter applied (`asset_universe.is_active=1`).
- [ ] Fiscal-period mapping uses `earnings_event_details` precedent when available; calendar-quarter fallback otherwise.
- [ ] `bootstrap_estimate_revisions()` seeds one row per `(ticker, fiscal_year, fiscal_period, metric)` with `prior_consensus_value = NULL`; re-running is idempotent.
- [ ] Re-running `collect_estimate_revisions()` on unchanged data produces no new rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] `finnhub.estimate_revisions` registered in `scheduler.py:COLLECTORS`.
- [ ] `bootstrap.run_all()` invokes `bootstrap_estimate_revisions()` after the existing calendar bootstraps.
- [ ] Unit tests with mocked SDK responses cover seed, changed-consensus, unchanged-consensus, missing `numberAnalysts`, fiscal-period mapping fallback, and idempotent re-run.
- [ ] `uv run pytest` passes.
