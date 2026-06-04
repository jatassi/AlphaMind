# 07c — strategist test_input_bundle golden-render collapse

## Goal

`tests/decision/strategist/test_input_bundle.py` (1,603 LOC) has \~22 tests that each re-render the whole strategist input bundle and substring-check one section of the output. Collapse them into one golden full-render assertion (the bundle rendered against a fixed fixture, compared exactly) plus focused branch tests for the conditional sections — replacing 22 fragile substring probes with one exact pin + the genuine branches.

## Reading

* `tests/decision/strategist/test_input_bundle.py` — the whole-bundle substring tests + any conditional-section tests.
* `src/alphamind/decision/strategist/` — the input-bundle assembler (to identify the conditional branches that need their own test).
* Parent `ALP-783`.

## Depends on

* none.

## Scope

* Introduce one golden full-render test: render the bundle against a representative fixture and assert the exact output (a golden string / structured snapshot). This subsumes the \~22 per-section substring assertions.
* Keep a focused test for each genuine conditional branch (a section that appears/changes based on input — empty vs populated, a flag toggling a block). These are real branches the single golden render does not exercise.

**Safety guard:** the golden render + branch tests together must not drop coverage of any `src/alphamind/decision/strategist/` line the 22 substring tests covered.

## Acceptance criteria

- [ ] One golden full-render test replaces the per-section substring cluster; the conditional-branch sections each retain a focused test.
- [ ] `coverage report` for `src/alphamind/decision/strategist/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/decision/strategist/test_input_bundle.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; the file is materially shorter with the same source-line coverage.