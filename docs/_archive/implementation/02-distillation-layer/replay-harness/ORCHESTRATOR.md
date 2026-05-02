You are orchestrating completion of the AlphaMind distillation replay harness. Nine user stories live at `docs/implementation/02-distillation-layer/replay-harness/` (files `01-...md` through `09-...md`). Each story is self-contained — read it before you act on it.

## Cross-feature gate (read first)

The replay harness reuses the live distillation orchestrator (`run_external_distillation`) and every primitive feeding it. Before dispatching this work tree's story 05, the parent distillation work tree (`docs/implementation/02-distillation-layer/` stories 02 through 12) must all be `done`. Stories 01–04 here can dispatch independently of that gate; story 09 depends on a runnable engine, which depends on the gate. Survey the parent's `status:` frontmatter via `rg "^status:" docs/implementation/02-distillation-layer/[0-9]*` before scheduling story 05+ — if any parent story below 12 is not `done`, hold this work tree's stories 05+ until it lands.

If the parent work tree's story 12 (`run_external_distillation`) does not expose a flag to suppress its Phase 6 archive write, story 05 must add one (mentioned in story 05's Notes). That requires a coordinated edit to the parent story 12's spec. Surface this to the user before dispatching story 05; do not modify the parent story's frontmatter or scope without confirmation.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when overhead exceeds the work — frontmatter updates, the single-line README edit in story 01, file-existence checks while planning a batch.

**Model selection.** Per CLAUDE.md, mechanical changes go to Sonnet and everything else to Opus. The split here:

- **Sonnet:** 01 (one-line README link).
- **Opus:** 02 through 09. The skeleton story (02) is borderline-mechanical but the argparse layout, version-module naming, and test design need judgment; default to Opus to avoid coordination cost. The other stories all carry algorithmic, architectural, or integration nuance that warrants Opus.

Tag the model name in every `Agent` tool `description` per CLAUDE.md.

**Run independent stories in parallel.** Stories 01 and 02 dispatch immediately in one batch. After 02 lands, 03 and 04 dispatch in parallel. After 03+04, the chain serializes: 05 → 06 → 07 → 08 → 09. The filename convention reflects this — no letter-suffix groups in this work tree because the runner stories are linearly dependent.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, CLI exits as documented). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/02-distillation-layer/replay-harness/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, CLI help text rendering) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by CLI invocation Z").

If you hit a blocker — schema gap, ambiguous spec, parent-distillation-work-tree primitive missing or shaped differently than the story expected, test that won't pass without scope creep — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/02-distillation-layer/replay-harness/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`. For story 05 onward, also confirm the parent distillation work tree's stories 02–12 are `done` before dispatching.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Tag the model name in the `description`.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — story 01 edits the design README (confirm the link resolves and renders); story 02 lands a CLI stub (confirm `python -m alphamind.distillation.replay_harness --help` runs); story 09 commits binary fixture files (confirm size and presence).
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 9 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent — README link)
02 (independent — package skeleton + CLI stub)
              ↓
        ┌─────┴─────┐
       03           04         (fixture loader, config loader — parallel)
        └─────┬─────┘
              ↓
              05               (engine — gated on parent distillation 02–12 done)
              ↓
              06               (aggregation primitives)
              ↓
              07               (report renderer)
              ↓
              08               (CLI entry point)
              ↓
              09               (E2E verification)
```

01 and 02 dispatch immediately in parallel. After 02, 03 and 04 dispatch in parallel. After 03 and 04 land, the runner chain serializes: 05 → 06 → 07 → 08 → 09. Total wall-clock with the parent gate met: ~7 sequential agent runs (the 03+04 pair collapses one step).

The longest path is 02 → 03 → 05 → 06 → 07 → 08 → 09 = 7 stories. There is no parallelism beyond the 01/02 and 03/04 batches; the runner stories build incrementally on each other's surfaces.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- The parent distillation work tree's story 12 not being `done` (or its archive-suppression flag missing — see Cross-feature gate above). Surface and pause; do not dispatch story 05.
- Schema drift between the parent distillation work tree's state schema (parent story 03) and what the engine expects when it spins up an isolated session. Surface; coordinate a follow-up.
- Test failures the subagent could not resolve.
- Test fixture coupling: story 09 commits binary SQLite files. If the parent distillation work tree's schema changes during this work tree's runtime, the committed fixture goes stale. Story 09's `test_generator_output_matches_committed_fixture` is the canary — re-run the generator and re-commit if it fails.
- Argparse surface evolution: stories 02 and 08 both touch `cli.py`; story 08 explicitly extends story 02's parser. If story 08 dispatches while story 02's tests are still in the parent's main checkout, expect a test-name collision; story 08 owns the renaming.

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
- Do not declare a story `done` without `uv run pytest` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification. The argparse-surface evolution between stories 02 and 08 is an exception called out in story 08's scope; the renaming-of-tests is mechanical and documented there.
- Do not pick up a story whose dependencies are not all `done` — and for stories 05+, confirm the parent distillation work tree's prerequisites too.
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without alerting the user first per CLAUDE.md. If the subagent reports having done so, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let the harness write to or read from the runtime distillation database. Story 05's tests enforce this structurally, but if a subagent reports test failures that suggest reaching for the runtime DB, treat it as an architectural error and re-dispatch with the isolation invariant emphasized.
