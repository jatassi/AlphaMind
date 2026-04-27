# Data layer collection

The data layer's only writer until the pipeline's data-layer phase comes online. The `data_sources` shared library — used by the standalone collector now and by the future pipeline — owns vendor adapters and idempotent collection functions. The collector is the thin scheduler that dispatches them.

## Documents

- [Storage](storage.md) — schema spec, retention, cross-cutting rules.
- [Data sources library](data-sources.md) — vendor adapters, retry-per-tier, rate limiting, idempotency contract.
- [Runner](runner.md) — the scheduler process; cadence, supervision, entry points.
- [Lifecycle](lifecycle.md) — bootstrap, catch-up, eventual pipeline integration.

## Principles

**Forward-only collection.** Steady-state writes new rows; no bulk historical re-fetch. Targeted single-record revisions are acceptable. Bootstrap is the one-time exception — backfill so distillation has baselines.

**Shared library, thin runner.** Vendor clients and collection functions live in `alphamind.data_sources`. The collector imports and dispatches them; the future pipeline imports the same library.

**Vendor-serialized scheduling.** APScheduler executors are one per vendor with `max_workers=1` so calls within a vendor share the rate budget; vendors run in parallel. A token-bucket rate limiter paces individual API calls.

**Idempotent functions.** Every collection function UPSERTs against natural composite keys. Bootstrap, catch-up, and steady-state share the same code path with different `since` parameters.

## Relationship to other layers

| Layer | Relationship |
|---|---|
| Distillation ([../../02-distillation-layer/README.md](../../02-distillation-layer/README.md)) | Reads the same SQLite tables; computes rolling baselines and anomaly flags. Calibration-state tagging (`calibrated` / `bootstrap` / `unavailable`) is downstream. |
| Internal data ([../internal/README.md](../internal/README.md)) | Coexists in `alphamind.db`; portfolio state and market data tables are disjoint. |
| External data domains ([../external/quantitative.md](../external/quantitative.md), [../external/qualitative.md](../external/qualitative.md)) | The "what" of each signal. This tree is the "how" — how each category lands in storage. |
| API failure handling ([../api-failure-handling.md](../api-failure-handling.md)) | Tier-based retry semantics at the collection-function level. |
| Configuration ([../../configuration-management.md](../../configuration-management.md)) | `data_sources.yaml`, `assets.yaml`, `collector_schedule.yaml`, `.env`. |
| Infrastructure ([../../../architecture/infrastructure.md](../../../architecture/infrastructure.md)) | NSSM Windows Service supervision pattern. |
