# Name pipeline steps by behavior, not ordinal phase

Status: Accepted — 2026-06-06

## Context

"phase1" / "phase2" had accreted onto three unrelated constructs, each meaning something
different and none self-describing at a call site:

- **Execution** — the two-transaction invocation: `phase1_completed_at` (drain + integrate
  settled fills at invocation start) and `phase2_completed_at` (validate + execute the PM's
  commands at invocation end). The ordinal was arbitrary — nothing makes collection "first"
  except convention.
- **Guardrail enforcement** — `compose_phase_1_enforcement` / `Phase1EnforcementResult`,
  the composition of regime-resolved risk parameters + drawdown-tier overrides. An **orphan
  ordinal**: there is no "phase 2 enforcement"; the rule-evaluation step that follows is
  "the engine."
- **Distillation** — a genuine seven-step ordered sequence (`_run_phase_2`,
  `# Phase 1 — Class B refresh` … `# Phase 7 — brief-store population`), where every step
  already carried a behavior label next to its number.

A reader hitting `phase1_completed_at`, `_update_row_phase1`, or *"the function the
orchestrator's Phase 2 calls"* learns nothing without chasing the convention, and the
execution `phase1` collided head-on with the guardrail `phase 1` inside the same layer.

## Decision

**Code steps are named for what they do; no construct is named `phase N`.** The behavior
label is the name, not an annotation on an ordinal.

- Execution: `fill_collection` / `command_execution` (DB columns, modules, summaries,
  progress labels, API fields, UI labels — full stack).
- Guardrails: `active_guardrails` (`compose_active_guardrails` / `ActiveGuardrails`).
- Distillation: the seven ordinals dropped; each step keeps only its behavior label
  (`_run_phase_2` → `_compute_category_indicators`).

**Boundary:** the rule governs *code constructs*. Project-lifecycle milestones
(`project-tracker.md` "Phase 0–3 design", "Phase 4 — maturation") stay ordinal — they are
sequential PM milestones like fiscal quarters, with no call site and no behavior to name.
"Two-phase invocation" is also retained: it describes the two-transaction *structure* (as
in "two-phase commit"), not an ordinal label for either transaction.

## Why

Behavior names carry their meaning to every call site for free; ordinals force a lookup and
invite collisions across subsystems. The names already existed — the design docs head the
execution writes "Collect" / "Execute", the `invocations` table already had
`fill_collection_summary_json` / `command_execution_summary_json` beside the `phase1/2`
timestamps — so this finishes a half-applied convention rather than inventing vocabulary.

## Consequences

- We give up the ordered-sequence signal the distillation `Phase 1…7` ordinals conveyed
  ("phase 4 runs after phase 3"). Accepted: each step's label plus its source order carries
  enough; the numbers were redundant with the labels already present.
- The execution rename is a schema change — `phase1_completed_at` /
  `phase2_completed_at` → `fill_collection_completed_at` / `command_execution_completed_at`
  via an Alembic column rename — and propagates through raw SQL, the JSON API contract,
  the frontend types, and operator-facing UI labels; it lands atomically.
- Canonical vocabulary recorded in [CONTEXT.md](../../CONTEXT.md) § Invocation lifecycle;
  the `_Avoid_` lines pin `phase 1` / `phase 2` as rejected, the same discipline as
  "Mirror (rejected)".
- Living docs ([0005](0005-single-writer-by-construction.md) and the
  `05-execution-layer/` design set still say "Phase 1/2") are swept; dated audit snapshots,
  `_archive/**`, and completed tracker lines are left as written.
