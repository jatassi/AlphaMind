# 05d — Hoist risk_guardrails/state_delivery fixture builders to a shared module

## Goal

Hoist the duplicated `_make_budget_entry` / view / parameter builders repeated across the 5 `tests/risk_guardrails/state_delivery/` files (\~600 LOC) into a shared module (extend the existing `fixtures/builders.py` if present, else a `conftest.py`). Pure test-infra dedup. Story 07b (render-fossil rewrites in the same directory) is `blockedBy` this, so land the hoist first.

## Reading

* `tests/risk_guardrails/state_delivery/*.py` — esp. `test_portfolio_manager.py`, `test_strategist.py`, `test_end_to_end.py`.
* The existing `tests/risk_guardrails/**/fixtures/builders.py` (centralization target/style).
* Parent `ALP-783`.

## Depends on

* none.

## Scope

Under `tests/risk_guardrails/state_delivery/`.

* Move builders defined in ≥2 files to the shared module; update imports; delete local copies.
* Hoist only — no assertion changes, no test deletion.

## Acceptance criteria

- [ ] Shared builders module holds the formerly-duplicated builders; no duplication across the 5 files.
- [ ] `uv run pytest tests/risk_guardrails/state_delivery -p no:xdist` green.
- [ ] `coverage report` for `src/alphamind/risk_guardrails/` shows no newly-missing lines vs. before.
- [ ] `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff shows no regression; lint clean.