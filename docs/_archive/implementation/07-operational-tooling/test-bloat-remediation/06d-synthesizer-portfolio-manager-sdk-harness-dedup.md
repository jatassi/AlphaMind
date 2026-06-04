# 06d — synthesizer + portfolio_manager SDK-harness dedup

## Goal

Two LLM-SDK-harness test files re-test machinery already covered by the canonical cross-harness suite. `analysis/synthesizer/test_harness.py` re-tests core machinery that `analysis/test_harness_core.py` already covers (≈9 of 14 deletable); `decision/portfolio_manager/test_harness.py` has ≈11 zero-marginal harness tests that collapse onto one shared invocation fixture. Delete the redundant re-tests; keep the component-specific behavior.

## Reading

* `tests/analysis/synthesizer/test_harness.py` and `tests/analysis/test_harness_core.py` (the canonical cross-harness contract).
* `tests/decision/portfolio_manager/test_harness.py`.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

* `synthesizer/test_harness.py`: delete the ≈9 tests subsumed by `test_harness_core.py` (retry-message shape, diagnostic-record schema, stop-reason classification, `ValidationResult` ownership — all asserted canonically there). KEEP the synthesizer-specific behavior tests.
* `decision/portfolio_manager/test_harness.py`: collapse the ≈11 zero-marginal harness tests onto one shared invocation fixture; keep any distinct assertion.

**Safety rule:** a deletion is allowed only if it does not drop coverage of `src/alphamind/analysis/synthesizer/`, `src/alphamind/decision/portfolio_manager/`, or the shared harness source.

## Acceptance criteria

- [ ] `synthesizer/test_harness.py` retains only synthesizer-specific tests; the cross-harness re-tests are gone.
- [ ] `pm/test_harness.py` collapsed onto a shared invocation fixture; distinct assertions retained.
- [ ] `coverage report` for the two source packages + the shared harness shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/analysis/synthesizer tests/analysis/test_harness_core.py tests/decision/portfolio_manager -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; lint clean.