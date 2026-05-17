# 07-follow-up — Propagate compute/load split to qualitative_derived

## Goal

Apply the compute/load boundary split to `qualitative_derived.py` (3 public computations: news/price divergence, sentiment percentile, prediction-market deltas). After this story, qualitative becomes pure compute and lifts into its own TaskGroup task in Phase 2.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — L6 (P1 violation)
* `src/alphamind/distillation/q1/_loaders.py` + `q1/assemble.py:assemble_q1_blocks_from_inputs` — the q1 pilot model
* `src/alphamind/distillation/qualitative_derived.py` — current 868 LOC module covering three classifiers
* `src/alphamind/distillation/contract_scope.py` — the prediction-market scope resolver (already pre-computed by the orchestrator)

## Depends on

* <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> — <issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue> establishes the DistillationRepository(Protocol) seam

## Scope

In scope: decompose `qualitative_derived.py` into a `qualitative/` package with per-classifier `*_compute.py` + `*_loaders.py` (news_price_divergence, sentiment_percentile, prediction_market_deltas). Whole-category `qualitative/_loaders.py` returns `QualitativeInputs`. Orchestrator Phase 2 lifts qualitative into its own TaskGroup task. The contract-scope resolver stays on `contract_scope.py` and threads through inputs (already does today).

## Acceptance criteria

- [ ] Each qualitative classifier has its own `*_compute.py` + `*_loaders.py` pair.
- [ ] `qualitative/_loaders.py` exposes `QualitativeInputs` + `load_qualitative_inputs(repo, *, config, ticker_scope, contract_scope, as_of)`.
- [ ] `qualitative/assemble.py:assemble_qualitative_blocks_from_inputs(inputs, config)` is pure compute.
- [ ] `orchestrator.py` Phase 2: qualitative lifts into its own TaskGroup task; the legacy serialized subtask drops to `(q12,)` only.
- [ ] `distillation-compute-no-sqlalchemy` contract enumerates every new `*_compute.py`.
- [ ] `tests/distillation/qualitative/test_compute.py` exists with pure-compute tests (no SQLite).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "from sqlalchemy\|session\." src/alphamind/distillation/qualitative/*_compute.py` returns zero hits.