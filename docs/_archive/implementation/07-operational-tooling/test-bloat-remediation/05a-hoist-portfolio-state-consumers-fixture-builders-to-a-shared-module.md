# 05a — Hoist portfolio_state/consumers fixture builders to a shared module

## Goal

Eliminate the duplicated `_make_*` record builders across the 5 `tests/portfolio_state/consumers/` test files (\~900 LOC; `_make_thesis_quality` is md5-identical between files). Hoist them into one shared module so a record-shape change is a one-file edit, not a five-file edit. Pure test-infra dedup — no assertion changes, no source change.

## Reading

* `tests/portfolio_state/consumers/*.py` — the duplicated builders.
* `tests/portfolio_state/_fixtures.py` — existing shared-builder style to mirror.
* Parent `ALP-783` (decision B: regression guard).

## Depends on

* none.

## Scope

Under `tests/portfolio_state/consumers/`.

* Identify every `_make_*` builder defined in ≥2 of the consumer files (start with `_make_thesis_quality`).
* Move them to one shared module — a `conftest.py` (as fixtures) or a `_builders.py` imported by each file; match whichever pattern `_fixtures.py` uses.
* Update the 5 files to import the shared builders; delete the local copies.
* Do not change any test assertion or any `src/` file.

## Acceptance criteria

- [ ] A shared builders module exists under `tests/portfolio_state/consumers/`; no `_make_*` builder is defined in more than one consumer test file.
- [ ] `uv run pytest tests/portfolio_state/consumers -p no:xdist` is green.
- [ ] `coverage report` for `src/alphamind/portfolio_state/` shows no newly-missing lines vs. before the change (regression guard).
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Scoped pytest green; before/after coverage diff shows no source-line regression; lint clean.