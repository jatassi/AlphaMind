---
name: orchestrate
description: Use to drive an AlphaMind feature's implementation work tree end-to-end — fetch the parent Linear Issue, dispatch its sub-issues to subagents in dependency-respecting waves, verify each, integrate to a feature branch, then PR → /review → address feedback → land → update project-tracker → notify. Triggers on `/orchestrate <Feature>` and operator phrases like "orchestrate Breach behavior", "implement the Synthesizer feature", "drive Domain researchers to completion", "build out State persistence", "execute the Portfolio manager work tree", "land the X feature". Assumes the work tree has already been drafted via `/draft-user-stories` (parent Issue exists with sub-issues + dependency graph + orchestrator notes). Use this skill whenever the operator asks to implement, build, drive, execute, land, or complete an AlphaMind feature that has a drafted Linear work tree, even if they don't say "orchestrate". Do NOT use for one-off story dispatch (just call `Agent` directly), bug fixes, refactors, or features without a Linear parent Issue.
---

# Orchestrate an AlphaMind feature implementation

The operator names a feature whose Linear work tree has been drafted (typically via `/draft-user-stories`). You drive every sub-issue from `Todo` to `Done`, integrate the result to a feature branch, get it reviewed, land it, and notify on completion.

You are the conductor. Subagents do the work; you sequence them, verify their output, and own the integration story.

## Hard rules (non-negotiable)

These are project invariants that override any default behavior. Track them with explicit TaskCreate entries up front (see [Set up the task list](#set-up-the-task-list)) so none get dropped.

- **Take a feature branch off `main` before any subagent dispatches.** Branch name: `<initials>/alp-<parent-issue-number>-<slug>` matching the Linear `gitBranchName` for the parent Issue. Never let subagents work directly off `main`.
- **Max 6 concurrent active subagents.** When a parallel wave has more than 6 eligible stories, batch into sub-waves of ≤6. Wait for a sub-wave to finish before dispatching the next.
- **Always pass `isolation: "worktree"` and `run_in_background: true` to `Agent`.** Worktree isolation is mandatory per CLAUDE.md and prevents parallel-checkout collisions; background mode is mandatory per CLAUDE.md. You'll be notified as each completes — do not poll.
- **Always tag the model in the `Agent` tool's `description` field** (`[Sonnet] 04a — Zone classifier`, `[Opus] 03 — Canonical types`). Visible-at-a-glance model selection is a CLAUDE.md requirement.
- **Run the full lint + test chain after each parallelized wave merges** to the feature branch. Catches integration issues that pass per-story but fail in combination. Commands: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -n auto`. The `-n auto` is mandatory per CLAUDE.md — never invoke `pytest` without it, including from subagents.
- **A story blocked mid-implementation gets `state="Blocked"` in Linear,** plus a `blockedBy` link to the blocking issue if one exists in Linear (use `save_issue(id=..., state="Blocked", blockedBy=[...])`). If the blocker isn't in Linear, surface it in the final summary instead.
- **Do not skip the post-completion sequence.** PR → /review → address feedback → land → update `docs/project-tracker.md` → clean local git → PushNotification. Add these as tasks before the work begins (see below).

## Pre-flight

### 1. Resolve the feature

The operator's argument names a feature (e.g., `Breach behavior`, `Synthesizer`, `Portfolio manager`).

```
list_issues(team="AlphaMind", query="<Feature name>")
```

Filter for the parent Issue (the one with sub-issues and the description containing design-doc links + dependency graph + orchestrator notes). If no clean match, surface the candidates and ask. If the feature has no parent Issue at all, tell the operator they probably need `/draft-user-stories <Feature>` first.

Then:

```
get_issue(id="<parent ID>", includeRelations=true)
list_issues(team="AlphaMind", parentId="<parent ID>", limit=50)
```

Capture: parent ID, parent description (which contains the dependency graph + orchestrator notes you'll honor), and every sub-issue with its current `status`, `title`, `blockedBy` relations, and the `gitBranchName` from the parent (for your feature branch name).

### 2. Read the orchestrator notes

The parent Issue's description contains a "Notes for the orchestrator" section produced by `/draft-user-stories`. **Read it before dispatching anything.** It contains feature-specific guidance not duplicated in this skill — sibling work-tree gates, model-selection nuances for specific stories, architectural invariants the orchestrator must enforce, surfacing conditions. Treat its instructions as additive to this skill's defaults; when they conflict (rare), prefer the parent Issue's note since it has feature context this skill lacks.

### 3. Sanity-check sub-issue state

Before any dispatch:

- If any sub-issue is already `In Progress`, surface and ask — another orchestrator may be running, or a previous run was interrupted mid-flight.
- If any sub-issue is `Blocked`, surface its blocker and confirm whether to skip or wait.
- If sub-issues' `blockedBy` links don't form a DAG matching the dependency graph in the parent description, surface — the work tree drafted incorrectly and wants a fix before execution.

### 4. Set up the task list

Use `TaskCreate` once, up front, to register everything you must not drop. Two groups:

- **One task per sub-issue** — title `<NN — Title>`, status `pending`. As you dispatch, mark `in_progress`; as you verify and merge, mark `completed`.
- **Completion-sequence tasks** — register all six before the work begins so they cannot be forgotten:
  1. `Open PR to main`
  2. `Spawn /review subagent`
  3. `Address review feedback`
  4. `Update docs/project-tracker.md status to _done_`
  5. `Land PR and clean local git state`
  6. `Send PushNotification summarizing completed work`

### 5. Create and push the feature branch

From a clean `main` checkout:

```bash
git checkout main
git pull --ff-only
git checkout -b <gitBranchName-from-parent-Issue>
git push -u origin <gitBranchName-from-parent-Issue>
```

**Push immediately on creation, before any subagent dispatches.** Wave-1 subagents run the rebase block in their dispatch prompt (`git fetch origin <feature-branch> ; git rebase origin/<feature-branch>`); if `origin/<feature-branch>` does not exist yet, the fetch fails silently and the subagent works off `origin/main` instead. On wave 1 the content is identical, so nothing breaks visibly — but it masks the broken fallback for later resumption or for waves where the same code path matters. Push as part of branch creation, not as part of the wave-end gate.

If the branch already exists locally (resuming a prior run), check it out instead and surface its state.

**Stale `index.lock` recovery.** If any of the above commands fail with `Unable to create '.../.git/index.lock': File exists`, this is almost always a stale orphan from a prior crashed git operation (typical: VS Code's git extension polling `git diff --numstat HEAD`, or your own earlier `git pull` that crashed). Diagnose:

- `lsof <repo>/.git/index.lock` — if no process holds it, the lock is stale.
- `stat -f "%Sm" <repo>/.git/index.lock` (macOS) shows the lock's mtime. Locks older than ~30 seconds with no holding process are stale.

Recovery: `rm -f <repo>/.git/index.lock` and immediately re-run the chained git operation. The watcher polls fast enough that a re-acquired transient lock can re-appear within ~1 second; chain the `rm -f` and the git command on a single line so they execute back-to-back rather than running them as separate Bash calls. The same procedure applies to every git invocation in this skill — wave merges, pushes, worktree removals — not just branch creation. The fix is the same each time.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use `Agent` (`subagent_type: general-purpose`, `isolation: "worktree"`, `run_in_background: true`) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — Linear status updates, frontmatter fixes, single-line README edits, file-existence checks while planning a wave.

**Model selection.** Per CLAUDE.md: mechanical changes → Sonnet; everything else → Opus. Mechanical means: a single-line README edit, a one-file frontmatter change, a verbatim file copy. Anything involving algorithmic judgment, schema design, fixture construction, or test-list reasoning is Opus. The parent Issue's orchestrator notes may flag specific stories that override this default — honor those flags. Tag the model in the `Agent` description as `[Sonnet]` or `[Opus]`.

**Run independent stories in parallel.** The dependency graph is in the parent Issue's description; the per-sub-issue `blockedBy` relations are the source of truth. A story is dispatch-eligible when: its status is `Todo` AND every story it `blockedBy` has status `Done`. Stories at the same dependency rank dispatch together in one wave (one `Agent` call per story, all in the same message), capped at 6 concurrent.

**Each story runs in its own worktree.** `isolation: "worktree"` makes the harness create a fresh branch + checkout **from `main`** (not the feature branch), runs the subagent there, and reports the branch name and worktree path on completion. Subagents must rebase onto `origin/<feature-branch>` before starting work — otherwise they won't see prior waves' commits. The dispatch prompt below includes the rebase block; honor it verbatim. Subagents commit on the worktree branch; you cherry-pick or merge onto the feature branch after verification. Never run subagents on the feature branch checkout itself — parallel stories would collide.

**Push the feature branch to `origin` after each wave's wave-end gate passes**, so the next wave's worktrees can rebase onto its latest tip. Without this, wave-2+ subagents only see content from `main`, missing all prior waves' commits.

**Verify before marking done.** A story is `Done` when every acceptance-criteria checkbox passes a verification step *you can describe* — typically `uv run pytest -n auto` plus a spot-check of each non-test criterion (file exists, schema validates, function exhibits the documented behavior). Do not trust the subagent's self-report alone (`feedback_subagent_must_commit`).

## Dispatching a story

Each implementation subagent receives a prompt of this exact shape (replace `<Linear ID>`):

```
Implement story <Linear ID>.

You are running in an isolated git worktree on a fresh branch. IT IS CRITICAL that you only make changes and commits inside the worktree. ALWAYS use relative paths (e.g. `docs/project-tracker.md`), NEVER use fully-qualified paths (e.g. `/Users/jatassi/Git/AlphaMind/docs/project-tracker.md`). Do not push, switch branches, or merge.

**Verify your base before starting work.** The harness creates worktree branches off `main`. The integration branch for this work tree is `<feature-branch>` (it carries all prior waves' commits). Fetching is allowed; pushing is not. Run:

    git fetch origin <feature-branch>
    if ! git merge-base --is-ancestor origin/<feature-branch> HEAD; then
      git rebase origin/<feature-branch>
    fi

Confirm with `git log --oneline -10` that prior waves' commits are reachable from HEAD before proceeding.

Fetch the user story from Linear and read it first. It names the design docs to read, the scope, and the acceptance criteria. Treat the acceptance criteria as your test list.

Use the `/tdd` skill (`Skill("tdd")`) to drive the work: red → green → refactor.
- Each acceptance criterion that admits a programmatic test gets one.
- Criteria that don't (file existence, doc structure, NSSM service config) are verified by post-implementation inspection.

After tests are green and before your final commit, invoke the `simplify` skill (`Skill("simplify")`) to review and clean up your changes, then run `uv run ruff check .`, `uv run ruff format .`, and `uv run mypy` and address all findings from your changes only.

When done:
1. Run `uv run pytest -n auto` and confirm green.
2. Stage all changes with `git add` and create the final commit. **REQUIRED — DO NOT SKIP.** After committing, run `git log --oneline <feature-branch>..HEAD` and confirm at least one of YOUR commits is listed. If `git status` shows untracked or modified files, you have NOT committed — `git add` and commit them.
3. Report back with **the verbatim output of `git log --oneline <feature-branch>..HEAD`** (paste the exact lines from your terminal, not a paraphrase or summary), followed by a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z"). A report without verbatim git-log output signals to the orchestrator that the commit step was skipped — the orchestrator will reject the report and re-dispatch.

If you hit a blocker — schema gap, ambiguous spec, sibling-work-tree primitive missing or shaped differently than the story expected, test that won't pass without scope creep — stop and report. Do not improvise.

Do not change the status of the user story in Linear; the orchestrator owns status transitions.
```

When dispatching multiple parallel-eligible stories, send all `Agent` calls in a single message (one block per story). Use `description` like `[Opus] 04a — Zone classifier` for visibility.

## Status tracking loop

Repeat each cycle until all sub-issues are `Done` or surfaced as `Blocked`:

### Survey

```
list_issues(team="AlphaMind", parentId="<parent ID>", limit=50)
```

Identify dispatch-eligible stories: status `Todo` AND every `blockedBy` story is `Done`. Stop iterating when no eligible stories remain — either you're finished, or the remaining stories are blocked (real blockers or stale-status drift).

The parent issue's dependency-graph diagram is illustrative, not prescriptive. Do not fix the wave plan up front from the diagram — re-derive eligibility from the actual sub-issue `blockedBy` relations on every survey. Stories at different "depths" in the rendered diagram can land in the same wave whenever their real `blockedBy` lists are satisfied. For example, after the wave that landed three siblings completes, a deep-in-diagram story whose only blocker was one of those siblings becomes eligible alongside a shallow-in-diagram story whose blockers were satisfied earlier — dispatch them together rather than stretching the plan into an extra wave. Compressing the wave plan saves wall-clock time and one full subagent dispatch + verify cycle per compression.

### Dispatch

Group eligible stories by parallelism rank (same rank = same wave). Cap each wave at 6 concurrent.

For each story in the wave:
- Update Linear: `save_issue(id=<sub-issue ID>, state="In Progress")`.
- Add the dispatch `Agent` call to the message.

Send all calls in one message. Each runs in background; you'll receive a notification per completion.

### Verify (per agent result)

Each agent result includes the worktree path and branch name. Per result:

0. **Verbatim-git-log gate.** Before any lint/test, scan the agent's report for the verbatim `git log --oneline <feature-branch>..HEAD` output the dispatch prompt mandates (one or more lines of the form `<short-sha> <commit-message>`). If the report ends with the `simplify` skill's findings, with prose like "All tests pass / I'm done", or with any final message that is NOT at least one such git-log line, the commit step was skipped. Verify directly: `git -C <worktree-path> log --oneline <feature-branch>..HEAD`. If zero commits are listed, the agent did not commit. Decide:
   - **Re-dispatch** when the work has substantive issues (lint failures, missing AC, unwarranted suppressions) on top of the missed commit.
   - **Commit-yourself** when the work is otherwise sound and only the commit step was skipped — assess the lint state with `uv run ruff check . && uv run mypy` against the worktree first, then `git add` + `git commit` with a descriptive message and proceed to step 1.
   Do NOT proceed to step 1 (Tests) on uncommitted state — the agent's "tests pass" claim is unverifiable, and pytest will collect different files than what would land at merge time.
1. **Tests.** `cd` into the worktree and run `uv run pytest -n auto`. First run pays a one-time `uv sync` cost for the fresh `.venv` — that's fine.
2. **Lint.** `uv run ruff check .` and `uv run mypy` against the worktree. Clean for the changed files.
3. **Linter suppressions.** Grep the diff for `# noqa`, `# type: ignore`, `per-file-ignores`, `ignore` keys in `pyproject.toml`. For each suppression, assess whether it was warranted (per `feedback_lint_suppression_triage`):
   - **Trivial-fix unwarranted ones yourself** — directly in the worktree before merging.
   - **Non-trivial unwarranted ones** — re-dispatch the story with explicit instruction to remove the suppression and address the underlying issue.
   - **Warranted ones** — note in your verification report and accept.
4. **Spot-check non-test acceptance criteria.** For each criterion not verified by an automated test, confirm it manually (file exists at the expected path, schema validates against a sample, function signature matches the story's spec).
5. **Architectural integration gaps invisible to stubs.** Stub-heavy unit tests can pass while the framework itself rejects the constructed options at runtime. When a story touches an external SDK or framework's option-shape construction (e.g., `ClaudeAgentOptions.mcp_servers`, `Alembic.Config`'s logger configuration, `pytest` plugins, `pydantic` discriminated unions), confirm at least one test exercises the constructed shape end-to-end — not just stubbing the framework's response. If every test stubs the SDK, the harness can build wrong-shaped options that silently degrade in production (e.g., tools registered with `allowed_tools` but no `mcp_servers` — the LLM emits `<tool_use>` and the SDK returns "tool not found", and the agent falls back to its non-tool path). Add such a test before merging or surface as a follow-on issue.
6. **Decision:**
   - **Pass:** From the feature-branch checkout, `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts (this is a "trivial conflict" you handle directly per CLAUDE.md "When you handle work directly"). Then update Linear: `save_issue(id=<sub-issue ID>, state="Done")`. Mark the corresponding TaskUpdate to `completed`. Clean up: `git worktree remove -f -f <path>` then `git branch -d <branch>`. The double `-f` is required: the Claude agent harness places a `claude agent agent-...` lock on the worktree on completion, and single `-f` fails the unlock check. Double-force overrides; safe because the agent has already returned and you own the worktree's lifecycle.
   - **Fail (test failure, lint failure, blocker reported, criterion miss):** Diagnose the gap. If the subagent reported a blocker that exists as another Linear issue, set `state="Blocked"` and add the `blockedBy` link. Otherwise re-dispatch with the specific gap noted in the prompt. Clean up the failed worktree first: `git worktree remove -f -f <path>` and `git branch -D <branch>`.

### Wave-end gate

After all stories in a wave have been verified and merged (or blocked), and *before* dispatching the next wave:

- Run the full chain on the feature branch: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -n auto`. Catches integration issues that pass per-story but fail combined.
- **Push the feature branch to `origin`** (`git push origin <feature-branch>`) so the next wave's worktrees can rebase onto its latest tip. Skipping this means wave-N+1 subagents will only see content reachable from `main`, missing every story merged in waves 1..N.
- If clean, proceed to the next survey.
- If the global run fails, the failure is in the integration boundary between this wave's stories. Diagnose; fix directly if trivial; re-dispatch the relevant story if not. Do not advance to the next wave until the global run is clean.
- **If the global run flakes — passes some runs, fails others on the same code — do not defer it as a finding. Bisect.** The flake exists because some test in this wave (or in the work tree's accumulated additions to the suite) mutates global state that another test depends on; xdist surfaces it intermittently because workload distribution to workers shifts run-to-run. Procedure: confirm by running `for i in 1 2 3 4 5; do uv run pytest -n auto 2>&1 | tail -1; done`; if mixed pass/fail, narrow with `--ignore=<test-dir>` to drop test groups until the flake stops, then narrow within the offending dir to a single file; read the offending file for `sys.modules` mutation, `logging.config.fileConfig` calls (default `disable_existing_loggers=True` is a classic trap), `os.environ` writes, shared filesystem-state mutations, `caplog` interactions, or fixture-leak across tests. The fix usually lands in production code (e.g., pass `disable_existing_loggers=False` to the offending `fileConfig` call), not in the test that surfaces the flake. CLAUDE.md is strict that test-order dependence is a real bug; a flake from your work tree counts as a wave-gate failure even if a previous run passed.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Linear status transitions (`Todo` → `In Progress` → `Done`).
- Reading sub-issues and the parent Issue to plan the next wave.
- Resolving trivial merge conflicts when a subagent's `--ff-only` fails.
- Trivial-fix unwarranted lint suppressions.
- The single-line `docs/project-tracker.md` status update.
- Trivial story work the parent Issue's orchestrator notes mark for inline handling (e.g., a one-line README link).

For everything else, delegate.

## Completion sequence

When every sub-issue is `Done` (no blockers, no deferrals), execute the six completion-sequence tasks you registered up front. Mark each `in_progress` when you start, `completed` when finished.

### 1. Open PR to main

```bash
git push -u origin <feature-branch>
gh pr create --head <feature-branch> --base main --title "<feature-name>: implement work tree" --body "$(cat <<'EOF'
## Summary
<2–4 bullets covering what the work tree delivers — drawn from the parent Issue's description>

Closes <parent Linear issue URL>.

## Test plan
- [ ] `uv run pytest -n auto` green on the feature branch
- [ ] Linter chain (`ruff check`, `ruff format --check`, `mypy`) clean
- [ ] All <N> sub-issues marked Done in Linear

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The explicit `--head <feature-branch> --base main` is required: without it, gh refuses to create the PR with a confusing `you must first push the current branch to a remote` error even when the branch IS pushed. The friction is caused by gh's local-state safety check tripping on the leftover `.claude/worktrees/agent-*` directories as untracked content. Explicit `--head`/`--base` bypass the check.

Capture the PR URL.

### 2. Spawn /review subagent

```
Agent({
  subagent_type: "general-purpose",
  description: "[Opus] /review PR <PR number>",
  model: "opus",
  prompt: "Run the `review` skill (`Skill('review')`) against PR #<PR number> at <PR URL>. Report findings as a structured list of suggestions, each tagged with severity (blocker / suggested / nit). Do not push commits or modify the PR.",
  run_in_background: true
})
```

Wait for completion. The result is a list of suggestions.

### 3. Address review feedback

Assess each suggestion with **bias toward acceptance** — the reviewer is calibrated and the feedback typically warrants action. Reject only with explicit reason (e.g., "this would re-introduce the X anti-pattern", "this contradicts the Y design constraint"). For accepted suggestions:

- **Trivial fixes** (typo, import order, comment cleanup) — apply directly on the feature branch, commit with a descriptive message.
- **Non-trivial fixes** — dispatch an Opus subagent in a worktree off the feature branch:

```
Agent({
  subagent_type: "general-purpose",
  description: "[Opus] address /review feedback on PR <PR number>",
  model: "opus",
  isolation: "worktree",
  run_in_background: true,
  prompt: "Address the following /review feedback on PR <PR number>: <bulleted list of accepted suggestions with file:line references>. Run the test + lint chain before committing. Make the final commit with a message describing what feedback was addressed."
})
```

After the subagent reports back, verify and merge into the feature branch as in the standard wave loop.

After all accepted feedback is addressed, push the new commits to the PR.

### 4. Update docs/project-tracker.md

Edit the feature's bullet under "Ready for implementation": change `_in progress_` (or whatever transient status it had) to `_done_`. Commit with a message like `chore(project-tracker): mark <feature> done`. This commit goes on the feature branch and rides the same PR.

### 5. Land PR and clean local git state

- Wait for CI green on the PR (if CI exists).
- Merge the PR (squash-merge or merge per repo convention; check `gh pr view` for repo defaults).
- Locally:

```bash
git checkout main
git pull --ff-only
git branch -d <feature-branch>          # safe; the merge into main makes -d non-destructive
git remote prune origin
```

Then dispose of stale subagent worktrees the run accumulated. Worktrees under `.claude/worktrees/agent-*` are gitlinks (mode 160000) with their own working trees on disk. After the PR merges, three patterns of noise commonly remain inside them and surface as "modified content" in main's `git status`:

* **Formatter churn.** Running `uv run ruff format .` from the main repo's CWD recurses into worktree subdirectories (they are physical Python-containing trees regardless of the gitlink), reformatting whichever copy of each file the worktree's branch HEAD held. Multiple worktrees often show the *same* small set of files modified with whitespace-only diffs (multi-line strings collapsed, signatures rewrapped). These are pure cosmetic noise.
* **Stale post-merge state.** A worktree whose work already landed via this PR (or an earlier one) still carries the working-tree edits the subagent made, because the worktree's branch HEAD is older than main and the changes were merged into main rather than back into the worktree branch. The diff against the worktree's HEAD shows the now-landed edits as "uncommitted." Compare to main: if the file content is byte-identical, it's stale.
* **Subagent-leak noise.** Per `feedback_worktree_leak_compensation`, subagents occasionally write to the wrong tree. If the dispatch prompts pinned operations to `$WORKTREE_ROOT` correctly, this should be rare, but worth scanning.

Triage and clean:

```bash
# List worktrees with dirty content.
git status --porcelain .claude/worktrees/ | grep -E '^.[Mm?]'
```

For each dirty worktree, inspect the diff once (`git -C .claude/worktrees/agent-XXX diff --stat HEAD` and `git -C .claude/worktrees/agent-XXX status --short`). Classify:

* **Whitespace-only diffs** (confirmed via `git diff --shortstat` showing only insertion/deletion counts and no `(+)`/`(-)` net add when ignoring whitespace) — formatter churn. Discard.
* **Diffs whose content is byte-identical to current `main`** (verify via `diff -u <main-path> <worktree-path>` returning empty) — stale post-merge state. Discard.
* **Diffs that represent real divergence from `main`** — pause. The work belongs somewhere; surface to the operator and decide whether to cherry-pick onto a follow-up branch or discard. Do not auto-clean these.

After triage, dispose of the noise-only worktrees:

```bash
for d in <list of confirmed-noise worktree paths>; do
  git -C "$d" restore .
  git -C "$d" clean -fd
done
```

This discards uncommitted edits and untracked files inside the worktree without changing the recorded gitlink SHA in main's index — the worktree's identity is preserved; only the dirty inner state is removed.

Verify clean state: `git status` shows nothing pending.

### 6. PushNotification

Send a notification summarizing the run:

```
PushNotification({
  title: "Orchestration complete: <Feature>",
  body: "<N> stories landed across <M> waves. PR <number> merged to main. <any caveats — e.g., 'with 2 warranted lint suppressions noted'>."
})
```

## Communication with the user

Terse. Updates in this shape:

- **Pre-flight summary:** one paragraph. Parent Issue ID, sub-issue count, dependency-graph shape ("3 waves: 03 → 4-way 04* → 06"), feature branch name.
- **Per wave dispatch:** one line. "Dispatching wave 2: [Opus] 04a, [Opus] 04b, [Opus] 04c, [Opus] 04d."
- **Per story verification:** one line. "ALP-230 04a — pass, merged commit `abc1234`."
- **Per wave gate:** one line. "Wave 2 global lint+test green; advancing to wave 3."
- **Final summary:** one paragraph. PR URL, total stories landed, any warranted lint suppressions, any blockers surfaced.

Surface blockers immediately, do not work around them:

- **Cross-feature dependencies not done.** A sibling work tree's gating story isn't `Done`; the parent Issue's orchestrator notes named it as a hard gate. Surface; the operator decides whether to wait or proceed against a stub.
- **Schema drift between this work tree's expected typed inputs and a sibling's actual produced shape.** Surface; the resolution is a coordinated edit between work trees, not unilateral here.
- **Subagent reports of disabled linter rules that aren't trivially-fixable.** Per `feedback_lint_suppression_triage`, trivial-fix unwarranted ones yourself; dispatch for non-trivial fixes; accept warranted ones with a note in the verification report. Do not pause to ask except for genuinely ambiguous cases.
- **Subagent reports of an untyped or shape-mismatched cross-feature dependency.** Surface; coordinate before re-dispatching.
- **Missing API keys** (any vendor adapter's connectivity acceptance criterion will fail). Surface to the operator.
- **Schema-spec ambiguities discovered mid-implementation.** Surface — the design needs a clarifying edit before the story can land.
- **Test failures the subagent could not resolve** despite a re-dispatch. Surface; pause execution.

## Boundaries

- **Do not push to remote branches other than the feature branch** until the post-completion sequence. Pushing the feature branch to `origin/<feature-branch>` after each wave is REQUIRED for worktree rebasing (the wave-end gate covers this).
- **Do not amend commits** — create new commits instead. If a hook fails, the commit didn't happen, and `--amend` would corrupt the previous commit.
- **Do not skip hooks** (`--no-verify`, `--no-gpg-sign`). If a hook fails, fix the underlying issue.
- **Do not dispatch a subagent without `isolation: "worktree"`** — parallel work on the feature branch checkout corrupts state.
- **Do not dispatch a subagent without `run_in_background: true`** — CLAUDE.md mandates async dispatch; foreground subagents block parallelism.
- **Do not declare a story `Done` without `uv run pytest -n auto` green, lint clean, and a spot-check of every acceptance criterion.**
- **Do not modify a sub-issue's description in Linear** — only the `state` and `blockedBy` fields. Description ownership lives with `/draft-user-stories`.
- **Do not pick up a story whose `blockedBy` stories are not all `Done`.**
- **Do not exceed 6 concurrent active subagents.** Sub-wave instead.
- **Do not let a subagent disable a linter rule without triage** per `feedback_lint_suppression_triage`. Re-dispatch if a subagent suppressed without warrant.
- **Do not skip the post-completion sequence** even if the operator seems to want a fast wrap. The TaskCreate entries exist precisely to defeat that drift; verify each is `completed` before reporting done.

## Anti-patterns

- **Dispatching all stories at once "to save time".** Wave structure exists because dependencies are real. Out-of-order dispatch produces stories that depend on absent code and waste subagent cycles.
- **Dispatching multiple stories that share a target file in the same wave.** When two or more stories all create or edit the same file (e.g., three sub-stories each adding a test case to one shared file), parallel worktrees produce independent versions of the file and the cherry-picks conflict at integration time. Either sequence them across waves, merge them into one story, or — if the parent Issue's notes say "story A creates the file; siblings ADD to it" — dispatch story A first, wait for merge + push, then dispatch the siblings.
- **Running `pytest` without `-n auto`** anywhere — your verification, the subagent's verification, the wave gate. CLAUDE.md is strict; serial pytest runs hide xdist-only failures.
- **Trusting subagent self-reports.** They sometimes report "done" with uncommitted changes (`feedback_subagent_must_commit`). Always verify with `git log <feature-branch>..<subagent-branch>` and `git status` in the worktree.
- **Skipping the wave-end global lint+test gate.** Per-story verification doesn't catch integration issues. The global gate is cheap; skipping it costs more later.
- **Restating the parent Issue's orchestrator notes here.** This skill provides defaults; the parent Issue provides feature-specific overrides. Read both; apply them additively.
- **Marking the post-completion tasks `completed` early.** Mark each only after the action observably succeeded (PR open and visible, /review subagent returned, etc.).

## Self-improvement

While executing, note moments where this skill let you down: a wave-handling edge case it didn't anticipate, a Linear/git/subagent gotcha you hit and worked around, a verification step that missed a real failure mode, a recovery path the procedure didn't describe, guidance that turned out wrong.

Don't fix the skill mid-flight — the orchestration loop has too many moving parts to safely edit while running. Keep working notes mentally (or in a scratch TaskCreate), and at the end — *after* the PushNotification — propose specific edits in this shape:

- **Where:** the section/heading in this SKILL.md to change.
- **What:** the concrete edit (added bullet, replaced sentence, new subsection).
- **Why:** what went wrong without it.

Skip silently if nothing came up. The bar is "would have saved a step" or "would have prevented a mistake", not "could be marginally smoother". The operator decides what to apply.
