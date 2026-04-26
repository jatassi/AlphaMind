---
status: done
completed_date: 2026-04-26
commit_id: ca14a17
---

# 05d — BLS vendor adapter

## Goal

Implement `src/alphamind/data_sources/bls/` to pull employment and inflation series (NFP, unemployment rate, CPI components) from the BLS v2 API into `macro_observations`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `macro_observations` schema
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — 24-month depth for monthly series
- `docs/design/01-data-layer/api-key-checklist.md` § BLS — endpoint inventory and series IDs
- `docs/design/01-data-layer/api-failure-handling.md` — Q6 macro is critical

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/bls/` —
- `client.py` — HTTP wrapper around BLS v2 API (POST-based; no official SDK; use `httpx`). Integrates `with_retries(critical)`, `RateLimiter`, `verify_connectivity()`.
- `macro.py`:
  - `collect_series(series_ids, since)` and `bootstrap_series()`. POST to `/publicAPI/v2/timeseries/data/` with `{seriesid: [...], startyear, endyear, registrationkey}`.
  - Writes `macro_observations` with `source='bls'`, `series_id` matching BLS's series ID format (e.g., `CES0000000001` for total nonfarm payrolls).
- A configurable series list at `src/alphamind/data_sources/bls/series.py` covering total nonfarm payrolls (`CES0000000001`), unemployment rate (`LNS14000000`), CPI-U detailed (`CUSR0000SA0`), and any other BLS series referenced in `api-key-checklist.md`.
- Unit tests with mocked HTTP responses.

Out of scope:
- FRED-sourced BLS series (FRED hosts many BLS series; if a series is also on FRED, prefer FRED in story 05b for simplicity — this story handles BLS-only series like detailed CPI components).

## Notes

BLS v2 API allows up to 50 series IDs per request (with API key) or 25 (without). Batch requests accordingly.

BLS API rate limit per documentation: 500 queries/day with key. Set `data_sources.yaml.providers.bls.rate_limit_per_minute` conservatively (e.g., 10/min).

Observation date construction: BLS returns `year` + `period` (e.g., `M01` for January). Convert to ISO date as the first of the month for monthly series.

Revision detection: BLS revises NFP and other series. Apply the same revision-detection approach as story 05b (compare to most recent stored row; insert new row with `revision_number+1` on value change).

Frequency: hardcode per series in the configurable list — BLS doesn't return frequency metadata in the response.

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API key works.
- [ ] `client.py` integrates `with_retries(critical)`, `RateLimiter`, and `track_run`.
- [ ] `macro.collect_series()` writes `macro_observations` rows for the configured series.
- [ ] `macro.bootstrap_series()` pulls 24 months of history per series.
- [ ] Revisions are stored as new rows with incremented `revision_number`.
- [ ] Re-running on the same window produces no duplicate rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
