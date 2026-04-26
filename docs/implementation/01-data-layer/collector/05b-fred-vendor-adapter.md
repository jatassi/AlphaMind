---
status: not_started
completed_date:
commit_id:
---

# 05b — FRED vendor adapter

## Goal

Implement `src/alphamind/data_sources/fred/` to pull macro time series from FRED into `macro_observations`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `macro_observations` schema
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — depths (90d daily, 24m monthly)
- `docs/design/01-data-layer/api-key-checklist.md` § FRED — endpoint inventory and series IDs
- `docs/design/01-data-layer/schema/macro.py` — field-level expectations downstream
- `docs/design/01-data-layer/api-failure-handling.md` — Q6 macro is critical

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/fred/` —
- `client.py` — wraps `fredapi` SDK with `with_retries(critical)`, `RateLimiter` (120 req/min default), `verify_connectivity()`.
- `macro.py`:
  - `collect_series(series_ids, since)` and `bootstrap_series()` —  pulls FRED observations for the configured series list. Writes `macro_observations` with one row per `(series_id, observation_date, revision_number)`. Detect revisions by comparing the latest pull to the most recent stored row for the same `(series_id, observation_date)`; insert a new row with `revision_number+1` when values differ.
- A configurable series list at `src/alphamind/data_sources/fred/series.py` (or a YAML loaded from config) listing daily series (90d backfill) and monthly series (24m backfill) separately. Coverage: every FRED series in `api-key-checklist.md` § FRED + breakeven inflation (`T5YIE`, `T10YIE`), credit spreads (`BAMLH0A0HYM2`, `BAMLC0A0CM`), funding (`SOFR`, `RRPONTSYD`), MMF flows.
- Unit tests with mocked SDK responses.

Out of scope:
- BLS- or EIA-sourced series (their own stories: 05c, 05d).
- Treasury auction data (story 05e).
- Series interpretation / regime classification (distillation layer's job).

## Notes

Series frequency varies. FRED returns daily, weekly, monthly, etc. Set `macro_observations.frequency` from the FRED series metadata (`fredapi.Fred.get_series_info(series_id).frequency_short`).

`macro_observations.units` — set per series. FRED's metadata gives a verbose units string; use a small mapping table to normalize to the storage schema's short forms (`pct`, `bp`, `index`, `usd`, `bbl`, etc.).

`release_date` is distinct from `observation_date`. FRED's series_observations endpoint returns observation values keyed by date; the release date for monthly series is reported via `release_dates` endpoint or via series metadata. For the daily series (yields, breakevens, etc.), `release_date` equals `observation_date` (or null — treat as null when not distinct).

Revision detection: pull `realtime_start` and `realtime_end` from the FRED API's vintage support. A revision is when `realtime_end` for an observation has changed relative to the last pull. Implement carefully — most series don't revise; only national-accounts data (NFP, GDP, CPI) revises systematically.

Rate limit per API documentation: 120 req/min. Set `data_sources.yaml.providers.fred.rate_limit_per_minute = 120` (story 03a).

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API key works.
- [ ] `client.py` integrates `with_retries(critical)`, `RateLimiter`, and `track_run`.
- [ ] `macro.collect_series()` writes `macro_observations` rows for the configured series list.
- [ ] `macro.bootstrap_series()` pulls 90 days for daily series and 24 months for monthly series.
- [ ] When a series' value at an observation_date differs from the most recent stored row, a new row with incremented `revision_number` is inserted.
- [ ] `macro_observations.frequency` is populated from FRED metadata.
- [ ] `macro_observations.units` is populated from the normalized units mapping.
- [ ] Re-running on the same window produces no duplicate rows (idempotency on `(source, series_id, observation_date, revision_number)`).
- [ ] On full failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked SDK responses cover the above.
- [ ] `uv run pytest` passes.
