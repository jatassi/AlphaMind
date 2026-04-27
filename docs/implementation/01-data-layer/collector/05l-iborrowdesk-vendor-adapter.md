---
status: done
completed_date: 2026-04-27
commit_id: eceef43
---

# 05l — iBorrowDesk vendor adapter

## Goal

Implement `src/alphamind/data_sources/iborrowdesk/` covering Q4:4b (borrow cost / fee dynamics). Source is iBorrowDesk's undocumented JSON endpoint, which delivers ~1 trading year of EOD daily history plus trailing intraday snapshots in a single per-ticker call.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `borrow_cost_daily`, `borrow_cost_intraday`
- `docs/design/01-data-layer/api-key-checklist.md § iBorrowDesk` — endpoint, observed rate-limit behavior, on-demand pattern, and "no sanctioned API tier exists at any price" constraint
- `docs/design/01-data-layer/api-failure-handling.md` — Q4 is `optional`
- `docs/design/01-data-layer/schema/short_selling.py` — `BorrowCost`, `BorrowCostSnapshot` entities
- `docs/design/01-data-layer/external/quantitative.md § Q4:4b` — borrow-cost signal context

## Depends on

- 04 (shared library `_common.py`)
- 03b (persistence — table DDL applied)

## Scope

In scope: under `src/alphamind/data_sources/iborrowdesk/` —

- `client.py` — HTTP wrapper for `https://www.iborrowdesk.com/api/ticker/{TICKER}`. Critical behaviors:
  - Sends a browser-like `User-Agent` header (e.g., `Mozilla/5.0 ... Chrome/...`) on every request — the linked write-up explicitly recommends this. Non-browser User-Agents may be silently dropped.
  - Follows redirects (apex `iborrowdesk.com` 301-redirects to `www.`).
  - Distinguishes three response classes:
    - **Success (HTTP 200)** — parse JSON, return.
    - **Not in coverage (HTTP 404 with `{"errors": [{"code": "not_found", ...}]}`)** — return a sentinel (e.g., raise `IBorrowDeskCoverageError`) for the caller to warn-log and skip. Not retryable.
    - **Blocked (HTTP 444 *or* TCP empty-reply within ~150ms)** — distinct from regular 4xx/5xx. Trigger a heavy back-off path: exponential, starting at 5 minutes, max 1 hour. Surface as `IBorrowDeskBlockedError` so callers can record it cleanly in `collection_runs.error_summary`.
  - Integrates `RateLimiter` (`data_sources.yaml.providers.iborrowdesk.rate_limit_per_minute = 12` → ~5s spacing).
  - `verify_connectivity()` probes a known-coverage ticker (e.g., `AAPL`) and exits 0 on HTTP 200.
- `borrow_cost.py`:
  - `collect_borrow_cost(ticker_scope=None)` — daily universe sweep. For each ticker in `ticker_scope` (default: `asset_universe.is_active=1`), one paced HTTP fetch through the rate-limited client. Parse JSON and write rows:
    - Each `daily` array entry → upsert into `borrow_cost_daily` keyed on `(observation_date, ticker)`. Map fields: `daily.date → observation_date`, `daily.fee → fee_pct`, `daily.rebate → rebate_pct`, `daily.available → available_shares`, `daily.high_fee → intraday_high_fee_pct`, `daily.low_fee → intraday_low_fee_pct`, `daily.high_available → intraday_high_available_shares`, `daily.low_available → intraday_low_available_shares`.
    - Each `real_time` array entry → upsert into `borrow_cost_intraday` keyed on `(snapshot_at, ticker)`. Map: `real_time.datetime → snapshot_at` (UTC), `real_time.fee → fee_pct`, `real_time.available → available_shares`.
    - On `IBorrowDeskCoverageError` (404): warn-log, continue to next ticker (universe coverage gap).
    - On `IBorrowDeskBlockedError`: stop the sweep early, mark `collection_runs` failed, raise so the runner schedules the next attempt at the next cron fire (do not loop within one run).
  - `refresh_ticker(ticker)` — on-demand single-ticker refresh callable from analyst tools / distillation. Same parse + upsert path as the sweep, scoped to one ticker. Respects the same `RateLimiter` (analyst-tool callers don't bypass global pacing). Returns the freshness timestamp (most recent `real_time.datetime` from the response, or `latest_*.updated`).
- Register the daily sweep in `src/alphamind/collector/scheduler.py:COLLECTORS`:
  - `"iborrowdesk.borrow_cost": iborrowdesk.borrow_cost.collect_borrow_cost`
- No bootstrap function — each call returns ~1 trading year of EOD daily history, so the first daily-cron run *is* the bootstrap. Do not extend `bootstrap.run_all()`.
- Unit tests with mocked HTTP responses covering: success (write to both tables), 404 (skip with warn-log), 444 (raise blocked, halt sweep), TCP empty-reply (raise blocked), 200 with malformed JSON, idempotency on re-run.

Out of scope:
- Patron / Premium tier features — no sanctioned programmatic-access tier exists at any price.
- Headless browser scraping — the JSON endpoint suffices.
- Bootstrap function — the daily sweep covers historical fill on first run.

## Notes

The empirical rate-limit was characterized by a POC documented in the api-key-checklist update: ~4 unpaced requests trip the 444 block; 5s spacing under the configured `rate_limit_per_minute = 12` keeps sustained access stable. The block recovery time is unknown — assume hours-scale, not seconds.

Do not retry within the same `collect_borrow_cost` run on a 444. The next cron fire is the natural retry; immediate retries deepen the block.

`real_time` array snapshots are at ~16-min cadence (matching IBKR's upstream refresh). One daily call captures the trailing 3–5 trading days of intraday detail — distillation's `fee_trend_1d` and `fee_spike_active` computations have everything they need from one daily pull. `refresh_ticker` exists for analyst tools that want fresher-than-daily borrow data on a specific name.

`borrow_cost_intraday` is pruned at 90 days per `storage.md § Retention` — the 16-min granularity is only useful inside the trend-computation window. Pruning is an ops-time policy, not part of this story.

CUSIP cross-validation is available (the response includes a `cusip` field) but is not currently consumed — `asset_universe` doesn't normalize CUSIPs today. Skip.

## Acceptance criteria

- [ ] `client.py` sends a browser-like `User-Agent` header on every request.
- [ ] `client.py` follows the apex → www redirect.
- [ ] `client.py` has `verify_connectivity()` that exits 0 on a successful fetch for a known-coverage ticker.
- [ ] `client.py` raises `IBorrowDeskCoverageError` on HTTP 404 with the structured `not_found` body.
- [ ] `client.py` raises `IBorrowDeskBlockedError` on HTTP 444 *or* TCP empty-reply, distinct from generic 4xx/5xx.
- [ ] `client.py` integrates `RateLimiter` such that sustained sweeps observe ≥5s spacing.
- [ ] `borrow_cost.collect_borrow_cost()` writes `borrow_cost_daily` rows from each `daily` array entry with `source='iborrowdesk'`.
- [ ] `borrow_cost.collect_borrow_cost()` writes `borrow_cost_intraday` rows from each `real_time` array entry with `source='iborrowdesk'`.
- [ ] `borrow_cost.collect_borrow_cost()` warn-logs and continues on `IBorrowDeskCoverageError` (universe coverage gap).
- [ ] `borrow_cost.collect_borrow_cost()` halts the sweep and marks `collection_runs` failed on `IBorrowDeskBlockedError` — no further requests within the same run.
- [ ] `borrow_cost.refresh_ticker()` performs the same write path for a single ticker on demand and respects the global `RateLimiter`.
- [ ] Re-running on unchanged data produces no duplicate rows (idempotent upsert on the documented composite keys).
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] `iborrowdesk.borrow_cost` registered in `scheduler.py:COLLECTORS`.
- [ ] Unit tests with mocked HTTP responses cover success, 404, 444, TCP empty-reply, malformed JSON, and idempotency.
- [ ] `uv run pytest` passes.
