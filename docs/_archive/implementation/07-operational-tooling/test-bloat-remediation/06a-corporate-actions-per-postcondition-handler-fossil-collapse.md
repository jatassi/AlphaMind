# 06a — Corporate-actions per-postcondition handler-fossil collapse

## Goal

Every corporate-action handler funnels through one shared `integrate_ca_activity()` / `finalize_ca_handler` path, yet `test_splits.py`, `test_reverse_splits.py`, and `test_stock_dividends.py` each split that single call into 4–8 tests that re-seed the identical position cluster and assert quantity **or** basis **or** bracket-dissolved-reason **or** dedup-ledger-row separately. The combined-assertion form already exists (`test_split_options_projects_alpaca_state_and_clears_greeks` asserts position+bracket+ledger together). Collapse the split equity tests onto one combined test per CA branch and fold the holdout files onto `_handler_substrate.py`. Est. \~1,000 LOC. Deterministic math — redundancy, not weakness.

## Reading

* `tests/execution/corporate_actions/*.py` — the per-postcondition splits + the existing combined options test.
* `src/alphamind/execution/corporate_actions/` — the shared handler path.
* `scripts/mutation/reports/execution-corporate_actions.txt` (from story 04) — confirms the combined tests pin the math.
* Parent `ALP-783`.

## Depends on

* `04` ([ALP-787](https://linear.app/alphamind-jatassi/issue/ALP-787/04-scoped-mutation-testing-cosmic-ray-for-the-pure-logic-cores)) — mutation evidence that the combined tests kill the handler-math mutants before the per-facet duplicates are removed.

## Scope

Under `tests/execution/corporate_actions/`.

* Per CA branch (split / reverse-split / stock-dividend / …), collapse the per-postcondition equity tests onto one combined-assertion test asserting position + bracket + ledger together (mirror the existing options combined test).
* Fold the 4 holdout files onto `_handler_substrate.py`.

**PRESERVE (verifier —** `test_reconciliation.py`**):** keep the reconcile **return-count** assertions and the standalone minimal "drift → alert" spec for the options and equity domains. The writeback tests (`test_reconcile_writes_back_options_drift_and_emits_correction` L547, `…_equity_drift…` L461) cover the alert **payload** but do **not** assert reconcile's return count — do not fold those away. This is a financial safety guardrail.

## Acceptance criteria

- [ ] Each CA branch has one combined test asserting position+bracket+ledger together; the per-facet equity duplicates are removed.
- [ ] The reconciliation return-count assertions and the minimal drift→alert specs are retained.
- [ ] `coverage report` for `src/alphamind/execution/corporate_actions/` shows no newly-missing lines vs. before.
- [ ] Re-running `scripts/mutation/run_mutation.sh execution/corporate_actions` shows no mutation-score regression vs. the story-04 baseline. (If story 04 is blocked, fall back to the coverage guard + explicit assertion-preservation and note it in the PR.)
- [ ] `uv run pytest tests/execution/corporate_actions -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; mutation re-run no score regression; reconciliation return-count assertions present.