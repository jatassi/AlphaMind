---
name: linear-consolidate
description: Use to consolidate AlphaMind's Linear workspace under the free-tier 250-issue cap by rolling up Done feature sub-issues into the parent Issue's body and archiving the sub-issues. Triggers on `/linear-consolidate <Feature>` and operator phrases like "free up Linear slots", "I'm hitting the Linear free-tier limit", "consolidate Done features", "roll up the X sub-issues", "clean up Linear", "the Linear workspace is full", "archive shipped sub-issues without losing history". The skill verifies parity between Linear sub-issue bodies and local archive files at `docs/_archive/implementation/<layer>/<feature>/`, resolves drift via shipped `src/` as tiebreaker (the local archive is the in-repo canonical record), drafts a rolled-up parent body that opens with a non-bullet paragraph (Linear's renderer drops bullets in the second list otherwise), saves it, and archives the sub-issues via `scripts/archive_linear_issues.py` after operator approval. The supporting scripts (`check_linear_cap.py`, `export_linear_issues.py`, `linear_parity_diff.py`, `archive_linear_issues.py`) hit the Linear GraphQL API directly — the Linear MCP cannot count issues, archive them, or return un-truncated bodies. Do NOT use for non-AlphaMind workspaces, in-flight features, or features without local archive copies.
---

# Linear consolidation under free-tier ceiling

The operator names a Done AlphaMind feature whose Linear sub-issues should be rolled up into the parent Issue's body and then archived, freeing slots against the free-tier 250-issue cap. The output of a successful run is: the parent Issue body updated with a `## Shipped stories` index of every sub-issue, every Done sub-issue archived via `scripts/archive_linear_issues.py`, and the workspace's active (non-archived) issue count reduced by exactly the number archived.

This skill leans on four scripts under `scripts/` that hit the Linear GraphQL API directly — the Linear MCP server can neither count issues, archive them, nor return un-truncated issue bodies:

- `check_linear_cap.py` — exact active-issue count and buffer to the cap (`--breakdown` splits it by state and project).
- `export_linear_issues.py` — fetch sub-issue bodies (full, no truncation) into local archive files.
- `linear_parity_diff.py` — diff Linear sub-issue bodies against the local archive files.
- `archive_linear_issues.py` — archive a parent's Done sub-issues (dry-run by default; `--apply` to execute).

All four read `LINEAR_API_KEY` from the repo-root `.env`.

## Why this skill exists

Linear's free tier hard-caps the workspace at 250 active (non-archived) issues. AlphaMind hits this cap during long stretches of feature work because each feature parent has 10–30 sub-issues. The fix is to roll a shipped feature's Done sub-issues up into the parent Issue's body (a `## Shipped stories` index for archaeology), then archive the sub-issues — archived issues stop counting against the cap.

Archiving is done by `archive_linear_issues.py`, which calls the GraphQL `issueArchive` mutation. It archives rather than trashes: archived issues are recoverable indefinitely via Linear's archive view, whereas trashed issues are purged after 30 days. Auto-archive is not an option — its minimum cool-off is one month, too slow to unblock new Issue creation now.

## Inputs the operator provides

A feature name (e.g. `Configuration management`, `Collector`, `Portfolio state`). You resolve it to:

1. **Parent ALP-ID** — via `mcp__linear-server__list_issues` filtered by `query=<feature name>`, or by reading the project bullet in `docs/project-tracker.md`.
2. **Local archive path** at `docs/_archive/implementation/<layer>/<feature>/` — e.g. `foundation/configuration/`, `01-data-layer/collector/`, `06-risk-guardrails/breach-behavior/`. List with `ls` to confirm the directory exists and holds one `.md` per expected sub-issue.

If the operator doesn't know which feature to consolidate, run `uv run python scripts/linear_consolidation_candidates.py` — it lists every parent whose sub-issues are all Done, ranked by the slots each rollup would free.

If the parent isn't in `Done` status, **stop** — the feature is in flight, its sub-issues should not be archived (orchestration may still need the dependency graph). Surface to the operator and exit.

**If the local archive directory is missing, do NOT proceed to consolidation — but do NOT exit either.** Run Phase 1.5 (Materialize archives) first to write the sub-issue bodies to disk before they're archived. This is the most common drift between feature shipping and consolidation: features completed in long runs (typically execution-layer or analysis-layer waves) often skip the archive-move step that older features (foundation, data-layer, risk-guardrails) had. See Hard rule 0 and Phase 1.5 below.

## Hard rules

### 0. Local archives before archiving the sub-issues

The `## Shipped stories` index in the rolled-up parent body carries only titles + IDs. Every sub-issue's substantive content — the `Goal`, `Reading`, `Scope`, `Acceptance criteria`, `Verification` sections — lives only in the sub-issue body itself. The local archive at `docs/_archive/implementation/<layer>/<feature>/` is the in-repo canonical record of that content: it does not depend on Linear and travels with the codebase.

Archived Linear issues stay recoverable indefinitely via Linear's archive view, so archiving (unlike a true delete) is not a point of no return. But do not rely on the archive view as the design record — **materialize the local archive first via Phase 1.5** whenever the directory is missing or sparse. The step is a single `export_linear_issues.py` invocation; skipping it leaves the feature's design content reachable only by un-archiving issues one at a time in the Linear UI.

### 1. Body MUST start with a non-bullet paragraph

Linear's markdown renderer has a documented gotcha (and an undocumented one this skill exists to compensate for): when the body opens with a bullet list and is followed by `## heading` + bullet list, **all but the first bullet of the second list gets silently dropped on save**, even with the heading separator. This was hit in production during pilot 2 (ALP-30 Collector) — first save preserved 1 of 22 bullets.

The opening paragraph is load-bearing; lead with a one-sentence summary derivable from the rolled-up bullets themselves (e.g. "Multi-vendor data collector with bootstrap + steady-state runner, NSSM-supervised on Windows.") — *not* invented prose. After saving, **always re-fetch the issue via `get_issue` and confirm every bullet survived**. The save response contains the post-render body; checking it costs nothing and catches truncation immediately.

### 2. Drift resolution: `src/` is the tiebreaker

Linear sub-issue bodies and local archive files routinely drift — sub-issue bodies may have been edited via `/audit-user-stories`, or local files may have been touched post-implementation. When they disagree, **shipped `src/` code is ground truth**, not the spec. The local archive is the surviving in-repo record, so it must reflect shipped reality; if Linear's body matches reality and the local file doesn't, update the local file (and vice-versa). For substantive schema/contract drift, read the actual `src/` modules to determine which version describes shipped behavior.

This isn't optional. A pilot run found one cosmetic drift (`18 entries` vs `19 entries`) and one substantive drift (entire Pydantic model redesign for the regimes bundle); the substantive one would have lost design intent if blindly resolved either way.

### 3. Archiving runs through the script, not the MCP or the UI

The Linear MCP exposes no archive or delete tool — `save_issue` only updates fields. Archiving happens via `scripts/archive_linear_issues.py`, which calls the GraphQL `issueArchive` mutation. The script is **dry-run by default**: a plain invocation prints exactly what it would archive and changes nothing; `--apply` performs the archive. Never hand the operator a manual Cmd+Delete instruction — the script is the mechanism, and the operator's role is to approve the dry-run plan before you re-run with `--apply`.

### 4. Parity verification runs through the diff script

Parity-checking 10–30 sub-issue bodies against local files is a deterministic text comparison — `linear_parity_diff.py` does it directly, no subagent. It fetches every body via GraphQL (no ~5KB MCP truncation), matches each to a local file by user-story index, normalises away the differences the check is meant to ignore (frontmatter, heading level, `<issue id>` cross-ref markup), and reports `MATCH` / `DIFFERS` (with a unified diff) per story. The main thread keeps drift resolution and body drafting — those need cross-reference judgment.

## Procedure

### Phase 1 — Resolve and gather

1. Resolve the feature name to its parent ALP-ID and confirm `status=Done`.
2. Confirm the local archive directory exists at `docs/_archive/implementation/<layer>/<feature>/` (if missing or sparse, Phase 1.5 will populate it).
3. Fetch the parent body (`get_issue`) and the Done sub-issues (`list_issues parentId=ALP-X state=Done`).
4. List the local archive files (`ls`).
5. Build an ID-to-filename mapping by user-story index (the local files use `01-…`, `02-…`, `03a-…` prefixes; Linear sub-issue titles start with `01 — `, `02 — `, etc.).
6. Capture the baseline cap count: run `uv run python scripts/check_linear_cap.py` and note the `active (non-archived) issues` figure. Phase 6 re-runs the script; the drop should equal the number of sub-issues archived.

### Phase 1.5 — Materialize missing archives (when needed)

If `docs/_archive/implementation/<layer>/<feature>/` doesn't exist, or holds fewer files than the Done sub-issue count, write the sub-issue bodies to disk before continuing.

1. Confirm the `<layer>` segment matches the existing convention — `ls docs/_archive/implementation/` first (`foundation`, `01-data-layer`, `02-distillation-layer`, `03-analysis-layer`, `04-decision-layer`, `05-execution-layer`, `06-risk-guardrails`).
2. Run the export script — it resolves the parent, fetches every sub-issue body via GraphQL (full, no truncation), and writes one `.md` per sub-issue named by user-story index (`03a — Bracket-thesis coverage cross-validator` → `03a-bracket-thesis-coverage-cross-validator.md`):

       uv run python scripts/export_linear_issues.py ALP-X \
           --out-dir docs/_archive/implementation/<layer>/<feature>

   Add `--include-archived` if some sub-issues were already archived in a prior partial run.
3. Verify the file count matches the sub-issue count — the script's summary line reports `N written, M empty`. An `EMPTY` row means a sub-issue had no body; surface it to the operator.

**Skip Phase 2 when archives were just materialized this way.** The local file *is* the Linear body verbatim — parity is perfect by construction. Jump straight to Phase 4.

### Phase 2 — Verify parity

Run the parity-diff script against the pre-existing local archive:

    uv run python scripts/linear_parity_diff.py ALP-X \
        --archive-dir docs/_archive/implementation/<layer>/<feature>

It prints a per-story table — `MATCH`, `DIFFERS`, `LINEAR-ONLY` (a sub-issue with no local file), `LOCAL-ONLY` (a local file with no sub-issue) — followed by a unified diff for every `DIFFERS` row. Exit code is `0` when every story matches, `1` when any drift is found.

- All `MATCH` → proceed to Phase 4.
- Any `DIFFERS` / `LINEAR-ONLY` / `LOCAL-ONLY` → Phase 3.

### Phase 3 — Resolve drift

For each `DIFFERS` row, read the unified diff the script printed:

- **Cosmetic** (e.g. count off-by-one, minor wording): edit the loser inline. Tell the operator what you found before editing; trivial fixes don't need approval per the operator's existing policy, but visible drift is worth disclosing.
- **Substantive** (different schema, different file paths, different acceptance criteria): read the relevant `src/` modules to determine which version reflects shipped reality (Hard rule 2 — `src/` is the tiebreaker). Update the loser. Surface the finding and the resolution to the operator before proceeding to Phase 4.

A `LINEAR-ONLY` row means a sub-issue has no local archive file — re-run Phase 1.5's export (with `--include-archived` if needed); it writes the missing file. A `LOCAL-ONLY` row means a stray local file with no matching sub-issue — confirm with the operator whether it's an obsolete draft to delete or a misindexed file to rename.

If `src/` and *both* the Linear body and the local file disagree (rare), surface the three-way conflict to the operator and pause. Re-run the parity-diff script after resolving until it exits `0`.

### Phase 4 — Draft the rolled-up parent body

Compose the body with this exact shape:

```markdown
<one-sentence summary derivable from the rolled-up bullets — not invented prose>. [Design](<docs/design/<layer>/<feature>/>).

## Shipped stories

- ALP-XX — 01 — <title>
- ALP-YY — 02 — <title>
- ALP-ZZ — 03a — <title>
…
```

Bullets sorted by user-story index (01, 02, 03a–z, 04a–z, 05, 06a–b, 07, 08), not Linear ID. Use plain `ALP-XX` text — Linear auto-converts to `<issue id="UUID">ALP-XX</issue>` references that survive the sub-issue being archived as live links into the archive.

**Drop from the original parent body:**
- The `[Stories]` link to `docs/implementation/<feature>/` (those staging dirs are gone after consolidation)
- Orchestrator notes / dependency graph / `blockedBy` (moot post-ship)
- "Status: [x] done" checkboxes (redundant with the Issue's Done state)

### Phase 5 — Show operator and save

Show the proposed body in a markdown code block. Wait for operator approval before calling `save_issue`. After save:

1. Re-fetch the issue via `get_issue`.
2. Count the bullets in the returned `description`.
3. If the count matches the input bullet count, proceed. If it's lower, the renderer truncation bug fired — diagnose (almost always: missing or malformed opening paragraph), fix, re-save, re-verify.

### Phase 6 — Archive the sub-issues, then recount

1. Dry-run the archive to produce the plan — it changes nothing:

       uv run python scripts/archive_linear_issues.py --parent ALP-X

   The script lists every Done sub-issue as `WOULD-ARCHIVE` and reports any non-Done sub-issue as `SKIP-NOT-DONE` (see "Features with active gaps" below — handled automatically).
2. Show the operator the plan and get explicit approval.
3. On approval, re-run with `--apply`:

       uv run python scripts/archive_linear_issues.py --parent ALP-X --apply

4. Recount: `uv run python scripts/check_linear_cap.py`. Compare `active` against the Phase 1 baseline — the drop should equal the number archived. If it didn't, something went wrong (a per-issue archive failed, network issue) — the script's own output flags `FAILED` rows; surface and investigate. The freed `buffer` is the headline result of the run.

To archive a hand-picked set rather than a whole parent's Done children, use `--ids ALP-a,ALP-b,…` instead of `--parent`.

## Special cases

### Features with parallel sub-issue drafts in Done

Some features (Breach behavior ALP-212, Regime adaptation ALP-213) carry two parallel drafts of the same stories — typically because the work was redrafted at some point and both drafts shipped Done. The operator's policy is **canonical drafts only**: index only the canonical (later) draft in the rollup body, but archive both drafts. `archive_linear_issues.py --parent` archives every Done sub-issue, so both drafts go; confirm with the operator which draft is canonical before drafting the body if it isn't obvious from the title prefixes.

### Features with active gaps

Some features have non-Done sub-issues mixed into a contiguous Done range (e.g. Synthesizer ALP-114 with a gap at ALP-204; Domain researchers ALP-113 with gaps near ALP-185). `archive_linear_issues.py --parent` handles this automatically — it archives only the Done sub-issues and reports each non-Done one as `SKIP-NOT-DONE`, leaving the in-flight stories untouched. The rolled-up body should likewise index only the Done stories.

### Standalone To-dos and workspace-wide sweep

The "To-dos" project holds standalone Done issues with no sub-issues — one-off bug fixes (e.g. ALP-266 "session.py should fail loudly…", ALP-267 "Suppress benign aclose() warning…"). These need no rollup; archive them directly with `archive_linear_issues.py --ids ALP-a,ALP-b,…` after confirming the list with the operator.

For dead-weight beyond shipped features, `uv run python scripts/linear_stale_sweep.py` finds non-archived Canceled / Duplicate issues — they count against the cap too, and can be archived directly via `--ids`.

### Parent not in Done status

Skip the feature. The dependency graph is still load-bearing for `/orchestrate` if implementation is in flight. Surface this to the operator and offer to come back when the feature ships.

## Out of scope

- **Auto-archive policy changes.** The 1-month minimum is too slow; no point configuring it.
- **`docs/implementation/<feature>/` references in the rollup body.** Those staging dirs don't survive consolidation. Local copies live at `docs/_archive/implementation/<layer>/<feature>/` only.
- **Inventing description prose.** The opening sentence must be derivable from the rolled-up bullets — if you can't summarize the feature factually from its own shipped stories, ask the operator for one.

## Cumulative tracking

After each successful consolidation, the project memory file `project_linear_consolidation.md` should reflect the cumulative recovery. The operator may ask for a status check ("what have we cleaned up so far?"); pull the running tally from there, or get the current standing directly with `uv run python scripts/check_linear_cap.py --breakdown`.
