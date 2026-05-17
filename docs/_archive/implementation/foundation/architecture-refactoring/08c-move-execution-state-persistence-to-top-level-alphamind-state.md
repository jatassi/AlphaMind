# 08c — Move `execution/state_persistence/` to top-level `alphamind/state/`

## Goal

The 41-file `execution/state_persistence/` subpackage is imported 125 times from outside execution (93× from `scripts/`, 21× from `scheduler/`, plus analysis/decision/distillation/persistence/config) — strong evidence it's cross-cutting infrastructure that happens to be co-located. Promote to top-level `src/alphamind/state/`. Keep only the OMS-specific write paths (`write_paths/phase1/`, `phase2/`, `fill_persistence.py`, `ca_integration_ledger.py`) inside execution if they're truly execution-private.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L9 high-yield "Misplaced modules" row, plus LB2 from the execution-subdivision audit
* `src/alphamind/execution/state_persistence/` — 41 files, 4,659 LOC, 125 inbound imports from outside
* Story 06c (<issue id="b1fd5046-d1da-4c51-958f-21ce576d3e50">ALP-465</issue>) — `write_paths/phase2/` already decomposed (the OMS-specific portion stays in execution; this story moves the rest)

## Depends on

* 06c (<issue id="b1fd5046-d1da-4c51-958f-21ce576d3e50">ALP-465</issue>) — phase2 must be decomposed first so the "OMS-specific write paths stay in execution; codecs/tables/repository move out" boundary is clear

## Scope

In scope: rename `execution/state_persistence/` → top-level `alphamind/state/` for everything except the OMS-write-path subset; system-wide import-path update for 125 inbound imports.

### 1\. Move to `alphamind/state/`

Move (with `git mv` to preserve history):

* `execution/state_persistence/tables/` → `state/tables/`
* `execution/state_persistence/repository/` → `state/repository/`
* `execution/state_persistence/invocation_context/` → `state/invocation_context/`
* `execution/state_persistence/codecs/` (if separate) → `state/codecs/`
* Other shared modules (anything that has consumers outside execution per the audit's 125-count)

### 2\. Keep in `execution/state_persistence/` (or rename to `execution/write_paths/`)

* `write_paths/phase1.py` and `write_paths/phase2/` (post-06c decomposition)
* `fill_persistence.py`
* `ca_integration_ledger.py`
* Other modules that exclusively serve execution

Consider renaming the remaining package to `execution/write_paths/` for clarity (no more "state_persistence" misnomer).

### 3\. Update all 125 inbound imports

Sweep across `scripts/`, `scheduler/`, `analysis/`, `decision/`, `persistence/`, `distillation/`, `config/`. Replace `from alphamind.execution.state_persistence.X` with `from alphamind.state.X`. Use `find . -name '*.py' -exec grep -l ...` + sed (with caution).

### 4\. Update import-linter

Add `state/*` to the layer contracts at the right tier (boundary-side, below distillation; ideally `state` joins `config`/`data_sources`/`persistence` in the bottom tier).

### Out of scope

Renaming `execution/state_persistence/` to `execution/write_paths/` (a sub-decision); operator can decide during the PR. Move codecs to a separate `codecs/` top-level if they justify it (probably not needed at current scope).

## Acceptance criteria

- [ ] `src/alphamind/state/` exists with `tables/`, `repository/`, `invocation_context/`, plus other cross-cutting subpackages.
- [ ] `src/alphamind/execution/state_persistence/` (or renamed `execution/write_paths/`) contains only OMS-specific write paths.
- [ ] Zero hits for `from alphamind.execution.state_persistence` across `src/alphamind/{scripts,scheduler,analysis,decision,persistence,distillation,config}/`; all retargeted to `alphamind.state`.
- [ ] `tests/` mirror updated (test files move alongside).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from alphamind.execution.state_persistence" src/` returns only hits inside `src/alphamind/execution/` itself (for the truly-execution-private OMS write paths). `git log --follow src/alphamind/state/tables/orders.py` shows continuity with the previous location.