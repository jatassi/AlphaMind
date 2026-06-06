---
name: implement-issue
description: Use to implement a single Linear issue end-to-end — read the issue, take a feature branch, implement via TDD, run `code-review`, address feedback, open a PR, run `/review`, address feedback, land. Triggers on `/implement-issue ALP-XXX` and operator phrases like "implement ALP-525", "ship ALP-525", "land ALP-525", "do ALP-525", "build out ALP-525", "fix ALP-525". Requires the issue body to be implementation-ready per `docs/agents/implementation-ready-issue.md` (a spec with Scope + Acceptance criteria); if it isn't yet, run `/refine-issue ALP-XXX` first. Do NOT use for parent feature Issues — use `/orchestrate`. Do NOT use for issues without a spec (refine them first) or for multi-issue work.
---

# Implement a single Linear issue

The operator names a single Linear issue (e.g., `ALP-525`). Read it, implement in the main conversation context, run `code-review` on the local diff and address its feedback, open a PR, spawn a `/review` subagent and address its feedback, land, notify.

## Hard rules

Register as `TaskCreate` entries up front.

- **Always enter an isolated worktree before reading any files or making code changes.** Use `EnterWorktree` immediately after resolving the issue (Pre-flight step 1) and before reading the issue's Reading list — every file read happens from inside the worktree, never the primary checkout. The PR's branch name is the Linear `gitBranchName` from the issue. Never work on `main`.
- **Implement in the main thread.** No `Agent` dispatch for the implementation. Only the post-PR `/review` is an explicit `Agent` dispatch; `code-review` runs in the main thread via `Skill`.
- **Drive the work with TDD** via `Skill("tdd")`: red → green → refactor. Each acceptance criterion that admits a programmatic test gets one.
- **Run the full lint chain before opening the PR and again before merging:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports`. **Do NOT run the full pytest suite locally** — per CLAUDE.md "Testing", CI (`.github/workflows/ci.yml`) is the authoritative full-suite gate on every PR. Use scoped pytest during implementation (`uv run pytest tests/<area>/ -n auto` or single test node-id) and rely on the CI run for the wave-spanning check.
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
- The body isn't implementation-ready per `docs/agents/implementation-ready-issue.md` (no Scope or Acceptance criteria, unresolved open decisions, a `Surface to operator` gate, or a reported-but-uninvestigated prod bug) — stop, suggest `/refine-issue <id>`. Don't implement against guesses.

### 2. Enter a worktree on the feature branch

Do this **before reading any repo files** — the Reading list (step 3) is read from inside the worktree, not the primary checkout. The only thing that precedes it is `get_issue` (a Linear API call, no file access), which yields the `gitBranchName` you need here.

```
EnterWorktree({name: "<short-issue-id>"})   # e.g., "alp-525"
```

`EnterWorktree` branches from `origin/main` (the default `worktree.baseRef: fresh`), creates `.claude/worktrees/<short-issue-id>/`, and switches the session into it on branch `worktree-<short-issue-id>`. The Linear `gitBranchName` is typically too long (>64 chars) to pass directly to `EnterWorktree`, so pick a short slug for the worktree and rename to the canonical branch before pushing so the PR uses the Linear name:

```bash
git branch -m <gitBranchName-from-issue>
git push -u origin <gitBranchName-from-issue>
```

Run every subsequent command — and every file read below — from inside the worktree. Do not `cd` to the primary repo.

**Stale `index.lock` recovery.** If a git command fails with `Unable to create '.../.git/index.lock': File exists`, run `rm -f .git/index.lock && <git-command>` on one line — the watcher re-acquires the lock within ~1s.

### 3. Read the issue body and Reading list

From inside the worktree, read the body end-to-end. Then read every file in the Reading list end-to-end.

Pause and surface if (in each case, `/refine-issue <id>` is the fix — it sharpens the spec to ready before you build):

- The spec has unresolved gaps (e.g., a `Surface to operator` gate).
- Acceptance criteria reference symbols or files that don't exist.
- The Reading list references a sibling issue whose typed contract the spec assumes — verify the contract matches.

### 4. Set up the task list

`TaskCreate` once, up front. Two groups:

- **Implementation tasks** — one per acceptance criterion or named deliverable in Scope. Cluster related criteria into one task; split a multi-faceted deliverable into multiple.
- **Completion-sequence tasks** — register all eight before work begins:
  1. `Run code-review on the local diff`
  2. `Address code-review feedback`
  3. `Open PR to main`
  4. `Spawn /review subagent against the PR`
  5. `Address /review feedback`
  6. `Land PR and clean local git state`
  7. `Send PushNotification`
  8. `Write final writeup`

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

**Test-quality constraints** (canonical rules in `CLAUDE.md` "## Testing" → "### Test-quality rules" and `docs/agents/testing.md`):
- Mock only at the four sanctioned boundaries (LLM / Claude Agent SDK, broker API, system clock, database) — not internal collaborators.
- Coverage is a floor: ask "does this test fail for a reason no other test fails for?" before adding a test.
- Refactor step includes the tests — consolidate and delete scaffolding the coarser tests subsume.
- Architecture/purity invariants go in `.importlinter` / ruff rules, not in ceiling or source-grep tests.

On blocker mid-implementation — schema gap, ambiguous spec, sibling-issue primitive missing or shaped differently than expected, test that won't pass without scope creep — **stop and surface**.

### Lint + scoped-test chain

After green:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run lint-imports
uv run pytest tests/<issue-area>/ -n auto       # scoped — NOT the full suite
```

The pytest invocation is **scoped to the issue's tests** — typically the single test file or the immediate parent directory. Per CLAUDE.md "Testing", the full pytest suite is not run locally; CI (`.github/workflows/ci.yml`) runs it on every PR push and is the authoritative gate. Use `-n auto` to keep the scoped run fast.

**Do not pipe `pytest` to `tail`** — the pipe's exit code is `tail`'s (always 0), masking failures. Use `uv run pytest <scoped-path> -n auto; echo "exit=$?"` and read the summary line.

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

### 1. Run code-review on the local diff

Run `code-review` in the main thread via `Skill("code-review")` against the feature branch's diff (`main..HEAD`) — this pass runs **before** the PR exists, on the local diff. Capture findings as a list.

**Effort selection.** `code-review` accepts an effort level — `low`/`medium` yield fewer, higher-confidence findings; `high`/`max` broaden coverage at the cost of more uncertain findings. Default to **medium**. Bump to **high** when the change is substantial (multi-file feature, schema or persistence contract edit, algorithmic seam, anything where a missed correctness bug would be expensive to recover from). Drop to **low** for one-file mechanical edits, doc-only changes, or surgical bug fixes where you only want the highest-confidence signal. Do not pass `--comment` — the PR does not exist yet at this step, so there is nothing to comment on; you apply the findings yourself on the feature branch in step 2.

### 2. Address code-review feedback

Assess each finding with bias toward acceptance. Reject only with explicit reason. Apply directly on the feature branch.

Re-run the lint chain plus scoped pytest:

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports
uv run pytest tests/<issue-area>/ -n auto
```

The pytest invocation stays scoped per CLAUDE.md "Testing"; the full-suite verification happens in CI when you push.

Commit (`fix: address code-review findings on <issue ID>`), push.

### 3. Open PR to main

```bash
gh pr create --head <feature-branch> --base main --title "<concise summary>" --body "$(cat <<'EOF'
## Summary
<2–4 bullets drawn from the issue's Goal>

Closes <Linear issue URL>.

## Test plan
- [ ] CI (`.github/workflows/ci.yml`) green on the PR — lint on Linux + full pytest on Windows
- [ ] Local lint chain clean (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`)
- [ ] <issue ID> acceptance criteria all met

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the PR URL and PR number.

### 4. Spawn /review subagent against the PR

Spawn `/review` as a background subagent against the now-open PR, then **wait for it to return** before addressing its feedback (nothing runs concurrently — this pass is sequential after `code-review` and the PR open):

```
Agent({
  subagent_type: "general-purpose",
  description: "[Opus] /review PR <PR number>",
  model: "opus",
  prompt: "Run the `review` skill (`Skill('review')`) against PR #<PR number> at <PR URL>. Report findings as a structured list of suggestions, each tagged with severity (blocker / suggested / nit). Do not push commits or modify the PR.",
  run_in_background: true
})
```

**Verify the working branch after `/review` returns.** `/review` may check out the PR branch (`pr-<N>`) when it inspects the diff; that can leave the primary on something other than the feature branch when control returns. Run `git branch --show-current` and `git checkout <feature-branch>` if it doesn't match — the bar is "on the feature branch," not "not on `main`." System-reminder file snapshots taken on a different branch show that branch's file content, which can mislead the address-feedback pass into editing stale state. The destructive failure mode is applying review fixes to off-branch file content, then committing the merged result over the feature branch.

Do not push commits while the `/review` subagent is still running.

### 5. Address /review feedback

Assess each `/review` finding with bias toward acceptance. Reject only with explicit reason. Apply directly on the feature branch.

Re-run the lint chain plus scoped pytest:

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports
uv run pytest tests/<issue-area>/ -n auto
```

The pytest invocation stays scoped per CLAUDE.md "Testing"; the full-suite verification happens in CI when you push.

Commit (`fix: address /review findings on <issue ID>`), push.

### 6. Land PR and clean local git state

**Wait for CI green on the post-feedback push — and iterate until it IS green.** The address-feedback push triggered a fresh `ci` workflow run; that is the authoritative full-suite gate per CLAUDE.md "Testing". This is an iteration loop, not a one-shot wait. Watch the run **by ID** per CLAUDE.md "Watching CI on a PR" — `gh pr checks --watch` invoked right after `git push` exits early on the empty pre-registration window:

```bash
sleep 5
RUN_ID=$(gh run list --branch <feature-branch> --workflow ci.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

1. Watch to completion.
2. **If green:** proceed to merge.
3. **If red:** read the failure log via `gh run view "$RUN_ID" --log-failed --job <job ID>` (the failed-job ID is printed by `gh run watch`). Diagnose. Fix on the feature branch. Commit with `fix: address CI <category> failure in <area>`. Push. Re-fetch `RUN_ID` and watch the fresh run.
4. **Do not merge while CI is red on the latest pushed commit.** Do not declare the issue done. The implementation is not complete until CI is green on whatever commit will land at merge time.

**Failure-class diagnosis:**

- **Lint failure** — fix on the feature branch, commit, push, re-watch.
- **Test failure on Windows that you can't reproduce locally** — Windows-specific (path separators, file-handle behavior, line endings, timezone-naive datetime drift, signal handling, `cp1252` default text encoding, missing env vars CI doesn't have, Unix-only stdlib modules like `fcntl`). Read the failing test's traceback from the CI log; reproduce locally if you can; fix; push; re-watch.
- **Test failure that looks flaky** — re-run via `gh run rerun <run ID> --failed`. If it persists, treat as real (test-order dependence — bisect per CLAUDE.md "Testing"). Fix the offending test or production code, push, re-watch.
- **Pre-existing failure orthogonal to this issue** — verify by checking the most recent push-to-main CI run on origin/main. If reproducible on `main`, surface to the operator, open a Linear "To-dos" issue with symptom + scope, and xfail/skip the test in this PR with `reason="ALP-<new-issue-id>: ..."` so this PR's CI goes green without masking the bug. Do NOT silently downgrade an in-scope failure to "pre-existing" — verify against main first. Do NOT xfail without an open tracking issue.

Server-side branch protection is not enforced (CLAUDE.md "Branch policy") but the policy still holds: do not merge on red.

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

### 7. PushNotification

```
PushNotification({
  title: "Issue complete: <issue ID> — <title>",
  body: "PR <PR number> merged. <any caveats — warranted lint suppressions, deferred follow-ups>"
})
```

### 8. Final writeup

The operator may have stepped away during execution. Conclude with a short writeup so they can pick up cold on return. Output it as your final user-facing message — no separate file, no preamble.

Four sections, in order, each with this exact heading:

- **Summary of the issue as reported.** One or two sentences on what the Linear issue described — the symptom and scope, not the spec.
- **Findings after investigating.** What the code actually showed: where the problem lived, which spec assumptions held vs. broke, anything surprising. Cite `file:line` when it tightens the story.
- **The fix, in plain English.** No code blocks, no jargon dump. What changed and why, at the level a non-implementer can follow.
- **What should now be improved.** The functional upside of this change — what the system should now do better as a result. Frame in terms of observable behavior (e.g., "X no longer mis-classifies Y", "downstream consumers of Z now get a non-null signal"), not implementation details.

A handful of sentences per section. Brevity beats completeness — the operator can read the diff for detail.

## In-flight discoveries

While implementing, it's likely that you may discover latent bugs, missing wiring, duplicative implementations, dead code, or other anomalies. These discoveries may or may not block or complicate your scope of work. If a discovery does block or complicate your work, **stop and report it, do not attempt to work around it**. Even if the discovery doesn't directly affect your work, **you must report it upon completion**.

## Boundaries

- Do not push to remote branches other than the feature branch until the PR merges.
- Do not push directly to `main`. Per CLAUDE.md "Branch policy", main is PR-only; the convention is not server-enforced but is treated as a hard rule.
- Do not declare the issue `Done` without scoped `uv run pytest tests/<issue-area>/ -n auto` green, lint chain clean, every acceptance criterion verified, CI green on the PR, and the PR merged. The full pytest suite runs in CI — do NOT run it locally.
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
- Reading any repo file before entering the worktree — `EnterWorktree` comes immediately after `get_issue` and before the Reading list.
- Opening the PR before running `code-review` and addressing its feedback — `code-review` runs first, on the local diff.
- Spawning `/review` before the PR exists — `/review` reviews the open PR, after `code-review` and the PR open.
- Running `/review` and `code-review` concurrently — they are sequential now: `code-review` on the local diff first, then `/review` on the PR.
- Passing `--comment` to `code-review` — it runs before the PR exists, so there is nothing to comment on; apply the findings yourself on the feature branch.
- Pushing partial address-feedback commits while the `/review` subagent is still running.
- **Running the unscoped full pytest suite locally.** CLAUDE.md "Testing" forbids this by default — CI runs the full suite on PR. Per-implementation local pytest stays scoped to the issue's area.
- **Merging the PR without waiting for CI green.** Server-side branch protection isn't enforced but the policy holds. The CI run is the authoritative full-suite gate; merging on red defeats the regime.
- **Walking away from a red CI run.** The step is an iteration loop, not a one-shot wait. Failures from this issue's changes get fixed and re-pushed until CI is green on the latest commit. Pre-existing failures orthogonal to this issue get a Linear ticket + xfail with the ticket ID. There is no "merge red" path.
- Mocking what you don't own.
- Trusting your own self-report. Verify with `git log main..HEAD` and `git status`.
- Acceptance criteria at the granularity of "the module works" — surface as underspecified before implementing.
