You are orchestrating completion of the AlphaMind risk-guardrails Rules & Limits feature. Five user stories live at `docs/implementation/06-risk-guardrails/rules-and-limits/` (files `01a-...md` through `01e-...md`). All five are independent — none depends on another — and dispatch in a single parallel wave. Each story is self-contained — read it before you act on it.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use the `Agent` tool (`subagent_type: general-purpose`, `isolation: "worktree"` always) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — frontmatter updates, file-existence checks, trivial union merges between parallel branches.

**Model selection per CLAUDE.md.** For mechanical changes use Sonnet; for everything else use Opus. The split for this work tree:

- **Opus on every story.** The five stories all carry behavioral spec depth — predicate logic with edge cases (`01b`, `01c`, `01e`), file-system atomicity (`01d`), cross-cutting helpers (`01a` joins the registry with `ResolvedConfig.rule_values`). None is mechanical. The cost of Opus is justified.

Always include the model name in the `Agent` tool's `description` field per CLAUDE.md: `[Opus] Implement story 01a — rule registry`. Per the user's memory, this makes model selection visible at a glance.

**Run independent stories in parallel.** All five stories' "Depends on" sections list "None" — they read configuration-management outputs (`ProfileConfig`, `GuardrailsConfig`, `ResolvedConfig`) but not each other. Dispatch them as a single five-way parallel wave. Send one `Agent` tool call per story in a single message.

**Each story runs in its own worktree.** With `isolation: "worktree"`, the harness creates a fresh branch + checkout from current `main`, runs the subagent there, and returns the branch name and worktree path on completion (or auto-cleans if no changes were made). Subagents commit on that branch; you merge into `main` after verification. Never run subagents on the main checkout — parallel stories would collide on the working tree.

**Source of truth: story frontmatter.** Each file's frontmatter (`status`, `completed_date`, `commit_id`) is the canonical record. Maintain it. Before dispatching a story, set `status: in_progress`. After verifying acceptance criteria, set `status: done`, fill `completed_date` (`YYYY-MM-DD`), fill `commit_id` (the SHA of the final commit closing the story).

**Verify before marking done.** A story is `done` when every acceptance-criteria checkbox passes a verification step you can describe — typically `uv run pytest -n auto` plus a spot-check that the criterion's outcome holds (file exists, function exhibits the documented behavior, validator raises on a documented mutation). Do not trust the subagent's self-report alone.

## Dispatching a story

Each implementation subagent receives a prompt of this shape:

```
Implement story <ID> at `docs/implementation/06-risk-guardrails/rules-and-limits/<file>.md`.

You are running in an isolated git worktree on a fresh branch. Commit your work there; the orchestrator merges to `main` after verification. Do not push, switch branches, or merge yourself.

Read the story file first. It names the design docs to read, the dependencies, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes. Then run the linter chain per CLAUDE.md and address all findings from your changes only:

  uv run ruff check .
  uv run ruff format .
  uv run mypy

Per CLAUDE.md, alert the orchestrator before disabling the linter or any rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`). Do not silently suppress.

When done:
1. Run `uv run pytest -n auto` and confirm green.
2. Make the final commit including all changes.
3. Report back: the list of commit SHAs you made (most recent last) and a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z").

If you hit a blocker — schema gap, ambiguous spec, test that won't pass without scope creep, design-doc cross-reference that doesn't resolve, missing dependency (e.g., `ruamel.yaml` not in `pyproject.toml` for story 01d) — stop and report. Do not improvise.

The orchestrator updates the story's frontmatter after verifying your report. Do not edit the story file.
```

## Status tracking loop

Each cycle:

1. **Survey.** `rg "^status:" docs/implementation/06-risk-guardrails/rules-and-limits/` lists current statuses. With five stories all `not_started` and all unblocked, the first cycle dispatches the full wave.
2. **Dispatch.** Set `status: in_progress` on each, commit (`chore: dispatch 01a-01e`), then send five `Agent` calls in a single message. Always tag the model name in the `description` per CLAUDE.md.
3. **Verify.** Each agent result includes the worktree path and branch name. For each:
   - `cd` into the worktree and run `uv run pytest -n auto` to confirm green (first run pays a one-time `uv sync` cost for the fresh `.venv`).
   - Run `uv run ruff check .` and `uv run mypy` to confirm the linter chain is clean for the changed files.
   - Spot-check non-test acceptance criteria against the worktree state — every story lands a new module under `src/alphamind/risk_guardrails/rules_and_limits/` plus new entries in the package's `__init__.py` re-export list.
   - On pass:
     a. From the main checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts. **Expect every parallel pair to conflict on `src/alphamind/risk_guardrails/rules_and_limits/__init__.py`** — five branches all add re-exports there. Resolve by union (keep every story's re-exports). The conflict is mechanical; you handle it directly without re-dispatching.
     b. Update frontmatter on `main` (`status: done`, `completed_date`, `commit_id` = the SHA now on `main` for this story's final commit), commit (`chore: mark story <ID> done`).
     c. Clean up: `git branch -d <branch>` and `git worktree remove <path>`.
   - On fail: remove the worktree (`git worktree remove --force <path>` and `git branch -D <branch>`) and re-dispatch with the specific gap noted; a fresh worktree will be created.
4. **Repeat** until all five are `done`.

## Critical-path note

The dependency graph is trivial:

```
        ┌───────────────────────────────┐
       01a  01b  01c  01d  01e   (parallel — all unblocked at start)
        └───────────────────────────────┘
```

The longest path is one story. Five-way parallelism collapses wall-clock to a single sequential agent run plus the merge-and-verify cycle.

## Communication with the user

Terse. After each batch dispatch returns: one line per story — ID, status, commit SHA prefix, any blockers. After the full wave completes: a single summary message naming the final state.

Surface blockers immediately, do not work around them:

- **`ruamel.yaml` dependency for story 01d.** The story prefers `ruamel.yaml` for the `main.yaml` round-trip rewrite (preserves comments). If it is not in `pyproject.toml`, the story's Notes name a fallback (targeted regex rewrite of just the `active_profile:` line). The subagent should surface a preference for adding the dependency rather than silently choosing the fallback; you decide whether to amend `pyproject.toml` or accept the regex approach.
- **`StrEnum` import surface.** Stories `01b`, `01c`, `01e` use `StrEnum` (Python 3.11+). Verify `pyproject.toml`'s Python version pin. The configuration-management feature uses `StrEnum` already (e.g., `EnforcementTier` in `models/guardrails.py`), so this should be a non-issue; if a subagent reports an import failure, the cause is almost certainly an environment mismatch rather than a Python-version issue and warrants a `uv sync` retry.
- **`risk_priority` enum value drift.** Story 01e keys off `RiskPriority.signal_quality`. The configuration-management story 04a's Notes lock the four members at `signal_quality`, `concentration_management`, `exposure_management`, `exposure_and_execution_management`. If a subagent on 01e cannot resolve `RiskPriority.signal_quality` (because the enum landed under a different snake_case form), surface — do not guess.
- **`ProfileConfig.capital_range_usd` semantics for stories 01b / 01d.** The configuration-management story 04a typed it as `tuple[int, int]` with `[lower, upper]`. Story 01b reads both bounds; story 01d does not depend on the bounds at all but reads the profile filename derived from the `Profile` enum value. A subagent finding the typing as something else (e.g., `list[int]`) should surface — the resolver and validators rely on tuple semantics.
- **Activity-log table absence for story 01d.** Story 01d ships a *factory* for the activity-log entry payload (`build_profile_switch_activity_log_entry`). The actual SQLite `activity_log` table does not exist yet (the persistence machinery is forthcoming under the execution layer). The factory returns a dict; persistence integration is explicitly out of scope. If a subagent attempts to write to a non-existent table, redirect it to the factory-only contract.
- **Cross-doc edits.** The five stories collectively land new code under `src/alphamind/risk_guardrails/rules_and_limits/`. None of them should edit `docs/design/06-risk-guardrails/rules-and-limits.md` or any sibling design doc. If a subagent proposes a design-doc edit to reconcile a found inconsistency, surface — design-doc edits are operator decisions, not subagent improvisation.

## When you handle work directly

Skip delegation only when overhead exceeds the work:

- Frontmatter status updates between dispatches.
- Reading files to plan the next batch.
- Resolving the predictable `__init__.py` re-export-list conflicts when the five parallel branches finish out of order — the merges are syntactic union.
- Updating the project tracker entry in `docs/project-tracker.md` when all five stories are done (replace `_requirements pending_` with `_done_` and add the `[stories]` link).

For everything else, delegate.

## Boundaries

- Do not push to remote — the user owns push timing.
- Do not amend commits — create new commits instead.
- Do not skip hooks (`--no-verify`, `--no-gpg-sign`).
- Do not dispatch a subagent without `isolation: "worktree"` — parallel work on the main checkout corrupts state.
- Do not declare a story `done` without `uv run pytest -n auto` green, `uv run ruff check .` clean, `uv run mypy` clean, and a spot-check of every acceptance criterion.
- Do not modify story files except for frontmatter updates after verification.
- Do not pick up a story whose dependencies are not all `done` (vacuously true here — every story's `Depends on` is `None`, but the rule remains in force for any future stories added to this work tree).
- Do not let a subagent disable a linter rule in any form (`ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`) without alerting the user first per CLAUDE.md. If the subagent reports having done so, treat the story as failed verification and re-dispatch with explicit instructions to remove the suppression.
- Do not let a subagent edit a sibling story file's content (only its own frontmatter is fair game, and only after the orchestrator's verification — actually only the orchestrator edits frontmatter, never the subagent).
- Do not coordinate cross-doc edits silently. If a subagent on any story needs to update `docs/design/06-risk-guardrails/rules-and-limits.md`, `state-delivery.md`, `breach-behavior.md`, `regime-adaptation.md`, or any other design doc to reconcile a found inconsistency, surface the conflict before the subagent writes the edit. Cross-cutting design-doc edits are operator decisions.
