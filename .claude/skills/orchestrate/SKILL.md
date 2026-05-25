---
name: orchestrate
description: Use to drive an AlphaMind feature's implementation work tree end-to-end — fetch the parent Linear Issue, dispatch its sub-issues to subagents in dependency-respecting waves, verify each, integrate to a feature branch, then PR → pre-review triage → wait for CI green → /review → address feedback → update `docs/project-tracker.md` → land → clean local git → PushNotification. Triggers on `/orchestrate <Feature>` and operator phrases like "orchestrate Breach behavior", "implement the Synthesizer feature", "drive Domain researchers to completion", "build out State persistence", "execute the Portfolio manager work tree", "land the X feature". Assumes the work tree has already been drafted via `/draft-user-stories` (parent Issue exists with sub-issues + dependency graph + orchestrator notes). Use this skill whenever the operator asks to implement, build, drive, execute, land, or complete an AlphaMind feature that has a drafted Linear work tree, even if they don't say "orchestrate". Do NOT use for one-off story dispatch (just call `Agent` directly), bug fixes, refactors, or features without a Linear parent Issue.
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
- **Run the full lint chain after each parallelized wave merges** to the feature branch. Catches integration issues that pass per-story but fail in combination. Commands: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports`. **Do NOT run the full pytest suite locally** — per CLAUDE.md "Testing", the CI workflow (`.github/workflows/ci.yml`) is the authoritative full-suite gate on every PR push. Per-story and per-wave pytest is scoped to the changed paths only (`uv run pytest tests/<area>/ --testmon -n auto` or a single test node-id), kept fast and used as a sanity check during integration. The CI run that fires when you open the PR (or push subsequent commits to it) is the wave-spanning full-suite check.
- **A story blocked mid-implementation gets `state="Blocked"` in Linear,** plus a `blockedBy` link to the blocking issue if one exists in Linear (use `save_issue(id=..., state="Blocked", blockedBy=[...])`). If the blocker isn't in Linear, surface it in the final summary instead.
- **Do not skip the post-completion sequence.** PR → pre-review triage → wait for CI green → /review → address feedback → update `docs/project-tracker.md` → land → clean local git → PushNotification. Add these as tasks before the work begins (see below).

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
- **Completion-sequence tasks** — register all eight before the work begins so they cannot be forgotten:
  1. `Open PR to main`
  2. `Pre-review triage of work-tree residue`
  3. `Wait for CI green on the PR`
  4. `Spawn /review subagent`
  5. `Address review feedback`
  6. `Update docs/project-tracker.md status to _done_`
  7. `Land PR and clean local git state`
  8. `Send PushNotification summarizing completed work`

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

**Stale `index.lock` recovery.** If any of the above commands fail with `Unable to create '.../.git/index.lock': File exists`, this is almost always a stale orphan from a prior crashed git operation (typical: VS Code's git extension polling `git diff --numstat HEAD`, or your own earlier `git pull` that crashed). **Expect to handle this on roughly every other git operation on macOS with VS Code's git extension running** — the lock recurs constantly throughout an orchestration run. Reach for `rm -f .git/index.lock && <git-command>` reflexively rather than diagnosing each occurrence. Diagnose only if the chained recovery itself fails:

- `lsof <repo>/.git/index.lock` — if no process holds it, the lock is stale.
- `stat -f "%Sm" <repo>/.git/index.lock` (macOS) shows the lock's mtime. Locks older than ~30 seconds with no holding process are stale.

Recovery: `rm -f <repo>/.git/index.lock` and immediately re-run the chained git operation. The watcher polls fast enough that a re-acquired transient lock can re-appear within ~1 second; chain the `rm -f` and the git command on a single line so they execute back-to-back rather than running them as separate Bash calls. The same procedure applies to every git invocation in this skill — wave merges, pushes, worktree removals — not just branch creation. The fix is the same each time.

## Operating posture

**Delegate by default.** You drive sequencing and status; subagents do the work. Use `Agent` (`subagent_type: general-purpose`, `isolation: "worktree"`, `run_in_background: true`) for every implementation story. Write code yourself only when the work is smaller than dispatch overhead — Linear status updates, frontmatter fixes, single-line README edits, file-existence checks while planning a wave.

**Model selection.** Per CLAUDE.md: mechanical changes → Sonnet; everything else → Opus. Mechanical means: a single-line README edit, a one-file frontmatter change, a verbatim file copy. Anything involving algorithmic judgment, schema design, fixture construction, or test-list reasoning is Opus. The parent Issue's orchestrator notes may flag specific stories that override this default — honor those flags. Tag the model in the `Agent` description as `[Sonnet]` or `[Opus]`.

**Run independent stories in parallel.** The dependency graph is in the parent Issue's description; the per-sub-issue `blockedBy` relations are the source of truth. A story is dispatch-eligible when: its status is `Todo` AND every story it `blockedBy` has status `Done`. Stories at the same dependency rank dispatch together in one wave (one `Agent` call per story, all in the same message), capped at 6 concurrent.

**Each story runs in its own worktree.** `isolation: "worktree"` makes the harness create a fresh branch + checkout **from `main`** (not the feature branch), runs the subagent there, and reports the branch name and worktree path on completion. Subagents must rebase onto `origin/<feature-branch>` before starting work — otherwise they won't see prior waves' commits. The dispatch prompt below includes the rebase block; honor it verbatim. Subagents commit on the worktree branch; you cherry-pick or merge onto the feature branch after verification. Never run subagents on the feature branch checkout itself — parallel stories would collide.

For wave-1 subagents, the rebase block is a no-op because at dispatch time `origin/<feature-branch>` is at the same SHA as `main` (you just created the branch). Wave-2+ subagents are the ones whose rebase actually advances their HEAD — and only because the orchestrator's wave-end `git push origin <feature-branch>` published the prior waves' merges. If a subagent reports "the integration branch reference exists locally as `<feature-branch>` (not `origin/...`)" or "the rebase did nothing", that's expected behavior on wave 1 — not a sign that the rebase machinery is broken.

**Push the feature branch to `origin` after each wave's wave-end gate passes**, so the next wave's worktrees can rebase onto its latest tip. Without this, wave-2+ subagents only see content from `main`, missing all prior waves' commits.

**Verify before marking done.** A story is `Done` when every acceptance-criteria checkbox passes a verification step *you can describe* — typically a scoped `uv run pytest tests/<story-area>/ --testmon -n auto` (or single test node-id) plus a spot-check of each non-test criterion (file exists, schema validates, function exhibits the documented behavior). The full-suite check happens later in CI; per-story local pytest stays scoped to the story's own tests, per CLAUDE.md "Testing". Do not trust the subagent's self-report alone (`feedback_subagent_must_commit`).

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

If the story scaffolds a new module, service, or package — anything beyond extending an existing file with one more function — invoke `Skill("python-architecture")` in design mode before writing code, scoped to the new component. Use the brief's package layout, named typed records, and testing seam to anchor your implementation. The story's Scope already names the concrete deliverables; the brief tells you *how* to shape them so the result is testable, deeply-modular, and respects the functional-core / imperative-shell split. Skip when the story is purely additive (one new function in an existing file, a config knob, a doc edit) — the overhead exceeds the value there.

**Commit often during implementation — do not save all the work for one final commit.** A good cadence: each red→green→refactor cycle that lands a coherent piece of behavior gets its own commit. For a multi-part story (extending a Protocol + refactoring the consumers + writing the integration test, say) commit each part separately as soon as its tests are green and its lint is clean. This protects against mid-flight termination (token-limit cutoffs, OOM, accidental kill): if you get cut off after committing wave A but before wave B, the orchestrator can recover wave A and re-dispatch just B, instead of throwing away both. It also makes review easier — small focused commits beat one mega-commit. The harness's worktree branch persists across these commits; only the final report-back step matters for the orchestrator's verbatim-git-log gate. Commit messages should be conventional-commits-style and reference the Linear ID. The story's deliverables are usually 2–6 such commits; one commit is fine for a genuinely-small story (one new function + its test).

After tests are green and before your final commit, run the lint chain on your changes:

    uv run ruff check .
    uv run ruff format .
    uv run mypy
    uv run lint-imports

Address all findings from your changes only. **Do NOT invoke the `code-review` skill in your dispatch.** The orchestrator runs `code-review` in the main thread after each wave merges to the feature branch (with the integration view across multiple stories), and decides whether each finding should be applied inline or dispatched to a follow-up subagent based on scope. Running `code-review` in the dispatch prompt duplicates this work, produces narrower findings than the post-wave pass, and burns tokens on diff coverage the orchestrator will redo at integration-tip anyway.

When done — **REQUIRED — DO NOT SKIP THE COMMIT STEP.**
1. Run a scoped pytest covering this story's tests: `uv run pytest tests/<story-area>/ --testmon -n auto` (or the single-file / single-node-id form). Confirm green. **Do NOT run the full pytest suite locally** — the CI workflow (`.github/workflows/ci.yml`) is the authoritative full-suite gate on every PR push, per CLAUDE.md "Testing". The unscoped `uv run pytest` is forbidden during dispatch.
2. **STAGE AND COMMIT** any remaining uncommitted work. `git add` then `git commit`. After committing, run `git log --oneline <feature-branch>..HEAD` and confirm your commits are listed. If `git status` shows untracked or modified files, you have NOT committed — `git add` and commit them.
3. Report back with **the verbatim output of `git log --oneline <feature-branch>..HEAD`** as the FIRST item in your report (before any prose), followed by a one-line attestation per acceptance criterion ("met by test X", "met by file Y exists", "met by manual inspection of Z"). A report without verbatim git-log output as its first item signals to the orchestrator that the commit step was skipped — the orchestrator will reject the report and re-dispatch.

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

0. **Verbatim-git-log gate.** Before any lint/test, scan the agent's report for the verbatim `git log --oneline <feature-branch>..HEAD` output the dispatch prompt mandates (one or more lines of the form `<short-sha> <commit-message>`). If the report ends with prose like "All tests pass / I'm done", or any final message that is NOT at least one such git-log line, the commit step was skipped. Verify directly: `git -C <worktree-path> log --oneline <feature-branch>..HEAD`. If zero commits are listed, the agent did not commit. Decide:
   - **Re-dispatch** when the work has substantive issues (lint failures, missing AC, unwarranted suppressions) on top of the missed commit.
   - **Commit-yourself** when the work is otherwise sound and only the commit step was skipped — assess the lint state with `uv run ruff check . && uv run mypy` against the worktree first, then `git add` + `git commit` with a descriptive message and proceed to step 1.
   Do NOT proceed to step 1 (Tests) on uncommitted state — the agent's "tests pass" claim is unverifiable, and pytest will collect different files than what would land at merge time.
1. **Tests.** `cd` into the worktree and run a scoped pytest for the story's changed paths: `uv run pytest tests/<story-area>/ --testmon -n auto` (or the single-test node-id form). First run pays a one-time `uv sync` cost for the fresh `.venv` — that's fine. **Do NOT run the unscoped full suite here** — per CLAUDE.md "Testing", CI runs the full suite on PR and is the authoritative gate; the per-story local run stays narrow and fast. **DO NOT pipe to `tail` to truncate output** — the pipe's exit code is `tail`'s (always 0), masking pytest failures. Use `uv run pytest <scoped-path> --testmon -n auto; echo "exit=$?"` and read the test summary line for `X passed, Y failed`. The same applies to subagent dispatch prompts — explicitly forbid `| tail` there; a subagent's "all tests pass" report can otherwise hide a real failure that the eventual CI run would catch.
2. **Lint.** `uv run ruff check .` and `uv run mypy` against the worktree. Clean for the changed files.
3. **Linter suppressions.** Grep the diff for `# noqa`, `# type: ignore`, `per-file-ignores`, `ignore` keys in `pyproject.toml`. For each suppression, **first cross-check the matching sibling-feature module** (e.g., for `synthesizer/harness.py`, check `qualitative_research/harness.py` and `adaptive_research/harness.py`; for a domain-researcher story, check the other two domain-researcher modules). If the sibling carries the same suppression with the same rationale, the suppression is warranted by precedent — accept and note. This avoids re-litigating established codebase patterns. The grep is `grep -n 'noqa: <RULE>\|<symbol-name>' src/<sibling-paths>/<file>.py`. If no sibling precedent exists, then assess on its own merits (per `feedback_lint_suppression_triage`):
   - **Trivial-fix unwarranted ones yourself** — directly in the worktree before merging.
   - **Non-trivial unwarranted ones** — re-dispatch the story with explicit instruction to remove the suppression and address the underlying issue.
   - **Warranted ones** — note in your verification report and accept.
4. **Spot-check non-test acceptance criteria.** For each criterion not verified by an automated test, confirm it manually (file exists at the expected path, schema validates against a sample, function signature matches the story's spec).
5. **Architectural integration gaps invisible to stubs.** Stub-heavy unit tests can pass while the framework itself rejects the constructed options at runtime. When a story touches an external SDK or framework's option-shape construction (e.g., `ClaudeAgentOptions.mcp_servers`, `Alembic.Config`'s logger configuration, `pytest` plugins, `pydantic` discriminated unions), confirm at least one test exercises the constructed shape end-to-end — not just stubbing the framework's response. If every test stubs the SDK, the harness can build wrong-shaped options that silently degrade in production (e.g., tools registered with `allowed_tools` but no `mcp_servers` — the LLM emits `<tool_use>` and the SDK returns "tool not found", and the agent falls back to its non-tool path). Add such a test before merging or surface as a follow-on issue.
6. **Decision:**
   - **Pass:** **First confirm CWD is the main repo, not a worktree. The shell's CWD persists across tool invocations** — after ANY command that `cd`s into a worktree (even implicitly via a chained `&&` series running inside the worktree), the NEXT bash invocation operates from that CWD until you explicitly `cd` out. Symptoms: `git merge --ff-only worktree-X` reports `Already up to date` (because you're merging the worktree branch into itself); `git worktree remove` fails with `Unable to read current working directory`; `git status` reports the worktree's branch instead of the feature branch. Defenses (any one suffices): (a) chain `cd /Users/.../<repo> && <merge command>` as the first thing in the bash invocation, (b) run all merge / worktree-cleanup operations via `git -C /absolute/path/to/main/repo` so CWD is irrelevant, (c) never let a verification step's lint/test command shift CWD — use `git -C <worktree-path>` or absolute paths from main. After the merge: `git merge --ff-only <branch>`. If FF fails (parallel branches diverged), `git merge --no-ff <branch>` and resolve conflicts (this is a "trivial conflict" you handle directly per CLAUDE.md "When you handle work directly"). Verify the merge actually happened: `git log --oneline -3` should show the worktree's commit on the feature branch. Then update Linear: `save_issue(id=<sub-issue ID>, state="Done")`. Mark the corresponding TaskUpdate to `completed`. Clean up: `git worktree remove -f -f <path>` then `git branch -d <branch>`. The double `-f` is required: the Claude agent harness places a `claude agent agent-...` lock on the worktree on completion, and single `-f` fails the unlock check. Double-force overrides; safe because the agent has already returned and you own the worktree's lifecycle.
   - **Fail (test failure, lint failure, blocker reported, criterion miss):** Diagnose the gap. If the subagent reported a blocker that exists as another Linear issue, set `state="Blocked"` and add the `blockedBy` link. Otherwise re-dispatch with the specific gap noted in the prompt. Clean up the failed worktree first: `git worktree remove -f -f <path>` and `git branch -D <branch>`.

### Wave-end gate

After all stories in a wave have been verified and merged (or blocked), and *before* dispatching the next wave:

- Run the full lint chain on the feature branch: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports`. Catches lint/type/import-graph integration issues that pass per-story but fail combined. **Do NOT run the full pytest suite here** — per CLAUDE.md "Testing", CI runs the full suite on PR. A scoped pytest covering the union of paths the wave touched (e.g., `uv run pytest tests/synthesizer/ tests/analyst/ --testmon -n auto`) is fine as a sanity check if the wave touched cross-cutting code; keep it narrow.
- **Push the feature branch to `origin`** (`git push origin <feature-branch>`) so the next wave's worktrees can rebase onto its latest tip. Skipping this means wave-N+1 subagents will only see content reachable from `main`, missing every story merged in waves 1..N.
- **Regenerate fixture artifacts ONCE per wave, not per-story.** When 3+ parallel stories in the same wave each modify a generated artifact (typical: `tests/fixtures/replay_harness/fixture_store/*/raw_inputs.sqlite` after migrations land — every story regenerates from `python tests/fixtures/replay_harness/generate_fixtures.py`), each per-story merge produces a binary-file conflict. Resolve trivially with `git checkout --theirs <path>` during cherry-pick (the regenerated content is wrong-for-the-final-state anyway), then run the regen script ONCE after all parallel stories merge and commit the result as a `test(<feature>): regenerate fixtures after wave-N <thing>` follow-up commit. Avoids N-1 useless conflict-resolution cycles.
- **Run `code-review` in the main thread** (`Skill("code-review")`) on the post-wave state — unless the wave is docs-only (no `*.py` or other code-bearing files changed). The orchestrator owns the integration view across the wave's stories; `code-review` surfaces correctness bugs and integration gaps that per-story subagents cannot see (one story's helper duplicating another's; a state mutation in story A that breaks an invariant story B's caller relies on; a wave's worth of validators all repeating the same boilerplate that could centralize; a Protocol extension in story A whose new method story B implements but story C's caller never invokes). Subagent dispatch prompts no longer invoke `code-review` — the post-wave main-thread pass is the single point of consolidation. Skip the pass on docs-only waves (`docs/`, `*.md`) since the skill targets code-correctness signal, not prose; record the skip explicitly ("Wave-N is docs-only; code-review skipped") and proceed.

  **Effort selection.** `code-review` accepts an effort level — `low`/`medium` yield fewer, higher-confidence findings; `high`/`max` broaden coverage at the cost of more uncertain findings. Default to **medium** for routine waves. Bump to **high** when the wave (a) touches an architectural seam — Protocol contracts, persistence boundary, cross-feature primitive; (b) edits a schema, migration, or wire-format; (c) lands a multi-story feature whose stories have non-trivial cross-story integration (Protocol extension + consumers + integration tests all in one wave); or (d) collectively changes >300 lines or >5 production files. Drop to **low** for waves that are predominantly mechanical (verbatim renames, generated-fixture regenerations, single-file additive changes, doc-adjacent code). The cost of `high`/`max` is operator-triage time on uncertain findings; spend it where a missed bug would force a re-dispatch + re-merge cycle, not on a wave that's near-impossible to break. Do not pass `--comment` — the wave runs against the integration branch, not a PR; the orchestrator consolidates and applies findings itself, and `--comment` requires a PR target anyway.

  **Bias toward accepting findings.** Reject only with explicit reason ("this duplication is across feature boundaries and consolidating would couple them", "this branch isn't dead — it handles the documented edge case at line X"). For accepted findings, decide based on scope:
   - **In-line** when the fix is a contained tweak (drop unused import, collapse a redundant conditional, rename one local variable, delete one dead branch) — apply directly on the feature branch and commit with a descriptive message like `refactor(<area>): apply code-review findings — <one-line summary>`.
   - **Dispatch to a follow-up subagent** when the fix spans multiple files, touches an algorithmic seam, or requires test rewrites. Dispatch an Opus subagent in an isolated worktree off the feature branch with the consolidated finding list; verify and merge as in the standard wave loop. Use this path when in-line application would push the orchestrator's edit volume past ~3 files or 50 lines.
   - **Defer with a Linear ticket** when the fix touches a sibling work tree's contract or requires design judgment the orchestrator doesn't have. Open a follow-up issue with the finding's rationale; note the deferral in the run summary.

  When the code-review pass produces no findings, record that explicitly ("Wave-N code-review: no findings at <effort> effort") and proceed. If you ran `medium` and surfaced zero findings on a wave you'd flagged as architectural-seam-touching, re-run once at `high` before concluding the wave is clean — a zero-finding `medium` on a high-risk wave is more likely under-coverage than genuine cleanliness.
- If clean, proceed to the next survey.
- If the global run fails, the failure is in the integration boundary between this wave's stories. Diagnose; fix directly if trivial; re-dispatch the relevant story if not. Do not advance to the next wave until the global run is clean.
- **If a CI run flakes — passes some runs, fails others on the same code — do not defer it as a finding. Bisect.** The flake exists because some test in this wave (or in the work tree's accumulated additions to the suite) mutates global state that another test depends on; xdist surfaces it intermittently because workload distribution to workers shifts run-to-run. Diagnosing a CI flake is one of the "operator-explicitly-directed" cases where running the full pytest suite locally is permitted (per CLAUDE.md "Testing"); say so in your status update before doing it. Procedure: confirm by running `for i in 1 2 3 4 5; do uv run pytest -n auto 2>&1 | tail -1; done` locally; if mixed pass/fail, narrow with `--ignore=<test-dir>` to drop test groups until the flake stops, then narrow within the offending dir to a single file; read the offending file for `sys.modules` mutation, `logging.config.fileConfig` calls (default `disable_existing_loggers=True` is a classic trap), `os.environ` writes, shared filesystem-state mutations, `caplog` interactions, or fixture-leak across tests. The fix usually lands in production code (e.g., pass `disable_existing_loggers=False` to the offending `fileConfig` call), not in the test that surfaces the flake. Test-order dependence is a real bug; a flake from your work tree counts as a CI-gate failure even if a previous run passed.

## When you handle work directly

Skip delegation only when overhead exceeds the work:
- Linear status transitions (`Todo` → `In Progress` → `Done`).
- Reading sub-issues and the parent Issue to plan the next wave.
- Resolving trivial merge conflicts when a subagent's `--ff-only` fails.
- Trivial-fix unwarranted lint suppressions.
- The single-line `docs/project-tracker.md` status update.
- Trivial story work the parent Issue's orchestrator notes mark for inline handling (e.g., a one-line README link).

**Subagent token-limit recovery.** If a dispatch reports `You've hit your limit · resets <date>` (or any other mid-flight termination short of the verbatim-git-log report), the work may still be substantively complete — agents typically commit last, so a token cutoff during the staging step leaves all the implementation as untracked files in the worktree. Investigate before re-dispatching:

- `git -C <worktree-path> log --oneline <feature-branch>..HEAD` — if a commit is listed, the agent finished and the cutoff was during reporting; verify normally.
- `git -C <worktree-path> status --short` — list untracked + modified files. If a coherent set of files exists (production module + tests + any required doc/config edits matching the story's scope), the work is likely complete-but-uncommitted.
- `wc -l <files>` to gauge volume; spot-read the largest 1–2 files for shape coherence (does the script have an entry point? does the test file have the expected test cases?).
- `cd <worktree-path> && uv run ruff check <changed-paths> && uv run mypy <changed-paths> && uv run pytest <test-path> --testmon -n auto` — if lint + scoped pytest pass against the new files, the work is sound; copy the files into the main repo, commit directly with a descriptive `feat(<feature>): <story summary> (ALP-<N>)` message, and proceed as if the dispatch had returned cleanly. `<test-path>` must be the story's own tests, not the full suite — CI does full-suite verification on the PR.
- If the staged work is partial (missing a test, an obvious untouched file the story called out, lint failures, or any sign the agent stopped mid-implementation rather than mid-reporting), re-dispatch with a fresh worktree.

The bar for direct-commit recovery: you can describe each file's purpose in one sentence and the test/lint chain is green. Otherwise re-dispatch.

For everything else, delegate.

## Completion sequence

When every sub-issue is `Done` (no blockers, no deferrals), execute the seven completion-sequence tasks you registered up front. Mark each `in_progress` when you start, `completed` when finished.

### 1. Open PR to main

```bash
git push -u origin <feature-branch>
gh pr create --head <feature-branch> --base main --title "<feature-name>: implement work tree" --body "$(cat <<'EOF'
## Summary
<2–4 bullets covering what the work tree delivers — drawn from the parent Issue's description>

Closes <parent Linear issue URL>.

## Test plan
- [ ] CI (`.github/workflows/ci.yml`) green on the PR — lint on Linux + full pytest on Windows
- [ ] Linter chain locally clean (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`)
- [ ] All <N> sub-issues marked Done in Linear

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The explicit `--head <feature-branch> --base main` is required: without it, gh refuses to create the PR with a confusing `you must first push the current branch to a remote` error even when the branch IS pushed. The friction is caused by gh's local-state safety check tripping on the leftover `.claude/worktrees/agent-*` directories as untracked content. Explicit `--head`/`--base` bypass the check.

Capture the PR URL.

### 2. Pre-review triage of subagent-reported deferrals

Throughout the run, implementing subagents will report items they deferred or scope-shrunk — narrowed implementations, unresolved follow-ups, design questions they didn't have authority to answer, work they explicitly handed back to the orchestrator. Track these as you go (a scratch list in your head or in TaskCreate notes is fine; the per-story task notifications also preserve them). Before /review, walk the consolidated list and reason about each one with a bias toward addressing now.

The orchestrator has the integration view that per-story subagents lack and that /review will rediscover at cost. Addressing the obvious now reduces /review's surface, focuses its findings on architectural / cross-cutting issues, and avoids re-doing work between the reviewer's recommendation and your fix.

**Triage each deferral into one of three buckets:**

- **Address now** — the fix is contained, the substrate to support it exists, and a reviewer would flag it as a blocker or strong-suggested. Most subagent-flagged deferrals fall here once you ask "what's actually preventing this from being done?"
- **Defer with explicit Linear ticket** — the fix requires meaningful design work, sibling-work-tree coordination, or scope expansion the operator hasn't authorized. Open a Linear follow-up issue with the gap analysis. Do not let deferrals live only in runbook caveats or commit messages — those decay; Linear tickets surface in `/draft-user-stories` planning.
- **Skip** — cosmetic, premature optimization, or future-proofing without a current incident.

**The bias toward "now".** The default is to address; the burden of proof is on deferral. Reasons that survive the bias:

- Substrate the fix needs genuinely doesn't exist yet (cross-feature primitive missing, vocabulary extension would touch a sibling work tree's contract).
- Risk of regression exceeds the value of closing the gap.
- Operator-decision territory (algorithmic change, design ambiguity, scope expansion).

Anything else, fix now.

**Dispatch.** When the triage produces a non-empty "address now" list, dispatch a single Opus subagent on a fresh worktree with the consolidated list. The subagent can group fixes into one or a small handful of well-scoped commits. After the dispatch returns, verify and cherry-pick onto the feature branch, then run the wave-end gate (full lint + test) to confirm the additions don't regress.

When the triage list is empty, record that explicitly ("Pre-review triage: no addressable deferrals") and proceed to the drift-check.

### 3. Wait for CI green on the PR — and iterate until it IS green

The `ci` workflow runs on every PR push: lint chain on Linux, full pytest suite on Windows (matches production). It is the authoritative full-suite gate; this skill no longer runs the full suite locally. Watch the PR run:

```bash
gh pr checks <PR number> --watch
```

`gh pr checks --watch` polls until every check has a terminal state. **This step is not "wait once and proceed regardless" — it is an iteration loop.** Stay in the loop until CI is green:

1. Watch the run to completion.
2. **If green:** proceed to /review (step 4).
3. **If red:** read the failure log via `gh run view <run ID> --log-failed --job <job ID>`. Diagnose. Fix on the feature branch. Commit with a `fix(<feature>): address CI <category> failure in <area>` message. Push. Go back to step 1 — the push triggers a fresh CI run that you must watch to completion.
4. **Do not declare the post-completion sequence done while CI is red.** Do not advance to /review. Do not merge. Do not move on to the next feature. The orchestration is not complete until CI is green on the latest pushed commit.

**Failure-class diagnosis:**

- **Lint failure** — the wave-end lint chain should have caught this; if it didn't, the failure is in a path the wave-end gate didn't fully cover. Read the CI log, fix on the feature branch, commit as `fix(<feature>): address CI lint failure in <area>`, push. Re-watch.
- **Test failure on Windows that you can't reproduce locally** — the failure is platform-specific (path separators, file-handle behavior, line endings, timezone-naive datetime drift, signal handling, `cp1252` default text encoding, missing env vars CI doesn't have, Unix-only stdlib modules like `fcntl`). Read the failing test's full traceback from the CI log. Reproduce by running the scoped pytest path locally if you can; if not, the fix is informed by reading the test and the production code under suspicion. Commit and push. Re-watch.
- **Test failure that looks flaky** — re-run via `gh run rerun <run ID> --failed`. If it persists, treat as a real failure (xdist flake from shared global state — bisect per CLAUDE.md "Testing" guidance on test-order dependence). Bisect the offending test; fix; push; re-watch.
- **Pre-existing failure orthogonal to this feature's work** — if the failure is in code/tests this feature didn't touch and is reproducible on `main` itself (verify by checking the most recent push-to-main CI run on origin/main), surface to the operator with the diagnosis. Open a Linear issue under "To-dos" describing the symptom + scope, and xfail (or skip-with-reason) the offending test in this PR with `reason="ALP-<new-issue-id>: ..."` so this PR's CI goes green without masking the real bug. Do NOT silently downgrade an in-scope failure to "pre-existing" — verify against main first. Do NOT xfail without an open tracking issue.

Pair the wait with a final local lint sanity (cheap, catches anything that drifted between wave-end and now):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
```

When CI is green on the latest pushed commit and local lint is clean, proceed to /review.

### 4. Spawn /review subagent

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

### 5. Address review feedback

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

### 6. Update docs/project-tracker.md

Edit the feature's bullet under "Ready for implementation": change `_in progress_` (or whatever transient status it had) to `_done_`. Commit with a message like `chore(project-tracker): mark <feature> done`. This commit goes on the feature branch and rides the same PR.

### 7. Land PR and clean local git state

- Confirm CI is still green on the PR (`gh pr checks <PR number>`) — completion-sequence step 3 waited for the initial run, but the address-feedback push (step 5) triggered a fresh CI run. Wait for that one to complete green before merging.
- **Before `gh pr merge`, sweep stale `main`-bearing worktrees.** Run `git worktree list` and look for orphan worktrees from prior sessions checked out to `main` (typical naming: `.claude/worktrees/<random-name>` with no `agent-` prefix). `gh pr merge` switches the local checkout to `main` to apply the merge and fails with `fatal: 'main' is already used by worktree at <path>` if a stale `main` worktree exists. Confirm the orphan's `git -C <path> status --short` is clean (no uncommitted work), then `git worktree remove -f -f <path>`. Diagnose only if the worktree has uncommitted work — rare for orphans, but possible if it represents the operator's in-progress side work.
- Squash-merge the PR per CLAUDE.md "Git / GitHub Instructions" (`gh pr merge <PR number> --squash --delete-branch`).
- Locally:

```bash
git checkout main
git pull --ff-only
git branch -d <feature-branch>          # safe; the merge into main makes -d non-destructive
git remote prune origin
# Sweep orphaned subagent branches accumulated across this AND prior orchestration runs.
# These are leftover `worktree-agent-<id>` branches whose worktrees were removed but the
# branch reference stuck around. Their commits either landed via prior PRs or were
# abandoned by the operator — force-delete is safe.
git branch | grep '^[[:space:]]*worktree-agent-' | xargs -r git branch -D
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

### 8. PushNotification

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
- **Do not declare a story `Done` without scoped `uv run pytest tests/<story-area>/ --testmon -n auto` green, lint clean, and a spot-check of every acceptance criterion.** The full-suite check happens in CI on the PR; per-story local pytest stays scoped.
- **Do not modify a sub-issue's description in Linear** — only the `state` and `blockedBy` fields. Description ownership lives with `/draft-user-stories`.
- **Do not pick up a story whose `blockedBy` stories are not all `Done`.**
- **Do not exceed 6 concurrent active subagents.** Sub-wave instead.
- **Do not let a subagent disable a linter rule without triage** per `feedback_lint_suppression_triage`. Re-dispatch if a subagent suppressed without warrant.
- **Do not skip the post-completion sequence** even if the operator seems to want a fast wrap. The TaskCreate entries exist precisely to defeat that drift; verify each is `completed` before reporting done.
- **Do not abandon the CI-watch loop while CI is red on the latest pushed commit.** The step is "wait for CI green and iterate until it IS" (completion-sequence step 3). Failures caused by this work tree's changes get fixed on the feature branch and re-pushed until the run goes green. Pre-existing failures orthogonal to this feature get a Linear ticket + xfail with the ticket ID. There is no "merge red" or "merge and fix later" path.

## Anti-patterns

- **Dispatching all stories at once "to save time".** Wave structure exists because dependencies are real. Out-of-order dispatch produces stories that depend on absent code and waste subagent cycles.
- **Dispatching multiple stories that share a target file in the same wave.** When two or more stories all create or edit the same file (e.g., three sub-stories each adding a test case to one shared file), parallel worktrees produce independent versions of the file and the cherry-picks conflict at integration time. Either sequence them across waves, merge them into one story, or — if the parent Issue's notes say "story A creates the file; siblings ADD to it" — dispatch story A first, wait for merge + push, then dispatch the siblings.
- **Running `pytest` without `-n auto`** anywhere — your verification, the subagent's verification, the wave gate. Serial pytest runs hide xdist-only failures.
- **Running the unscoped full pytest suite locally.** CLAUDE.md "Testing" forbids this by default — CI runs the full suite on every PR push and is the authoritative gate. This skill's per-story checks, dispatch prompts, mid-wave checks, and wave-end gates all use scoped pytest (`tests/<area>/ --testmon -n auto` or a single test node-id). When you write a verbatim pytest command into a dispatch prompt, default to the scoped form. There is no longer a pre-/review local full-suite drift-check — the CI run on the PR replaces it (completion-sequence step 3).
- **Pushing directly to `main` instead of via PR.** CLAUDE.md "Branch policy" makes main PR-only — even though server-side branch protection isn't enforced. Push to the feature branch, open the PR, wait for CI, then merge.
- **Trusting subagent self-reports.** They sometimes report "done" with uncommitted changes (`feedback_subagent_must_commit`). Always verify with `git log <feature-branch>..<subagent-branch>` and `git status` in the worktree. Per-story tests cover per-story acceptance criteria, but they often don't exercise the production-call path end-to-end. The /review can surface integration gaps the wave gates miss — e.g., a Phase 1 transaction commit and a repository snapshot-read each working in isolation, but the production caller unable to string them together because a sentinel field (`phase1_completed_at`) is never set on the production write path. When a story's tests rely on synthetic timestamps or state stamps that production code should but doesn't write, treat them as a yellow flag — scan for such constructs during verification and mark them as a pre-merge follow-up.
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
