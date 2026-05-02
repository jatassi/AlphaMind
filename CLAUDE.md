# CLAUDE.md

## Linting

Run the linter after every batch of changes:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
```

Alert the user before disabling the linter or any rule in any form — including `ignore`, `per-file-ignores`, `# noqa`, and `# type: ignore`. If a subagent suppresses the linter, do not pause execution to alert user. Instead, assess whether each suppression was warranted and fix unwarranted suppressions.

## Testing

Always run the test suite parallelized via pytest-xdist:

```bash
uv run pytest -n auto
```

`-n auto` allocates one worker per CPU core. Never invoke `pytest` without `-n auto` — including from subagents and worktree verification. When verifying a narrow slice, scope to the relevant path: `uv run pytest tests/config/ -n auto`.

If a test passes serially but fails under xdist, the cause is test-order dependence (typically `sys.modules` mutation or shared filesystem state). Fix the test — do not fall back to serial.

## Spawning Subagents

- For mechanical changes, use Sonnet
- For all other changes, use Opus
- Always spawn subagents in background (async) mode
- Always list model name (Sonnet or Opus) in subagent title like this: [Sonnet | Opus] <Title>

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
- **The Linear renderer collapses sub-bullet lists nested under numbered list items**, dropping all but the first sub-bullet. When writing issue bodies, render structured detail under a numbered item as inline prose or a separate section, not as a nested bullet list.