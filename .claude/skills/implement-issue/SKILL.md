---
name: implement-issue
description: Use to implement a single Linear issue end-to-end — read the issue, take a feature branch, implement via TDD, open a PR, run `/review` and `simplify` in parallel, address feedback, land. Triggers on `/implement-issue ALP-XXX` and operator phrases like "implement ALP-525", "ship ALP-525", "land ALP-525", "do ALP-525", "build out ALP-525", "fix ALP-525". Requires the issue body to be implementation-ready (actionable spec with Scope + Acceptance criteria). Do NOT use for parent feature Issues — use `/orchestrate`. Do NOT use for issues without a spec or for multi-issue work.
---

# Implement a single Linear issue

The operator names a single Linear issue (e.g., `ALP-525`). Read it, implement in the main conversation context, open a PR, run `/review` and `simplify` in parallel, address consolidated feedback, land, notify.

## Hard rules

Register as `TaskCreate` entries up front.

- **Take a feature branch off `main` before any code changes.** Branch name: the Linear `gitBranchName` from the issue. Never work on `main`.
- **Implement in the main thread.** No `Agent` dispatch for the implementation. Only the post-PR `/review` is an explicit `Agent` dispatch.
- **Drive the work with TDD** via `Skill("tdd")`: red → green → refactor. Each acceptance criterion that admits a programmatic test gets one.
- **Run the full lint + test chain before opening the PR and again before merging:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -n auto`.
- **Linear status transitions.** `Todo` → `In Progress` on dispatch; `In Progress` → `Done` after PR merges. On blocker mid-implementation: `Blocked` + `blockedBy` link to the blocking issue.

## Pre-flight

### 1. Resolve the issue

```
get_issue(id="<issue identifier>", includeRelations=true)
```

Capture: title, description body, current status, `blockedBy` relations, `gitBranchName`, parent issue ID, labels.

Surface and confirm before proceeding if:

- Status is `Done` or `Canceled`.
- Status is `In Progress` (another session may be working it).
- `blockedBy` non-empty with at least one blocker not `Done`.
- The issue has children (it's a parent Issue) — stop, suggest `/orchestrate <Feature>`.

### 2. Read the issue body and Reading list

Read the body end-to-end. Then read every file in the Reading list end-to-end.

Pause and surface if:

- The spec has unresolved gaps (e.g., a `Surface to operator` gate).
- Acceptance criteria reference symbols or files that don't exist.
- The Reading list references a sibling issue whose typed contract the spec assumes — verify the contract matches.

### 3. Set up the task list

`TaskCreate` once, up front. Two groups:

- **Implementation tasks** — one per acceptance criterion or named deliverable in Scope. Cluster related criteria into one task; split a multi-faceted deliverable into multiple.
- **Completion-sequence tasks** — register all six before work begins:
  1. `Open PR to main`
  2. `Spawn /review subagent (background)`
  3. `Run simplify in main (concurrent with /review)`
  4. `Address consolidated feedback`
  5. `Land PR and clean local git state`
  6. `Send PushNotification`

### 4. Create the feature branch

```bash
git checkout main
git pull --ff-only
git checkout -b <gitBranchName-from-issue>
git push -u origin <gitBranchName-from-issue>
```

**Stale `index.lock` recovery.** If a git command fails with `Unable to create '.../.git/index.lock': File exists`, run `rm -f .git/index.lock && <git-command>` on one line — the watcher re-acquires the lock within ~1s.

### 5. Mark the issue `In Progress`

```
save_issue(id="<issue ID>", state="In Progress")
```

## Implementation

### Architecture sketch (when scaffolding new modules)

If the issue scaffolds a new package, service, or non-trivial new module, invoke `Skill("python-architecture")` in design mode before writing code, scoped to the new component. Use the brief's package layout, named typed records, and testing seam to anchor implementation.

Skip when the issue is purely additive (one new function in an existing file, a config knob, a doc edit, a bug fix on an existing module).

### TDD loop

Invoke `Skill("tdd")`: red → green → refactor.

- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, NSSM service config) are verified by post-implementation inspection.
- Sociable tests per `python-architecture` P8 — real internal collaborators, faked / in-process I/O substitutes. No mockist tests.

On blocker mid-implementation — schema gap, ambiguous spec, sibling-issue primitive missing or shaped differently than expected, test that won't pass without scope creep — **stop and surface**.

### Lint + test chain

After green:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run lint-imports
uv run pytest -n auto
```

**Do not pipe `pytest` to `tail`** — the pipe's exit code is `tail`'s (always 0), masking failures. Use `uv run pytest -n auto; echo "exit=$?"` and read the summary line.

### Commit and push

`git commit` with a message mirroring in-tree style (`feat(<feature>):`, `fix:`, `refactor:`, `docs:`). Reference the Linear issue identifier in the body. Verify:

```bash
git log --oneline main..HEAD
```

shows at least one commit. If `git status` shows untracked or modified files, the commit didn't happen.

```bash
git push
```

## Completion sequence

### 1. Open PR to main

```bash
gh pr create --head <feature-branch> --base main --title "<concise summary>" --body "$(cat <<'EOF'
## Summary
<2–4 bullets drawn from the issue's Goal>

Closes <Linear issue URL>.

## Test plan
- [ ] `uv run pytest -n auto` green on the feature branch
- [ ] Linter chain clean
- [ ] <issue ID> acceptance criteria all met

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the PR URL and PR number.

### 2 + 3. /review subagent and simplify in parallel

**Spawn `/review` as a background subagent first:**

```
Agent({
  subagent_type: "general-purpose",
  description: "[Opus] /review PR <PR number>",
  model: "opus",
  prompt: "Run the `review` skill (`Skill('review')`) against PR #<PR number> at <PR URL>. Report findings as a structured list of suggestions, each tagged with severity (blocker / suggested / nit). Do not push commits or modify the PR.",
  run_in_background: true
})
```

**Then run `simplify` in the main thread.** Capture recommendations as a list to apply alongside `/review`'s findings.

Do not push commits while either pass is still running.

### 4. Address consolidated feedback

Merge the two finding lists — `/review` may flag a correctness issue in code that `simplify` recommended rewriting; resolve to one positive action per code region.

Assess each finding with bias toward acceptance. Reject only with explicit reason. Apply directly on the feature branch.

Re-run the lint + test chain:

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto
```

Commit (`fix: address /review and simplify findings on <issue ID>`), push.

### 5. Land PR and clean local git state

Wait for CI green on the PR.

**Sweep stale `main`-bearing worktrees before `gh pr merge`.** `git worktree list`, look for orphans checked out to `main` (typical naming: `.claude/worktrees/<random-name>`). Confirm `git -C <path> status --short` is clean, then `git worktree remove -f -f <path>`. The double `-f` overrides the Claude agent harness's lock.

Merge:

```bash
gh pr merge <PR number> --squash --delete-branch
```

Locally:

```bash
git checkout main
git pull --ff-only
git remote prune origin
git branch | grep '^[[:space:]]*worktree-agent-' | xargs -r git branch -D
```

Update Linear:

```
save_issue(id="<issue ID>", state="Done")
```

### 6. PushNotification

```
PushNotification({
  title: "Issue complete: <issue ID> — <title>",
  body: "PR <PR number> merged. <any caveats — warranted lint suppressions, deferred follow-ups>"
})
```

## Boundaries

- Do not push to remote branches other than the feature branch until the PR merges.
- Do not declare the issue `Done` without `uv run pytest -n auto` green, lint chain clean, every acceptance criterion verified, PR merged.
- Do not modify the issue's description — only `state` and `blockedBy`.
- Verify each completion-sequence task `completed` before reporting done.

## Self-improvement

Note moments during execution where reality diverged from what this skill led you to expect — ambiguous steps, Linear/git gotchas you worked around, verification gaps that masked a real failure. Surface them at the end, after PushNotification, as `Where / What / Why`. Skip silently if nothing came up.

## Anti-patterns

- Dispatching the implementation to a worktree subagent.
- Running `simplify` before opening the PR.
- Sequencing `/review` and `simplify` instead of running them concurrently.
- Pushing partial address-feedback commits while `/review` is still running.
- Mocking what you don't own.
- Trusting your own self-report. Verify with `git log main..HEAD` and `git status`.
- Acceptance criteria at the granularity of "the module works" — surface as underspecified before implementing.
