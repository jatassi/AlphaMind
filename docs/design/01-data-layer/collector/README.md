# Data layer collection

The data layer's only writer until the pipeline's data-layer phase comes online. The `data_sources` shared library — used both by the standalone collector now and by the future pipeline — owns vendor adapters and idempotent collection functions. The collector is the thin process that schedules and dispatches them.

## Documents

- [Storage](storage.md) — schema spec, retention, cross-cutting rules.
- [Data sources library](data-sources.md) — vendor adapters, retry-per-tier, rate limiting, idempotency contract.
- [Runner](runner.md) — the thin scheduler process; cadence, supervision, entry points.
- [Lifecycle](lifecycle.md) — bootstrap, catch-up, eventual pipeline integration.

## Principles

**Forward-only collection.** Steady-state collection writes new rows; it does not re-fetch historical data in bulk. Targeted single-record revisions are acceptable. The one exception is the initial bootstrap — a one-time backfill on first deploy so distillation has baselines to compute against.

**Shared library, thin runner.** Vendor clients and per-domain collection functions live in `alphamind.data_sources`. The collector is a thin scheduler that imports and dispatches them. The future pipeline will import the same library when its data-layer phase fires.

**Vendor-serialized scheduling.** APScheduler executors are configured one per vendor with `max_workers=1` so calls within a vendor share the rate budget; vendors run in parallel. A token-bucket rate limiter inside the shared library paces individual API calls within each call.

**Idempotent functions.** Every collection function UPSERTs against natural composite keys. Bootstrap, catch-up, and steady-state share the same code path with different `since` parameters.

## Relationship to other layers

| Layer | Relationship |
|---|---|
| Distillation ([../../02-distillation-layer/README.md](../../02-distillation-layer/README.md)) | Reads from the same SQLite tables; computes rolling baselines and anomaly flags. Calibration-state tagging (`calibrated` / `bootstrap` / `unavailable`) is downstream concern. |
| Internal data ([../internal/README.md](../internal/README.md)) | Coexists in the same `alphamind.db`; portfolio state and market data tables are disjoint. |
| External data domains ([../external/quantitative.md](../external/quantitative.md), [../external/qualitative.md](../external/qualitative.md)) | The "what" of each signal. This tree is the "how" — how each external category lands in storage. |
| API failure handling ([../api-failure-handling.md](../api-failure-handling.md)) | Tier-based retry semantics applied at the collection-function level. |
| Configuration ([../../configuration-management.md](../../configuration-management.md)) | `data_sources.yaml`, `assets.yaml`, `collector_schedule.yaml`, `.env`. |
| Infrastructure ([../../../architecture/infrastructure.md](../../../architecture/infrastructure.md)) | NSSM Windows Service supervision pattern. |
