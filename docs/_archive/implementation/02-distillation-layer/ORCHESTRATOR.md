You are orchestrating completion of the AlphaMind distillation layer. Twenty-five user stories live at `docs/implementation/02-distillation-layer/` (files `01-...md` through `17-...md`, with letter suffixes for parallel-eligible groups, plus a contract/emission split inside `14a`). Stories 01–13 implement the external distillation pipeline (config schema, state persistence, calibration framework, normalization, refresh, per-category indicators, regime classification, aggregation, assembly, orchestrator, verification). Stories 14–17 add the threshold-calibration audit and operator-tooling surface that wraps the framework. Each story is self-contained — read it before you act on it.

Two stories are presently `blocked` on the execution-layer state-persistence substrate (no `invocations` or `activity_log` SQL tables yet): `14a-config-change-emission` and `15-emergency-invocation-review-report`. Their contracts and design-doc edits land via `14a-config-change-contract` and the existing 15 spec respectively; emission/CLI wiring waits for the substrate. Do not dispatch them while `status: blocked` — the substrate work is the proper subject of an execution-layer track and is not in this orchestrator's scope.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, the single-line README edit in story 01, file-existence checks.

**Model selection per CLAUDE.md.** For mechanical changes use Sonnet; for everything else use Opus. The split for this work tree:
- **Sonnet:** 01 (one-line README link).
- **Opus:** every other story. Distillation involves algorithmic computations (correlation matrices, regime classification, lead-lag detection, anomaly thresholds), domain-spec interpretation, cross-cutting framework primitives, and the threshold-audit/operator-tooling stories' integration with multiple existing modules — Sonnet's mechanical-change strength is the wrong fit for the bulk of this work.

Always include the model name in the `Agent` tool's `description` field per CLAUDE.md: `[Opus] Implement story 04 — calibration framework`. Per the user's memory, this makes model selection visible at a glance.

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (letter suffixes — `08a`–`08f`, `11a`/`11b`, `14a`/`14b`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, threshold fires at the boundary, design-doc subsection landed). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/02-distillation-layer/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, source-comment presence, design-doc subsection landed) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only:

  uv run ruff check .
  uv run ruff format .
  uv run mypy

Per CLAUDE.md, alert the orchestrator before disabling the linter or any rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`). Do not silently suppress.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — schema gap, ambiguous spec, test that won't pass without scope creep, design-doc cross-reference that doesn't resolve — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/02-distillation-layer/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Always tag the model name in the `description` per CLAUDE.md.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — stories 14a, 15, 16, 17 edit shared design docs (`threshold-calibration.md`, `state-persistence.md`, `command-center.md`); confirm the documented subsections / table rows / event-type entries actually landed.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 24 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent)
02 (independent)        — config schema
03 (independent)        — state schema
                          ↓
                     04 ← 02, 03
                     05 ← 03
                14a-contract ← 02   (config-change activity-log contract; doc + in-memory)
                14a-emit     ← blocked on execution-layer state-persistence substrate
                14b          ← 02   (no-magic-numbers audit, operator tooling)
                          ↓
                     06 ← 04, 05
                     07 ← 03, 04
                          ↓
        ┌─────────────────┼─────────────────┐
       08a 08b 08c 08d 08e 08f               09
        └─────────────────┼─────────────────┘
                          ↓                   ↓
                          10                  15 ← blocked on execution-layer state-persistence substrate
                          ↓
                       11a  11b
                  16 ← 04, 05, 10            (flag-rate reporter, operator tooling)
                          ↓
                          12
                  17 ← 04, 12                (calibration-state snapshot + dashboard panel)
                          ↓
                          13
```

01, 02, and 03 can dispatch immediately in parallel. 04 and 05 unblock once their deps land. `14a-contract` and `14b` unblock once 02 lands and run in parallel — they're operator-tooling / contract work that doesn't extend the critical path. 06, 07 follow the 04/05 wave. The 08* + 09 group is the widest parallelism point — six per-category stories plus the regime story all dispatchable simultaneously. 10 collects. 11a/11b assemble in parallel; 16 dispatches alongside once 04/05/10 are done. 12 orchestrates; 17 dispatches once 12 is done. 13 verifies.

`14a-emit` and `15` stay blocked on the execution-layer state-persistence substrate (no `invocations` / `activity_log` SQL tables yet); they unblock when that substrate ships and are tracked in `docs/project-tracker.md` under the State persistence item.

The longest path is 03 → 04 → 06 → 08a → 10 → 11a → 12 → 13 (8 stories) — unchanged by the threshold-audit additions, which all branch off existing waves. Parallelism collapses it; expect total wall-clock to land around 5–7 sequential agent runs if the parallel groups dispatch cleanly.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- Substrate gaps discovered when a story implicitly assumes infrastructure that doesn't exist (e.g., the original 14a expected an `activity_log` SQL table that lives in the unbuilt execution-layer state-persistence track). Report; if a doc-only / contract slice is tractable in isolation, propose the split rather than expanding scope.
- Schema gaps discovered when a per-category story needs a state-table column the schema (story 03) doesn't carry. Report; coordinate a follow-up Alembic migration story rather than ad-hoc extending.
- Design-doc ambiguities (e.g., the "regime skip" definition discussed in story 09's Notes — VIX-band skip vs. four-label-ladder skip). Surface the resolution choice, do not silently pick.
- Test failures the subagent could not resolve.
- Cross-story coordination needs (e.g., per-ticker block payload convention discussed in story 11a's Notes — `payload["ticker"]` vs. `payload["per_ticker"][ticker]`). The first 08* story in flight should pin the convention; subsequent stories follow. Surface the choice so the user knows the convention.
- Shared-doc edit collisions: stories 14a, 17 (and possibly others) edit `threshold-calibration.md`, `state-persistence.md`, and `command-center.md`. If two stories touch the same file in parallel, expect merge conflicts at FF time — resolve cleanly without losing either change.
- Missing event-type registrations: story 16 reads a flag-event-types registry that the per-category stories (08*) populate. If the registrations differ from what the registry expects, surface and reconcile rather than silently dropping flag classes from the report.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Story 01 (one-line README link).
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving trivial conflicts when a subagent's commit fails to apply.

For everything else, delegate.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done`.
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without alerting the user first per CLAUDE.md. If the subagent reports having done so, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
