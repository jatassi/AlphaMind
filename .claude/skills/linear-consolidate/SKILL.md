---
name: linear-consolidate
description: Use to consolidate AlphaMind's Linear workspace under the free-tier 250-issue cap by rolling up Done feature sub-issues into the parent Issue's body and deleting the sub-issues. Triggers on `/linear-consolidate <Feature>` and operator phrases like "free up Linear slots", "I'm hitting the Linear free-tier limit", "consolidate Done features", "roll up the X sub-issues", "clean up Linear", "the Linear workspace is full", "delete shipped sub-issues without losing history". The skill verifies parity between Linear sub-issue bodies and local archive files at `docs/_archive/implementation/<feature>/`, resolves drift via shipped `src/` as tiebreaker (the local archive is what survives deletion), drafts a rolled-up parent body that opens with a non-bullet paragraph (Linear's renderer drops bullets in the second list otherwise), saves it, and instructs the operator on bulk-deletion in the UI — the Linear MCP exposes neither archive nor delete. Auto-archive isn't an option (1-month minimum window). Do NOT use for non-AlphaMind workspaces, in-flight features, or features without local archive copies.
---

# Linear consolidation under free-tier ceiling

The operator names a Done AlphaMind feature whose Linear sub-issues should be rolled up into the parent Issue's body and then deleted, freeing slots against the free-tier 250-issue cap. The output of a successful run is: parent Issue body updated with a `## Shipped stories` index of every sub-issue, all sub-issues deleted by the operator in the Linear UI, and the workspace's Done count reduced by exactly the number of sub-issues deleted.

## Why this skill exists

Linear's free tier hard-caps the workspace at 250 active issues. AlphaMind hits this cap during long stretches of feature work because each feature parent has 10–30 sub-issues. Three consolidation mechanisms exist; only this one works:

- **Auto-archive:** minimum cool-off is 1 month — too slow to unblock new Issue creation now.
- **Manual archive:** does not exist in Linear ("Archiving happens automatically with no option to manually archive items").
- **Delete + roll up into parent body:** what this skill does. The deleted sub-issues vanish from the count, the parent retains a `## Shipped stories` index for archaeology, and full story bodies survive at `docs/_archive/implementation/<feature>/`.

## Inputs the operator provides

A feature name (e.g. `Configuration management`, `Collector`, `Portfolio state`). You resolve it to:

1. **Parent ALP-ID** via `mcp__linear-server__list_issues` filtered by `query=<feature name>` or by reading the project bullet in `docs/project-tracker.md` if needed.
2. **Local archive path** at `docs/_archive/implementation/<layer>/<feature>/` — e.g. `foundation/configuration/`, `01-data-layer/collector/`, `06-risk-guardrails/breach-behavior/`. List with `ls` to confirm the directory exists and contains one `.md` per expected sub-issue.

If the parent isn't in `Done` status, **stop** — the feature is in flight, sub-issues should not be deleted (orchestration may still need the dependency graph). Same if the local archive directory is missing — without local copies, deletion would lose the story content. In both cases, surface to the operator and exit.

## Hard rules

### 1. Body MUST start with a non-bullet paragraph

Linear's markdown renderer has a documented gotcha (and an undocumented one this skill exists to compensate for): when the body opens with a bullet list and is followed by `## heading` + bullet list, **all but the first bullet of the second list gets silently dropped on save**, even with the heading separator. This was hit in production during pilot 2 (ALP-30 Collector) — first save preserved 1 of 22 bullets.

The opening paragraph is load-bearing; lead with a one-sentence summary derivable from the rolled-up bullets themselves (e.g. "Multi-vendor data collector with bootstrap + steady-state runner, NSSM-supervised on Windows.") — *not* invented prose. After saving, **always re-fetch the issue via `get_issue` and confirm every bullet survived**. The save response contains the post-render body; checking it costs nothing and catches truncation immediately.

### 2. Drift resolution: `src/` is the tiebreaker

Linear sub-issue bodies and local archive files routinely drift — sub-issue bodies may have been edited via `/audit-user-stories`, or local files may have been touched post-implementation. When they disagree, **shipped `src/` code is ground truth**, not the spec. The local archive is what survives deletion, so it must reflect shipped reality; if Linear's body matches reality and the local file doesn't, update the local file (and vice-versa). For substantive schema/contract drift, read the actual `src/` modules to determine which version describes shipped behavior.

This isn't optional. A pilot run found one cosmetic drift (`18 entries` vs `19 entries`) and one substantive drift (entire Pydantic model redesign for the regimes bundle); the substantive one would have lost design intent if blindly resolved either way.

### 3. The Linear MCP cannot archive or delete issues

`save_issue` can update state, but there is no `archive_issue` or `delete_issue` tool. Both must happen in the UI. Don't try; don't promise the operator otherwise. The skill's job ends after the parent body is saved and verified — the operator does the bulk-delete by selecting all sub-issues in the parent's sub-issue panel and pressing Cmd+Delete.

### 4. Verification step uses a Sonnet subagent

Parity-checking 10–30 sub-issue bodies against local files is purely mechanical comparison — no synthesis, no judgment. Delegate it to a Sonnet subagent with explicit mapping (`ALP-XX ↔ filename.md`) and a strict ignore list (frontmatter, heading-level, link rewriting, `<issue id>` injections). The main thread keeps drift resolution and body drafting because those need cross-reference judgment.

## Procedure

### Phase 1 — Resolve and gather

1. Resolve the feature name to parent ALP-ID and confirm `status=Done`.
2. Confirm the local archive directory exists at `docs/_archive/implementation/<layer>/<feature>/`.
3. Fetch the parent body (`get_issue`) and the Done sub-issues (`list_issues parentId=ALP-X state=Done`) in parallel.
4. List the local archive files (`ls`).
5. Build an ID-to-filename mapping by user-story index (the local files use `01-…`, `02-…`, `03a-…` prefixes; Linear sub-issue titles start with `01 — `, `02 — `, etc.).

### Phase 2 — Verify parity

Dispatch a Sonnet subagent with the explicit mapping and instructions to:
- Fetch each Linear issue's body via `get_issue`
- Read each local file
- Compare for substantive parity (Goal, Scope, Acceptance criteria, Verification)
- Ignore frontmatter, heading-level, link rewriting, `<issue id>` injections, `## Depends on` sections present locally but not in Linear
- Return a markdown table: `| ALP-ID | File | Status | Notes |` with status = MATCH / DRIFT / OTHER, and a summary `X/N MATCH, Y DRIFT, Z OTHER. Safe to delete: <yes/no/conditional>`

Title the dispatch with `[Sonnet]` per the project's subagent-title convention.

#### MCP `get_issue` truncates at ~5KB

The MCP server truncates `description` on `get_issue` responses at roughly 5KB with a `...(truncated)` marker, despite the CLAUDE.md note implying it returns the full body. There's no flag to bypass and no MCP resource exposing the raw body. For long stories (typically anything with extensive Scope or Acceptance-criteria sections), the subagent can verify the visible prefix but cannot see the cut-off portions.

Instruct the subagent to flag truncation explicitly in its Notes column ("Linear truncated mid-§N — visible content matches"). When the subagent returns MATCH on a truncated body, that's a partial verdict — visible content matched, the rest is unknown. The main thread must close the gap before treating the parent as safe to delete.

#### Git-log + `updatedAt` spot check (when subagent flagged truncation)

When the subagent reports MATCH on bodies it flags as truncated by the API, run this spot check from the main thread before saving the parent body:

1. **Local-side freeze check** — `git log --all --follow --pretty=format:'%h %ai %s' -- docs/_archive/implementation/<layer>/<feature>/<one-of-the-stories>.md` (and the broader `git log --all --pretty=format:'%h %ai %s' --diff-filter=AM -- 'docs/implementation/<layer>/<feature>/*' 'docs/_archive/implementation/<layer>/<feature>/*' | head -30` to cover the pre-archive path). Note the latest content-edit timestamp (the archive-move commit doesn't count — it's a `git mv`).
2. **Linear-side freeze check** — note the `updatedAt` from the original `list_issues` response for each truncated story. Status-bump updates (e.g. marking Done) count, but no body edits typically follow.
3. **Inversion** — if the local content was finalized BEFORE the Linear bodies were created, Linear inherited from frozen-local at creation; neither has changed since; the truncated portions cannot have drifted. If Linear was created BEFORE the local file's last edit, the local file may have moved past Linear after divergence — investigate.

Surface the result to the operator: "8 stories had API-truncated bodies; visible content matched + git log shows local frozen since 2026-04-29 12:27 + Linear `updatedAt` ≤ 2026-05-01 19:20 → no drift possible; safe to proceed." If the freeze check fails (recent edits to either side), pause and surface to the operator — the residual risk is no longer bounded and the operator should decide whether to defer or spot-check via the Linear web UI.

### Phase 3 — Resolve drift

For each DRIFT row:

- **Cosmetic** (e.g. count off-by-one, minor wording): edit the loser inline. Tell the operator what you found before editing; trivial fixes don't need approval per the operator's existing policy on subagent suppressions, but visible drift is worth disclosing.
- **Substantive** (different schema, different file paths, different acceptance criteria): read the relevant `src/` modules to determine which version reflects shipped reality. Update the loser. Surface the finding and the resolution to the operator before proceeding to Phase 4.

If `src/` and *both* the Linear body and the local file disagree (rare), surface the three-way conflict to the operator and pause.

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

Bullets sorted by user-story index (01, 02, 03a–z, 04a–z, 05, 06a–b, 07, 08), not Linear ID. Use plain `ALP-XX` text — Linear auto-converts to `<issue id="UUID">ALP-XX</issue>` references that survive the target's deletion as broken-link archaeology.

**Drop from the original parent body:**
- The `[Stories]` link to `docs/implementation/<feature>/` (those staging dirs are gone after deletion)
- Orchestrator notes / dependency graph / `blockedBy` (moot post-ship)
- "Status: [x] done" checkboxes (redundant with the Issue's Done state)

### Phase 5 — Show operator and save

Show the proposed body in a markdown code block. Wait for operator approval before calling `save_issue`. After save:

1. Re-fetch the issue via `get_issue`.
2. Count the bullets in the returned `description`.
3. If the count matches the input bullet count, proceed. If it's lower, the renderer truncation bug fired — diagnose (almost always: missing or malformed opening paragraph), fix, re-save, re-verify.

### Phase 6 — Operator deletes, you recount

Tell the operator:
- Open the parent Issue in the UI
- Sub-issue panel → click first → Shift-click last → Cmd+Delete → confirm
- Say "done" when finished

When the operator says done, recount via `list_issues state=Done`. The response will be too large to fit in context — it'll be saved to a file. Parse with:

```python
import json
with open('<dump-file-path>') as f:
    data = json.load(f)
print(f'Total Done issues: {len(data["issues"])}')
print(f'hasNextPage: {data.get("hasNextPage")}')
```

Confirm the count dropped by exactly the number of sub-issues deleted. If it didn't, something went wrong (operator partially completed, network issue, etc.) — surface and investigate.

## Special cases

### Features with parallel sub-issue drafts in Done

Some features (Breach behavior ALP-212, Regime adaptation ALP-213) carry two parallel drafts of the same stories — typically because the work was redrafted at some point and both drafts shipped Done. The operator's policy is **canonical drafts only**: include the canonical (later) draft in the rollup, delete both drafts in the UI. Confirm with the operator which draft is canonical before drafting the body if not obvious from the title prefixes.

### Features with active gaps

Some features have non-Done sub-issues mixed into a contiguous Done range (e.g. Synthesizer ALP-114 with a gap at ALP-204; Domain researchers ALP-113 with gaps near ALP-185). The active sub-issues should not be touched — roll up only the Done ones, leave the gaps in place. The operator's UI delete will only select Done sub-issues if they're filtered correctly, but call this out so they don't accidentally include the active stragglers.

### Standalone To-dos (no rollup needed)

The "To-dos" project contains 13 standalone Done issues with no sub-issues — one-off bug fixes (e.g. ALP-266 "session.py should fail loudly…", ALP-267 "Suppress benign aclose() warning…"). These don't need rollup; the operator deletes them directly in the UI. If the operator asks for the To-dos cleanup, just enumerate them and confirm before they delete.

### Parent not in Done status

Skip the feature. The dependency graph is still load-bearing for `/orchestrate` if implementation is in flight. Surface this to the operator and offer to come back when the feature ships.

## Out of scope

- **Auto-archive policy changes.** The 1-month minimum is too slow; no point configuring it.
- **`docs/implementation/<feature>/` references in the rollup body.** Those staging dirs don't survive deletion. Local copies live at `docs/_archive/implementation/<layer>/<feature>/` only.
- **Inventing description prose.** The opening sentence must be derivable from the rolled-up bullets — if you can't summarize the feature factually from its own shipped stories, ask the operator for one.
- **Trying to archive/delete via MCP.** The MCP doesn't expose those tools and won't ever; trust the UI workflow.

## Cumulative tracking

After each successful consolidation, the project memory file `project_linear_consolidation.md` should reflect the cumulative recovery. The operator may ask for a status check ("what have we cleaned up so far?"); pull the running tally from there or from `git log` of MEMORY.md edits.
