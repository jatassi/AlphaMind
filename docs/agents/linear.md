# Working with Linear

Referenced from the root `CLAUDE.md` "Linear" section.

## Issue hierarchy

- **Project** — one per section header in `docs/project-tracker.md`. Linear Projects
  mirror those headers verbatim: `Foundation`, `Data layer`, `Distillation layer`, `Risk
  guardrails`, `Analysis layer`, `Decision layer`, `Execution layer`, `Operational
  tooling`. Use these names directly for the `project` parameter.
- **Issue** (feature, e.g. ALP-212 "Breach behavior") — one per bullet under "Ready for
  implementation". The body holds design-doc links, cross-feature `blockedBy`, the
  dependency graph, and orchestrator notes.
- **Sub-issue** (user story, e.g. ALP-230 "04a — Zone classifier") — one per
  implementation step, drafted via `/draft-user-stories` using the User Story format:
  `Goal` / `Reading` / `Depends on` / `Scope` / `Acceptance criteria` / `Verification`.

## Active-issue cap

The repo is on Linear's free tier (250 active issues). For the exact active count, use
`uv run python scripts/check_linear_cap.py` (`--breakdown` / `--needed N` / `--json`) —
don't paginate `list_issues`. A recount must pass `includeArchived=false` to see
post-delete state. Cmd+Delete soft-archives (populates `archivedAt`) and frees the cap.

## MCP gotchas

- **Relation fields on `save_issue` are append-only.** Passing a different `blockedBy` /
  `blocks` / `relatedTo` / `links` list does NOT remove existing relations — it adds. Use
  `removeBlockedBy` / `removeBlocks` / `removeRelatedTo` to clear; pass both add and
  remove in the same call to swap atomically. The most common silent-corruption hazard.
- **Marking a duplicate requires both `state="Duplicate"` and `duplicateOf=<id>`** on
  `save_issue`. The state alone leaves the issue canceled but unlinked.
- **Status names are case-sensitive.** AlphaMind team statuses: `Backlog`, `Todo`, `In
  Progress`, `In Review`, `Blocked`, `Done`, `Canceled`, `Duplicate`. Pass via `state`;
  `"todo"` / `"To Do"` will not match.
- **`list_issues` truncates long descriptions** (with a `(truncated, use 'get_issue'…)`
  marker). For the full body, call `get_issue`.
- **`get_issue` omits `blockedBy`/`blocks`/`relatedTo` by default.** Pass
  `includeRelations=true` to see the dependency graph.

## Renderer drops content in these markdown patterns

The Linear renderer silently drops content — re-fetch and verify after every
`save_issue`:

1. Sub-bullet lists nested under numbered list items keep only the first bullet.
2. Bullet lists directly following a colon-ending paragraph keep only the first bullet —
   use inline prose or insert a heading before the list.
3. Long runs of same-prefix sub-headings interleaved with code blocks (e.g. `## Scope —
   Part A`, `## Scope — Part B`…) can drop intermediate headings and content; use
   bold-text labels (`**(A) Title.**`) under a single heading instead.
