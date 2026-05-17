# 07-follow-up — Propagate compute/load split to q7 (cross-asset / correlation)

## Goal

Apply the compute/load boundary split to the q7 package (8 modules: `_helpers.py`, `assemble.py`, `breadth_internals.py`, `correlation_regime_change.py`, `cross_sector_rotation.py`, `intermarket_regime.py`, `intra_sector_correlation.py`, `lead_lag.py`). Q7 is the largest distillation category by module count; its compute lift unlocks parallel execution with q1, q3, q6.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — L6 (P1 violation)
* `src/alphamind/distillation/q1/_loaders.py` + `q1/assemble.py:assemble_q1_blocks_from_inputs` — the q1 pilot model
* `src/alphamind/distillation/q7/*.py` — every module currently imports sqlalchemy; full split surface is largest in distillation

## Depends on

* <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> — <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> establishes the DistillationRepository(Protocol) seam

## Scope

In scope: per-module `q7/<sub>_compute.py` + `q7/<sub>_loaders.py` pairs across all 8 q7 modules; `q7/_loaders.py` (whole-category pre-load); `q7/assemble.py:assemble_q7_blocks_from_inputs`. The pair-correlation helper (`compute_pair_correlations`) is already pre-computed by the orchestrator — its load surface threads through `Q7Inputs`. Orchestrator Phase 2 lifts q7 into its own TaskGroup task.

Audit notes one indirect path via `data_sources._common` that is tracked separately as <issue id="6395f086-7f7c-4715-8d07-d52039cb11e5">ALP-473</issue> (punch-list #22); the q7 split is independent.

Out of scope: qualitative.

## Acceptance criteria

- [ ] Each q7 sub-module has `q7/<sub>_compute.py` + `q7/<sub>_loaders.py`; `q7/<sub>.py` is thin orchestration.
- [ ] `q7/_loaders.py` exposes `Q7Inputs` + `load_q7_inputs(repo, *, config, as_of, ticker_scope, pair_correlations)`.
- [ ] `q7/assemble.py:assemble_q7_blocks_from_inputs(inputs, config)` is pure compute.
- [ ] `orchestrator.py` Phase 2: q7 lifts into its own TaskGroup task.
- [ ] `distillation-compute-no-sqlalchemy` contract enumerates every new `*_compute.py`.
- [ ] `tests/distillation/q7/test_compute.py` exists with pure-compute tests (no SQLite).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from sqlalchemy\|session\." src/alphamind/distillation/q7/*_compute.py` returns zero hits.