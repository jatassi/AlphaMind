On a freshly-migrated DB (no prior `distillation_ticker_baseline` rows), the first orchestrator invocation tags every per-ticker baseline as `bootstrap` because `decide_calibration_state(observed_n, required_n)` only returns `CALIBRATED` when `observed_n >= required_n`. The verifier's high-freq band `[80%, _]` for `volume`/`atr`/`spread` then fails (0% calibrated).

**Fix options:**

**(A)** Extend the verifier to skip the band check when the DB-side row count matches the per-kind universe size and every row is `bootstrap` with `n_observations < window_days` (cold-start signature). Matches the existing `deferred=True` pattern on `distillation_contract_history`. Cheaper option.

**(B)** Backfill `distillation_ticker_baseline` from OHLCV history at migration time so the first invocation runs against pre-calibrated state.

*Note:* Pre-existing baseline rows on a long-running prod DB mask the issue — only surfaces on fresh migrations.