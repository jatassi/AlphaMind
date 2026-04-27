# Data sources library

The shared collection library at `alphamind.data_sources`, used by the [runner](runner.md) today and the future pipeline's data-layer phase. Owns the vendor SDK boundary, retry semantics, rate limiting, and the idempotent collection functions that produce rows in the [storage schema](storage.md).

## Module layout

```
src/alphamind/data_sources/
├── _common.py               # retry-per-tier, rate limiter, track_run context manager
├── polygon/
│   ├── client.py            # wraps polygon-api-client SDK
│   ├── equity.py            # collect_universe_bars(...)
│   ├── options.py           # collect_options_chains(...)
│   ├── corporate_actions.py
│   └── reference.py
├── fred/      { client.py, macro.py }
├── eia/       { client.py, energy.py }
├── bls/       { client.py, macro.py }
├── treasury/  { client.py, auctions.py }
├── finnhub/   { client.py, news.py, calendar.py }
├── marketaux/ { client.py, news.py }
├── sec_edgar/ { client.py, rss.py }
├── polymarket/ { client.py, contracts.py }
├── kalshi/    { client.py, contracts.py }
└── alpaca/    { client.py, equity.py }   # failover, deferred
```

Per-vendor packages own the HTTP/SDK boundary. Each `client.py` wraps the vendor SDK with `_common.py`'s retry, rate-limit, and run-tracking primitives. Per-domain modules expose narrow collection functions.

## Collection function shape

Each per-domain module exposes one or more `collect_*` functions:

```python
def collect_universe_bars(
    timeframes: list[str] | None = None,
    ticker_scope: list[str] | None = None,
    since: datetime | None = None,
) -> CollectionResult: ...
```

When `since` is `None`, the function defaults to:

```
since = max(latest_period_start_in_table_for(ticker, timeframe), now - default_lookback)
```

i.e., "since the latest row we have, falling back to a per-collector default lookback if the table is empty." Bootstrap, catch-up, and steady-state share the same code path with different `since` values — see [lifecycle.md](lifecycle.md).

Functions are idempotent — every UPSERT keys on the natural composite key from [storage.md](storage.md). See [Idempotency contract](#idempotency-contract).

## `_common.py` primitives

### Retry-per-tier

Three retry shapes per [api-failure-handling.md](../api-failure-handling.md), bound to vendors via `data_sources.yaml.providers.<v>.retry_shape`:

| Shape | Attempts | Backoff | Failover |
|---|---|---|---|
| `critical` | multiple | exponential | enabled where a fallback is configured |
| `important` | limited | exponential | none |
| `optional` | single | brief | none |

The retry decorator wraps the SDK call. On exhausted retries, `track_run` writes a `failed` row to `collection_runs` and the function raises. The runner catches the exception and continues with other jobs.

### Rate limiting

Token-bucket limiter per provider, driven by `data_sources.yaml.providers.<v>.rate_limit_per_minute`. Every API call passes through it.

Combined with the runner's vendor-serialized executors (one job per vendor at a time, see [runner.md](runner.md)), this keeps collection inside vendor rate budgets without per-job tuning. Vendor-serialization handles "no two collectors fight at once"; the rate limiter handles "individual jobs don't burst."

### Run tracking

`track_run(collector)` is a context manager that wraps each top-level collection call:

```python
with track_run("polygon.equity") as run:
    rows = client.fetch(...)
    write_to_db(rows)
    run.rows_written = len(rows)
```

Behavior:

- On enter: insert a `collection_runs` row with `status='running'` and a UUID.
- On normal exit: update to `status='success'` with `completed_at` and `rows_written`.
- On exception: update to `status='failed'` with `error_summary`, then re-raise.

## Idempotency contract

Every collection function is idempotent — re-running on the same window produces no duplicates. Mechanisms:

- Time-series UPSERTs key on the natural composite key from [storage.md](storage.md) (e.g., `(ticker, timeframe, period_start)` for OHLCV, `(source, series_id, observation_date, revision_number)` for macro).
- Reference-table writes use INSERT...ON CONFLICT UPDATE on the appropriate unique column.
- Multi-row writes within a single function call are wrapped in a single transaction so partial failures don't leave half-written state.

Bootstrap is interrupt-safe by extension: re-running resumes at the next un-written row; partial completion is not rolled back.

## Failure semantics

When a collection function exhausts its retry policy:

1. The retry decorator raises the underlying SDK exception.
2. `track_run` catches, writes `error_summary` to `collection_runs`, re-raises.
3. The caller (runner or REPL) handles the exception. APScheduler isolates it and continues other jobs.

Distillation reads what is in the data tables; absence is interpreted by the fail-closed policy in [api-failure-handling.md](../api-failure-handling.md).

**Partial responses commit.** When a call returns rows for some scope items and fails on others (e.g., 50 of 70 tickers succeed in a Polygon batch), the successful rows are written within the function's transaction and the failed scope is recorded in `collection_runs.error_summary`. The next scheduled fire targets only the missing scope via the `since`-based catch-up mechanism.

## Multi-source failover

Deferred for v1. `data_sources.yaml` lists `primary` and `failover` providers per category; the `critical` retry shape carries `failover: true`. Initial collectors implement only the primary path. Failover dispatch lands inside per-vendor `client.py` modules when a provider's reliability proves it necessary — a per-collector code change, not a schema change.

## Future pipeline reuse

When the pipeline's data-layer phase comes online, it imports the same `collect_*` functions and calls them at invocation start with `since=None` (catch-up semantics) or a specific window. The pipeline brings its own scheduling — APScheduler `AsyncIOScheduler` per [infrastructure.md](../../../architecture/infrastructure.md) — but the collection contract (function shape, idempotency, retry, rate limits) is shared.

Splitting work between standalone collector and pipeline is a configuration question (which sources stay on the standalone schedule vs. move to invocation-driven), not architectural. See [lifecycle.md § Eventual pipeline integration](lifecycle.md#eventual-pipeline-integration).

---

*Cross-references:*

- [Storage](storage.md) — what the collection functions write.
- [Runner](runner.md) — how the standalone collector schedules these calls.
- [Lifecycle](lifecycle.md) — bootstrap, catch-up, and pipeline-handoff posture.
- [API failure handling](../api-failure-handling.md) — tier definitions and fail-closed posture.
- [Configuration management](../../configuration-management.md) — `data_sources.yaml`.
- [API key checklist](../api-key-checklist.md) — provider/account inventory.
