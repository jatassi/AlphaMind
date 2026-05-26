# iBorrowDesk borrow_cost collector fails every run on HTTP 444 block — borrow_cost_daily covers only 10 of 65 active equities

## Symptom

The `iborrowdesk.borrow_cost` collector has failed on every scheduled run for at least 8 days. Every run from 2026-05-12 through 2026-05-19 is marked `failed` in `collection_runs` with an identical `error_summary`:

```
IBorrowDeskBlockedError: HTTP 444 from iBorrowDesk for SHOP — block active
```

Each run starts at 10:00 UTC and fails \~14 s later.

## Evidence

Production DB (`/Volumes/Users/jacks/AlphaMind/data/alphamind.db`, read 2026-05-20). The six most recent `iborrowdesk.borrow_cost` runs (2026-05-12, -13, -14, -15, -18, -19, all at 10:00 UTC) are each `failed` with the `error_summary` above — no `rows_written`.

`borrow_cost_daily` holds 2,750 rows but only **10 distinct tickers** — AAPL, AMZN, GOOG, GOOGL, META, MSFT, ORCL, PLTR, SNOW, TSLA — versus **65 active equities** in `asset_universe`. 55 active equities have no `borrow_cost_daily` row at all. The 10 covered tickers do carry rows dated 2026-05-18, so the collector reaches \~10 tickers before the block trips and then aborts.

## Root cause

`collect_borrow_cost` (`src/alphamind/data_sources/iborrowdesk/borrow_cost.py`) sweeps `active_universe_tickers(include_benchmarks=False)` in `asset_universe` row order — the query in `data_sources/_common/universe.py` carries no `ORDER BY`. When `IBorrowDeskClient.fetch_ticker` (`src/alphamind/data_sources/iborrowdesk/client.py:119`) sees HTTP 444 it raises `IBorrowDeskBlockedError`; `collect_borrow_cost` catches it, commits the rows fetched so far, and re-raises — aborting the sweep. The abort is intentional (the client docstring warns "Sustained requests will deepen the block. Caller must halt"), so every run lands only the tickers swept before the block and abandons the rest.

The block reproduces at the same ticker (`SHOP`) on every run, which points to a stable per-IP request quota at iBorrowDesk rather than a random rate-limit miss — the prod server's IP appears to be allowed a fixed number of requests, then 444'd. The ≥5 s pacing (`RateLimiter` keyed on `iborrowdesk`) is not preventing it.

## Consequences

borrow-cost data is stuck at roughly 15% universe coverage. This degrades several surfaces.

* The `borrow_cost_budget_pct_per_day` guardrail (wired by [ALP-586](https://linear.app/alphamind-jatassi/issue/ALP-586/borrow-cost-resolver-unwired-borrow-cost-budget-guardrail-is-a-silent)) only ever exercises the 10 covered mega-caps; a short-equity OPEN/ADD on any of the other 55 active equities returns `UNAVAILABLE` / `MISSING_BORROW_COST` — a resolver-coverage gap, graceful, but the thesis cannot be validated as an equity short.
* The `sec_lending` analysis tool returns `PARTIAL`/`UNAVAILABLE` for the 55 uncovered tickers.
* `collection_runs` shows a permanently `failed` collector, which masks any other future failure mode of this collector.

## Scope

The fix is whatever gets iBorrowDesk coverage past `SHOP` and through the rest of the universe. Candidate directions for the implementer to evaluate, not a prescription.

* Determine whether the 444 is a persistent IP-level block (needs a cooldown, a different egress IP, or contact with iBorrowDesk) or a same-run cumulative-quota trip (needs slower pacing or a per-run request budget).
* If the block cannot be lifted, make the sweep resumable — persist the last-swept ticker and start the next run after it (rotating cursor) so coverage fills in over several days instead of permanently stalling at the same 10.
* Consider lowering the sweep cadence or splitting the universe across runs so each run stays under the quota.

## Acceptance criteria

- [ ] `iborrowdesk.borrow_cost` runs reach `success` (or a documented partial-success state), not `failed`, in `collection_runs` on consecutive days.
- [ ] `borrow_cost_daily` distinct-ticker coverage expands materially beyond the current 10 toward the 65-equity active universe.
- [ ] A repeated 444 block no longer permanently strands the same tail of the universe — either the block is resolved, or the sweep resumes from where it left off so coverage rotates across runs.

## Verification

* Inspect `collection_runs` for `iborrowdesk.borrow_cost` over several days after the fix — `status` should not be a permanent `failed`.
* `SELECT COUNT(DISTINCT ticker) FROM borrow_cost_daily` climbs past 10.
* A short-equity OPEN on a previously-uncovered ticker no longer returns `MISSING_BORROW_COST` from `validate_guardrail` once its borrow row lands.

## Notes

Surfaced while verifying prerequisite data for [ALP-586](https://linear.app/alphamind-jatassi/issue/ALP-586/borrow-cost-resolver-unwired-borrow-cost-budget-guardrail-is-a-silent), which wired the borrow-cost resolver. [ALP-586](https://linear.app/alphamind-jatassi/issue/ALP-586/borrow-cost-resolver-unwired-borrow-cost-budget-guardrail-is-a-silent)'s per-ticker coverage-gap handling means this collector outage degrades gracefully rather than crashing — but borrow-cost budgeting and short-equity validation remain effectively limited to 10 mega-caps until this is fixed. Same "one ticker aborts the whole cycle" resilience shape as [ALP-420](https://linear.app/alphamind-jatassi/issue/ALP-420/newspy-one-failing-ticker-aborts-the-whole-cycle-discarding-already) (finnhub.news), though here the abort-on-block is deliberate per the client contract.
