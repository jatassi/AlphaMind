# Runner

The process that schedules and dispatches calls into the [data sources library](data-sources.md). Wrapped by NSSM as `alphamind-collector` for unattended operation.

## Process model

A single Python process running `python -m alphamind.collector run`. Roughly fifty lines:

1. Load `.env` via `python-dotenv`.
2. Load `config/collector_schedule.yaml` (per-collector cron) and `config/data_sources.yaml` (per-provider rate limits and retry shapes).
3. Construct APScheduler `BlockingScheduler` with one `ThreadPoolExecutor(max_workers=1)` per vendor.
4. Register one job per `(vendor, domain)` pair, with the cron trigger from the schedule file and the executor matching the vendor.
5. Call `scheduler.start()`.

Anything beyond this lives in the shared library or storage layer.

## Vendor-serialized executors

```python
executors = {v: ThreadPoolExecutor(max_workers=1) for v in
             ["polygon", "fred", "eia", "bls", "treasury", "finnhub",
              "marketaux", "sec_edgar", "polymarket", "kalshi"]}
scheduler = BlockingScheduler(executors=executors)

scheduler.add_job(polygon.equity.collect_universe_bars,
                  trigger="cron", minute="*/15", hour="9-16", day_of_week="mon-fri",
                  executor="polygon", max_instances=1, id="polygon.equity")
scheduler.add_job(polygon.options.collect_chains,
                  trigger="cron", minute="0,30", hour="9-16", day_of_week="mon-fri",
                  executor="polygon", max_instances=1, id="polygon.options")
# ...
```

`max_workers=1` per executor serializes calls within a vendor — `polygon.equity` and `polygon.options` never run concurrently. Different vendors run in parallel.

`max_instances=1` per job prevents same-job overlap — if a long-running cycle is still in flight when the next fire arrives, APScheduler skips and logs.

Vendor-serialization handles "no two collectors fight at once"; the library's per-call rate limiter (see [data-sources.md § Rate limiting](data-sources.md#rate-limiting)) handles "individual jobs don't burst."

## Cadence

`config/collector_schedule.yaml` (parallel to `scheduler.yaml`):

| Collector | Cron | Notes |
|---|---|---|
| `polygon.equity` | `*/15 9-16 * * mon-fri`, `0 17-23,0-8 * * mon-fri` | 15 min market hours, 1 h off-hours |
| `polygon.options` | `0,30 9-16 * * mon-fri` | 30 min market hours |
| `polygon.corporate_actions` | `0 6 * * mon-fri` | Daily |
| `polygon.reference` | `0 6 * * sat` | Weekly |
| `fred.macro` | `0 */4 * * *` | 4 h |
| `eia.energy` | `0 */4 * * *` | 4 h |
| `bls.macro` | `0 9 * * *` | Daily |
| `treasury.auctions` | `0 17 * * mon-fri` | Daily late afternoon |
| `finnhub.news` | `*/30 * * * *` | 30 min |
| `finnhub.calendar` | `0 6 * * *` | Daily |
| `marketaux.news` | `*/30 * * * *` | 30 min |
| `sec_edgar.rss` | `*/15 * * * *` | 15 min |
| `polymarket` | `*/30 * * * *` | 30 min |
| `kalshi` | `*/30 * * * *` | 30 min |

Times are `US/Eastern` per `scheduler.yaml`.

This cadence accepts staleness against the pipeline's freshness targets (Q1 nominally <5 min, Q3 <30 min). The standalone collector runs the whole day at fixed cadence rather than burst-pulling at invocation time. When the pipeline's data-layer phase comes online, high-frequency collectors move under invocation-driven pulls and the runner narrows to slow-cadence sources — see [lifecycle.md § Eventual pipeline integration](lifecycle.md#eventual-pipeline-integration).

## Entry points

| Command | Behavior |
|---|---|
| `python -m alphamind.collector run` | Long-running scheduler. NSSM service target. |
| `python -m alphamind.collector bootstrap` | One-time historical backfill. See [lifecycle.md § Bootstrap](lifecycle.md#bootstrap). |
| `python -m alphamind.collector catch-up` | One-shot sweep: invokes every collection function with `since=None`, exits. |

`run` and `catch-up` both call into the same `data_sources` library functions; only the scope and `since` differ.

## Process supervision

NSSM-managed Windows Service `alphamind-collector`, configured per [infrastructure.md](../../../architecture/infrastructure.md):

- `AppExit Default Restart` with 60 s throttle.
- `Start Automatic (delayed)` so the service comes up after Windows boot-time work.
- `AppStdout` / `AppStderr` redirect to `%USERPROFILE%\AlphaMind\logs\collector.out.log` and `collector.err.log`.
- Service runs under the operator's user account so `%USERPROFILE%` resolves correctly and outbound network credentials are inherited.

During development, the runner runs in a PowerShell window via the same command — no NSSM needed.

## Logging

Python `logging` with `TimedRotatingFileHandler` (daily rotation, 30-day retention) writes to `%USERPROFILE%\AlphaMind\logs\collector.log`. Lines are tagged with the collector name (`polygon.equity`, `fred.macro`, etc.) for grep filtering.

Errors, scheduler events, and collection-run summaries are logged. Per-API-call detail lives in `collection_runs.error_summary` on failure. Raise level via `LOG_LEVEL=DEBUG`.

## Configuration files

| File | Owns |
|---|---|
| `config/data_sources.yaml` | Per-provider rate limits, retry shapes, secret env-var references. Read by the data sources library at startup. |
| `config/collector_schedule.yaml` | Per-collector cron expression. Read by the runner at startup. |
| `config/assets.yaml` | Trading universe + benchmark tickers. Read by the data sources library to scope collection. |
| `.env` | API keys and secrets. Loaded via `python-dotenv` at process start. |

Config changes require a runner restart — APScheduler doesn't reload mid-run. `nssm restart alphamind-collector` applies changes.

---

*Cross-references:*

- [Data sources library](data-sources.md) — the collection functions the runner dispatches.
- [Storage](storage.md) — the tables those functions write.
- [Lifecycle](lifecycle.md) — bootstrap, catch-up, and the pipeline-handoff posture.
- [Infrastructure](../../../architecture/infrastructure.md) — NSSM supervision, APScheduler choice rationale.
- [Configuration management](../../configuration-management.md) — full configuration surface.
