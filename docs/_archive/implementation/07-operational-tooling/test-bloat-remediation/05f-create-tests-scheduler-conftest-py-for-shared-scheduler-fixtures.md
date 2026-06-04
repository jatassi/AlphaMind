# 05f — Create tests/scheduler/conftest.py for shared scheduler fixtures

## Goal

The `tests/scheduler/` shard has no `conftest.py`, so `async_factory` and the venue/lifetime fixtures are hand-copied into \~10 files (\~400 LOC). Create `tests/scheduler/conftest.py` and centralize them. Pure test-infra dedup.

## Reading

* `tests/scheduler/*.py` and `tests/scheduler/control/`, `tests/scheduler/debug_e2e/` — locate the duplicated `async_factory` + venue/lifetime fixtures.
* An existing package `conftest.py` (e.g. `tests/decision/conftest.py`) for placement/style.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Under `tests/scheduler/`.

* Create `tests/scheduler/conftest.py`; move the duplicated `async_factory` and venue/lifetime fixtures into it; update the \~10 files to consume the conftest fixtures; delete the local copies.
* Hoist only — no assertion changes, no test deletion.

## Acceptance criteria

- [ ] `tests/scheduler/conftest.py` exists and defines the formerly-duplicated fixtures once.
- [ ] No `async_factory` (or its venue/lifetime siblings) is redefined in the individual scheduler test files.
- [ ] `uv run pytest tests/scheduler -p no:xdist` green (e2e-gated tests skip as normal).
- [ ] `coverage report` for `src/alphamind/scheduler/` shows no newly-missing lines vs. before.
- [ ] `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff shows no regression; lint clean.