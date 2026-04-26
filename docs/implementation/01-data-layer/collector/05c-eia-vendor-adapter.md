---
status: not_started
completed_date:
commit_id:
---

# 05c — EIA vendor adapter

## Goal

Implement `src/alphamind/data_sources/eia/` to pull energy series (crude inventory, natural gas storage, WTI spot, refinery utilization) from the EIA API into `macro_observations`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `macro_observations` schema
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — 252-day depth
- `docs/design/01-data-layer/api-key-checklist.md` § EIA — endpoint inventory
- `docs/design/01-data-layer/api-failure-handling.md` — Q6 (incl. Q8 commodities via this adapter) is critical

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/eia/` —
- `client.py` — HTTP wrapper around the EIA v2 API (no official SDK; use `httpx`). Integrates `with_retries(critical)`, `RateLimiter`, `verify_connectivity()`.
- `energy.py`:
  - `collect_series(series_paths, since)` and `bootstrap_series()`. EIA's API uses path-based facet queries rather than series IDs; each "series" in our terms is a `(route, facets)` tuple that uniquely selects one timeseries.
  - Writes `macro_observations` keyed on a synthesized `series_id` (e.g., `eia.crude_inventory_total`, `eia.nat_gas_storage_lower_48`) so all rows live in one table.
- A configurable series list at `src/alphamind/data_sources/eia/series.py` covering: weekly crude inventory (`/v2/petroleum/stoc/wstk/`, facet `product=EPC0`), weekly natural gas storage (`/v2/natural-gas/stor/wkly/`), daily WTI spot (`/v2/petroleum/pri/spt/`, facet `series=RWTC`), weekly refinery utilization (`/v2/petroleum/pnp/wiup/`).
- Unit tests with mocked HTTP responses.

Out of scope:
- Futures curves and CFTC positioning (Q8 specialized — deferred per `storage.md`).
- FRED-sourced energy series (e.g., `DCOILWTICO` if the operator chooses FRED's WTI series instead — story 05b).

## Notes

EIA v2 API endpoint shape:

```
GET /v2/{route}/data/?api_key={k}&frequency=weekly&data[0]=value&facets[product][]=EPC0&sort[0][column]=period&sort[0][direction]=desc&length=10
```

`series_id` synthesis: pick a stable convention like `<route_short>.<facet_summary>` and document it in `series.py` as a comment so the synthesized IDs don't drift.

Frequency is explicit in the EIA query (`frequency=weekly` etc.) — write it directly to `macro_observations.frequency`.

Units come from the EIA response's `units` field per data point; map to short forms similar to FRED.

Rate limit per documentation: 5000 req/hour ≈ 83/min. Set `data_sources.yaml.providers.eia.rate_limit_per_minute = 80` (conservative).

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API key works.
- [ ] `client.py` integrates `with_retries(critical)`, `RateLimiter`, and `track_run`.
- [ ] `energy.collect_series()` writes `macro_observations` rows for each configured `(route, facets)` tuple, with synthesized `series_id`.
- [ ] `energy.bootstrap_series()` covers 252 days of history per series.
- [ ] `macro_observations.frequency` and `macro_observations.units` are populated correctly.
- [ ] Re-running on the same window produces no duplicate rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
