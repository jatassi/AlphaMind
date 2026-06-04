# 06b — q7 pure/DB double-altitude reduction

## Goal

Every q7 feature is tested twice — once against `compute_*_pure` in `test_compute.py`, then again through the session-accepting shim `compute_*(session, …)` in a dedicated DB-backed file that re-seeds OHLCV via `:memory:` SQLite and re-asserts the **same** narrative/velocity/regime labels. The DB layer's only unique behavior is row-loading + event persistence. Keep the pure-compute branch tests; reduce each DB-backed file to loader/persistence smoke plus the named firing/suppression pins. Est. \~900 LOC across 7 of 9 q7 files.

## Reading

* `tests/distillation/q7/*.py` — esp. `test_compute.py` (pure), `test_correlation_regime_change.py`, `test_intra_sector_correlation.py`.
* `src/alphamind/distillation/q7/` — the thin shims vs. the pure cores.
* `scripts/mutation/reports/distillation-q7.txt` (from story 04) — confirms the `*_pure` cores are constrained by `test_compute.py`.
* Parent `ALP-783`.

## Depends on

* `04` ([ALP-787](https://linear.app/alphamind-jatassi/issue/ALP-787/04-scoped-mutation-testing-cosmic-ray-for-the-pure-logic-cores)) — mutation evidence that `test_compute.py` pins the `*_pure` math before the DB re-tests are dropped.

## Scope

Under `tests/distillation/q7/`. Reduce the DB-backed files to loader/persistence smoke; keep `test_compute.py` pure tests intact.

**PRESERVE (verifier — two q7 items):**

* `test_correlation_regime_change.py`: keep `test_correlation_breakdown_fires_when_pair_correlation_shifts` (L142 — the only test isolating the breakdown block **and** asserting its `CR_BRIEF` audience through the DB entry point) and `test_narrative_lag_fires_when_breakdown_and_no_qualifying_news` (L317 — asserts `narrative_lag_flag` as its primary contract, not a no-crash side effect). Do not fold these into the malformed-tags no-crash test.
* `test_intra_sector_correlation.py`: keep `test_divergence_flag_suppressed_when_short_matches_long` (L337) — the suppression branch has **no** DB twin (only pure coverage). The fire test (L225) is genuinely redundant with the pure test + the persisted-event test and may go; the suppression test stays.

## Acceptance criteria

- [ ] `test_compute.py` pure tests are intact; the DB-backed files are reduced to loader/persistence smoke + the named pins.
- [ ] The four named preserve-tests still exist and pass.
- [ ] `coverage report` for `src/alphamind/distillation/q7/` shows no newly-missing lines vs. before.
- [ ] Re-running `scripts/mutation/run_mutation.sh distillation/q7` shows no mutation-score regression vs. the story-04 baseline (or coverage-guard fallback noted if 04 is blocked).
- [ ] `uv run pytest tests/distillation/q7 -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; mutation re-run no score regression; the four pins present.