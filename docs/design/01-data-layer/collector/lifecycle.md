# Lifecycle

How the data layer comes up on first deploy, recovers from downtime, and eventually splits responsibilities with the pipeline's data-layer phase. The shared [data sources library](data-sources.md) and [runner](runner.md) provide the mechanism; this document specifies the policy.

## Bootstrap

A one-time historical backfill on first deploy. Two facts force it (per [threshold-calibration.md § Bootstrap policy](../../02-distillation-layer/threshold-calibration.md#bootstrap-policy)):

1. Distillation's per-ticker baselines have lookback windows of 20–252 days. Waiting for full forward calibration would block paper trading for a year on `gap_fill_baseline_days` alone.
2. Decisions on missing data are worse than skipped decisions.

Bootstrap provides enough history that high-frequency baselines reach `calibrated` immediately; long-tail event-driven baselines start with sector-pooled fallbacks.

### Per-category scope

| Category | Source | Depth | Driver |
|---|---|---|---|
| Reference | Polygon reference + `validate_universe.py` | Current snapshot | No history needed. |
| Corporate actions (Q12) | Polygon `/v3/reference/{dividends,stock_splits}` | 252 trading days | Adjusted-bar correctness on backfilled bars. |
| Equity OHLCV (Q1) | Polygon aggregates | 252 trading days × 5 timeframes × universe + benchmarks | `gap_fill_baseline_days=252`. |
| Macro daily (Q6) | FRED | 90 days | `correlation_long_days=60`, `funding_stress_baseline_days=60`, `market_liquidity_baseline_days=60`. |
| Macro monthly (Q6) | FRED + BLS | 24 months | `macro_surprise_percentile=90` requires a 24-month percentile distribution. |
| EIA energy (Q6) | EIA | 252 days | Weekly inventory; one year covers seasonality. |
| Treasury auctions (Q6) | Treasury fiscal data | 12 months | Trend context for auction-quality metrics. |
| Event calendar (Q6g + Qual5 + earnings) | Finnhub | Forward 90 days | Forward-looking by nature. |
| Options chain (Q3) | — | None — start fresh | Historical options expensive; distillation accepts `bootstrap` state. |
| News (Qual1) | — | None — start fresh | Free-tier rate limits; forward-only. |
| Prediction markets (Qual3) | — | None — start fresh | Delta-over-level principle. |

### Execution order

1. Reference data (`asset_universe`, `sector_classification`, `etf_membership`) — every other table FKs to ticker.
2. Corporate actions — populated before bar fetch so the adjusted-bar pull is on a known footing.
3. Equity OHLCV — universe + benchmarks, all 5 timeframes, paired adjusted + unadjusted, 252 days.
4. FRED daily / EIA / Treasury auctions — independent of equity, runnable in parallel after step 1.
5. FRED + BLS monthly (24 months).
6. Finnhub event + earnings calendar (forward 90 days).
7. Hand off to scheduler — `python -m alphamind.collector run`. From this point everything is forward-only.

Steps 3 / 4 / 5 run in parallel across vendors via the standard executor split. Within Polygon, `corporate_actions` precedes `equity` (vendor serialization).

### Entry point

```
python -m alphamind.collector bootstrap [--only <vendor>]
```

A single orchestrator calls each `data_sources.<vendor>.<domain>.bootstrap_*()` in dependency order with progress logging. `--only` runs one vendor. Per-source functions remain callable from a Python REPL.

Bootstrap pulls bars through the last *completed* trading day. The current day's bars are collected by the ongoing 15-min `polygon.equity` schedule once the runner starts.

Idempotent (UPSERT on natural keys per [data-sources.md § Idempotency contract](data-sources.md#idempotency-contract)) and interrupt-safe — re-running resumes. Expected runtime 30–60 min, dominated by the equity OHLCV pull.

### Distillation interaction

After bootstrap completes, the calibration states the distillation layer assigns:

| Distillation baseline | State after bootstrap |
|---|---|
| Volume / ATR / spread per ticker (20d, min 30 obs) | `calibrated` |
| Correlation matrices (20d / 60d) | `calibrated` |
| Funding stress, market liquidity (60d) | `calibrated` |
| Macro surprise percentile (24m) | `calibrated` |
| Sentiment baseline (60d, min 30 obs) | `bootstrap` initially → `calibrated` after vendor delivers history or 30 days forward observation. |
| Gap-fill rate (252d, min 30 events per ticker) | `bootstrap` for most names; sector-pooled fallback per spec; resolves over months as events accrue. |
| Extended-hours confirmation rate (90d, min 20 events) | `bootstrap`; same pattern as gap-fill. |
| Lead-lag pair estimates (≥10 events) | `bootstrap` for ~1–2 months. |
| Prediction-market delta | `calibrated` (universe-wide 5pp threshold; no per-contract calibration). |

The data layer provides history; calibration-state tagging and bootstrap policy are owned by distillation per [threshold-calibration.md](../../02-distillation-layer/threshold-calibration.md).

### Volume after bootstrap

| Table | Approximate rows | Approximate size |
|---|---|---|
| `ohlcv_bars` (77 tickers × 5 timeframes × paired adj/unadj × 252 days) | ~600K | ~150 MB |
| `corporate_actions` | ~3K | <1 MB |
| `macro_observations` | ~50K | ~5 MB |
| `treasury_auctions` | ~50 | <1 MB |
| `event_calendar` (forward 90 days) | ~500 | <1 MB |

Total post-bootstrap database size: ~150–200 MB.

## Catch-up

Steady-state accommodates downtime gaps without a separate code path. Each collection function defaults `since` to:

```
since = max(latest_period_start_in_table_for(ticker, timeframe), now - default_lookback)
```

Bootstrap, ongoing collection, and catch-up share the same code path with different `since` values.

`python -m alphamind.collector catch-up` runs every collection function once with `since=None`, then exits. Used after extended downtime when the operator wants a single sweep before re-starting the long-running runner.

Principle: **forward-only by default; targeted single-record revisions acceptable; no routine bulk historical sweeps.** Bootstrap is the one-time exception. Catch-up is bounded by what's missing, so it's targeted-by-construction.

## Operator workflow on first deploy

1. Set up `.env` with API keys per [api-key-checklist.md](../api-key-checklist.md). Confirm provider connectivity via each adapter's smoke-test entry point.
2. Run `python -m alphamind.collector bootstrap`. Watch logs.
3. Spot-check row counts against the volume table above.
4. `nssm start alphamind-collector` to begin steady-state collection (or run in a PowerShell window during development).
5. Verify the next-scheduled cycle of each collector writes new rows successfully.

Revoked-key drill — revoke one provider's key, verify the runner logs the failure, writes a `failed` row to `collection_runs`, and continues with healthy sources — is the standard go-live check before unattended operation.

## Eventual pipeline integration

When the pipeline's data-layer phase comes online, roles split:

- Pipeline owns invocation-time pulls of high-frequency, freshness-critical sources (Q1 equity, Q3 options) that need <5 min currency at decision time.
- Runner continues handling slow-cadence sources (FRED, BLS, calendar, corporate actions) — pulling them every invocation is wasteful.

Both processes import the same [data sources library](data-sources.md), write to the same tables, and share the same idempotency contract. The split is configuration, not architecture.

Migration:

1. Pipeline's data-layer phase implements `collect_*()` calls for high-frequency sources at invocation start.
2. Operator removes the corresponding jobs from `config/collector_schedule.yaml`.
3. Runner restarts; schedules only slow-cadence sources.
4. Pipeline and runner coexist as long-running services.

No data migration. Both processes share the SQLite database via WAL mode per [data-and-state.md § Concurrency model](../../../architecture/data-and-state.md#concurrency-model).

---

*Cross-references:*

- [Storage](storage.md) — what bootstrap and steady-state write.
- [Data sources library](data-sources.md) — the idempotency contract that makes bootstrap and catch-up safe.
- [Runner](runner.md) — the long-running process that executes steady-state.
- [02-distillation-layer/threshold-calibration.md](../../02-distillation-layer/threshold-calibration.md) — calibration windows that drove bootstrap depth.
- [api-key-checklist.md](../api-key-checklist.md) — provider/account inventory.
- [api-failure-handling.md](../api-failure-handling.md) — fail-closed semantics during bootstrap.
