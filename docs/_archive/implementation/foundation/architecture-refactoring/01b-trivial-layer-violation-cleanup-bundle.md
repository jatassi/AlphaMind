# 01b — Trivial layer-violation cleanup bundle

## Goal

Five surgical layer-violation cleanups in one PR — file moves, deletions, and empty-stub strategies that the audit identified as Trivial-complexity, low-risk wins. Each cleanup either eliminates a misplaced module (a module that lives in the wrong layer per the architecture intent) or removes dead code (re-export shims with zero/redundant consumers, empty namespace stubs). None of these depend on the kernel work in 02a/02b; they can land in parallel with 01a.

Bundling rationale: each cleanup is a one-or-two-file change, none shares scope with another, and grouping them keeps the orchestrator from dispatching five sequential trivial PRs. A single Sonnet agent can carry the whole bundle.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — high-yield "Misplaced modules" row; § Worth knowing — W1 (empty namespace stubs), W2 (distillation re-export shims)
* `docs/project-tracker.md` § Operational tooling — confirms `command_center` (<issue id="fc8aac14-a390-4875-bafc-fa6d98cf5a50">ALP-128</issue>), `feedback_loop` (<issue id="e6e8f55b-904e-457b-8e3a-a3d322243837">ALP-131</issue>), `counterfactual_replay_engine` (<issue id="6c436e0f-7f48-4df6-8fdf-7bb036c01c0c">ALP-129</issue>), `paper_evaluation_harness` (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>) are pending features
* `src/alphamind/distillation/orchestrator.py:78` — single consumer of `q3_options.py`; updated to import from `q3/` directly
* `src/alphamind/execution/state_persistence/invocation_paths.py` — 33-line constants file moved to `_kernel/`
* `src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py` — 152-LOC FS-I/O module moved to config control handlers
* `src/alphamind/data_sources/news/clustering.py` — pure domain code moved to analysis
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decisions (A), (B) for kernel naming and empty-stub strategy

## Depends on

* (none — parallel with 01a; both are foundation gates)

## Scope

Five named deliverables across the codebase. Tests update alongside source moves.

### 1\. Move `invocation_paths.py` to `_kernel/`

Create `src/alphamind/_kernel/__init__.py` (empty for now; story 02a populates it with regime/calibration). Move `src/alphamind/execution/state_persistence/invocation_paths.py` to `src/alphamind/_kernel/invocations.py`. Update every consumer:

* `analysis/{adaptive_research,domain_researchers,qualitative_research,synthesizer}/harness.py` (4 imports)
* `decision/{analyst,portfolio_manager,strategist}/harness.py` (3 imports)
* `distillation/calibration_snapshot.py` (1 import)
* Any other consumers — confirm via `grep -rn "from alphamind.execution.state_persistence.invocation_paths"` returning zero hits after the move.

Update `tests/` mirrors. The import path everyone uses becomes `from alphamind._kernel.invocations import INVOCATIONS_DIRNAME` (and the other constants).

### 2\. Delete dead distillation re-export shims

Delete three files:

* `src/alphamind/distillation/q1_price_volume.py` (197 LOC, 0 consumers)
* `src/alphamind/distillation/q7_cross_asset.py` (96 LOC, 0 consumers)
* `src/alphamind/distillation/q3_options.py` (99 LOC, 1 redundant consumer)

Update the one redundant consumer at `src/alphamind/distillation/orchestrator.py:78` from `from alphamind.distillation.q3_options import assemble_q3_blocks` to `from alphamind.distillation.q3 import assemble_q3_blocks`.

Verify with `grep -rn "from alphamind.distillation.q1_price_volume\|from alphamind.distillation.q7_cross_asset\|from alphamind.distillation.q3_options"` returning zero hits.

### 3\. Empty-stub strategy

**Stub with** `__getattr__` (project-tracker features, namespace must be intentional):

* `src/alphamind/command_center/__init__.py` — `def __getattr__(name): raise NotImplementedError("command_center scheduled for ALP-128")`
* `src/alphamind/command_center/backend/__init__.py` — same with `ALP-128`
* `src/alphamind/feedback_loop/__init__.py` + 4 subpackage `__init__.py` files — `__getattr__` raising `NotImplementedError("feedback_loop scheduled for ALP-131")`
* `src/alphamind/execution/counterfactual_replay_engine/__init__.py` — `ALP-129`
* `src/alphamind/execution/orders_and_brackets/__init__.py` — link to project-tracker note (this one is partially shipped as <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue>; verify and either delete or stub)
* `src/alphamind/execution/paper_evaluation_harness/__init__.py` — `ALP-130`

**Plain delete** (no design ownership, no project-tracker bullet):

* `src/alphamind/data_sources/alpaca/` directory entirely (empty placeholder)
* `src/alphamind/analysis/domain_researchers/tech_semis/__init__.py` (empty)
* `src/alphamind/analysis/domain_researchers/financials/__init__.py` (empty)
* `src/alphamind/analysis/domain_researchers/energy/__init__.py` (empty)

For the analysis trinity: confirm the deletion doesn't break sector-resolution logic before deleting. The sector differentiation lives in `Sector` enum + `agents.yaml` + `_SECTOR_TO_AGENT` table; the empty `__init__.py` files are not load-bearing.

### 4\. Move `profile_switch.py` to config control handlers

Create `src/alphamind/config/control_handlers/__init__.py` (empty). Move `src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py` to `src/alphamind/config/control_handlers/profile_switch.py`. Update any consumers (likely sparse — this is an admin write-path called from operator tooling or a future command-center endpoint). The `risk_guardrails/rules_and_limits/__init__.py` re-export pointing at `profile_switch` must be removed.

### 5\. Move `news/clustering.py` to analysis

Create `src/alphamind/analysis/news_clustering/__init__.py` (empty). Move `src/alphamind/data_sources/news/clustering.py` to `src/alphamind/analysis/news_clustering/clustering.py`. Update consumers (likely `analysis/qualitative_research/news_digest.py` and possibly others — verify by grep).

### Out of scope

Story 02a will populate `_kernel/` with `regime.py` and `calibration.py`. Story 03 will tighten import-linter contracts. Story 09c will split `data_sources/_common.py` and hoist `_atomic_write` to `_kernel`. This story does NOT migrate any types or convert any Pydantic — purely structural moves and deletes.

## Acceptance criteria

- [ ] `src/alphamind/_kernel/__init__.py` exists (empty); `src/alphamind/_kernel/invocations.py` contains the former contents of `execution.state_persistence.invocation_paths.py`.
- [ ] Zero hits for `from alphamind.execution.state_persistence.invocation_paths` across `src/` and `tests/`.
- [ ] `distillation/q1_price_volume.py`, `q3_options.py`, `q7_cross_asset.py` are deleted; `distillation/orchestrator.py:78` imports from `distillation.q3` directly.
- [ ] Each project-tracker-feature empty package raises `NotImplementedError("... scheduled for ALP-XXX")` via `__getattr__`; attempting `from alphamind.command_center import anything` fails with that error.
- [ ] `data_sources/alpaca/` directory and the three analysis trinity `__init__.py` files are deleted.
- [ ] `config/control_handlers/profile_switch.py` exists with the former contents; `risk_guardrails/rules_and_limits/profile_switch.py` is deleted; `risk_guardrails/rules_and_limits/__init__.py` no longer re-exports `profile_switch`.
- [ ] `analysis/news_clustering/clustering.py` exists; `data_sources/news/clustering.py` is deleted; consumers updated.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

`grep -rn "from alphamind.execution.state_persistence.invocation_paths\|from alphamind.distillation.q1_price_volume\|from alphamind.distillation.q3_options\|from alphamind.distillation.q7_cross_asset\|from alphamind.risk_guardrails.rules_and_limits.profile_switch\|from alphamind.data_sources.news.clustering" src/ tests/` returns no hits. Test suite passes. Lint clean. Importing one of the stubbed packages directly raises the expected `NotImplementedError` with the named ALP issue.