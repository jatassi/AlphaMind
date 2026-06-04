# 05e — Hoist distillation/external test fixtures to conftest

## Goal

Extract the 4× verbatim `DistillationConfig(...)` literal / `_build_distillation_config()` (\~320 LOC) plus the \~14 duplicated engine/session fixtures across the 32 `tests/distillation/external/` files into `tests/distillation/external/conftest.py`. Pure test-infra dedup — no assertion changes.

## Reading

* `tests/distillation/external/*.py` — the repeated config literal + engine/session fixtures.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Under `tests/distillation/external/`.

* Move the duplicated `DistillationConfig` builder and the engine/session fixtures into `conftest.py`; update imports; delete local copies.
* Hoist only — no assertion changes, no test deletion.

## Acceptance criteria

- [ ] The `DistillationConfig` builder and engine/session fixtures are defined once in `conftest.py`; no duplication across the external test files.
- [ ] `uv run pytest tests/distillation/external -p no:xdist` green.
- [ ] `coverage report` for `src/alphamind/distillation/` shows no newly-missing lines vs. before.
- [ ] `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff shows no regression; lint clean.