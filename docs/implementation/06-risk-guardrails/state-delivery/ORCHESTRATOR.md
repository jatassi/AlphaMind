You are orchestrating completion of the AlphaMind risk-guardrails state-delivery layer. Ten user stories live at `docs/implementation/06-risk-guardrails/state-delivery/` (files `01-...md` through `08-...md`). Each story is self-contained — read it before you act on it.

## Cross-feature gates (read first)

State-delivery sits on top of two sibling work trees. Their stories must clear before specific stories here can dispatch:

- **`rules-and-limits` work tree** ships the production populator for `RiskBudgetConsumption` and `ActiveRiskParameterSet` (the typed records that flow into `PortfolioStateSnapshot.risk_budget` and `.active_risk_parameters`). The portfolio-state Protocol surface is already typed; the production implementation lives in the rules-and-limits tree. Stories 04a, 04b, 04c, and 08 take these as inputs. **Until the rules-and-limits stories that produce a populated `RiskBudgetConsumption` are `done`, dispatch stories 04a–04c and 08 against hand-constructed fixtures only — the unit tests work fine; the optional real-snapshot variant in story 08 is gated.**
- **`guardrail-evaluation` work tree** ships the deterministic primitives the validation tool composes: per-rule projection (`evaluate_per_rule_projection` or whatever final name), regime parameter resolution, feature-flag early-exit, Black-Scholes greeks, and the canonical `PerRuleResult` typed output. Story 07 (validation tool) and story 08 (E2E) consume these. **Until the guardrail-evaluation stories that produce these primitives are `done`, dispatch story 07 against a stub library — the cumulative-tracking, typed-I/O, and failure-guidance behavior are testable in isolation; replace the stub with the real library in a re-dispatch (or follow-up) once the gate clears.**

Survey both sibling work trees' frontmatter via:

```bash
rg "^status:" docs/implementation/06-risk-guardrails/rules-and-limits/[0-9]*.md \
              docs/implementation/06-risk-guardrails/guardrail-evaluation/[0-9]*.md \
   2>/dev/null
```

If either work tree's stories are absent (no implementation directory exists yet), dispatch this work tree's stories against fixtures and stubs as documented. The orchestrator alerts the user before dispatching stories 04a–04c, 07, or 08 if the upstream work trees are not yet `done`, and confirms the operator wants to proceed against fixtures.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when overhead exceeds the work — frontmatter updates, the single-line README edit in story 01, file-existence checks while planning a batch.

**Model selection.** Per CLAUDE.md, mechanical changes go to Sonnet and everything else to Opus. The split here:

- **Sonnet:** 01 (one-line README link).
- **Opus:** 02 through 08. Story 02 (package skeleton + minimal config) is borderline-mechanical, but the YAML loader shape and the `StateDeliveryConfig` Pydantic model want judgment; default to Opus to avoid coordination cost. The rendering stories (03, 04a/b/c), the wrappers (05, 06), the validation tool (07), and the integration tests (08) all carry algorithmic, contract, or composition nuance that warrants Opus.

Tag the model name in every `Agent` tool `description` per CLAUDE.md (`feedback_subagent_title_model_name`).

**Run independent stories in parallel.** Stories 01 and 02 dispatch immediately in one batch. After 02 lands, story 03 dispatches alone (every renderer depends on 03). After 03 lands, stories 04a, 04b, 04c, 06, and 07 dispatch in parallel — the file-naming convention reflects this (letter suffixes within story 04 indicate the renderer parallelism; stories 06 and 07 are independent of the renderer chain at the code level even though their numeric labels suggest sequence). After 04a/b/c land, story 05 dispatches alone (depends on all three renderers). Story 08 dispatches last, after every prior story is `done`.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest -n auto` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, fixture file content matches the design's worked example). Do not trust the subagent's self-report alone (`feedback_subagent_must_commit`).

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/06-risk-guardrails/state-delivery/<file>.md`.

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

1. **Survey.** `rg "^status:" docs/implementation/06-risk-guardrails/state-delivery/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`. For stories 04a–04c, 07, and 08, also confirm the cross-feature gates above.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Tag the model name in the `description`.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest -n auto` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — story 01 edits the design README (confirm the link resolves and renders); story 02 lands `config/state_delivery.yaml` and `StateDeliveryConfig` (confirm both exist and the loader runs); story 08 commits fixture `.txt` files (confirm size and presence).
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 10 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent — README link)
02 (independent — package skeleton + config)
              ↓
              03 (shared rendering primitives)
              ↓
        ┌─────┼─────┬──────┬──────┐
       04a   04b   04c    06     07         (renderers + emergency + validation tool — parallel)
        └─────┼─────┘      │       │
              ↓            │       │
              05           │       │         (halt-mode wrappers — needs all three renderers)
              └────┬───────┴───────┘
                   ↓
                   08                        (E2E verification — needs everything)
```

Wave 1 dispatches stories 01 and 02 in parallel.
Wave 2 dispatches story 03 alone (every downstream renderer depends on it).
Wave 3 dispatches stories 04a, 04b, 04c, 06, and 07 in parallel — five concurrent subagents, each in its own worktree.
Wave 4 dispatches story 05 alone (after 04a/b/c land).
Wave 5 dispatches story 08 alone.

Critical path: 02 → 03 → (04a/b/c) → 05 → 08 = 5 sequential subagent runs. Stories 06 and 07 ride along in wave 3 without extending the critical path.

The longest critical-path chain is 5 stories. The widest parallel batch is 5 concurrent subagents in wave 3.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:

- Either cross-feature work tree (rules-and-limits or guardrail-evaluation) being absent or its required stories not `done`. Surface and pause; the operator decides whether to proceed against fixtures/stubs or wait.
- Schema drift between this work tree's expected typed inputs (e.g., `RiskBudgetConsumption`, `ActiveRiskParameterSet`, `PerRuleResult`) and the upstream work tree's actual produced shape. Surface; coordinate a follow-up edit before re-dispatching.
- Test failures the subagent could not resolve.
- Subagent reports of disabled linter rules (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) that aren't trivially-fixable. Per `feedback_lint_suppression_triage`, assess each suppression: trivial-fix unwarranted ones yourself; dispatch for nontrivial fixes; accept warranted ones with a note in the verification report. Do not pause to ask the user except for genuinely ambiguous cases.
- Subagent reports of an untyped or shape-mismatched cross-feature dependency (e.g., the validation tool stub doesn't match the production `PerRuleResult` shape after the guardrail-evaluation work tree lands). Surface; the resolution is a coordinated edit between the two work trees, not a unilateral change here.

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
- Do not pick up a story whose dependencies are not all `done` — and for stories 04a–04c, 07, and 08, confirm the cross-feature gates too.
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without triaging per `feedback_lint_suppression_triage`. If the subagent reports having suppressed without warrant, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let any renderer touch the runtime database, the live distillation tables, or the engine's OMS state. The renderers are pure functions over typed snapshots; any I/O in a renderer is an architectural error and the story re-dispatches with the isolation invariant emphasized.
- Do not let a subagent invent a typed value object that duplicates an existing `portfolio_state.records.*` or `portfolio_state.snapshot` type. Per `feedback_no_inventing_component_names`, every typed input mirrors an existing upstream record; new types only land when they encode a state-delivery-specific concept (`HaltState`, `EmergencyContext`, `RegimeTransitionBreach`, `CrossConstraintImpact`, `ValidationToolState`, etc.) absent upstream.
