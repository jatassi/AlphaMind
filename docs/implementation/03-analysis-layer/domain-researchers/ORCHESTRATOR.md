You are orchestrating completion of the AlphaMind domain-researcher layer. Fourteen user stories live at `docs/implementation/03-analysis-layer/domain-researchers/` (files `01-...md` through `12-...md`, with letter suffixes `09a`/`09b`/`09c` for the parallel-eligible sector-prompt group). Each story is self-contained — read it before you act on it.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, the single-line README edit in story 01, file-existence checks.

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (letter suffixes — `09a`/`09b`/`09c`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, validator catches the boundary case, prompt's example output round-trips through the parser). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/03-analysis-layer/domain-researchers/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive code work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, prompt round-trip) are verified by post-implementation inspection.

For story 09a / 09b / 09c (system prompts), use the `agent-system-prompts` skill (`Skill("agent-system-prompts")`) — it is the project skill specifically for AlphaMind agent system prompt authoring per its frontmatter. The skill encodes the tag conventions, anti-pattern catalog, and the round-trip-with-parser discipline these prompts require.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only:

  uv run ruff check .
  uv run ruff format .
  uv run mypy

Per CLAUDE.md, alert the orchestrator before disabling the linter or any rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`). Do not silently suppress.

When done:
1. Run `uv run pytest` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — schema gap, ambiguous spec, test that won't pass without scope creep, design-doc cross-reference that doesn't resolve, distillation work-tree dependency not yet landed — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/03-analysis-layer/domain-researchers/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state. For prompt stories (09a/b/c), the spot-check includes round-tripping the prompt's example output through the parser (story 04) and validator (story 05) — those primitives must already be `done` on `main`, which the dependency graph below ensures.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 14 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
Level 0 (independent):
    01    02    03
                 ↓
Level 1 (deps on level 0):
                 ↓
            04   05   06
       (parser)(val.)(qual loader)
                 ↓
Level 2 (deps on level 1):
                 ↓
   07 ← 02,04,05    08 ← 06    09a,09b,09c ← 04,05
   (harness)        (bundle)   (sector prompts)
                 ↓
Level 3:    10 ← 07, 08
                 ↓
Level 4:    11 ← 10
                 ↓
Level 5:    12 ← 11, 09a, 09b, 09c
```

01, 02, 03 dispatch immediately in parallel — independent. 04 + 05 + 06 unblock once 03 is done, all three run in parallel with each other (05's unit tests construct `SectorBrief` instances directly via Pydantic, so it does not block on 04). 07 + 08 + 09a/b/c form the level-2 batch: 07 needs 02+04+05, 08 needs 06, the three prompts each need 04+05 for their round-trip-test acceptance criteria. The five level-2 stories run in parallel with each other once their respective predecessors land. 10 unblocks when 07 + 08 are done — it unit-tests against stub prompts, so it does not gate on 09a/b/c. 11 follows 10. 12 ties everything together — it needs 11 (the orchestrator) plus 09a/b/c (real prompts the live-SDK verification script consumes).

The longest path is 03 → 04 → 07 → 10 → 11 → 12 (6 stories). Parallelism collapses it; expect total wall-clock to land around 5–7 sequential agent runs if the parallel groups dispatch cleanly.

## Cross-work-tree dependency

Stories 08 and 11 reference types from the distillation work tree (`SectorOutput`, `DistillationOutputs`, `RegimeLabel`'s upstream feed). If the distillation work tree (`docs/implementation/02-distillation-layer/external/`) has not yet completed stories 11a and 12, this work tree's stories 08 and 11 declare a structural type protocol and the wiring is completed when distillation lands. **Surface this dependency at dispatch time** if not yet resolved — do not improvise type definitions that drift from the published distillation contract.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- Distillation cross-work-tree types not yet published (above).
- Schema gaps discovered when a story needs a column that the data layer's collector schema (story 03b of the collector tree) does not carry. Story 06 is the most likely surface for this — `event_calendar` may need a sector column added.
- Design-doc ambiguities (e.g., the rendering format for the qualitative slice in story 08, or the per-section sequential-indexing rule's interaction with empty sections in story 05). Surface the resolution choice; do not silently pick.
- Test failures the subagent could not resolve.
- API key or auth issues that block the verification script in story 12 (`CLAUDE_CODE_OAUTH_TOKEN` setup is operator territory).
- Cross-story coordination needs (e.g., the `field_path` shape on `ParseError` in story 04 must match the shape on `ValidationError` in story 05 because both feed the corrective-retry message construction in story 07). The first relevant story in flight should pin the convention; subsequent stories follow.

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
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without alerting the user first. If the subagent reports having done so, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let a subagent invent component names beyond those documented in the design docs (per the user's memory `feedback_no_inventing_component_names.md`). The story files are the authoritative contract; the subagent's creativity is bounded by them.
