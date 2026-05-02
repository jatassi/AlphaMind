You are orchestrating completion of the AlphaMind risk-guardrails breach-behavior layer. Twelve user stories live at `docs/implementation/06-risk-guardrails/breach-behavior/` (files `01-...md` through `08-...md`, with `04a`–`04d` and `05a`–`05c` parallel-eligible groups). Each story is self-contained — read it before you act on it.

## Cross-feature gates (read first)

Breach-behavior sits alongside the rules-and-limits, guardrail-evaluation, and state-delivery work trees. Two stories here have hard cross-feature dependencies:

- **Story 05b (secondary-breach check)** consumes guardrail-evaluation's `evaluate_proposals` entry point, the `LibraryOutput` shape, and the per-rule projection. Until guardrail-evaluation's stories 04 (projection engine + rule registry) and 05 (`evaluate_proposals` entry point) are `done`, dispatch story 05b against a Protocol stub matching the documented library output shape — the cumulative-tracking, classification logic, and cross-feature integration are all testable in isolation. Replace the stub with a production call in a re-dispatch (or follow-up edit) once the gate clears.

- **State-delivery integration** (one-way; not a dispatch gate but a coordination note): state-delivery's stories 05 and 06 currently declare `HaltState`, `EmergencyTrigger`, and `EmergencyContext` inline. After breach-behavior's story 03 (canonical types) lands, those local declarations should become imports from `alphamind.risk_guardrails.breach_behavior` in a coordinated edit when state-delivery dispatches. Surface this dependency to the user when coordinating the two work trees, but do not gate breach-behavior on state-delivery — the canonical types are owned here.

Survey both sibling work trees' frontmatter via:

```bash
rg "^status:" docs/implementation/06-risk-guardrails/rules-and-limits/[0-9]*.md \
              docs/implementation/06-risk-guardrails/guardrail-evaluation/[0-9]*.md \
              docs/implementation/06-risk-guardrails/state-delivery/[0-9]*.md \
   2>/dev/null
```

If the guardrail-evaluation work tree's stories 04 and 05 are absent or not `done` when story 05b is up for dispatch, alert the user before proceeding and confirm they want to dispatch against a stub.

Cumulative drawdown's progressive-tier configuration is sourced from `config/guardrails.yaml` via the configuration-management work tree — already shipped. Active risk parameter resolution (regime multipliers applied to base values) flows through the resolver — also shipped. No additional configuration-management gates apply here.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when overhead exceeds the work — frontmatter updates, the single-line README edit in story 01, file-existence checks while planning a batch.

**Model selection.** Per CLAUDE.md, mechanical changes go to Sonnet and everything else to Opus. The split here:

- **Sonnet:** 01 (one-line README link).
- **Opus:** 02 through 08. Story 02 (package skeleton + minimal config) is borderline-mechanical, but the YAML loader shape and `BreachBehaviorConfig` Pydantic model want judgment; default to Opus to avoid coordination cost. Stories 03 (canonical types), 04a–04d, 05a–05c, 06, 07 carry algorithmic, schema-conformance, or composition nuance that warrants Opus. Story 08 (E2E) involves fixture construction and integration-test design — Opus.

Tag the model name in every `Agent` tool `description` per CLAUDE.md (`feedback_subagent_title_model_name`).

**Run independent stories in parallel.** Stories 01 and 02 dispatch immediately in one batch. After 02 lands, story 03 dispatches alone (every downstream story depends on the canonical types). After 03 lands, stories 04a, 04b, 04c, 04d dispatch in parallel (four concurrent subagents, each in its own worktree). After 04* land, stories 05a, 05b, 05c dispatch in parallel (three concurrent — 05a depends on 04b; 05b on 04a + 04d; 05c on 04a). After 05* land, story 06 dispatches alone (depends on 04d + 05b). After 06 lands, story 07 dispatches alone. Story 08 dispatches last, after every prior story is `done`.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest -n auto` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, fixture produces the documented output). Do not trust the subagent's self-report alone (`feedback_subagent_must_commit`).

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/06-risk-guardrails/breach-behavior/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, fixture file presence) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only.

When done:
1. Run `uv run pytest -n auto` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by fixture-string comparison Z").

If you hit a blocker — schema gap, ambiguous spec, sibling-work-tree primitive missing or shaped differently than the story expected, test that won't pass without scope creep — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/06-risk-guardrails/breach-behavior/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`. For story 05b, also confirm the cross-feature gate above.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Tag the model name in the `description`.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest -n auto` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — story 01 edits the design README (confirm the link resolves and renders); story 02 lands `config/breach_behavior.yaml` and `BreachBehaviorConfig` (confirm both exist and the loader runs); story 08 commits fixture builder helpers (confirm they import).
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 12 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent — README link)
02 (independent — package skeleton + config)
              ↓
              03 (canonical types & enums)
              ↓
        ┌─────┼─────┬─────┐
       04a   04b   04c   04d              (parallel: zone, drawdown-tier, hard-rejection, position-selection)
        └────┬┴─────┘     │
             ↓            │
       ┌─────┼─────┐      │
      05a   05b   05c     │             (parallel after 04*: halt-state, secondary-breach, emergency-triggers)
       └────┬┴─────────────┘
            ↓
            06                            (engine-originated envelope assembler — needs 04d + 05b)
            ↓
            07                            (margin-call cascade — needs 06)
            ↓
            08                            (E2E verification — needs everything)
```

Wave 1 dispatches stories 01 and 02 in parallel.
Wave 2 dispatches story 03 alone.
Wave 3 dispatches stories 04a, 04b, 04c, 04d in parallel — four concurrent subagents.
Wave 4 dispatches stories 05a, 05b, 05c in parallel — three concurrent subagents.
Wave 5 dispatches story 06 alone.
Wave 6 dispatches story 07 alone.
Wave 7 dispatches story 08 alone.

Regime-transition breach detection (originally scoped here as story 04e) is owned by the regime-adaptation work tree (story 07 there). Breach-behavior consumers import `RegimeTransitionBreach` from `alphamind.risk_guardrails.regime_adaptation.types` directly; this work tree does not redeclare it.

Critical path: 02 → 03 → 04d (or any 04* on the path) → 05b → 06 → 07 → 08 = 7 sequential subagent runs. The widest parallel batch is 4 concurrent subagents in wave 3.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:

- Guardrail-evaluation work tree's stories 04 and 05 absent or not `done` when story 05b is up for dispatch. Surface and pause; the operator decides whether to proceed against the Protocol stub or wait.
- Schema drift between this work tree's expected typed inputs (e.g., `RiskBudgetConsumption`, `ActiveRiskParameterSet`, `LibraryOutput`) and the upstream work tree's actual produced shape. Surface; coordinate a follow-up edit before re-dispatching.
- Test failures the subagent could not resolve.
- Subagent reports of disabled linter rules (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) that aren't trivially-fixable. Per `feedback_lint_suppression_triage`, assess each suppression: trivial-fix unwarranted ones yourself; dispatch for nontrivial fixes; accept warranted ones with a note in the verification report. Do not pause to ask the user except for genuinely ambiguous cases.
- Subagent reports of an untyped or shape-mismatched cross-feature dependency (e.g., the secondary-breach Protocol stub doesn't match the production `LibraryOutput` shape after guardrail-evaluation lands). Surface; the resolution is a coordinated edit between the two work trees, not a unilateral change here.

## When you handle work directly

Skip delegation only when overhead exceeds the work:

- Story 01 dispatch with Sonnet is borderline; if the link edit is trivial, you may inline the edit yourself rather than spawn a subagent. Confirm against CLAUDE.md's "do not delegate trivial work" guidance.
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving trivial conflicts when a subagent's commit fails to apply.

For everything else, delegate.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest -n auto` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done` — and for story 05b, confirm the cross-feature gate too.
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without triaging per `feedback_lint_suppression_triage`. If the subagent reports having suppressed without warrant, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let any breach-behavior primitive perform I/O, persist to a database, write to the activity log, or call the broker. The primitives are pure functions over typed inputs returning typed outputs; persistence and side effects are the continuous monitor's and the OMS's responsibilities. Any I/O in a primitive is an architectural error and the story re-dispatches with the purity invariant emphasized.
- Do not let a subagent invent a typed value object that duplicates an existing `portfolio_state.records.*`, `config.models.guardrails.*`, `regime_adaptation.types.*`, or `state_delivery.types.*` type. Per `feedback_no_inventing_component_names`, every typed input mirrors an existing upstream record; new types only land when they encode a breach-behavior-specific concept (`HaltState`, `EmergencyContext`, `EngineEnvelope`, `EngineGuardrailTriggerRecord`, `HardRejectionPayload`, `PositionSelectionResult`, `CascadeContext`, etc.) absent upstream. `RegimeTransitionBreach` lives in `regime_adaptation.types` (story 02 of that work tree); breach-behavior consumers import directly from there. The canonical types module is story 03's territory; subsequent stories add their own internal value objects (e.g., `PositionLiquidity`, `MarginCallEvent`, `DrawdownSample`) only when no upstream equivalent exists.
- Do not let primitives embed numeric thresholds (70/85/95 zone boundaries, 8/10/12 cumulative drawdown tiers, 30-minute cooldown, 60% velocity threshold, 30-minute window, 95% short trim target, 110% total-short immediate threshold, 3-rule multi-rule breach count, 8-step cascade max). All come from configuration (`BreachBehaviorConfig` and `config/guardrails.yaml`). Per `feedback_avoid_numeric_anchors`. Suppress hardcoding by reviewing changed code against these thresholds and re-dispatching if any constant slips through.
