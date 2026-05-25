# CLAUDE.md

## Linting

Run the linter after every batch of changes:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run lint-imports
```

`lint-imports` (import-linter) enforces the architectural layer rules declared in `.importlinter`. Contracts will tighten as the architecture-refactoring work tree (ALP-454) lands; until then, each `ignore_imports` entry carries a comment naming the punch-list item that will retire it.

Alert the user before disabling the linter or any rule in any form — including `ignore`, `per-file-ignores`, `# noqa`, `# type: ignore`, and `ignore_imports`. If a subagent suppresses the linter, do not pause execution to alert user. Instead, assess whether each suppression was warranted and fix unwarranted suppressions.

## Testing

**Do not run the full pytest suite locally.** CI (`.github/workflows/ci.yml`) runs the full suite on a Windows runner against every PR and every push to `main`; that is the authoritative gate. Local full-suite runs are too resource-intensive to do on every change, so they are forbidden by default — the CI run is what blesses the diff.

**Narrow, scoped pytest is fine and encouraged** while implementing or debugging. Run only the tests directly relevant to the file or area you are changing:

```bash
uv run pytest tests/<sub-path>/ --testmon -n auto       # scoped run
uv run pytest tests/path/to/test_thing.py::test_case    # single test
```

Use `--testmon -n auto` on scoped runs to keep them fast. Scope tightly — single file, single directory, single test node-id. Treat the local pytest invocation as a TDD red-green loop or a targeted regression check, not as a release gate.

**The full suite (`uv run pytest -n auto`) runs locally ONLY when the operator explicitly asks for it** — e.g., "run the full suite", "do a full pytest before pushing", "I want to see all tests pass locally". Otherwise push the branch, let CI run it, and act on the CI result. Subagent dispatch prompts, skill files, story acceptance criteria, and PR test plans must NOT include unscoped `uv run pytest` invocations; the CI run is the singular full-suite check.

If a test passes locally but fails under xdist on CI, the cause is test-order dependence (typically `sys.modules` mutation or shared filesystem state). Fix the test — do not fall back to serial.

## Branch policy

`main` is PR-only. All changes land via pull request and require CI green (the `ci` workflow: ruff + ruff format + mypy + import-linter + Windows pytest). Do not push directly to `main`. Do not merge a PR until the `ci` workflow run completes successfully.

Branch protection is not currently enforced server-side (the repo is on GitHub Free and rulesets need GitHub Pro for private repos). The policy holds anyway — treat it as a hard rule, not an aspirational one. If you find yourself about to `git push origin main`, stop and open a PR instead.

Squash-merge every PR (see "Git / GitHub Instructions" below). The CI workflow uses `paths-ignore` for `**.md`, `docs/**`, `.archive/**`, `.claude/**`, and `audit-*.html` — pure docs/tooling PRs skip the test run and can merge as soon as you open them. Any change touching `src/`, `tests/`, `config/`, `prompts/`, `scripts/`, `pyproject.toml`, `uv.lock`, `.importlinter`, `alembic.ini`, or `.github/workflows/**` triggers the full CI run.

### Watching CI on a PR

**Do NOT invoke `gh pr checks <PR> --watch` immediately after `git push`.** GitHub takes 3–8 seconds to register the new workflow run as a check on the PR, and `--watch` interprets the empty pre-registration window as "no checks → exit." The watch exits with status 0 reporting "no checks reported on the '<branch>' branch" and the iterate-until-green loop falsely believes CI is done.

The reliable pattern is to **fetch the run ID directly and watch it by ID** — `gh run watch` blocks until the run reaches a terminal state, with no pre-registration race:

```bash
# After git push, give the run a moment to register, then watch it by ID.
sleep 5
RUN_ID=$(gh run list --branch <feature-branch> --workflow ci.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status     # --exit-status returns non-zero on failure
```

`--exit-status` is critical for the iterate-until-green loop: it makes the watch propagate the run's pass/fail as the command's exit code, so a shell pipeline like `gh run watch "$RUN_ID" --exit-status && gh pr merge ...` short-circuits correctly on failure.

If you must use `gh pr checks --watch` (e.g., because multiple workflows gate the PR and you want their joint status), guard against the pre-registration race with a poll-until-checks-appear preamble:

```bash
until [ "$(gh pr checks <PR> --json status -q 'length' 2>/dev/null)" -gt 0 ]; do sleep 2; done
gh pr checks <PR> --watch
```

On a failed run, fetch logs via `gh run view <RUN_ID> --log-failed --job <JOB_ID>` (the failed-job ID is printed by `gh run watch`). `--log-failed` filters to just the failing job's output so you don't have to scroll through gigabytes of passing-test noise.

## Spawning Subagents

- For mechanical changes, use Sonnet
- For all other changes, use Opus
- Always spawn subagents in background (async) mode
- Always list model name (Sonnet or Opus) in subagent title like this: [Sonnet | Opus] <Title>

## Git / GitHub Instructions

- always use squash merge

## Your Location
If you are running on Windows, you are on the production server. 

If you are running on MacOS, you are on the development machine. Production database and logs are located here:
- Database: `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`
- Logs: 
    - `/Volumes/Users/jacks/AlphaMind/logs/bootstrap.out` 
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.err.log`
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.log`
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.out.log`
If these locations aren't accessible, alert the user

## Working with Linear
Issue hierarchy:
- **Project** — one per section header in `docs/project-tracker.md`.
- **Issue** (feature, e.g., ALP-212 "Breach behavior") — one per bullet under "Ready for implementation". The body holds design-doc links, cross-feature `blockedBy`, the dependency graph, and orchestrator-specific notes.
- **Sub-issue** (user story, e.g., ALP-230 "04a — Zone classifier") — one per implementation step, drafted via `/draft-user-stories` using the User Story format: `Goal` / `Reading` / `Depends on` / `Scope` / `Acceptance criteria` / `Verification`.

### Linear MCP gotchas

- **Relation fields on `save_issue` are append-only.** Passing a different `blockedBy` / `blocks` / `relatedTo` / `links` list does NOT remove existing relations — it adds. Use `removeBlockedBy` / `removeBlocks` / `removeRelatedTo` to clear; pass both add and remove in the same call to swap atomically. Most common silent-corruption hazard when updating an issue's dependency graph.
- **Marking a duplicate requires both `state="Duplicate"` and `duplicateOf=<canonical-id>`** on `save_issue`. The state alone leaves the issue canceled but unlinked.
- **Status names are case-sensitive.** AlphaMind team statuses: `Backlog`, `Todo`, `In Progress`, `In Review`, `Blocked`, `Done`, `Canceled`, `Duplicate`. Pass via the `state` parameter; `"todo"` or `"To Do"` will not match.
- **`list_issues` truncates long descriptions** (with a `(truncated, use 'get_issue' for full description)` marker). For the full body of any issue, call `get_issue` directly.
- **`get_issue` omits `blockedBy`/`blocks`/`relatedTo` by default.** Pass `includeRelations=true` to see the dependency graph.
- **Linear Projects mirror `docs/project-tracker.md` section headers verbatim** — `Foundation`, `Data layer`, `Distillation layer`, `Risk guardrails`, `Analysis layer`, `Decision layer`, `Execution layer`, `Operational tooling`. Use these names directly for the `project` parameter.
- **The Linear renderer silently drops content in several markdown patterns.** (1) Sub-bullet lists nested under numbered list items keep only the first bullet. (2) Bullet lists directly following a colon-ending paragraph keep only the first bullet — use inline prose or insert a heading before the list. (3) Long runs of same-prefix sub-headings interleaved with code blocks (e.g., `## Scope — Part A`, `## Scope — Part B`, `## Scope — Part C` …) can drop intermediate headings and their content; use bold-text labels (`**(A) Title.**`) under a single heading instead. Re-fetch and verify after every `save_issue`.