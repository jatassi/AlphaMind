# 03 — Retire drifting-ceiling & contract-redundant invariant tests

## Goal

Delete the two test patterns the audit bans — the three drifting-count "audit-baseline ceiling" tests and the source-grep invariant tests already subsumed by `.importlinter` contracts. Resolves the currently-failing `test_l4_broad_except_count_below_audit_baseline` (it has been ratcheted 14 times and is failing again at 68 vs a 67 ceiling). Keeps the stable structural guards. Every disposition below is final — no judgment required.

## Reading

* `tests/test_architecture_invariants.py` — the 3 count ceilings + 3 keep-tests.
* `tests/_kernel/test_ids.py`, `test_regime.py`, `test_calibration.py`, `test_mode.py`, `test_money.py` — the `test_kernel_*_has_zero_first_party_imports` functions.
* `tests/commands/test_decision_execution_decoupling.py` — mixed file: 2 contract-redundant greps + keep-the-rest.
* `.importlinter` — confirm `kernel-leaf`, `commands-leaf`, `decision-not-execution` contracts exist (they do).
* Parent `ALP-783`.

## Depends on

* none.

## Scope

All under `tests/`. Exact dispositions:

### 1\. `tests/test_architecture_invariants.py`

DELETE: `test_l4_broad_except_count_below_audit_baseline`, `test_l9_internal_pydantic_count_within_warranted_band`, `test_l19_async_over_sync_count_at_protocol_residue`; plus the now-unused `antipattern_findings` fixture, the `ANTIPATTERN_SCAN` module constant, and the `antipattern_scan` import.
KEEP: `test_no_audit_target_modules_in_any_cycle`, `test_remaining_cycles_are_intra_package_only`, `test_all_expected_import_linter_contracts_present`, and the `import_analysis` fixture they use.

### 2\. `tests/_kernel/test_{ids,regime,calibration,mode,money}.py`

DELETE the single `test_kernel_*_has_zero_first_party_imports` function in each (5 total) — their own docstrings state the `kernel-leaf` import-linter contract enforces this at the package level. Remove any local `ast` / `pathlib.Path` imports left unused inside those functions. KEEP every other test in each file (the real primitive tests).

### 3\. `tests/commands/test_decision_execution_decoupling.py`

DELETE: `test_no_decision_execution_import_cycle` (subsumed by the `decision-not-execution` contract) and `test_commands_package_imports_only_kernel_first_party` (subsumed by `commands-leaf`).
KEEP: `test_broker_dispatch_protocol_exposed_from_commands`, `test_pm_harness_constructible_with_fake_broker_dispatch`, the signature tests, `test_execution_oms_has_no_getattr_lazy_loader`, `test_execution_has_no_decision_portfolio_manager_imports`, `test_no_legacy_execution_oms_imports_remaining` (behavior/structural guards, not drifting counts).

### Out of scope

* `tests/scripts/test_verify_command_center.py` — a weak source-grep shape test, but the only structural guard on an operator-driven script; left as-is per story 01's policy ("don't add more of these," not retro-delete this one).

## Acceptance criteria

- [ ] The three named count-ceiling tests no longer exist; `test_architecture_invariants.py`'s three cycle/contract tests remain and pass.
- [ ] The five `test_kernel_*_has_zero_first_party_imports` functions no longer exist; the rest of each `_kernel` test file passes.
- [ ] The two named greps in `test_decision_execution_decoupling.py` are gone; the rest of that file passes.
- [ ] `uv run lint-imports` is green — demonstrating the `kernel-leaf` / `commands-leaf` / `decision-not-execution` contracts that subsume the deleted tests are active.
- [ ] No `test_l4_broad_except_count_below_audit_baseline` remains anywhere; the scoped suite is green (the prior failure is resolved by deletion).

## Verification

`uv run pytest tests/test_architecture_invariants.py tests/_kernel tests/commands -p no:xdist` green; `uv run lint-imports` green; `uv run ruff check . && uv run mypy` clean.