---
name: implement-issue
description: Use to implement a single Linear issue end-to-end — read the issue, take a feature branch, implement via TDD, open a PR, run `/review` and `code-review` in parallel, address feedback, land. Triggers on `/implement-issue ALP-XXX` and operator phrases like "implement ALP-525", "ship ALP-525", "land ALP-525", "do ALP-525", "build out ALP-525", "fix ALP-525". Requires the issue body to be implementation-ready (actionable spec with Scope + Acceptance criteria). Do NOT use for parent feature Issues — use `/orchestrate`. Do NOT use for issues without a spec or for multi-issue work.
---

# Implement a single Linear issue

The operator names a single Linear issue (e.g., `ALP-525`). Read it, implement in the main conversation context, open a PR, run `/review` and `code-review` in parallel, address consolidated feedback, land, notify.

## Hard rules

Register as `TaskCreate` entries up front.

- **Always enter an isolated worktree before any code changes.** Use `EnterWorktree`; never edit from the primary checkout. The PR's branch name is the Linear `gitBranchName` from the issue. Never work on `main`.
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
- **Completion-sequence tasks** — register all seven before work begins:
  1. `Open PR to main`
  2. `Spawn /review subagent (background)`
  3. `Run code-review in main (concurrent with /review)`
  4. `Address consolidated feedback`
  5. `Land PR and clean local git state`
  6. `Send PushNotification`
  7. `Write final writeup`

### 4. Enter a worktree on the feature branch

```
EnterWorktree({name: "<short-issue-id>"})   # e.g., "alp-525"
```

`EnterWorktree` branches from `origin/main` (the default `worktree.baseRef: fresh`), creates `.claude/worktrees/<short-issue-id>/`, and switches the session into it on branch `worktree-<short-issue-id>`. The Linear `gitBranchName` is typically too long (>64 chars) to pass directly to `EnterWorktree`, so pick a short slug for the worktree and rename to the canonical branch before pushing so the PR uses the Linear name:

```bash
git branch -m <gitBranchName-from-issue>
git push -u origin <gitBranchName-from-issue>
```

Run every subsequent command from inside the worktree. Do not `cd` to the primary repo.

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
- For audit/cleanup issues where the deliverable is verified by re-running the full lint chain (e.g., removing a suppression directive, restoring strict mypy on a directory), TDD is the wrong frame — drive by the failure list (`mypy` / `ruff` output) and converge to zero. No new tests written; existing test suite is the regression guard.

On blocker mid-implementation — schema gap, ambiguous spec, sibling-issue primitive missing or shaped differently than expected, test that won't pass without scope creep — **stop and surface**.

### Lint + test chain

After green:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run lint-imports
uv run pytest --testmon -n auto
```

`--testmon` is mandatory per CLAUDE.md; drop it only for the explicit pre-PR / pre-merge full-suite runs below, or when the change touches `conftest.py` / fixtures / collection hooks or non-Python files tests depend on.

**Do not pipe `pytest` to `tail`** — the pipe's exit code is `tail`'s (always 0), masking failures. Use `uv run pytest --testmon -n auto; echo "exit=$?"` and read the summary line.

**For audit / docs-only issues** where the deliverable is prose making factual claims about counts, structures, or edge classifications, verify each claim against the source after writing. The lint chain validates that the file still parses, not that the prose is correct — a false count or miscategorized edge will sail through ruff/mypy/lint-imports/pytest and survive into the PR. Walk every entity the prose enumerates (each `ignore_imports` line against its source-file site, each LOC count against `wc -l`, each "TYPE_CHECKING only" claim against the actual `if TYPE_CHECKING:` block).

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

### 2 + 3. /review subagent and code-review in parallel

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

**Then run `code-review` in the main thread** via `Skill("code-review")` against the feature branch's diff. Capture findings as a list — *do not apply them as edits* until `/review` returns and the two lists are merged. Both passes are correctness-focused (one against the PR view, one against the local diff); resolve any overlap to a single positive action per code region before editing.

**Effort selection.** `code-review` accepts an effort level — `low`/`medium` yield fewer, higher-confidence findings; `high`/`max` broaden coverage at the cost of more uncertain findings. Default to **medium**. Bump to **high** when the change is substantial (multi-file feature, schema or persistence contract edit, algorithmic seam, anything where a missed correctness bug would be expensive to recover from). Drop to **low** for one-file mechanical edits, doc-only changes, or surgical bug fixes where you only want the highest-confidence signal. Do not pass `--comment` — you consolidate findings with `/review`'s list and apply them yourself; `--comment` would post intermediate findings to the PR before the merge step and create noise the operator would have to triage by hand.

**Verify the working branch after both passes return.** `/review` may check out the PR branch (`pr-<N>`) when it inspects the diff; that can leave the primary on something other than the feature branch when control returns. Run `git branch --show-current` and `git checkout <feature-branch>` if it doesn't match — the bar is "on the feature branch," not "not on `main`." System-reminder file snapshots taken on a different branch show that branch's file content, which can mislead the address-feedback pass into editing stale state. The destructive failure mode is applying review fixes to off-branch file content, then committing the merged result over the feature branch.

Do not push commits while either pass is still running.

### 4. Address consolidated feedback

Merge the two finding lists — `/review` and `code-review` may flag overlapping concerns on the same code region; resolve to one positive action per region.

Assess each finding with bias toward acceptance. Reject only with explicit reason. Apply directly on the feature branch.

Re-run the lint + test chain:

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto
```

Commit (`fix: address /review and code-review findings on <issue ID>`), push.

### 5. Land PR and clean local git state

AlphaMind has no CI worth waiting on — merge as soon as the PR is open and the address-feedback push has landed.

**Sweep stale `main`-bearing worktrees before `gh pr merge`.** `git worktree list`, look for orphans checked out to `main` (typical naming: `.claude/worktrees/<random-name>`). Confirm `git -C <path> status --short` is clean, then `git worktree remove -f -f <path>`. The double `-f` overrides the Claude agent harness's lock.

**Merge.** From inside the worktree, `gh pr merge --delete-branch` exits non-zero after a successful remote merge: `gh` tries to switch the local branch to `main`, which is checked out in the primary worktree, and aborts with `fatal: 'main' is already used by worktree at '<primary>'`. The remote merge and remote-branch deletion both still succeed — verify directly:

```bash
gh pr merge <PR number> --squash --delete-branch
gh pr view <PR number> --json state    # expect "MERGED"
```

**Update `main` against the primary repo, not from inside the worktree.** `git checkout main` from a worktree fails with the same "already used" error; fast-forward and prune via `-C <primary-repo-path>`:

```bash
git -C <primary-repo-path> pull --ff-only origin main
git -C <primary-repo-path> remote prune origin
git -C <primary-repo-path> branch | grep '^[[:space:]]*worktree-agent-' | xargs -r git -C <primary-repo-path> branch -D
```

Leave the implementation worktree on disk; the harness will prompt to keep or remove it at session end.

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

### 7. Final writeup

The operator may have stepped away during execution. Conclude with a short writeup so they can pick up cold on return. Output it as your final user-facing message — no separate file, no preamble.

Four sections, in order, each with this exact heading:

- **Summary of the issue as reported.** One or two sentences on what the Linear issue described — the symptom and scope, not the spec.
- **Findings after investigating.** What the code actually showed: where the problem lived, which spec assumptions held vs. broke, anything surprising. Cite `file:line` when it tightens the story.
- **The fix, in plain English.** No code blocks, no jargon dump. What changed and why, at the level a non-implementer can follow.
- **What should now be improved.** The functional upside of this change — what the system should now do better as a result. Frame in terms of observable behavior (e.g., "X no longer mis-classifies Y", "downstream consumers of Z now get a non-null signal"), not implementation details.

A handful of sentences per section. Brevity beats completeness — the operator can read the diff for detail.

## Boundaries

- Do not push to remote branches other than the feature branch until the PR merges.
- Do not declare the issue `Done` without `uv run pytest --testmon -n auto` green (or `uv run pytest -n auto` without `--testmon` for the pre-PR / pre-merge final-verification runs), lint chain clean, every acceptance criterion verified, PR merged.
- Do not modify the issue's description — only `state` and `blockedBy`.
- Verify each completion-sequence task `completed` before reporting done.

## Self-improvement

Note moments during execution where reality diverged from what this skill led you to expect. Surface after PushNotification.

The bar is "would have saved a step" or "would have prevented a mistake" — generic critiques ("could be clearer", "more context would help", "consider adding X") fail it and must be skipped. Each entry must cite the specific event from this session that exposed the gap: a failed command and its error, a step you re-did, a gotcha you worked around, an acceptance criterion that passed but missed a real bug. If you can't cite the event, the entry is generic — drop it. Skip the section entirely if nothing met the bar; do not pad with affirmations that the skill worked.

Shape per entry:

- **Where:** section heading + the specific paragraph or bullet.
- **What:** the concrete edit — proposed new wording, or "delete this", or "add a bullet after X saying Y".
- **Why:** the cited event (command + error, commit, file:line, or quoted output).

## Anti-patterns

- Dispatching the implementation to a worktree subagent.
- Running `code-review` before opening the PR.
- Sequencing `/review` and `code-review` instead of running them concurrently.
- Passing `--comment` to `code-review` during the pre-merge pass — it posts intermediate findings to the PR and short-circuits the consolidate-with-`/review` step.
- Pushing partial address-feedback commits while `/review` is still running.
- Mocking what you don't own.
- Trusting your own self-report. Verify with `git log main..HEAD` and `git status`.
- Acceptance criteria at the granularity of "the module works" — surface as underspecified before implementing.
