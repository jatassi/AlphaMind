---
status: not_started
completed_date:
commit_id:
---

# 05a — Polygon vendor adapter

## Goal

Implement `src/alphamind/data_sources/polygon/` covering Q1 equity bars, Q3 options chains, Q12 corporate actions, and reference data — every Polygon-served category in the v1 schema.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — tables `ohlcv_bars`, `options_contracts`, `options_contract_snapshots`, `corporate_actions`, `asset_universe` columns populated by reference
- `docs/design/01-data-layer/collector/data-sources.md` — collection function shape, idempotency contract
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — depths
- `docs/design/01-data-layer/api-key-checklist.md` § Polygon — endpoint inventory
- `docs/design/01-data-layer/api-failure-handling.md` — retry tiers (Q1/Q12 critical, Q3 important)
- `config/assets.yaml` — universe + benchmarks scope

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/polygon/` —
- `client.py` — wraps `polygon-api-client` SDK with `with_retries`, `RateLimiter`, and a `verify_connectivity()` smoke function. Translates SDK exceptions to the generic types `with_retries` recognizes.
- `equity.py` — `collect_universe_bars(timeframes, ticker_scope, since)` and `bootstrap_universe_bars()`. Pulls 5 timeframes (`15min`, `1h`, `4h`, `1d`, `1w`) for universe + benchmarks. Two API calls per (ticker, timeframe) pair: one with `adjusted=true`, one with `adjusted=false`; merges into a single `ohlcv_bars` row with paired `adj_*` and `unadj_*` columns. Bootstrap depth: 252 trading days.
- `options.py` — `collect_options_chains(ticker_scope, since)` and `bootstrap_options_chains()`. Pulls full chain snapshot per universe ticker (no benchmarks). Writes `options_contracts` (UPSERT new contracts, update `last_seen_at` on known) and `options_contract_snapshots` (one row per contract per snapshot). Bootstrap: none — start fresh.
- `corporate_actions.py` — `collect_corporate_actions(ticker_scope, since)` and `bootstrap_corporate_actions()`. Pulls dividends (`/v3/reference/dividends`) and stock splits (`/v3/reference/stock_splits`) for universe tickers. Writes `corporate_actions`. Bootstrap depth: 252 trading days.
- `reference.py` — `collect_reference(ticker_scope)`. Pulls ticker details (`/v3/reference/tickers/{ticker}`) and updates `asset_universe` mutable fields (`market_cap_usd`, `shares_outstanding`, `float_shares`, `last_updated`). Run weekly. No bootstrap-specific function — same call.
- Unit tests with mocked SDK responses for each collection function:
  - Idempotency: re-calling produces no duplicates.
  - UPSERT semantics on natural keys.
  - Failure semantics: full failure → `failed` collection_runs row, no data rows; partial failure → successful rows written, `error_summary` populated.

Out of scope:
- Tick-level data (Q2 — deferred per `storage.md § Deferred categories`).
- Failover to Alpaca (deferred per `data-sources.md § Multi-source failover`).
- Bootstrap orchestration across vendors (story 06a).

## Notes

`client.py` reads the API key from `data_sources.yaml.providers.polygon.api_key_env` (resolved via `.env`). Both Polygon Stocks Starter and Options Starter share one key.

Polygon's aggregate API supports any multiplier × unit. For `4h` use `range/4/hour/...`. The endpoint returns adjusted bars by default; pass `adjusted=false` for unadjusted.

`session` column in `ohlcv_bars`: derive from bar timestamp + market calendar (`exchange_calendars` package, already a dependency). NYSE regular session is 09:30–16:00 ET; pre-market 04:00–09:30; after-hours 16:00–20:00; overnight otherwise.

`benchmarks` are populated from `assets.yaml`'s `benchmarks:` block. They're equities/ETFs and use the same Polygon equity endpoints — only the universe scope differs.

For `options.py`, the snapshot endpoint `/v3/snapshot/options/{underlying}` returns the full chain. Contracts not present in a given snapshot are not pruned from `options_contracts` — `last_seen_at` just doesn't update. Retention policy at 120 days is operational, not implemented here.

`corporate_actions` — when an action's `ex_date` falls within the bar window we already wrote, do not re-fetch bars. Per the no-backfill principle (`storage.md § Cross-cutting rules`), the stored adjusted bars reflect adjustments-known-at-ingestion-time; downstream consumers tolerate the artifact.

For `reference.py`, `asset_universe` rows for benchmarks (`asset_role='benchmark'`) get the same treatment as universe rows but skip fields that aren't meaningful (`analyst_count`, `cik` may stay null).

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API key works and non-zero on auth failure.
- [ ] `client.py` integrates `with_retries(critical)` for Q1/Q12 endpoints and `with_retries(important)` for Q3.
- [ ] `client.py` integrates `RateLimiter` per the `polygon` provider config.
- [ ] All collection functions wrap their work in `track_run(...)`.
- [ ] `equity.collect_universe_bars()` writes paired adjusted + unadjusted columns to `ohlcv_bars` for the requested ticker × timeframe × window scope.
- [ ] `equity.bootstrap_universe_bars()` covers 252 trading days × 5 timeframes × universe + benchmarks.
- [ ] `options.collect_options_chains()` writes new `options_contracts` rows on first sight, updates `last_seen_at` on known contracts, and writes `options_contract_snapshots` rows.
- [ ] `corporate_actions.collect_corporate_actions()` writes `corporate_actions` rows for dividends and splits with `action_type` set correctly.
- [ ] `corporate_actions.bootstrap_corporate_actions()` covers 252 trading days.
- [ ] `reference.collect_reference()` updates `asset_universe.market_cap_usd`, `shares_outstanding`, `float_shares`, `last_updated` for the requested ticker scope.
- [ ] Re-running any collection function on the same window produces zero new rows (idempotency).
- [ ] On full API failure, `collection_runs` records a `failed` row and no data-table rows are written.
- [ ] On partial failure, successful rows write, the failure scope is recorded in `collection_runs.error_summary`.
- [ ] Unit tests with mocked SDK responses cover all of the above.
- [ ] `uv run pytest` passes.
