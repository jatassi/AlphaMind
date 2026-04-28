You are orchestrating completion of the AlphaMind configuration-management feature. Twenty-one user stories live at `docs/implementation/foundation/configuration/` (files `01-...md` through `08-...md`, with letter suffixes for parallel-eligible groups). Stories 01–02 lay the directory and models-package scaffolding; 03a–03i schema each flat-tail YAML file in parallel; 04a–04e schema each bundle directory in parallel; 05 implements the composition resolver; 06a/06b implement the two cross-cutting validation layers; 07 implements snapshot persistence; 08 ties everything together as the pipeline-facing entry point. Each story is self-contained — read it before you act on it.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, the single-line README edit in story 01, file-existence checks.

**Model selection per CLAUDE.md.** For mechanical changes use Sonnet; for everything else use Opus. The split for this work tree:
- **Sonnet:** 01 (one-line README link). 02 may be Sonnet — it is a pure refactor with zero behavioral change, but the package conversion touches several existing imports; if the subagent reports it as anything but mechanical, fall back to Opus on retry.
- **Opus:** every other story. The 03* / 04* schema stories carry per-file invariants and validator design choices; 05 is algorithmic (cascade arithmetic, feature-flag closure); 06a/06b coordinate across every model and exercise judgment about how to aggregate failures; 07 hashes deterministically with platform-portable atomic writes; 08 is integration-test-heavy with cross-layer failure injection.

Always include the model name in the `Agent` tool's `description` field per CLAUDE.md: `[Opus] Implement story 05 — composition resolver`. Per the user's memory, this makes model selection visible at a glance.

**Run independent stories in parallel.** Each story's "Depends on" section is the canonical eligibility check. The filename convention (letter suffixes — `03a`–`03i`, `04a`–`04e`, `06a`/`06b`) marks parallel-eligible groups. When dispatching parallel stories, send multiple `Agent` tool calls in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, validator raises on a documented mutation). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/foundation/configuration/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, source-comment presence) are verified by post-implementation inspection.

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

1. **Survey.** `rg "^status:" docs/implementation/foundation/configuration/` lists current statuses. Identify `not_started` stories whose `Depends on` are all `done`.
2. **Dispatch.** Group eligible stories by parallelism. Set `status: in_progress` on each, commit (`chore: dispatch <IDs>`), then send one `Agent` call per story in a single message. Always tag the model name in the `description` per CLAUDE.md.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — story 01 edits `docs/design/README.md`; stories 03a–04e land both YAML files and Pydantic models; stories 06a/06b touch validation directories; story 08 lands an integration test against the full shipped tree.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts. Stories 03a–03i all add new modules under `models/` plus new entries in `models/__init__.py`'s re-export list — expect the `__init__.py` re-export list to conflict on every parallel pair. Resolve by union (keep both stories' re-exports). Stories 04a–04e all extend `loaders.py` — same pattern.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all 21 are `done`.

## Critical-path note

The dependency graph for this work tree:

```
01 (independent)        — README link
02 (independent)        — models package skeleton
                          ↓
        ┌──────────────────────────────────┐
       03a 03b 03c 03d 03e 03f 03g 03h 03i  (parallel, all depend on 02)
        └──────────────────────────────────┘
                          ↓
                ┌─────────┴──────────┐
              04a (deps: 03a, 03f, 03i)
              04b (deps: 03f; sequential after 04a — both need 03f, but 04b also reads regime IDs that 04a establishes for cross-doc consistency notes)
              04c (deps: 03i)
              04d (deps: 03f)
              04e (deps: 03c, 03i)
                          ↓
                          05 (deps: 04a, 04b, 04c, 04d, 04e)
                          ↓
                ┌─────────┴──────────┐
                06a                  06b
                          ↓
                          07 (deps: 05; can run in parallel with 06a/06b)
                          ↓
                          08 (deps: 06a, 06b, 07)
```

01, 02 can dispatch immediately in parallel (no shared files). 02 and 01 are independent; 02 unblocks all of 03* / 04*.

The 03* group is the widest parallelism point — nine per-file stories all dispatchable simultaneously. They share `models/__init__.py`'s re-export list and conflict at FF time; the resolution is a union merge.

The 04* group has internal dependencies: 04a (profiles) depends on 03a, 03f, 03i; 04b (regimes) depends on 03f and is order-sensitive after 04a only for review-doc clarity; 04c, 04d, 04e are independent of 04a/04b. Pragmatic dispatch: launch 04a, 04c, 04d, 04e in parallel as soon as their 03* deps are done; launch 04b in the same wave (the 04a-before-04b "dependency" is documentation-only).

05 collects the 04* outputs. 06a and 06b dispatch in parallel after 05; 07 dispatches alongside (also depends on 05 but not on 06a/06b). 08 collects everything.

The longest path is 02 → 03f → 04a → 05 → 06a → 08 (6 stories) — parallelism collapses it; expect total wall-clock to land around 6–8 sequential agent runs if the parallel groups dispatch cleanly.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full critical path completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:
- **Rule-ID convention drift.** Story 03f locks the suffixed convention (`position_max_size_pct`, etc.) over the unsuffixed one in `configuration-management.md`'s worked example. If a subagent on 04a/04b/04d objects to the convention, surface — do not reconcile silently. The lock is in 03f's Notes; downstream stories follow it.
- **Agent-name convention drift.** Story 03i locks `_researcher` (matching `state-persistence.md` and the existing domain-researcher implementation stories) over `_analyst` (in `configuration-management.md`'s worked example). Same surfacing rule.
- **Distillation overlap.** `config/distillation.yaml` and its model are owned by the separate `docs/implementation/02-distillation-layer/02-config-schema.md` story (not_started at the time this work tree was scoped). Do not have configuration-management subagents land `distillation.yaml` — coordinate by dependency: if the distillation story lands first, our story 08's loader integrates the existing model; if our work lands first, the distillation story extends our package layout. Either ordering works; surface if a subagent asserts the file is missing and tries to create it.
- **Existing `agents.yaml` references.** The drafted-but-not-started `docs/implementation/03-analysis-layer/domain-researchers/02-agent-configuration-scaffolding.md` story also creates `config/agents.yaml`. The configuration-management `agents.yaml` (story 03i) supersedes it — the analysis-layer story should be re-scoped to consume the configuration-management `AgentsConfig`. Coordinate when the analysis-layer story dispatches; do not let two work trees create conflicting `agents.yaml` files.
- **Schema gaps in the design.** `configuration-management.md`'s worked examples are not exhaustive; some fields documented in `regime-adaptation.md`, `breach-behavior.md`, `state-delivery.md` are not reflected in the worked YAML examples. Each story's Reading list names the source-of-truth doc; subagents should consult those when the worked example is silent. Surface any case where the docs disagree and one cannot be picked from context.
- **Test fixtures for shipped vs. synthetic YAML.** The shipped `config/` tree must remain valid throughout. Subagents writing failure-injection tests should construct synthetic Pydantic instances or temporary fixture YAMLs in `tmp_path`; do not mutate `config/*.yaml` in tests.
- **Path-resolution pitfalls.** `main.yaml` paths use Windows `%USERPROFILE%` expansion. The loader (story 08) does not resolve these; the upstream pipeline-runtime layer does. If a subagent writes a test that fails because `%USERPROFILE%` is literal, the fix is to construct a fixture `paths.archive` value the test owns, not to add resolution to the loader.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Story 01 (one-line README link).
- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving the predictable `models/__init__.py` and `loaders.py` re-export-list conflicts when stories 03* / 04* finish out of order — the merges are syntactic union.

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
- Do not let a subagent edit a sibling story file's content (only its own frontmatter is fair game, and only after the orchestrator's verification — actually only the orchestrator edits frontmatter, never the subagent).
- Do not coordinate cross-doc edits silently. If a subagent on 03f, 03i, or any other story needs to update `configuration-management.md` or `state-persistence.md` to reconcile a found inconsistency, surface the conflict before the subagent writes the edit. Cross-cutting design-doc edits are operator decisions.
