---
status: in_progress
completed_date:
commit_id:
---

# 03 — Distillation state persistence schema

## Goal

Add SQLAlchemy 2.0 declarative models and an Alembic migration for the six distillation-state tables named in the threshold-calibration spec. These tables hold the Class B rolling state every per-category computation reads and writes each invocation.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Where each threshold lives — authoritative table naming all six state tables and the per-table content one-liner
- `docs/design/02-distillation-layer/external.md` § 4 Persistent state and composites — what each table's content is for (rolling baselines, composites, prediction market state, regime classification)
- `docs/design/01-data-layer/collector/storage.md` § Cross-cutting rules — timestamp conventions, primary-key style, FK style, `source` / `ingested_at` provenance
- `docs/architecture/data-and-state.md` § 4 Distillation state (rolling, persistent) — high-level shape of the four distillation-state table groups
- `src/alphamind/persistence/models.py` and `src/alphamind/persistence/migrations/versions/` — existing SQLAlchemy + Alembic scaffolding the new models extend

## Depends on

None — `persistence/` and `migrations/` are landed.

## Scope

In scope: under `src/alphamind/persistence/models.py` (or a sibling file the operator splits to keep diffs reviewable) —

Six SQLAlchemy 2.0 declarative models, one per table:

- **`distillation_ticker_baseline`** — per-ticker rolling state for the volume / ATR / spread / sentiment-distribution baselines. Composite key `(ticker, baseline_kind, as_of)` where `baseline_kind` ∈ {`volume`, `atr`, `spread`, `sentiment`}; columns `mean`, `stdev`, `n_observations`, `window_days`, `calibration_state`, `ingested_at`. FK on `ticker` to `asset_universe.ticker`. Index `(ticker, baseline_kind, as_of DESC)`.
- **`distillation_pair_lag`** — per-pair lead-lag timing estimate. Composite key `(lead_ticker, lag_ticker, as_of)`; columns `lead_lag_days_estimate`, `n_pair_events`, `last_overdue_flag`, `calibration_state`, `ingested_at`. FK on both ticker columns. Index `(lead_ticker, lag_ticker, as_of DESC)`.
- **`distillation_contract_history`** — per-contract prediction-market trailing probability series. Composite key `(contract_id, snapshot_ts)`; columns `yes_probability`, `delta_pp_since_prior`, `liquidity_usd`, `calibration_state`, `ingested_at`. FK on `contract_id` to `prediction_market_contracts.contract_id`. Index `(contract_id, snapshot_ts DESC)`.
- **`distillation_event_history`** — per-ticker gap-event and extended-hours-event records with outcomes (used for gap-fill and extended-hours confirmation rate baselines). Composite key `(ticker, event_kind, event_ts)` where `event_kind` ∈ {`gap`, `extended_hours`}; columns `direction`, `magnitude_atr_multiple`, `outcome` (e.g., `filled`/`unfilled`/`confirmed`/`reversed`), `outcome_observed_at` (nullable until outcome lands), `ingested_at`. FK on `ticker`. Index `(ticker, event_kind, event_ts DESC)`.
- **`distillation_regime_state`** — current and historical volatility regime label with the supporting indicator state. One row per invocation appended forward-only. Primary key `as_of` (UTC ISO datetime); columns `regime_label` ∈ {`low_vol_compression`, `vol_expansion`, `crisis_spike`, `vol_normalization`} (mapped to the four-tier ladder per [external.md §4](../../../design/02-distillation-layer/external.md#4-persistent-state-and-composites)), `vix_level`, `term_structure_basis`, `vvix_percentile`, `realized_vol`, `indicator_agreement_count`, `invocations_held`, `transition_state` ∈ {`stable`, `early-weak`, `early-strong`, `confirmed`}, `prior_label`, `ingested_at`. Index `(as_of DESC)`.
- **`distillation_composite_state`** — funding-stress and market-liquidity composite trailing distributions. Composite key `(composite_kind, as_of)` where `composite_kind` ∈ {`funding_stress`, `market_liquidity`}; columns `composite_value`, `component_breakdown_json` (TEXT containing the per-component values that fed the composite, for audit), `percentile_60d`, `alert_active`, `calibration_state`, `ingested_at`. Index `(composite_kind, as_of DESC)`.

Plus:

- One Alembic migration adding all six tables, every column, every index, every FK, every NOT NULL.
- Unit tests:
  - Round-trip insert + select against an in-memory SQLite database for every model.
  - Composite-key uniqueness violations raise `IntegrityError` for at least one of each multi-column key.
  - FK violations raise `IntegrityError` on `ticker` (insert against a non-existent universe row).
  - `alembic upgrade head` against an empty DB creates every table with the expected columns; `alembic downgrade base` reverses cleanly.

Out of scope:
- Refresh logic (story 07).
- The `calibration_state` enum implementation in Python (story 04).
- Code that populates these tables (stories 08*, 09).
- Retention pruning of historical regime / composite rows (operational concern).

## Notes

`calibration_state` is a TEXT column with a CHECK constraint enumerating `calibrated` / `bootstrap` / `unavailable` rather than a SQLAlchemy `Enum` type — keeps the schema portable and matches the existing `session` column convention in `ohlcv_bars`.

`as_of` columns are ISO 8601 UTC TEXT per [storage.md § Cross-cutting rules](../../../design/01-data-layer/collector/storage.md#cross-cutting-rules). Timezone conversion happens at presentation, never in storage.

`distillation_regime_state` is forward-only (append per invocation). The other tables are read-modify-write per Class B refresh: collection_runs-style append-yesterday-drop-oldest is the access pattern owned by story 07.

`distillation_event_history.outcome_observed_at` is nullable because gap-fill and extended-hours confirmation outcomes resolve hours after the event row is first written. The refresh story populates it; the schema just needs to allow null.

`distillation_composite_state.component_breakdown_json` is TEXT (JSON serialized) rather than separate columns because the component count differs per composite (4 for funding stress, 1 plus a few sub-metrics for market liquidity) and the breakdown is for audit, not query. Use `sqlalchemy.JSON` if the team wants typed access at the ORM layer; otherwise plain TEXT is fine.

Match column-name-to-attribute exactly with the names listed above — downstream readers in stories 04, 07, 08*, 09 reference these names directly.

For the FK to `prediction_market_contracts.contract_id`, the collector's persistence story (03b) already created that table; the FK target exists.

## Acceptance criteria

- [ ] Six SQLAlchemy 2.0 declarative models exist with the columns, types, nullability, primary keys, foreign keys, indexes, and CHECK constraints listed above.
- [ ] An Alembic migration creates every table on `alembic upgrade head` against an empty database.
- [ ] `alembic downgrade base` reverses cleanly.
- [ ] Unit tests round-trip every model.
- [ ] Composite-key violations raise `IntegrityError` for at least one of each table's multi-column key.
- [ ] Foreign-key violations on the `ticker` columns raise `IntegrityError`.
- [ ] `calibration_state` CHECK constraint rejects values outside `calibrated` / `bootstrap` / `unavailable`.
- [ ] `transition_state` CHECK constraint on `distillation_regime_state` rejects values outside `stable` / `early-weak` / `early-strong` / `confirmed`.
- [ ] `regime_label` CHECK constraint rejects values outside the four-label set.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
