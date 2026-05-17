# 06c — Decompose `execution/state_persistence/write_paths/phase2.py` by OMS command kind

## Goal

Split the 1,684-LOC `phase2.py` god module into per-command-kind submodules under `write_paths/phase2/`. Each file owns the writeback + ledger emission for one OMS command variant (OPEN, CLOSE, ADJUST, CANCEL, ADD), so a contributor adding capability for a new OMS command kind touches one new file instead of growing the monolith. After story 02b's cycle break, this file imports `commands.command_models` and `decision.portfolio_manager.validation` directly (no more cycle workarounds); after 02b's `BrokerDispatch` Protocol move, the persistence layer doesn't need to know about the engine-stub at all.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L8 — phase2 decomposition plan
* `src/alphamind/execution/state_persistence/write_paths/phase2.py` — 1,684 LOC, 25 cross-package imports
* Story 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — eliminated the decision↔execution cycle; this story's persistence path now imports `commands.*` and `decision.*` cleanly
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` (637 LOC, coherent) — leave for now; story 06c is phase2 only
* Story 06b (<issue id="8fded466-a77b-4bda-aa7f-9a3e91a65609">ALP-464</issue>) — decomposing `submit_envelope.py` in parallel; both stories touch the persistence path's consumers but not the same files

## Depends on

* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — must land first; the cycle-break unlocks normal top-level imports inside `phase2/`

## Scope

In scope: convert single-file `phase2.py` into `phase2/` package with one file per OMS command kind + shared helpers. Tests update accordingly.

### Decomposition

Create `execution/state_persistence/write_paths/phase2/__init__.py` (curated re-exports) and per-command-kind submodules:

* `phase2/open.py` — `_process_open_command`, `_writeback_open`, capital reservation, position creation, thesis-link emission for OPEN
* `phase2/close.py` — `_process_close_command`, `_writeback_close`, capital release, position closure, bracket dissolution for CLOSE
* `phase2/adjust.py` — `_process_adjust_command`, bracket adjustment, stop/target modification for ADJUST
* `phase2/cancel.py` — `_process_cancel_command`, capital release, bracket dissolution for CANCEL
* `phase2/add.py` — `_process_add_command`, position add, thesis-component append for ADD
* `phase2/_shared.py` — `_reserve_capital`, `_release_capital`, `_emit`, role/protective-leg helpers, ledger emission helpers (anything two or more variants share)

### Public surface

`phase2/__init__.py` re-exports the symbols `submit_envelope.persist` calls: `phase2_write_pm_envelope` (or whichever entry-point is the boundary), plus the error types. Consumers continue to `from alphamind.execution.state_persistence.write_paths.phase2 import phase2_write_pm_envelope`.

### Cross-cutting helpers

`_reserve_capital` / `_release_capital` (Money-aware after story 05b) live in `_shared.py` and are imported by `open.py`, `close.py`, `cancel.py`. `_emit` (activity log emission helper) lives in `_shared.py`.

### Out of scope

`phase1.py` (637 LOC) is coherent — leave it alone for now; if it grows past 1000 LOC or accretes cross-package imports, a future story can decompose. Story 06c is phase2 only.

## Acceptance criteria

- [ ] `execution/state_persistence/write_paths/phase2/` is a package; the former single file is split into `open.py`, `close.py`, `adjust.py`, `cancel.py`, `add.py`, `_shared.py`, plus curated `__init__.py`.
- [ ] Each command-kind file is 200-400 LOC; `_shared.py` is ≤500 LOC.
- [ ] Public surface (re-exported via `__init__.py`) is unchanged; the existing `phase2_write_pm_envelope` entrypoint signature is preserved.
- [ ] No inline / function-local imports of `commands.*` or `decision.*` remain anywhere in `phase2/`; all imports are at the top of the file.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

Manual: `wc -l src/alphamind/execution/state_persistence/write_paths/phase2/*.py` shows each command-kind file 200-400 LOC. Existing phase2 integration tests pass unmodified. `grep -rn "from alphamind\." src/alphamind/execution/state_persistence/write_paths/phase2/` shows all imports at top-of-file (no `def`-level imports).