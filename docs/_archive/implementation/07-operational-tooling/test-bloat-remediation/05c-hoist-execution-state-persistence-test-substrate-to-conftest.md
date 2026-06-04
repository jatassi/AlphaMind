# 05c — Hoist execution/state_persistence test substrate to conftest

## Goal

Extract the duplicated `_make_process_lifetime` / cash / drawdown / fill substrate builders repeated across the `tests/execution/state_persistence/` files (\~450 LOC) into `tests/execution/state_persistence/conftest.py`. Pure test-infra dedup — no assertion changes. (This shard holds `test_phase2_write_path.py` at 4,128 lines and `test_phase1_write_path.py` at 2,332 — the dedup makes them maintainable.)

## Reading

* `tests/execution/state_persistence/*.py` — esp. `test_phase1_write_path.py`, `test_phase2_write_path.py`, `test_phase1_strategy.py`.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Under `tests/execution/state_persistence/`.

* Identify the builders/fixtures duplicated across ≥2 files; move them to `conftest.py`; update imports; delete local copies.
* Do not delete or rewrite any test, and do not change any assertion — this is a hoist only.

**Preserve (do not delete while here):** the private-dispatcher tests in `test_phase1_write_path.py` covering the three equity OPEN-direction routings (LONG-BUY-add, SHORT-BUY-cover, SHORT-SELL-add) — the verifier flagged these as having no DB-level guardian. They stay untouched (named so a zealous "while I'm here" cleanup doesn't remove them).

## Acceptance criteria

- [ ] Shared substrate lives in `tests/execution/state_persistence/conftest.py`; no builder is duplicated across files.
- [ ] All `state_persistence` tests still present and green (`uv run pytest tests/execution/state_persistence -p no:xdist`).
- [ ] `coverage report` for `src/alphamind/execution/` shows no newly-missing lines vs. before.
- [ ] `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff shows no regression; the three OPEN-direction routing tests still exist.