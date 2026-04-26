---
status: in_progress
completed_date:
commit_id:
---

# 05i — Polymarket vendor adapter

## Goal

Implement `src/alphamind/data_sources/polymarket/` to pull Qual3 prediction-market data from Polymarket's public APIs into `prediction_market_contracts` and `prediction_market_snapshots`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `prediction_market_contracts`, `prediction_market_snapshots`
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — forward-only (no historical backfill)
- `docs/design/01-data-layer/api-key-checklist.md` § Polymarket — endpoint inventory (Gamma + CLOB APIs)
- `docs/design/01-data-layer/external/qualitative.md` § 3 — contract category taxonomy
- `docs/design/01-data-layer/api-failure-handling.md` — Qual3 optional

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/polymarket/` —
- `client.py` — HTTP wrapper around Polymarket Gamma API (`/markets`) and CLOB API (`/books`, `/prices`); no auth. Integrates `with_retries(optional)`, `RateLimiter` (300 req/10s = 30/sec), `verify_connectivity()`.
- `contracts.py`:
  - `collect_snapshots(since)` — pulls active markets from Gamma `/markets` filtered by category mapping. UPSERTs into `prediction_market_contracts` (insert new contracts on first sight; update `last_seen_at` and `resolution_outcome` on close). Then per active contract pulls `prices` from the CLOB API and writes one `prediction_market_snapshots` row per contract per fire.
  - `category` derivation: a small mapping dict keyed on Polymarket's `tags` and `series_ticker` patterns (e.g., FED-related → `monetary_policy`, OPEC-related → `opec`, antitrust-related → `antitrust`, etc.). Unknown categories default to `other` — log a warn for ops review.
  - `contract_id` is Polymarket's `condition_id` (uniquely identifies a market).
  - No bootstrap function — forward-only.
- Unit tests with mocked HTTP responses.

Out of scope:
- Order book depth (`/books`) — useful for liquidity but not yet part of distillation. Defer.
- Historical price series — Polymarket's API doesn't expose deep history reliably.

## Notes

Polymarket Gamma API returns hundreds of markets. Filter aggressively at query time: only markets that match `monetary_policy | antitrust | trade | financial_reg | tax | election | opec | conflict | sanctions` patterns and have non-trivial liquidity (`liquidity_usd >= 10000` per `threshold-calibration.md`'s `prediction_market_low_liquidity_volume_min_usd`).

`yes_probability` mapping: Polymarket markets are typically binary (YES/NO outcomes). The `prices.yes` field gives the YES probability directly (0.0–1.0). For non-binary markets that Polymarket sometimes lists, skip and log a warn — the storage schema is binary-shaped.

`resolution_outcome` is updated when a market closes. Polymarket's market record carries `closed: true` plus the resolved outcome — read both and write to `prediction_market_contracts.resolution_outcome` (`yes` / `no` / `undecided`).

Rate limit: 300 req/10s = 1800/min. Set `data_sources.yaml.providers.polymarket.rate_limit_per_minute = 1800`.

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 on a successful Gamma `/markets` reach.
- [ ] `client.py` integrates `with_retries(optional)`, `RateLimiter`, and `track_run`.
- [ ] `contracts.collect_snapshots()` UPSERTs `prediction_market_contracts` rows for in-scope categories.
- [ ] `category` derivation uses a documented mapping; unknown categories default to `other` with a warn-log.
- [ ] `prediction_market_snapshots` rows written per active contract per cron fire with `yes_probability`, `volume_24h_usd`, `liquidity_usd`, `bid`, `ask`.
- [ ] Markets below `liquidity_usd < 10_000` are skipped at query time.
- [ ] Closed markets update `resolution_outcome` on the contract row.
- [ ] Re-running on the same window produces no duplicate snapshot rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
