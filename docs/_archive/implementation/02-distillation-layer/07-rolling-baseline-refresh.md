---
status: done
completed_date: 2026-04-28
commit_id: 89e3edbae8eb3d079302c4f6a3ce93e4ef19f0c1
---

# 07 — Rolling baseline refresh primitive

## Goal

Implement the Class B refresh primitive every per-category baseline computation will call. The primitive owns the read-current-state → append-newest-point → drop-oldest → write-incrementally pattern that keeps refresh cost bounded regardless of run history, and the fail-closed semantics: an unrecoverable refresh aborts the invocation rather than letting downstream anomaly detection run on a stale denominator.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — Class B refresh policy: incremental, fail-closed
- `docs/design/02-distillation-layer/threshold-calibration.md` § Where each threshold lives — the six state tables this primitive writes to
- `docs/design/01-data-layer/api-failure-handling.md` — fail-closed principle the refresh enforces
- `docs/design/mid-pipeline-failure-handling.md` — no-checkpoint, no-resume policy that justifies "abort, wait for next trigger" rather than partial-state cleanup
- Stories 03, 04 — state schema and calibration framework the refresh produces values for

## Depends on

- 03 (state schema)
- 04 (calibration framework — the refresh emits `CalibratedValue` for each baseline it updates)

## Scope

In scope: under `src/alphamind/distillation/baselines.py` —

- `class RollingBaselineRefresh` — encapsulates the refresh of one logical baseline kind. Constructor takes the SQLAlchemy session, the kind identifier (`"volume" | "atr" | "spread" | "sentiment"` for ticker baselines; analogous for pair / contract / event tables), and the target window in days.
- A unified entry point per state-table family:
  - `refresh_ticker_baselines(session, kind, ticker_scope, as_of)` — for `distillation_ticker_baseline`. Per ticker:
    1. Load existing baseline row(s) for `(ticker, kind)` ordered by `as_of`.
    2. Read the newest underlying observation from the source table (`ohlcv_bars` for volume/ATR/spread; the sentiment-distribution source per [external.md § 3 News–price divergence](../../../design/02-distillation-layer/external.md#3-anomaly-detection) for sentiment) covering the window `[as_of - window_days, as_of]`.
    3. Compute the new mean / stdev / observation count.
    4. Compute `calibration_state` via the framework from story 04 against the relevant `*_min_observations` threshold.
    5. UPSERT a new row keyed `(ticker, kind, as_of)` with the new values; do NOT delete prior rows (history is forward-only — pruning is an operational concern owned by retention).
    6. Return a `CalibratedValue` for the caller to use immediately if it wants to read its own write.
- `refresh_pair_lag(session, pair_scope, as_of)` — analogous for `distillation_pair_lag`. Pair scope is the cartesian-product of the named lead-lag pairs from `threshold-calibration.md § Lead-lag and narrative-lag`.
- `refresh_contract_history(session, contract_scope, as_of)` — analogous for `distillation_contract_history`. Pulls newest probability snapshot from `prediction_market_snapshots`, computes `delta_pp_since_prior` from the immediately preceding row, writes new row.
- `refresh_event_history(session, event_kind, ticker_scope, as_of)` — analogous for `distillation_event_history`. Two distinct call patterns: (a) detect new events since the last refresh (gap appears, extended-hours move appears) and append rows with `outcome = NULL`; (b) update prior rows whose outcomes have now resolved (gap-fill confirmed at the next session open, extended-hours direction confirmed/reversed in the first 30 min of regular trading) by setting `outcome` and `outcome_observed_at`.
- `refresh_composite_state(session, composite_kind, as_of)` — analogous for `distillation_composite_state`. Recomputes the `funding_stress` composite from its 4 component series (per [external.md § 2 macro](../../../design/02-distillation-layer/external.md#2-technical-indicators-and-derived-metrics)) or the `market_liquidity` composite from its inputs (per [external.md § 2 cross-asset](../../../design/02-distillation-layer/external.md#2-technical-indicators-and-derived-metrics)); appends a new row with the value, the per-component breakdown JSON, the percentile against the 60-day trailing distribution, and an `alert_active` flag derived from the relevant Class A thresholds.
- All refresh entry points wrap their work in a single SQLAlchemy transaction. On any uncaught exception the transaction is rolled back and the exception propagates up to the orchestrator (story 12), where it triggers the fail-closed pipeline abort per `mid-pipeline-failure-handling.md`.
- Unit tests:
  - Per refresh entry point: a happy-path test against a fixture DB seeded with enough underlying data to produce a `calibrated` baseline.
  - Per refresh entry point: a bootstrap-path test where insufficient observations exist; verify the `calibration_state` is `bootstrap` and the row is still written.
  - Re-running any refresh on the same `(scope, as_of)` produces no duplicate primary-key violations — the row UPSERTs (the test asserts the post-row count equals the pre-row count plus exactly the expected new rows, not double).
  - A fault-injection test where the underlying table query raises mid-refresh: verify the transaction rolls back (no partial writes) and the exception propagates.
  - `refresh_event_history` outcome-resolution test: insert a gap-event row with `outcome = NULL`, advance fixture clock past the resolution window, re-run refresh, verify the row's `outcome` and `outcome_observed_at` are populated and no new event row is duplicated.

Out of scope:
- Per-category indicator computations downstream of the baselines (stories 08*).
- Persistence of the regime label history (covered by story 09 and uses `distillation_regime_state` directly; that's its own read-modify-write pattern, not generic baseline refresh).
- Operational retention pruning of the state tables.
- Bootstrap orchestration across the universe — the refresh is per-call; the orchestrator (story 12) iterates `ticker_scope` against `assets.yaml`'s sectors block.

## Notes

The refresh primitive is the canonical place where the "Refresh at the start of each invocation's distillation phase, before anomaly detection runs" policy from `threshold-calibration.md` is enforced. Story 12's orchestrator calls these entry points in dependency order; this story owns the per-baseline mechanics.

Incremental computation matters at scale: at 65 universe tickers × 4 baseline kinds × 252-day max window, the naive recompute-from-scratch path is O(N × W) per invocation. Use the running-mean / running-stdev (Welford's algorithm) update so refresh is O(1) per ticker per kind regardless of window. Tests should cover both the incremental update and a full-recompute fallback when the prior baseline row is missing (first-time deployment).

`distillation_event_history` is the trickiest table because outcome resolution is asynchronous to event detection — a gap detected at 9:30 AM doesn't have its `filled` outcome until the next session at the earliest. Keep the two refresh modes (event detection vs. outcome resolution) in separate code paths within `refresh_event_history` so the invariants are obvious.

Do NOT leak SQLAlchemy ORM objects out of the refresh functions — return plain `CalibratedValue` instances (or, for the multi-row entry points, dicts of them keyed by ticker / pair / contract). The orchestrator and per-category consumers should not depend on the persistence layer's classes.

The "no checkpoint, no resume" policy means a refresh failure aborts the invocation entirely. There is no partial-refresh recovery path. The transaction wrapping is what makes this safe — partial writes either commit atomically or roll back atomically.

## Acceptance criteria

- [ ] `refresh_ticker_baselines` correctly computes mean / stdev / observation count for each of the four kinds against fixture data.
- [ ] `refresh_pair_lag` updates lead-lag estimates and event counts for the named pairs.
- [ ] `refresh_contract_history` writes a new row per contract with `delta_pp_since_prior` derived from the previous row.
- [ ] `refresh_event_history` distinguishes event-detection from outcome-resolution and applies each correctly.
- [ ] `refresh_composite_state` recomputes the funding-stress and market-liquidity composites with the per-component breakdown JSON populated.
- [ ] Each refresh entry point returns a `CalibratedValue` (or dict thereof) carrying the correct calibration state per the framework from story 04.
- [ ] Re-running any refresh on the same `(scope, as_of)` does not duplicate rows.
- [ ] Fault injection test: a query failure during refresh rolls back the transaction and propagates the exception.
- [ ] Welford / running-mean update path is exercised under a test that asserts O(1) incremental cost (verifiable by counting underlying rows scanned, not by wall-clock).
- [ ] Bootstrap-path test produces a row with `calibration_state = 'bootstrap'`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
