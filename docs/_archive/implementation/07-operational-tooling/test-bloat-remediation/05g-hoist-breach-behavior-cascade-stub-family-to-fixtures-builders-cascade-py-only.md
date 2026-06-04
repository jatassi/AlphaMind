# 05g — Hoist breach_behavior cascade stub-family to fixtures/builders ([cascade.py](<http://cascade.py>) only)

## Goal

`tests/risk_guardrails/breach_behavior/test_cascade.py` defines a local stub family (`_StubRuleProjection`, `_StubLibraryOutput`, `_StubLibraryConfig`, `_StubPortfolioState`, `_StubMarketInputs`, `_ScriptedLibrary`, `_InputAwareLibrary`, \~L65–202) that is field-for-field identical to the package's `fixtures/builders.py` and used heavily (19 `_ScriptedLibrary` + 6 `_InputAwareLibrary` sites). Import them from `fixtures/builders.py` and delete the local copies — the safest consolidation class (pure test-infra dedup, no asserted behavior touched).

## Reading

* `tests/risk_guardrails/breach_behavior/test_cascade.py` (L65–202 stub family) and `fixtures/builders.py` (the canonical versions).
* `tests/risk_guardrails/breach_behavior/test_secondary_breach.py` and `test_hard_rejection.py` — **for confirmation only that they must NOT be touched** (see preserve note).
* Parent `ALP-783`.

## Depends on

* none.

## Scope — `test_cascade.py` ONLY

* Replace the local stub family in `test_cascade.py` with imports from `fixtures/builders.py`; delete the local class definitions.
* Fix the docstring drift: `test_cascade.py`'s `_ScriptedLibrary` docstring (\~L106–109) wrongly calls itself "input-aware"; `fixtures/builders.py` correctly says "not input-aware". The canonical version is authoritative.

**PRESERVE — do NOT touch these (verifier, critical — the surveyor's scope was wrong):**

* `test_secondary_breach.py` — its `_ScriptedLibrary` (L78–114) has a DIFFERENT, incompatible API (`baseline_output` / `post_close_output` / `raise_on_call` / `raise_exception`, error-injection) that `test_library_evaluation_error_propagates_with_wrapper` (L397) depends on. Replacing it with the `fixtures` version would drop error-injection. Leave it entirely alone.
* `test_hard_rejection.py` — contains only the two simple dataclasses `_StubRuleProjection` / `_StubLibraryOutput`, not the family; nothing to consolidate. Leave it alone.

## Acceptance criteria

- [ ] `test_cascade.py` imports the stub family from `fixtures/builders.py`; its local definitions are gone; the docstring drift is reconciled.
- [ ] `test_secondary_breach.py` and `test_hard_rejection.py` are byte-unchanged.
- [ ] `uv run pytest tests/risk_guardrails/breach_behavior -p no:xdist` green.
- [ ] `coverage report` for `src/alphamind/risk_guardrails/` shows no newly-missing lines vs. before.
- [ ] `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; `git diff --stat` shows `test_secondary_breach.py` / `test_hard_rejection.py` untouched; coverage diff shows no regression.