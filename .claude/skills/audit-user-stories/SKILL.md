---
name: audit-user-stories
description: Use to audit and repair an existing AlphaMind feature's Linear work tree when the stories were drafted before some upstream layer landed (or otherwise drifted from current ground truth). Triggers on `/audit-user-stories <Feature>` and operator phrases like "audit the Domain researchers stories", "review existing user stories for the Synthesizer", "the Breach behavior stories were drafted before regime-adaptation completed — verify they still match", "check the X work tree's assumptions against current code", "the X stories may be stale", "reconcile the X stories with what landed". The skill resolves the Linear parent + sub-issues, captures a starting SHA for mid-flight commit detection, gathers current ground truth (design docs + the actual src/ code that landed since drafting + optional `python-architecture` audit-mode sweep), audits each story body for stale paths, contract drift, type-collision, schema mismatch, and duplicates/copy-paste body errors, then surfaces a severity-tagged findings report and — after operator confirms scope — runs a Linear cap pre-flight (only if repairs create new sub-issues) and applies targeted repairs (canceling moot stories, rewriting bodies to the User Story template with the fresh-agent/atomicity/editorial-discipline quality bars, wiring missing `blockedBy`, optionally rewriting the parent body with the breach-behavior pattern's renderer-safe structure), spot-checks the result, diffs mid-flight commits against the rewritten stories, and commits any repo edits — so the work tree is dispatch-ready via `/orchestrate`. Companion to the `draft-user-stories` skill — that one drafts a fresh work tree; this one repairs an existing one. Do NOT use for one-off bug fixes or work outside AlphaMind.
---

# Audit user stories for an AlphaMind feature

The operator names a feature whose Linear work tree exists but may have drifted from current ground truth — typically because the stories were drafted before an upstream layer (distillation, risk guardrails, etc.) completed, or because the project workflow shifted (e.g., file mirrors archived). You produce a severity-tagged discrepancy report, surface it for operator direction on repair scope, and then apply the chosen repairs to Linear.

The output of a successful run is a Linear work tree that an agent can pick up via `/orchestrate` and execute end-to-end without further requirements work — the same exit criterion as `draft-user-stories`, reached from a different starting state.

## Hard rule: do not delegate to subagents

You — the conversation thread the operator is in — must do the gathering, auditing, and drafting yourself. Do not spawn `Agent(...)` calls of any subagent type for any phase of this skill.

**Why:** Auditing a work tree requires holding the whole feature's design + the up/downstream contracts + the current state of the src/ tree + every sub-issue's body simultaneously. Drift findings emerge from cross-referencing — "story 08 names a `RegimeLabel` Pydantic class but `alphamind.distillation.regime.RegimeLabel` already exists as a StrEnum" is a synthesis only the thread that read both can produce. Fragmenting across subagents collapses that synthesis: each one re-reads partial context, finds isolated drift signals without seeing the full collision pattern, and produces a sprawl of unrelated nits instead of the coherent severity-grouped report the operator can act on.

**How to apply:** When the work feels heavy and you reach for `Agent`, instead read the next file yourself. If you genuinely run out of context, surface that to the operator and pause — do not paper over it with delegation.

The Explore agent for *finding* a file you can't locate by name is fine — that's a lookup, not delegation of judgment. The line is: you may delegate searches; you must not delegate reading, synthesis, drafting, or the severity grouping.

## Inputs

One required argument: the exact feature name as written in `docs/project-tracker.md` (e.g., `Domain researchers`, `Synthesizer`, `Breach behavior`). Match is case-insensitive but otherwise verbatim.

Optional context the operator may volunteer (capture and act on it):

- **Why now** — "the X stories were drafted before Y completed" tells you which sibling work tree to weight in Phase 2's gathering.
- **Adapt the procedure** — "no need to follow the procedure to the letter" or "focus on schema drift" tells you which phases to compress and which to deepen. Honor it; surface the adaptation in your Phase 4 report so the operator can confirm the audit scope was right.

If the feature name doesn't resolve to a single bullet under "Ready for implementation" or higher status, surface the candidates and ask which one. If the feature's status is `_requirements pending_` (no stories drafted yet), redirect to `draft-user-stories` — there is nothing to audit.

## Procedure

Six phases. Work through them in order. After each phase, briefly tell the operator what you found / chose so they can redirect early if you're off track.

### Phase 1 — Resolve the work tree

Read `docs/project-tracker.md` and locate the bullet matching the argument. Confirm status is `_stories drafted_`, `_in progress_`, or `_done_` (anything but `_requirements pending_`). Capture the section it lives under (the Linear Project name) and the design/architecture doc paths the bullet links.

**Capture the starting SHA.** Run `git rev-parse HEAD` and stash the value in working notes — Phase 5's spot-check wave uses it to diff mid-flight commits that landed while the audit ran. Audits routinely take 30-60+ minutes and the codebase moves; catching a sibling work tree's commit that overlaps audited paths is much cheaper here than after dispatch.

Then locate the Linear work tree:

```
list_issues(team="AlphaMind", project="<Section name>", query="<Feature name>")
get_issue(id="<parent ID>", includeRelations=true)
list_issues(team="AlphaMind", parentId="<parent ID>", limit=50)
```

Capture:

- The **parent Issue** — its current description (verbatim — you'll diff against this in Phase 5), its current `blockedBy` / `blocks` relations, its status.
- The **full sub-issue inventory** — for each sub-issue, the ID, title, current status, and `blockedBy` set. Note canceled sub-issues separately (status `Canceled` or `Duplicate`); they are part of the audit trail but not part of the active dispatch graph.
- The **local archive** — if `docs/_archive/implementation/<section>/<feature>/` exists, the archived files are a snapshot of the work tree as drafted. Useful for diffing intent vs. current Linear state, but the archive is **not authoritative** — Linear is.

Surface a short inventory to the operator before moving on (parent ID, count of active sub-issues, count of canceled, any obvious red flags you noticed during enumeration like duplicate numbering).

### Phase 2 — Gather current ground truth

The audit's job is to reconcile three layers:

1. **Design docs** — authoritative for the *contract* (what should be built).
2. **Actual `src/alphamind/` code** — authoritative for what *has* been built since the stories were drafted.
3. **Linear sub-issues** — the drift target. The audit's findings are the discrepancies between this layer and the first two.

Read in this order — design first, code second, archive last — so contract intent frames your reading of the as-built code.

#### Design + cross-cutting policy reads

Read the feature's design docs end-to-end. Then walk the up/downstream graph:

- **Upstream features whose contracts this feature consumes.** Read enough of each upstream design doc to understand the *shape* of the contract (typed records, function signatures, side-effect boundaries). Pay special attention to upstream features the operator named as "completed since drafting."
- **Downstream features that consume this feature's output.** Same depth.
- **Cross-cutting policies referenced in story bodies** — `docs/architecture/llm-integration.md`, `docs/design/llm-agent-failure-handling.md`, `docs/design/testing/llm-output-validation.md`, `docs/design/configuration-management.md`, `docs/architecture/infrastructure.md`, etc. Any policy a story mentions in its `## Reading` section gets read.

#### Source-tree reconnaissance

Survey the actual code that has landed. Critical questions:

- **What named types already exist** in upstream src packages that this feature's stories propose to define? Grep aggressively: `grep -rn "class <ProposedName>" src/`. Story 03's proposed `Sector` enum collides with nothing if no `Sector` is grep-able; collides with two existing definitions if grep returns hits. Per the user memory `feedback_no_inventing_component_names`, every collision is a P1+ finding.
- **What functions already produce the types this feature consumes?** `assemble_sector_output()`, `current_regime_label()`, `load_sector_roster()` — find them. Stories that propose to re-render or re-derive what already exists are doing redundant work. Per `feedback_simplify_before_building`, this is a P1 finding.
- **What schema columns actually exist** that stories propose to read or write? `grep -n "__tablename__\|class.*Base.*:" src/alphamind/persistence/models.py`. Stories that declare `tickers: tuple[str, ...]` against a column that's actually a single-row `ticker: TEXT NULL` need schema reconciliation. P1 finding.
- **What scaffolding is already in place?** Empty `__init__.py` stubs in the feature's package directory mean the structural decisions were made; new stories shouldn't redesign the layout.
- **What sibling work shipped recently** — `git log --oneline -30`, `git log --since=<stories drafted date>` — that the stories were drafted before? The commits that reference upstream feature names are the relevant ones.

**Use `python-architecture` audit mode for systematic coverage.** Ad-hoc grepping catches the obvious collisions but misses structural drift — primitive obsession that's metastasized since drafting, a shallow-module swarm an upstream introduced, layer violations that crossed into the feature's intended scope, mutable-default landmines in records the stories propose to consume. Invoke `Skill("python-architecture")` in audit mode scoped to the feature's package and each upstream package whose contract the stories lean on. Treat the audit findings as inputs to Phase 3's per-story checks:

- **Load-bearing findings that touch the audited stories' surface** (e.g., a `Money` primitive obsession in a record story 04 imports; a shallow-module swarm an upstream introduced that story 06 navigates) become P1 findings on the affected stories, with the repair being "fold the upstream fix into this story's scope" or "surface a coordinated-edit note to the operator".
- **High-yield findings inside the feature's planned scope** (naive datetimes, missing timeouts, mutable defaults) that the audited stories silently inherit become P2 findings — small acceptance-criteria additions, not new stories.
- **Findings outside the audited feature's scope** are *not* this audit's job. Surface them to the operator separately at Phase 4 close-out as candidate follow-on Linear issues — do not silently expand the audit's repair scope.

Skill invocation runs in your thread (no Agent dispatch), so it's compatible with the no-delegation rule. Skip the systematic audit when the feature is small, purely additive, and the source-tree grep above caught every named drift you needed.

#### Existing config + prompts reconnaissance

Read every config file the stories propose to create or modify. If `config/agents.yaml` already has the entries story 02 says to "create from scratch," that's a P1 finding (story scope is largely done). If `prompts/analysis/<agent>_researcher.md` already exists with substantial content, the prompt-drafting story should be re-scoped to verification.

Read these end-to-end, do not skim:

- `config/agents.yaml` — agent registry shape, current entries.
- `config/<feature>.yaml` if any — feature-specific config the stories reference.
- `prompts/analysis/*.md` and `prompts/decision/*.md` — existing system prompts.

#### Local archive reconciliation (advisory only)

If `docs/_archive/implementation/<section>/<feature>/` exists, it's a snapshot of the stories as drafted (often including an `ORCHESTRATOR.md` with the originally-intended dispatch notes). Useful for:

- Recovering rich orchestrator notes that may not have been carried forward to the parent issue's description.
- Confirming what the original drafting intended when a current Linear story body is corrupt or unclear.

But: **the archive is not load-bearing for the audit's findings**. Linear is the live work tree; the archive is history. Don't recommend the archive as a Reading source for orchestrator dispatch.

#### Bar for "enough context"

You can write each sub-issue's findings without re-opening any file. You know which functions, types, and config entries this feature should touch. You know the upstream contract names. You can identify a copy-pasted body without comparing the two raw bodies side by side. If you can't, keep reading.

### Phase 3 — Audit each sub-issue

For each active (non-canceled) sub-issue, read the full body via `get_issue(id, includeRelations=true)` and check it against ground truth across these dimensions. Group findings by issue ID; the severity grouping happens in Phase 4.

#### Body-integrity checks

These catch the cheap-but-catastrophic errors. Run first:

- **Body matches title.** A title says "11 — Parallel orchestrator" and the body opens with "06 — Sector qualitative input loader" — that's a copy-paste corruption. Surface immediately as P0; the orchestrator dispatching from this body would build the wrong thing.
- **Pure duplicates.** Two sub-issues with the same number prefix and near-identical bodies are duplicates that should be marked Duplicate-of one another. Note them; the cleanup is in Phase 6.
- **Bodies that match an archived story's stale framing** rather than the current Linear-only workflow (references to `docs/implementation/<feature>/...` files that should now be Linear sub-issues). Note for Phase 4.

#### Reading-list path resolution

Every path in the story's `## Reading` section must resolve in the current repo. Common stale patterns:

- `docs/implementation/<X>` paths — if the workflow shifted to Linear-only and these were archived, every reference is stale. Check `git log --diff-filter=D -- docs/implementation/` or `ls docs/_archive/implementation/<X>/` to confirm.
- `docs/architecture/<X>.md` paths that should be `docs/design/<X>.md` (and vice versa). Verify each by `find docs -name <X>.md`.
- Sub-feature design doc paths that have been renamed or moved.

P2 findings unless the entire Reading section dissolves, in which case P1.

#### Type / contract reconciliation

For each typed value object the story proposes to define or import:

- **Does the name already exist somewhere in src/alphamind?** Grep `class <Name>(`, `<Name> =`, `def <Name>(`. Document any collision and where the existing one lives.
- **If reused, does the values list match the existing one's vocabulary?** A story that proposes a `Sector` enum with values `Tech`, `Financials`, `Energy` collides with an upstream module already pinning `tech_semis`, `financials`, `energy` — vocabulary drift is a real finding even when the names match.
- **If newly introduced, does the proposed name collide with an existing distinct concept?** A new `RegimeContext` Pydantic model is fine even when `RegimeLabel` enum exists; a new `RegimeLabel` Pydantic model on top of the existing enum is naming drift. Per `feedback_no_inventing_component_names`, the bar is "either reuse or rename."
- **Does the story's prose describe the type's contract correctly?** A `time_horizon_hours: str` field whose docstring says "the integer hour count" is internally inconsistent — surface as P1.

For each function or service the story proposes to call:

- **Does the function exist?** Stories that name `run_external_distillation()` or `current_regime_label()` should resolve to live signatures; if not, surface the gap.
- **Does the signature match what the story expects to pass?** A story expecting `assemble_input_bundle(sector, regime_payload, distillation_output)` against an as-built `assemble_input_bundle(*, sector, distillation_text, qualitative_input)` is contract drift.

#### Schema reconciliation

For each database table or column a story proposes to read or write, confirm against `src/alphamind/persistence/models.py`:

- Column **exists** at the named name.
- Column **type** matches story expectations (TEXT vs. ARRAY vs. JSON; nullable vs. NOT NULL).
- Column **arity** matches (single-row `ticker: TEXT NULL` vs. story expecting `tickers: tuple[str, ...]` is a real divergence — the loader has to GROUP BY the parent key).
- The column-set the story names is complete (no `is_high_priority: bool` proposed against a table that has no such column).

Each schema mismatch is a P1 finding because it cannot be silently absorbed at implementation time — either the schema needs migration or the story scope changes.

#### Scope-already-done check

A story that describes work largely already shipped is a P1 finding. Indicators:

- The named output file exists and contains substantive content (`prompts/analysis/<agent>_researcher.md` with 13KB of prose, `config/agents.yaml` with the relevant entries).
- The named function exists in src/ with tests passing.
- The named scaffolding directory exists with non-empty contents.

Repair direction: rewrite the story as a verification story (round-trip the existing artifact through the parser/validator/schema-loader; only edit if verification fails). Do not delete the story unless every acceptance criterion is already met by shipped artifacts.

#### Dependency-graph integrity

For each sub-issue, compare the prose `## Depends on` list (free text) against the Linear `blockedBy` relations (typed):

- **Prose lists predecessor with no Linear relation** — silent ordering hazard; the orchestrator will dispatch this story before its real predecessor lands. P1.
- **Linear relation with no prose mention** — the prose is incomplete but Linear is correct; rewrite the prose to match. P2.
- **Prose lists a story that's been canceled** — surface; the prose probably listed an old story ID that's now Duplicate-of-something-else. P1.
- **`blockedBy` points to a Duplicate-marked story** rather than the canonical Issue — Linear silently keeps the Duplicate ID in the relation; surface as P1 and re-point.
- **Cycle in the graph** — A blocks B blocks C blocks A. Should be impossible by construction but check. P0 if found.

For the **parent issue's stated graph** (the ASCII diagram or prose graph in its body), verify each named edge is in fact wired as a Linear `blockedBy` relation. Mismatch is a P1 — the orchestrator skill reads the parent's graph as advisory and the relations as authoritative; a divergence between them produces wave-dispatch errors.

#### Parent-issue structural completeness

Audit the parent issue body separately. The dispatch-ready shape (matching the breach-behavior pattern) requires:

- A **purpose sentence** at the top.
- A `## Design and architecture` section linking the design docs.
- A `## Cross-feature dependencies` section naming upstream features by ALP ID with a one-line description of which contracts this feature consumes from each.
- A `## Dependency graph` section with the ASCII wave structure.
- A `## Notes for the orchestrator` section covering: type-reuse rules, model-selection guidance, architectural invariants (no-magic-numbers, fail-closed propagation, etc.), and surfacing conditions (when to pause and ask the operator vs. improvise).

Missing sections are P0 if the parent body is so thin the orchestrator skill can't dispatch from it (e.g., no dependency graph, no orchestrator notes); P1 otherwise.

### Phase 4 — Surface findings

Compose a discrepancy report, grouped by severity. Stop here and pause for operator direction — do not start applying repairs without confirmation.

**Severity rubric:**

- **P0 — must-fix before any dispatch.** Body corruption, true duplicates, missing parent dependency graph, broken Linear relations (cycles, points-to-canceled). Anything that would cause an orchestrator agent to do clearly-wrong work or refuse to start.
- **P1 — substantive ground-truth drift.** Type collisions with existing upstream names, schema mismatches with the data layer, scope already done, missing `blockedBy` relations the prose claims. Anything where dispatch would silently produce wrong output or duplicate work.
- **P2 — should-fix path/reference cleanup.** Stale Reading-list paths, prose vs. relation drift the relations are correct on, numeric anchors that should be config-loaded. Cosmetic but worth fixing for orchestrator clarity.

**Report format:**

For each finding, give the operator: the issue ID, a one-line description of the problem, the concrete repair you'd apply. Group by severity; within each group, group by issue ID for navigability.

End the report with a **path-of-action menu** offering the operator at least three repair-scope options. Default-recommended:

1. **Tactical patch.** Fix only P0 + relation cleanup. Smallest blast radius; lands in 30-60 min.
2. **Targeted refactor.** Tactical patch plus repurposing the wrongly-scoped stories (verification stories, schema-reconciliation stories) and any new shared-types story that consolidates collisions. Couple of hours.
3. **Targeted refactor + parent rewrite.** All of (2) plus rewriting the parent body to the breach-behavior pattern (cross-feature deps, dep graph, orchestrator notes). Most thorough; required when the parent body is missing structural sections.

Recommend one based on the severity profile you found. If the audit found one P0 (body corruption) and many P2s (path drift), tactical is right. If the audit found 5+ P1s and the parent body is thin, recommend (3).

**Then stop and wait.** The operator picks the path; do not start Phase 5 unilaterally.

### Phase 5 — Apply repairs

Execute the operator's chosen scope in this wave order. Each wave can run in parallel within itself (one `save_issue` call per issue, sent in a single message); waves are sequential because later waves depend on earlier waves' outcomes.

#### Pre-flight: Linear free-tier cap check

If the operator's chosen scope creates any new sub-issues (e.g., a shared-types consolidation story, a verification-conversion that splits one story into two), run the cap check before any writes. Linear's free tier caps the workspace at roughly 250 active (non-archived) issues; hitting it mid-repair leaves the work tree half-fixed and forces an out-of-band consolidation rollup before you can resume.

Compute the count and run the check:

- `needed` = number of new sub-issues the repair will create. If the repair only rewrites + cancels existing issues (the common case), `needed = 0` and you can skip — `save_issue(id=...)` updates don't consume cap.
- For `needed > 0`:

```
uv run python scripts/check_linear_cap.py --needed <needed> --json
```

The script paginates the workspace via Linear's GraphQL API and prints one JSON line with `active`, `cap`, `buffer`, `needed`, `margin`, `required`, and `ok`. It applies a 2-issue safety margin internally. Exit code mirrors `ok`: `0` = clear, `1` = cap risk, `2` = error.

Decision:

- **`ok == true`** — proceed to Wave 1.
- **`ok == false`** — surface to the operator *before any writes*. Report the numbers from the JSON. Recommend running `/linear-consolidate` (per the `project_linear_consolidation` memory) or `scripts/linear_consolidation_candidates.py` to free slots. Ask whether to (a) pause for rollup, (b) proceed accepting cap-risk and inline the un-created stories in the hand-off, or (c) trim repair scope to drop the new creations.
- **exit 2** — script failure (missing `LINEAR_API_KEY` in `.env`, network/API error). Surface and resolve before proceeding; don't fall back to manual MCP counting.

Skip this pre-flight entirely if the repair scope is pure-update (no `save_issue` calls without an `id`).

#### Wave 1 — Duplicate cleanup

For each pair of duplicate sub-issues:

1. Pick the canonical one (usually the older `createdAt`, but check operator preference if multiple were touched recently).
2. On the duplicate: `save_issue(id=<dup>, state="Duplicate", duplicateOf=<canonical>)`. Linear's `Duplicate` state automatically routes to the canonical issue.
3. On the canonical (if also moot for other reasons, e.g., its target was archived): `save_issue(id=<canonical>, state="Canceled", description=<body explaining why and pointing to the dup>)`.

For genuinely moot stories (e.g., one whose sole purpose was a link to an archived directory):

1. `save_issue(id=<id>, state="Canceled", description="<short body explaining why moot, with audit trail>")`.

Do **not** delete sub-issues. Keep the audit trail visible to future reviewers.

#### Wave 2 — Parent rewrite (if scope includes it)

Compose the new parent body in the breach-behavior pattern. Use the existing parent body's purpose sentence verbatim if it's accurate; rewrite if not.

Sections to cover (drop a section only if it genuinely doesn't apply):

- One-sentence purpose.
- `## Design and architecture` — bullet list of design docs + cross-cutting policy refs.
- `## Cross-feature dependencies` with two subsections: hard contract dependencies (upstream features by ALP ID, with the named types/functions this feature consumes) and sequencing context (recently-landed work that doesn't gate dispatch but is worth naming).
- `## Pre-resolved configuration decisions` — only if any drafting-time decisions were resolved during the audit (rare; usually audit findings either land as story-body rewrites or surface as operator follow-ups). When present, use bold-text paragraphs not bullet lists.
- `## Dependency graph` — ASCII showing wave structure (see rendering constraints below).
- `## Notes for the orchestrator` — type-reuse hard rule, model-selection nuances, architectural invariants, surfacing conditions.

**ASCII-graph rendering constraints (Linear).** Linear renders ASCII graphs as plain code blocks, but the container wraps on narrower viewports. Keep the graph readable:

- Cap line width at ~60 characters before any trailing arrow / box-drawing character. Pad short lines with spaces so column-anchored arrows stay aligned.
- Avoid right-edge box-drawing characters (`│`, `┐`, `┘`) past column 60 — they wrap and the visual mapping breaks.
- For wide work trees (5+ parallel stories at one position), prefer multi-line stanzas with `(parallel: ...)` annotations on a separate line below rather than a single horizontal fan-out.
- Re-fetch the parent after saving and skim the rendered graph; narrow and re-save if any line wrapped.

**Inline-code-in-bold-prefix gotcha.** A bold prefix containing inline code — `**(A) `code` rest of label.**` — is truncated by Linear's renderer at the first backtick; the closing `**` lands inside the inline-code span and bold ends prematurely. Keep the bold prefix free of inline code (move backticks into the following prose) or rephrase the label without the code reference.

**Bullet-list-after-colon-or-heading collapse.** The Linear renderer drops bullet lists that immediately follow a colon-ending paragraph or a heading-then-prose stanza, keeping only the first bullet. Use inline prose, insert a heading break, or use bold-text paragraphs (`**Label.** Body sentence.`) instead.

**Auto-converted `ALP-XXX` references.** When you re-fetch a body you just wrote, the renderer will have auto-converted naked issue references (`ALP-227`) into `<issue id="...">ALP-227</issue>` tags. This is cosmetic — the rendered display is unchanged — but the round-tripped body is not byte-identical to what you sent. Don't chase the diff.

Apply via `save_issue(id=<parent>, description=<new body>)`. If the original body had operator-authored content (a custom "additional context" section etc.), preserve it — diff and merge mentally before saving. After saving, re-fetch with `get_issue(id, includeRelations=true)` and verify the dependency graph, bold-paragraph stanzas, and section headers rendered as intended; if any section collapsed (the bullet-list-after-colon trap is the most common), reshape and re-save.

#### Wave 3 — Sub-issue body rewrites

For each story flagged P0 or P1 with body-rewrite repairs, apply via `save_issue(id=<id>, title=<new if changed>, description=<new body>)`.

Order matters within this wave: rewrite stories that **define types** before stories that **import them**. Story 02's repurposing as the shared-types module owner needs to land before story 03's "import from `_shared`" rewrite, because story 03's rewrite references `_shared.py`. In practice the rewrites are all save calls (no actual code lands at this stage), so ordering matters only for cross-references in prose — but readers (including the operator skimming the diff) benefit from reading them in dependency order.

For verification-conversion stories (the 09a/b/c pattern — convert "draft the prompt" to "round-trip the existing prompt through parser+validator"), use the original story's structural sections as the template; rewrite the Goal, Scope, and Acceptance criteria; preserve the Reading list and Out-of-scope sections where still applicable.

**User Story body shape (mirror this for every rewritten story).** Each story has `## Goal`, `## Reading`, `## Depends on`, `## Scope` (with numbered named-deliverable subsections + `### Out of scope`), `## Acceptance criteria`, and `## Verification` — the same template `draft-user-stories` Phase 7 uses. When rewriting, keep the rewritten body within this skeleton; don't invent new top-level sections or drop required ones. If a section genuinely doesn't apply (rare), state that inline rather than removing the heading.

**Quality bars every rewrite must clear:**

- **Fresh-agent test.** A competent engineer who has read no design docs and only the rewritten story can implement it. They must follow the Reading list, but they should not have to ask clarifying questions. If the rewrite leaves an agent guessing about an interface contract, scope boundary, or dependency mechanic, it's underspecified — fix before saving.
- **Atomicity test.** Each acceptance criterion asserts one observable outcome, mark-able pass/fail by running one command or reading one file. Umbrella criteria like "the module works" or "tests pass" are not atomic — split into specific named-function-with-named-input behaviors.
- **Editorial discipline.** Write the final shape, not your drafting process. Phrases like "Wait, this is a third method. Let me include it", "Actually, on second thought…", or "Let me consider…" are thinking-out-loud residue. They survive Linear's render and clutter the body for the agent picking it up. Edit them out before `save_issue`. Same applies to internal contradictions ("X is NOT in the API. … Implement X as a third method.") — converge to one positive statement. Don't write decision-trail prose ("this story used to say X but now says Y") — the audit trail lives in Linear's revision history, not the body.

**No-numeric-anchor reminder.** Per `feedback_avoid_numeric_anchors`, don't bake numeric thresholds (`70/85/95`, `60%`, `30 minutes`) into acceptance criteria. Reference the config source (`config/<feature>.yaml`, the relevant `Config` class) and let the verification test load the value.

**No-invented-names reminder.** Per `feedback_no_inventing_component_names`, every typed value object the rewritten Scope section names either mirrors an existing upstream record or is named by the design doc. If the rewrite introduces a new name without a design-doc precedent, stop — re-read or surface to the operator before saving.

The Linear renderer caveats in Wave 2 (bullet-list-after-colon collapse, inline-code-in-bold-prefix, ASCII-graph constraints, ALP-XXX auto-conversion) apply to sub-issue bodies too — apply the same defensive choices.

#### Wave 4 — Relations cleanup

Compute the desired `blockedBy` set for each sub-issue from the new parent's dependency graph. For each:

- `desired - current` → `blockedBy` (Linear is append-only, so this just adds; no risk of stomping the existing set).
- `current - desired` → `removeBlockedBy`.
- If both non-empty, pass both in the same `save_issue` call.

Also check `blocks` relations (the inverse direction). Linear typically maintains both directions automatically when you set `blockedBy`, so an explicit `blocks` adjustment is rarely needed — but if Phase 3 found `blocks` relations pointing at canceled issues, clean them up here.

#### Wave 5 — Spot-check verification

After all waves apply, spot-check 2–3 critical issues with `get_issue(id, includeRelations=true)`:

- The most-rewritten story (verify body looks right after the renderer's quirks).
- The story with the largest `blockedBy` change (verify the relation set is exactly the desired set).
- The end-of-graph story (verify it's gated on the right predecessors).

If anything's off, surface immediately and re-issue the corrective `save_issue` calls. Don't pretend a partial state is the desired state.

**Check for mid-flight commits to `main`.** Use the starting SHA captured in Phase 1: `git log <starting-sha>..main --oneline` (or `git log --oneline -20` as a fallback). For each commit that landed during the audit, run `git show --stat <sha>` and ask whether its file changes overlap any path or symbol referenced in the rewritten stories' Reading lists, Scope sections, or the parent body's cross-feature dependencies. Common overlap patterns: a fix to a file your rewrites extend (typed-record edits, MCP-server wrappers); a sibling work tree shipping a typed record a rewritten story depends on; a refactor renaming a symbol an acceptance criterion mentions. If overlap exists, surface to the operator with a one-line summary of each commit's impact — Reading-list pointers, Scope deliverables, acceptance criteria, and the parent's Pre-resolved decisions can all need touch-ups. The cost of catching this here is minutes; the cost of catching it after dispatch is a subagent diverging from a freshly-stale spec.

### Phase 6 — Close out

If the audit edited any tracked files in the repo (e.g., touched `docs/project-tracker.md` to fix a stale link, repaired a renamed design-doc path the stories reference), stage and commit those edits before reporting. Stage only the audit-edited files, commit with a short imperative subject mirroring the in-tree style (`chore(project-tracker): fix stale Reading-list paths for <feature>` or `docs(<area>): repair link drift surfaced by <feature> audit`), and push to the default remote. Linear-only repairs don't touch the repo, so the common case is no commit — but when the audit does change files, leaving them uncommitted creates a hand-off mismatch between the work tree's stated truth (post-audit) and what `git status` shows. Skip the commit only if the audit did not complete cleanly (a surfacing condition fired, the cap blocked late stories, etc.) — in that case leave the edits uncommitted and let the operator decide.

Report back to the operator in this shape (one short paragraph plus a structured summary):

- The total number of sub-issues touched (rewritten / canceled / relations updated).
- The final dependency-graph shape in one line.
- Any unresolved gaps the operator should know about (e.g., the `output_token_budget` divergence in `agents.yaml` that story 02 surfaces but does not unilaterally resolve).
- Confirmation that `/orchestrate <Feature>` is now a viable next step.
- The commit hash from any tracker/docs edits that landed, or "no repo edits" if the audit was Linear-only.
- A one-line offer to schedule a follow-up if the work tree has natural verification windows ("schedule an agent to verify the first dispatch wave goes cleanly?").

Then stop. The operator drives next steps from there.

## Linear specifics

### Project mapping

Same table as `draft-user-stories`:

| `docs/project-tracker.md` section | Linear Project          |
| --------------------------------- | ----------------------- |
| Foundation                        | Foundation              |
| Data layer                        | Data layer              |
| Distillation layer                | Distillation layer      |
| Risk guardrails                   | Risk guardrails         |
| Analysis layer                    | Analysis layer          |
| Decision layer                    | Decision layer          |
| Execution layer                   | Execution layer         |
| Operational tooling               | Operational tooling     |

### Audit-specific status posture

`Canceled` is for stories that are moot (preserve why in the body). `Duplicate` is for true duplicates of another active story. `Todo` is the default for sub-issues post-rewrite. **Leave `In Progress` stories untouched** — if the audit finds one that needs body changes, surface to the operator before applying; an audit doesn't reach into in-flight work.

For Linear MCP mechanics — append-only relations, status case-sensitivity, the renderer's sub-bullet collapse, `list_issues` truncation, `get_issue` omitting relations, the duplicate-marking dance — see `CLAUDE.md`'s "Linear MCP gotchas" section.

## Anti-patterns to avoid

- **Auditing the parent body and the sub-issues separately, in different threads.** The sub-issue findings often only make sense in the context of the parent's intended structure (e.g., "this story is duplicating types because the parent's type-reuse rule isn't documented"). Audit them together; structure the report so a parent finding can reference sub-issue findings and vice versa.
- **Treating the local archive as authoritative.** The archived `docs/_archive/implementation/<feature>/` directory is a snapshot of intent at drafting time. It is not what should be built today; the design docs and src code are. Use the archive only to recover orchestrator notes worth migrating to the parent.
- **Applying repairs before the operator confirms scope.** Phase 4's pause-and-ask is load-bearing. The operator may have context the audit didn't surface — a deferred design decision, an in-flight refactor, a story that's intentionally aspirational. Don't unilaterally repair past where you've been authorized.
- **Restating the story's original intent in the new body.** When rewriting a story from "draft this thing" to "verify this thing," the new body should describe the verification work — don't include a long preamble narrating "this story used to say X but now says Y." The audit trail lives in Linear's revision history, not in the description body.
- **Leaving relations unwired because the prose says it.** `## Depends on` prose is descriptive; `blockedBy` relations are authoritative for orchestration. If they disagree, fix the relations even when the prose looks fine.
- **Bundling a draft-from-scratch story into the audit's repair scope.** If the audit finds the work tree is missing a story (e.g., no shared-types story to consolidate the type collisions), surface to the operator. They may want to add it via `draft-user-stories` or have you do it inline — but inline-creating a new sub-issue without explicit scope from the operator is overreach.
- **Counting "saved successfully" responses from `save_issue` as proof the body is right.** The Linear renderer can subtly mangle a saved body (e.g., the sub-bullet collapse). Spot-check critical issues in Wave 5 by re-fetching with `get_issue` and reading the actual saved description.

## When you're done

Report back to the operator in this shape (concise):

- **Issues touched** — count and ID range, broken down by repair type (canceled / rewritten / relations-only).
- **Dependency-graph shape** — one line summarizing the wave structure of the post-repair tree.
- **Cross-feature `blockedBy` count** — how many edges cross to sibling work trees.
- **Unresolved gaps surfaced for follow-up** — specific items the operator needs to decide on (e.g., a config-value divergence, a schema migration scope question). These become the operator's next inputs.
- **Operator's path-of-action choice** — restate which scope they confirmed (so the conversation log captures it).
- **Suggested next step** — typically "dispatch via `/orchestrate <Feature>`" once the operator confirms.

Then stop.

## Self-improvement

While executing, note moments where this skill let you down: a step that was ambiguous, an edge case the procedure didn't anticipate, a Linear/MCP gotcha you hit and worked around, guidance that turned out wrong, a kind of drift the dimensions in Phase 3 didn't catch.

Don't fix the skill mid-flight — interrupting the audit costs more than it saves. Keep working notes mentally, and at the end — *after* reporting the work tree to the operator — propose specific edits in this shape:

- **Where:** the section/heading in this SKILL.md to change.
- **What:** the concrete edit (added bullet, replaced sentence, new subsection).
- **Why:** what went wrong without it.

Skip silently if nothing came up. The bar is "would have saved a step" or "would have prevented a mistake," not "could be marginally smoother." The operator decides what to apply.
