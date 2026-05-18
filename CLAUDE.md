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

**Always run pytest with `--testmon` and `-n auto`** unless one of the explicit exceptions below applies. This is mandatory for every pytest invocation — your own, subagent dispatches, worktree verification, the orchestrator's per-story checks, the implement-issue intermediate runs. The canonical invocation:

```bash
uv run pytest --testmon -n auto
```

`--testmon` skips tests whose Python dependencies haven't changed since the last run (cache in `.testmondata`, gitignored). `-n auto` allocates one worker per CPU core. When verifying a narrow slice, scope to the relevant path: `uv run pytest tests/config/ --testmon -n auto`.

**Drop `--testmon` and run the full suite ONLY when one of these specific scenarios applies:**
- Changing `conftest.py`, fixtures, or other test-collection hooks — testmon doesn't track collection-time graph changes
- Changing non-Python files tests depend on (YAML configs, JSON fixtures, SQL, schema files) — testmon only tracks Python imports
- Before claiming a feature done or opening a PR — final verification must exercise everything (this is the pre-PR / pre-merge gate, not intermediate per-story or per-wave checks)
- After pulling main or rebasing — the local cache reflects your prior state, not the merged state
- If you suspect the cache is stale or are seeing implausible skips — delete `.testmondata` and retry

Outside these documented exceptions, **never invoke `pytest` without both `--testmon` and `-n auto`** — including from subagents and worktree verification. When writing a verbatim pytest command into a dispatch prompt, skill file, story acceptance criterion, or PR test plan, include `--testmon -n auto` by default and only omit `--testmon` when the invocation is the explicit pre-PR / pre-merge final-verification run. If a test passes serially but fails under xdist, the cause is test-order dependence (typically `sys.modules` mutation or shared filesystem state). Fix the test — do not fall back to serial.

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