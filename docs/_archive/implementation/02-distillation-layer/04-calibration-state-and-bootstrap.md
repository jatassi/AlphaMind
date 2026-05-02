---
status: done
completed_date: 2026-04-28
commit_id: e7294809c7a03de1d2b08c0198bf76cc7eb326a1
---

# 04 — Calibration state and bootstrap fallback framework

## Goal

Implement the calibration-tagging primitives every per-category computation will use: a `CalibrationState` enum, a per-output tagging contract, and the cross-sectional pooled-prior fallback functions that substitute for per-ticker / per-pair / per-contract baselines when those baselines are below their `*_min_observations` thresholds.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — three-state tag definition, cross-sectional fallback table, downstream propagation, warm-up duration estimate
- `docs/design/02-distillation-layer/threshold-calibration.md` § Calibration classes — Class A/B/C definitions; the framework here is what makes Class B rolling state safely consumable in `bootstrap` mode
- `docs/design/01-data-layer/api-failure-handling.md` § Design principle — fail-closed semantics the bootstrap path explicitly bends (produce-with-tag, not abort) and why
- `docs/design/01-data-layer/collector/storage.md` § Tables — `asset_universe`, `sector_classification` (cross-sectional pools key off `alphamind_sector`)
- Story 02's config schema — the `*_min_observations` thresholds drive the calibration decision
- Story 03's state schema — the `calibration_state` column on baseline tables is what this story populates

## Depends on

- 02 (config schema — `sentiment_min_observations`, `gap_fill_min_events`, `extended_hours_min_events` and the corresponding `_baseline_days` are read here)
- 03 (state schema — every fallback returns a value paired with the tag that the persistence layer stores)

## Scope

In scope: under `src/alphamind/distillation/` —

- A new module (e.g., `calibration.py`) defining:
  - `class CalibrationState(StrEnum)` with members `CALIBRATED`, `BOOTSTRAP`, `UNAVAILABLE`. Values match the strings in the state-table CHECK constraints from story 03.
  - `@dataclass(frozen=True) class CalibratedValue` — a small wrapper carrying `value`, `state: CalibrationState`, `bootstrap_reason: str | None` (None when state is `CALIBRATED`; populated otherwise with a short string naming the missing input — e.g., `"per_ticker_observations_below_minimum: 12 < 30"`).
- A `decide_calibration_state(observed_n, required_n)` helper returning `CALIBRATED` when `observed_n >= required_n`, `BOOTSTRAP` otherwise. Returns `UNAVAILABLE` only when callers explicitly invoke a separate `mark_unavailable(reason)` constructor — the framework does not infer `UNAVAILABLE` from low counts.
- A `cross_sectional_fallback` module (or sibling functions inside `calibration.py`) implementing each row of the `Cross-sectional priors` table from `threshold-calibration.md § Bootstrap policy`:
  - `sector_pooled_volume_baseline(sector, as_of)` — returns mean/stdev computed across all calibrated per-ticker volume baselines whose tickers share the sector.
  - `sector_pooled_atr_baseline(sector, as_of)` — same shape, ATR.
  - `universe_pooled_sentiment_distribution(as_of)` — universe-wide pooled distribution for percentile lookup.
  - `sector_pooled_gap_fill_rate(sector, as_of)` — sector-pooled rate; refreshed monthly per the spec, but the function itself is stateless (caller decides whether to recompute or read a cached aggregate).
  - `universe_pooled_extended_hours_confirmation_rate(as_of)` — universe-wide; cold-start default 50% per the spec.
  - `default_lead_lag_pair_estimate(pair_key)` — returns the Class A `_max_days` bound for the named pair when fewer than 10 observed events exist; the prior itself.
  - `prediction_market_delta_default(contract_id)` — returns the universe-wide 5pp threshold; no per-contract bootstrap.
- A `tag_with_fallback(observed, required, computed_value, fallback_callable)` higher-order helper that:
  1. Decides the state via `decide_calibration_state`.
  2. If `CALIBRATED`: returns `CalibratedValue(computed_value, CALIBRATED, None)`.
  3. If `BOOTSTRAP`: invokes `fallback_callable()`; if it returns a value, returns `CalibratedValue(fallback_value, BOOTSTRAP, reason)`; if it returns `None` (cross-sectional pool also empty — pre-bootstrap deployment), returns `CalibratedValue(None, UNAVAILABLE, reason_extended_with_pool_empty_note)`.
- Unit tests:
  - Each fallback function returns a sensible value against a fixture database with fully-populated state.
  - Each fallback function returns the documented prior (or None for `UNAVAILABLE`) when the relevant pool is empty.
  - `tag_with_fallback` produces the three states correctly across observed-vs-required boundaries (`n=29` vs. `n=30` for `sentiment_min_observations`).
  - `bootstrap_reason` strings include both the missing input identifier and the count comparison.

Out of scope:
- Class B state refresh logic itself (story 07).
- Per-output envelope wrapping (story 05) — this story produces the value+state pair; story 05 wraps it inside an `OutputBlock`.
- Threshold consumers — anomaly detection, regime classification (stories 08*, 09).
- Operator monthly refresh of sector-pooled gap-fill / extended-hours pools (operational; the function is stateless on call).

## Notes

The three `calibration_state` strings in the enum must equal the strings the schema CHECK constraint accepts. Centralize them in the enum module and have `models.py` import from there if it makes the schema more obviously correct — but do not let the enum import anything from `models.py` (the framework is upstream of the schema in dependency order).

The `bootstrap_reason` field is critical for the downstream-propagation contract. Domain researchers reading the calibration tag must be able to surface a meaningful explanation in their `Signal quality: ... DEGRADED` line. Keep the string compact and machine-readable: `"<missing_input_name>: <observed> < <required>"` works well; avoid free-form prose.

Cross-sectional pools that can themselves be empty (pre-bootstrap deployment) must not silently fall through to a magic default — return `None` explicitly. The `tag_with_fallback` helper is the only place `UNAVAILABLE` is produced; downstream consumers see the missing block rather than a misleading zero.

Sector membership comes from `sector_classification.alphamind_sector` per [storage.md](../../../design/01-data-layer/collector/storage.md). The fallback functions take the SQLAlchemy session as an argument; do not capture a global session.

The cold-start 50% extended-hours confirmation rate is documented in `threshold-calibration.md` as "no information." Implement it as a named constant `EXTENDED_HOURS_BOOTSTRAP_RATE = 0.5` in the fallback module, not a magic number.

## Acceptance criteria

- [ ] `CalibrationState` enum exists with exactly three members; values equal the schema CHECK constraint strings from story 03.
- [ ] `CalibratedValue` dataclass is frozen, carries `value` / `state` / `bootstrap_reason`.
- [ ] `decide_calibration_state(observed_n, required_n)` returns `CALIBRATED` iff `observed_n >= required_n`, else `BOOTSTRAP`. `UNAVAILABLE` is only produced via `tag_with_fallback` when the fallback returns `None`, never inferred from low counts.
- [ ] All seven cross-sectional fallback functions exist and return the documented shape.
- [ ] Each fallback function returns the documented value against a fixture DB with full state.
- [ ] Each fallback function returns `None` (or the documented prior) when the relevant pool is empty.
- [ ] `tag_with_fallback` correctly produces all three states across observed-vs-required boundaries.
- [ ] Boundary unit tests cover `n == required - 1` (BOOTSTRAP) and `n == required` (CALIBRATED).
- [ ] `bootstrap_reason` is `None` when state is `CALIBRATED`; otherwise carries the documented `"<input>: <observed> < <required>"` shape.
- [ ] `EXTENDED_HOURS_BOOTSTRAP_RATE = 0.5` is a named constant, referenced by the cold-start path.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
