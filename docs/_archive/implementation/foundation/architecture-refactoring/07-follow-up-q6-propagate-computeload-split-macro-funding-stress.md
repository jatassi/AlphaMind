# 07-follow-up — Propagate compute/load split to q6 (macro / funding stress)

## Goal

Apply the compute/load boundary split to `q6_macro.py` (1380 LOC). Q6 has the largest funding-stress / market-liquidity composite refresh logic, and its `session.flush()` calls are the load-bearing reason Phase 2 was originally serialized. After this story, q6 becomes pure compute over pre-loaded composite-state inputs and runs in its own TaskGroup task in parallel with q1/q3.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — L6 (P1 violation)
* `src/alphamind/distillation/orchestrator.py:706-769` — original Phase 2 comment names q6 as the second flush-conflict source
* `src/alphamind/distillation/q1/_loaders.py` + `q1/assemble.py:assemble_q1_blocks_from_inputs` — the q1 pilot model

## Depends on

* <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> — <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> establishes the DistillationRepository(Protocol) seam

## Scope

In scope: `q6_macro.py` decomposition into `q6/_loaders.py` (DB shell — loads FRED-series / breakeven / dollar / surprise data + funding-stress / market-liquidity composites) + `q6/_compute.py` (or split per classifier — yield-curve / inflation / FX / surprise / funding-stress / liquidity). `compute_q6_blocks` becomes a session-accepting shim around the pure path. Orchestrator Phase 2 lifts q6 into its own TaskGroup task. The composite-refresh writes (which session.flush()) move into the load shell, not the compute core.

Out of scope: q7, qualitative.

## Acceptance criteria

- [ ] `q6/_loaders.py` + `q6/_compute.py` (or per-classifier split) exist; legacy `q6_macro.py` is the thin shim re-exporting the public surface.
- [ ] `q6/_compute.py` imports zero ORM types; pinned by the import-linter contract.
- [ ] `orchestrator.py` Phase 2: q6 lifts into its own TaskGroup task running in parallel with q1, q3; the legacy serialized subtask drops to `(q7, q12, qualitative)`.
- [ ] `tests/distillation/q6/test_compute.py` exists with pure-compute tests (no SQLite).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from sqlalchemy\|session\." src/alphamind/distillation/q6/*_compute.py` returns zero hits. The "Session is already flushing" comment in `orchestrator.py` (already removed in <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue>) does not regress.