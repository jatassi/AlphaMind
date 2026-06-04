# 05b — Centralize pipeline test fixtures and retire \_CallLog plumbing tests

## Goal

The `pipeline/` shard is 94% zero-marginal: most tests monkeypatch the internal runner collaborators and then assert on a `_CallLog` that records the forwarded kwargs — i.e. they assert what the test's own monkeypatch emits (the altitude-rule violation). Two-part cleanup: (a) centralize the triplicated result-builders + `_patch_runners`/`_drive` helpers into `tests/pipeline/_fixtures.py`; (b) delete the tautological `_CallLog` kwarg-echo tests, keeping every test that asserts a real production behavior.

## Reading

* `tests/pipeline/*.py` — the `_CallLog` battery + duplicated helpers.
* `src/alphamind/pipeline/` — what the glue actually does (to tell echo from behavior).
* `tests/analysis/test_harness_core.py` — already covers the real per-harness agent-request/response emit (so the pipeline echoes are redundant).
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Under `tests/pipeline/`.

1. Hoist the duplicated result-builders + the `_patch_runners` / `_drive` helpers into a new `tests/pipeline/_fixtures.py`; update the files to import them.
2. Delete tests whose only assertions are on the `_CallLog` / captured kwargs of monkeypatched **internal** collaborators (tautological plumbing). KEEP any test that asserts a real production outcome — a returned value, a persisted row, an error path, a phase-boundary sequence.

**Safety rule (mechanical):** a `_CallLog` test may be deleted only if removing it does **not** drop coverage of any `src/alphamind/pipeline/` line (the real emit is covered by `test_harness_core.py`). If deletion would drop a source line, keep the test.

## Acceptance criteria

- [ ] `tests/pipeline/_fixtures.py` exists; the result-builders and `_patch_runners`/`_drive` helpers are defined once.
- [ ] The tautological `_CallLog` kwarg-echo tests are removed; the behavior tests pass.
- [ ] `coverage report` for `src/alphamind/pipeline/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/pipeline -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff on `src/alphamind/pipeline/` shows no regression (this is what proves the deleted tests were echo, not behavior).