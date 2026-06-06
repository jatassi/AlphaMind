# Ordinal-phase rename — scope & plan

Status: Spec (2026-06-06). No code changed yet — packaging deferred per operator.

Governing decision: **[ADR 0006](../adr/0006-name-steps-by-behavior-not-ordinal-phase.md)** —
code steps are named for what they do; nothing is named `phase N`. Canonical execution
vocabulary lives in [CONTEXT.md](../../CONTEXT.md) § Invocation lifecycle.

## The problem

`phase1` / `phase2` had accreted onto **three unrelated code constructs**, none
self-describing at a call site, with the execution `phase1` colliding head-on with the
guardrail `phase 1` inside the same layer. A fourth `Phase N` usage (project-roadmap
milestones) is **deliberately out of scope**.

| # | Construct | Canonical name | Reach |
|---|---|---|---|
| 1 | Execution two-transaction invocation | **`fill_collection` / `command_execution`** | full-stack: DB → raw SQL → API → TS → UI labels → SSE string |
| 2 | Distillation seven-step sequence | **de-ordinalize all 7** + 3 identifier renames | src + living docs |
| 3 | Guardrail-enforcement composition | **`active_guardrails`** | guardrail_enforcement + pipeline + monitor |
| 4 | Project-roadmap lifecycle phases | **OUT — legitimate PM milestones** | — |

## Decisions (grill outcomes)

- **Name by behavior, never ordinal** — absolute for code constructs.
- **#1 register:** `fill_collection` / `command_execution` (matches the pre-existing
  `fill_collection_summary_json` / `command_execution_summary_json` columns — finishes a
  half-applied convention; the `invocations` table currently disagrees with itself).
- **#1 reach:** full-stack through the operator UI; DB+API+frontend land **atomically** in
  one PR so the JSON contract never desyncs.
- **#2:** de-ordinalize all seven steps (each already carries its behavior label); rename
  the 3 bare identifiers.
- **#3:** `active_guardrails` (the bundle is the active limit *parameters* — colloquially
  the guardrails in force).
- **"Two-phase invocation" is kept** as a structural term (two transactions, like
  "two-phase commit"); only the per-phase ordinals go.
- **Doc sweep = living docs only.** Dated audits, `_archive/**`, and completed `_done_`
  tracker lines are immutable history.
- **Roadmap `Phase 0–4` stays** — sequential milestones, no call site, no behavior.

## #1 Execution — `fill_collection` / `command_execution`

The big one: a schema change that propagates across the stack and lands atomically.

**DB (Alembic migration):**
- `invocations.phase1_completed_at` → `fill_collection_completed_at`
- `invocations.phase2_completed_at` → `command_execution_completed_at`
- Summary columns already `fill_collection_summary_json` / `command_execution_summary_json`
  — unchanged.
- Mechanics: new version under `persistence/migrations/versions/`,
  `op.batch_alter_table("invocations")` + `batch_op.alter_column(..., new_column_name=...)`
  (SQLite ≥3.25 RENAME COLUMN; batch mode for SQLite). **No CHECK constraint references
  these columns** (only trigger_type / trigger_source / active_mode / staleness_flag), so
  no constraint rebuild. Data preserved, no backfill. Provide a downgrade. Add a migration
  test (rename preserves rows).

**Python (string-literal & query sites — ~20 files):**
- `state/tables/invocations.py` — model columns + class docstring.
- `state/invocation_context/context.py` — `_PhaseColumn = Literal["phase1_completed_at",
  "phase2_completed_at"]` and the `stamp_phase_completion(column="…")` **string args**.
- `state/invocation_context/records.py`, `state/repository/sql_repository.py` (the
  snapshot-isolation guard reads `phase1_completed_at`).
- `scheduler/`: `orchestrator.py`, `invocation.py`, `driver.py`, `runtime.py`,
  `emergency.py`, `control/adapters.py`.
- `execution/write_paths/…`, `decision/portfolio_manager/submit_envelope/server.py`,
  `scripts/verify_genesis.py`.
- **Raw SQL:** `command_center/views/live_operations.py:219` hand-writes
  `" phase1_completed_at, phase2_completed_at,"` — update the SQL string.

**Modules / functions / classes:**
- `execution/write_paths/phase1.py` → `fill_collection.py`
- `execution/write_paths/phase2/` (pkg) → `command_execution/`
- `scheduler/phase1_inputs.py` → `fill_collection_inputs.py`
- `scheduler/phase2_dispatch.py` → `command_execution_dispatch.py`
- `Phase1Summary`→`FillCollectionSummary`; `Phase2Summary`→`CommandExecutionSummary`
- `Phase1Inputs`→`FillCollectionInputs`; `gather_phase1_inputs`→`gather_fill_collection_inputs`
- `dispatch_phase2`→`dispatch_command_execution`
- `orchestrator._run_phase1_write_unit`→`_run_fill_collection_write_unit`;
  `_update_row_phase1`/`_update_row_phase2`→`_update_row_fill_collection`/`…_command_execution`
- `process_unprocessed_fills()` — **already behavior-named; keep.**

**`.importlinter` (CI gate — must update or `lint-imports` fails):**
- Contract edges (lines ~413, 439, 440): `…execution.write_paths.phase2[.atomic]` →
  `…command_execution[.atomic]`.
- Module lists (lines ~490–491): `scheduler.phase1_inputs` / `scheduler.phase2_dispatch`.
- Plus the explanatory comments (241, 378, 387, 435–438).

**Config (triggers snapshot re-pin):**
- `config/portfolio_state.yaml: max_phase1_to_snapshot_seconds` →
  `max_fill_collection_to_snapshot_seconds`, plus the `config/models/…portfolio_state`
  field that reads it → **re-pin `tests/config/test_snapshot.py`** (resolved-config hash).
- Comment-only: `config/agents.yaml:27` ("pre-Phase-2"), `config/continuous_monitor.yaml:72`
  ("Phase-2 entry fill"). (`agents.yaml:59` "phase before the first token" and
  `run_types/…:22` "Tool phase" are generic — leave.)

**Progress / SSE labels (live consumer chain):**
- `orchestrator.py` `progress.phase_start/done("phase1"|"phase2")` → `"fill_collection"` /
  `"command_execution"`.
- `live_operations.py:242` derives `current_phase = "phase2" if phase1_completed_at else
  "phase1"` → new strings. Flows to the live UI via the `phase_transition` SSE event.

**API + frontend (atomic with the above):**
- `command_center/views/history.py` (`HeaderRow`, `_derive_status`), `live_operations.py`,
  `risk.py` — JSON field names.
- `frontend/src/api/queries.ts` — typed fields `phase1_completed_at`/`phase2_completed_at`.
- `frontend/src/views/history/invocation-detail/header-pane.tsx` — **UI copy**:
  "Phase 1 completed" → "Fill collection completed"; "Phase 2 completed" → "Command
  execution completed".
- Fixtures: `__tests__/views/history/invocation-detail.test.tsx`, `views/live/__tests__/
  live.test.tsx`.

**Living docs (#1):** `docs/design/05-execution-layer/architecture.md` ("Phase 1 — Collect"
/ "Phase 2 — Execute" + body), `state-persistence.md` (§ Phase 1/2 write-path **section
anchors** — referenced cross-doc; update every referrer: corporate-actions.md,
oms-command-ids.md, paper-evaluation-harness.md, orders-and-brackets.md,
short-equity-write-path.md), plus broker-adapter.md, oms-commands.md, README.md,
regt-margin-attribution.md, broker-boundary-redesign.md; `docs/architecture/*.md`;
**ADR 0005** phase refs; **CLAUDE.md invariants** — `execution/CLAUDE.md` ("**Two-phase
invocation**: fill collection commits, then command execution commits atomically" — keep
"two-phase"), the `write_paths/` module-table line, and `scheduler/CLAUDE.md`
(`phase1_inputs`/`phase2_dispatch` lines).

**Test files to rename (7):** `tests/execution/state_persistence/test_phase{1,2}_*.py`
(write_path, regt_attribution, pending_skeleton, strategy, options, package_layout),
`tests/scheduler/test_phase1_inputs.py` / `test_phase2_dispatch.py`,
`tests/pipeline/test_decision_phase1_enforcement.py` (→ guardrails, see #3).

## #3 Guardrails — `active_guardrails`

Shared across three subsystems (wider than first mapped).

- `execution/guardrail_enforcement/orchestrator.py`: `compose_phase_1_enforcement` →
  `compose_active_guardrails`; `Phase1EnforcementResult` → `ActiveGuardrails`; module
  docstring "Phase 1 guardrail-enforcement orchestrator" → "Active-guardrails composition".
- `guardrail_enforcement/__init__.py` — exports.
- `pipeline/_shared.py`: `build_phase1_enforcement_inputs` → `build_active_guardrail_inputs`
  (+ `__all__`).
- `pipeline/decision.py`: call sites; vars `phase1_regime/_drawdown/_tiers` →
  `guardrail_regime/_drawdown/_tiers`; `phase1_result` → `active_guardrails`.
- `execution/continuous_monitor/breach_loop/`: `task.py`, `__init__.py`,
  `production_substrate.py` — call + `Phase1EnforcementResult` type.
- `risk_guardrails/regime_adaptation/active_parameters.py` — docstrings.
- **Separate, related:** `risk_guardrails/guardrail_evaluation/types.py`
  `PortfolioStateSnapshot` docstring "Phase-1 portfolio snapshot" and
  `proposal-pre-processor.md:15` "the same Phase 1 snapshot" name the *portfolio-state
  snapshot baseline*, not `active_guardrails` — de-ordinalize to "pre-decision snapshot".
- Living docs: `06-risk-guardrails/state-delivery.md`, `guardrail-evaluation.md`,
  `05-execution-layer/architecture.md` § 3.

## #2 Distillation — de-ordinalize 7 + identifiers

~82 ordinal lines across 25 files; only 3 are identifiers.

- `distillation/orchestrator.py`:
  - `_run_phase_2` → `_compute_category_indicators`
  - `_compute_legacy_phase2_blocks` → `_compute_legacy_session_bound_blocks`
  - `_PHASE_2_PLACEHOLDER_GAPS` → `_CATEGORY_COMPUTE_PLACEHOLDER_GAPS`
  - Section headers + `run_distillation` inline comments: `# Phase 1 — Class B refresh` →
    `# Class B refresh`; `# Phase 2 — per-category …` → `# Per-category indicator compute`;
    `# Phase 3 — regime classification` → `# Regime classification`; `# Phase 4 —
    aggregation` → `# Aggregation`; `# Phase 5 — assembly` → `# Assembly`; `# Phase 6 —
    invocation-archive write` → `# Invocation-archive write`; `# Phase 7 — brief-store
    population` → `# Brief-store population`.
  - Module docstring "sequences seven phases" → "sequences seven steps".
- ~24 `q*/`, `qualitative/`, `_severity_cap.py`, `calibration_snapshot.py` docstring
  cross-refs ("the function the orchestrator's Phase 2 calls" → name the behavior).
- `.importlinter:90` comment "orchestrator phase-6 invokes" + line 241 "Phase 2 of the
  orchestrator".
- Living docs: `docs/design/02-distillation-layer/threshold-calibration.md`.
- Tests: `tests/distillation/q1/test_phase2_parallel.py`, `q3/test_phase2_parallel.py`.

**Cross-contamination to coordinate:**
- `distillation/replay_harness/engine.py` seeds execution `"phase1_completed_at"`/
  `"phase2_completed_at"` (these are **#1** DB columns — belong to PR1) **and** carries
  distillation "Phase 1 refresh" (#2).
- `distillation/orchestrator.py:1073` comment "downstream consumers (Phase 1 attribution)"
  means **execution** fill_collection (#1), not distillation.
  → Whichever PR lands second must not revert the other's tokens in these shared files.

## Packaging (deferred — recommended shape)

Three **independent** PRs, each separately reviewable:

1. **PR1 · #1 execution** — migration + Python + raw SQL + `.importlinter` + config key +
   API + frontend + execution design docs + ADR 0005 + CLAUDE.md. CI-gated (Windows pytest
   + frontend toolchain + `lint-imports` + snapshot re-pin). Atomic. **Largest.**
2. **PR2 · #3 guardrails** — `active_guardrails` across 3 subsystems + docs.
3. **PR3 · #2 distillation** — de-ordinalize 7 + 3 identifiers + docs.

Order PR1 first (touches the shared replay_harness/orchestrator tokens); PR2/PR3 in either
order. Renames need cross-file coherence (string literals, raw SQL, frontend, contracts) —
dispatch **Sonnet**, not Grok.

## Out of scope

- Project-roadmap `Phase 0–4` milestones (`project-tracker.md`, `command-center.md`,
  `asset-universe-validation.md`, `llm-agent-failure-handling.md`).
- Dated snapshots: `docs/audit-summary-2026-05-12.md`,
  `docs/deferral-comment-audit-2026-05-24.md`, `docs/_archive/**`, completed `_done_`
  tracker lines.
- Generic "tool phase" / "phase before the first token" wording (not ordinal).
