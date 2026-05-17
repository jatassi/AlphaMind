# 07-follow-up — Propagate compute/load split to q3 (anomalies, atm_iv_baseline, etf_iv_divergence)

## Goal

Complete the q3 compute/load boundary split started in <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> (flow_classification). Each remaining q3 sub-module gets a `q3/<sub>_compute.py` (pure) + `q3/<sub>_loaders.py` (DB shell) pair, and `q3/assemble.py` decomposes its loading into per-sub loaders + an `assemble_q3_blocks_from_inputs(inputs, config)` pure-compute entry point. After this story, `_compute_q3_blocks` in `orchestrator.py` becomes a `to_thread(compute_q3_blocks_from_inputs, q3_inputs, config)` call that runs in parallel with q1 inside the Phase 2 `TaskGroup`.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — L6 (P1 violation)
* `src/alphamind/distillation/q3/flow_classification_compute.py` + `q3/flow_classification_loaders.py` — <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue>'s propagation proof; mirror the shape
* `src/alphamind/distillation/q1/_loaders.py` + `q1/assemble.py:assemble_q1_blocks_from_inputs` — the q1 pilot's full-category split (the model to follow)
* `src/alphamind/distillation/orchestrator.py:Phase 2` — where this story's compute lift hooks into the TaskGroup

## Depends on

* <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> — <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> establishes the `DistillationRepository(Protocol)` seam and the q1 + q3.flow_classification pilot

## Scope

In scope: q3 sub-modules NOT covered by <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> — `anomalies.py`, `atm_iv_baseline.py`, `etf_iv_divergence.py`. Plus `q3/_loaders.py` (whole-category pre-load returning `Q3Inputs`) and `q3/assemble.py:assemble_q3_blocks_from_inputs`. Each pure compute imports zero ORM types; the `distillation-compute-no-sqlalchemy` import-linter contract gains entries for the new `*_compute.py` modules. Orchestrator Phase 2 lifts q3 out of the legacy serialized subtask into a third TaskGroup task. Tests at `tests/distillation/q3/test_compute.py` (new).

Out of scope: q6, q7, qualitative (tracked separately).

## Acceptance criteria

- [ ] `q3/anomalies_compute.py` + `q3/anomalies_loaders.py` exist; `q3/anomalies.py` is thin orchestration.
- [ ] Same split for `q3/atm_iv_baseline` and `q3/etf_iv_divergence`.
- [ ] `q3/_loaders.py` exposes `Q3Inputs` + `load_q3_inputs(repo, *, config, as_of, ticker_scope, pair_correlations)`.
- [ ] `q3/assemble.py:assemble_q3_blocks_from_inputs(inputs, config)` is pure compute.
- [ ] `orchestrator.py` Phase 2: q3 lifts into its own TaskGroup task running in parallel with q1; the legacy subtask drops to `(q6, q7, q12, qualitative)`.
- [ ] `distillation-compute-no-sqlalchemy` contract enumerates every new `*_compute.py`.
- [ ] `tests/distillation/q3/test_compute.py` exists with pure-compute tests (no SQLite).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from sqlalchemy\|session\." src/alphamind/distillation/q3/*_compute.py` returns zero hits. Phase 2 timing: synthetic test shows q1+q3 wall-clock equal to max(q1, q3) plus thread overhead, not sum.