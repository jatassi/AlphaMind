---
status: not_started
completed_date:
commit_id:
---

# 06a — Bootstrap orchestrator

## Goal

Implement the one-time bootstrap entry point that calls each vendor's `bootstrap_*()` function in dependency order and produces a primed database ready for steady-state collection.

## Reading

- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — per-category scope and execution order
- `docs/design/01-data-layer/collector/data-sources.md` — collection function shape, idempotency contract
- `docs/design/01-data-layer/collector/runner.md` § Entry points — `bootstrap` subcommand expectation

## Depends on

- 05a–05j (every vendor adapter must expose `bootstrap_*()` functions before this orchestrator can call them)
- 04 (`_common.py` for config loading, `track_run`)

## Scope

In scope: under `src/alphamind/collector/` —
- `bootstrap.py`:
  - `run_all(only_vendor=None)` — orchestrator function. Calls vendor `bootstrap_*` functions in the order specified by `lifecycle.md`:
    1. Reference: `polygon.reference.bootstrap_*` and seed `asset_universe` + `sector_classification` + `etf_membership` from `assets.yaml`.
    2. Corporate actions: `polygon.corporate_actions.bootstrap_corporate_actions()`.
    3. Equity OHLCV: `polygon.equity.bootstrap_universe_bars()`.
    4. (Parallel after step 1) FRED daily: `fred.macro.bootstrap_series(daily_only=True)`. EIA: `eia.energy.bootstrap_series()`. Treasury: `treasury.auctions.bootstrap_auctions()`.
    5. FRED + BLS monthly: `fred.macro.bootstrap_series(monthly_only=True)` and `bls.macro.bootstrap_series()`.
    6. Calendar: `finnhub.calendar.bootstrap_earnings_calendar()`, `bootstrap_economic_calendar()`, IPO + FDA forward 90d.
  - `--only <vendor>` flag runs a single vendor's bootstrap (e.g., `--only polygon` runs steps 1, 2, 3 for Polygon only; `--only fred` runs FRED's monthly + daily macro).
  - Progress logging: per-vendor start/end with row counts; per-step timing.
  - Idempotent: re-running picks up where it left off (vendor `bootstrap_*` functions are idempotent per their stories).
  - Interrupt-safe: each vendor's bootstrap commits its own rows; partial completion is not rolled back.
- Wire the `bootstrap` subcommand into `src/alphamind/collector/__main__.py` argparse dispatch (the file may not exist yet — story 06b creates it; coordinate by leaving a stub here that 06b fills).
- Unit tests with mocked vendor `bootstrap_*` functions verifying:
  - Order of calls matches lifecycle.md.
  - `--only` flag dispatches correctly.
  - Progress logging emits expected lines.
  - An exception in one vendor does not abort the orchestrator — others continue (log + report).

Out of scope:
- The vendor `bootstrap_*` implementations (stories 05a–05j).
- The `run` and `catch-up` subcommands (story 06b).
- NSSM service registration (story 07).

## Notes

The orchestrator is single-process, single-thread. Parallelism across vendors is left to the runner's APScheduler executors during steady-state — bootstrap runs in series for simpler progress reporting and predictable rate-limit behavior.

Step 1's "seed `asset_universe` from `assets.yaml`" step is special — it doesn't come from a vendor API. It reads `assets.yaml`'s `sectors:` and `benchmarks:` blocks and inserts rows into `asset_universe`. The Polygon reference call then enriches with `cik`, `figi`, `shares_outstanding`, etc.

Failure handling at orchestrator level: if a vendor's `bootstrap_*` raises after exhausting retries, the orchestrator logs the failure, writes a summary line to a `bootstrap_runs` activity log (or just to the runner log file), and continues with the next vendor. The operator inspects logs at the end and decides whether to re-run `--only <failed_vendor>`.

`run_all` is callable from a Python REPL as well as from `__main__.py` — the function takes a config object (from `load_config()`) explicitly rather than reading globals.

## Acceptance criteria

- [ ] `bootstrap.run_all()` exists and calls vendor `bootstrap_*` functions in the documented order.
- [ ] `--only <vendor>` runs a single vendor's bootstrap.
- [ ] Step 1's `assets.yaml` seed inserts rows into `asset_universe` for both `sectors:` (role `universe`) and `benchmarks:` (role `benchmark`) before any vendor API call.
- [ ] Re-running the orchestrator on a primed database produces no duplicate rows (the underlying vendor functions are idempotent; this exercises the orchestration glue).
- [ ] Progress logging emits per-vendor start/end with row counts.
- [ ] An exception inside one vendor's bootstrap does not stop the orchestrator from running subsequent vendors.
- [ ] The `bootstrap` subcommand is wired into `__main__.py` argparse (or a stub exists for story 06b to extend).
- [ ] Unit tests with mocked vendor functions cover all of the above.
- [ ] `uv run pytest` passes.
