---
name: draft-user-stories
description: Use to draft Linear user stories for an AlphaMind feature listed in the "Ready for implementation" section of `docs/project-tracker.md`. Triggers on `/draft-user-stories <Feature>` and operator phrases like "draft user stories for Breach behavior", "decompose Domain researchers into stories", "write requirements for the Synthesizer feature", "break State persistence into user stories", "set up the work tree for Portfolio manager". The skill gathers context from the feature's design and architecture docs (and up/downstream features), decomposes the work into ordered user-story candidates, creates or updates the parent Linear Issue with design-doc links + cross-feature `blockedBy` + dependency graph + orchestrate-skill instructions, and drafts each user story as a sub-issue using the User Story template — atomic testable acceptance criteria, parallelism-aware naming (1, 2, 3a/3b, 4), `blockedBy` links between stories, all in Todo status. Use this skill whenever an AlphaMind feature needs its implementation work tree drafted, even when the operator says "plan the work for X" or "set up X for implementation" — those phrasings still mean drafting user stories. Do NOT use for one-off bug fixes, refactors, or work outside AlphaMind.
---

# Draft user stories for an AlphaMind feature

The operator names a feature from the "Ready for implementation" section of `docs/project-tracker.md` (e.g., "Breach behavior", "Domain researchers", "Portfolio manager"). You produce the Linear work tree for it: one parent Issue (with cross-feature gates, dependency graph, and orchestrator-specific instructions) and an ordered set of sub-issue user stories with `blockedBy` links wired up.

The output of a successful run is a Linear work tree that an agent can pick up via `/orchestrate` and execute end-to-end without further requirements work.

## Hard rule: do not delegate to subagents

You — the conversation thread the operator is in — must do the gathering and drafting yourself. Do not spawn `Agent(...)` calls of any subagent type for any phase of this skill.

**Why:** Drafting good user stories requires holding the whole feature's design + the up/downstream contracts + the dependency graph + the parallelism shape simultaneously. That synthesis collapses when fragmented across subagents — each one re-reads partial context, redrafts the dependency graph from scratch, and produces stories that don't compose. The orchestrator-level coherence is the value here, and it lives only in the thread that read everything in order.

**How to apply:** When the work feels heavy and you reach for `Agent`, instead read the next file yourself. If you genuinely run out of context, surface that to the operator and pause — do not paper over it with delegation.

The Explore agent for *finding* a file you can't locate by name is fine — that's a lookup, not delegation of judgment. The line is: you may delegate searches; you must not delegate reading, synthesis, or drafting.

## Inputs

One argument: the exact feature name as written in `docs/project-tracker.md` (e.g., `Breach behavior`, `Domain researchers`, `Portfolio manager`). Match is case-insensitive but otherwise verbatim — if the operator's argument doesn't resolve to a single bullet under "Ready for implementation", surface the candidates and ask which one.

## Procedure

Seven phases. Work through them in order. After each phase, briefly tell the operator what you found / chose so they can redirect early if you're off track.

### Phase 1 — Resolve the feature

Read `docs/project-tracker.md` and locate the bullet under "Ready for implementation" matching the argument. Capture:

- The **section** it lives under (e.g., "Risk guardrails", "Analysis layer") — this maps to the Linear Project (see [Linear specifics](#linear-specifics)).
- The **status marker** — `_requirements pending_`, `_stories drafted_`, `_in progress_`, `_done_`. If status is anything other than `_requirements pending_`, surface this and confirm with the operator before proceeding (they may want to add stories to an in-progress feature, or they may have named the wrong feature).
- The **design/architecture doc paths** linked from the bullet — typically one or more files under `docs/design/<section>/` and possibly `docs/architecture/`. Capture every link.

Then check Linear for an existing parent Issue:

```
list_issues(team="AlphaMind", project="<Section name>", query="<Feature name>")
```

If a parent Issue already exists with a matching title, you'll **update** it in Phase 7 rather than create. If multiple match, surface them and ask. If none match, you'll create one in Phase 7.

### Phase 2 — Gather context

Read the feature's design docs end-to-end. Then walk the up/downstream graph:

- **Upstream:** features whose contracts this feature consumes. The design doc usually names them; if not, grep for imports / cross-references. Read enough of each upstream design doc to understand the *shape* of the contract this feature relies on (typed records, function signatures, side-effect boundaries) — you do not need to read the upstream feature's own implementation stories.
- **Downstream:** features that consume this feature's output. Same depth.
- **Cross-cutting policies:** anything under `docs/architecture/` referenced by the design — failure-handling policy, asset universe methodology, source-to-target mappings, scenario tests, test plans.
- **Existing Linear sub-issues in sibling work trees:** for each upstream feature, run `list_issues(team="AlphaMind", parentId="<sibling parent ID>", limit=50)` and capture the IDs + short titles. These are the candidates for your cross-feature `blockedBy` links.

The bar for "enough context": you can write each user story's acceptance criteria without having to re-open the design doc. If you can't, keep reading.

While gathering, jot down (in your working memory, not as files):

- Typed value objects this feature owns vs. imports from upstream.
- Configuration knobs sourced from `config/*.yaml`.
- Any cross-feature dependencies that are hard gates (sibling story must land before this feature can dispatch a particular story) versus coordination notes (one-way dependencies that don't block dispatch).
- **Open decisions** you'll need the operator to resolve in Phase 6 — places where the design doc presents options without picking one, where the as-built code diverges from the design (yaml entries, schema columns, prompt content), where a numeric threshold is named without explicit go/no-go on encoding (named constant vs. yaml-loaded), or where pipeline cadence is implied but not named. Capture each as one terse note: what's open, what the candidate options are, what your recommended default would be. These are the input to Phase 6.

### Phase 3 — Decompose into epics (mental scaffolding only)

Identify 3–6 high-level work groupings — *epics* — that together cover the feature's scope. Examples from the breach-behavior work tree (after the fact): "package skeleton + config", "canonical types", "per-rule deterministic primitives (zone, drawdown, hard rejection, position selection)", "halt/emergency/secondary breach logic", "envelope assembly + cascade", "end-to-end verification".

You do not write these out. They exist only to structure your story decomposition in Phase 4. Skip directly to Phase 4 if the structure is already obvious from the design doc.

### Phase 4 — Story candidates as short titles

Brainstorm a flat list of user-story candidates as short titles only — no scope, no acceptance criteria yet. Each title names *one cohesive deliverable*. Aim for stories that an Opus agent (or Sonnet for mechanical work) can complete in one focused session.

Indicators of right-sized stories:

- One or two new files under `src/alphamind/<package>/`, plus tests.
- Acceptance criteria fit comfortably in 5–10 atomic checkboxes.
- The story can be described to a fresh agent in 2–3 paragraphs of context plus a Reading list.

Indicators a candidate is too big — split it:

- The title contains "and" linking two distinct algorithmic concerns.
- You'd need more than ~12 acceptance criteria to cover it.
- It produces multiple independent typed records that downstream stories consume independently.

Indicators a candidate is too small — fold it:

- It's a one-line edit (often these belong in another story's scope).
- Its acceptance criteria are entirely subsumed by a larger story's tests.

Don't worry about ordering or naming conventions yet — those happen in Phase 5.

**Always include a final story for end-to-end verification + runbook updates.** Every feature work tree concludes with one story covering three deliverables:

- A per-feature `scripts/verify_<feature>.py` that exercises the feature's golden path against real or representative inputs and prints a pass/fail summary an operator can read.
- A per-feature `scripts/RUNBOOK_<feature>.md` with operator prerequisites, the verify-script invocation, expected output shape, and a failure-mode triage table.
- An update to the central `scripts/RUNBOOK_end_to_end_verification.md` inserting the new feature into the dependency-ordered phase list, including any stage-artifact handoff to its downstream consumer.

This story sits last in the sequence — its `blockedBy` list names every story whose deliverable the verify script must exercise. If the feature genuinely produces no operator-runnable behavior (e.g., a pure typed-records package with no executable surface), surface that in Phase 6 and get the operator's explicit nod before omitting the story.

### Phase 5 — Order by dependency, name with parallelism convention

Build the dependency graph between candidates. A depends on B if A's tests cannot run until B's code lands.

Then assign names following AlphaMind convention. Numbers are sequence; letter suffixes mark parallel-eligible groups within a sequence position:

```
01, 02, 03, 04a, 04b, 04c, 04d, 05a, 05b, 05c, 06, 07, 08
```

means: 01 → 02 → 03 → {04a, 04b, 04c, 04d in parallel} → {05a, 05b, 05c in parallel} → 06 → 07 → 08.

Conventions:

- **Two-digit zero-padded numbers** (`01`, not `1`) for sortable file/issue ordering.
- **Lowercase letter suffix** (`04a`, not `04A`) when stories at the same sequence position are parallel-eligible.
- **No suffix** when only one story occupies the sequence position.
- **Title separator** is em-dash with surrounding spaces: `04a — Zone classifier`.

A canonical example to mirror is the breach-behavior work tree (parent issue ALP-212, sub-issues ALP-227 through ALP-239). Look at its structure if you're unsure.

If your graph has the wrong shape (too sequential, or stories falsely marked parallel that actually share a file), revise Phase 4 — it's cheap; ordering errors compound downstream.

### Phase 6 — Resolve open decisions interactively

Before writing anything into Linear, surface the open decisions from your Phase 2 notes and resolve them with the operator in one batched exchange. Drafting-time decisions belong in drafting, not in story bodies as "Surface to operator before X" gates that bottleneck dispatch.

**The test for drafting-time vs. dispatch-time:**

A question is **drafting-time** if it can be answered without running code — config values, encoding choices (named constant vs. yaml-loaded), pipeline cadence, naming conventions, scope-boundary calls, scheduling shape, default behaviors, stub strategies for missing upstream features. Settle these now.

A question is **dispatch-time** if it requires actual implementation discovery — schema drift between an upstream's expected and produced shape, a third-party API behavior the docs don't pin down, an algorithm that turns out to need an additional case the design didn't name. Leave these as Surfacing conditions in the parent issue and trust the orchestrator to escalate when they hit.

**Procedure:**

1. Compile every open decision from your Phase 2 notes into a single batched message to the operator. Don't pepper them one question at a time — the cognitive cost of context-switching across decisions exceeds the cost of reviewing them in one pass.
2. For each decision, give: a one-line summary of what's open, a two-or-three-line option list, and a recommended default with a one-sentence rationale. The recommendation matters — operators have limited bandwidth and most decisions have a clear "right under current constraints" answer.
3. Wait for the operator's resolutions. Apply each — usually a one-line annotation in your working notes — before moving to Phase 7.
4. Bake every resolved decision into either (a) the relevant story's body as a positive specification (what the story will do), or (b) a "Pre-resolved configuration decisions" section in the parent Issue body (see the template in Phase 7a). Do not write "Surface to operator" guidance for resolved decisions.

**Common decision categories that surface here:**

- **As-built / design divergences.** The yaml or schema differs from what the design doc names. Recommend keeping or changing; don't punt.
- **Threshold encoding.** Definitional cutoff (named constant) vs. Class A tunable (yaml-loaded). Default to definitional unless the design doc explicitly names a calibration cadence.
- **Pipeline cadence.** Per-invocation render vs. cron-scheduled producer + on-demand consumer. Default to producer-cadence-named-by-design-doc.
- **Tool / contract scope.** Ship N tools or N+1; defer one for downstream-data-readiness reasons.
- **Stub strategy for missing upstream.** Empty-tuple loader vs. fake-record loader vs. block-the-story. Default to empty-tuple when the consumer prompt handles the empty case gracefully.

If you discover a new decision *during* Phase 7 drafting (you didn't catch it in Phase 2), pause, batch any other late-discovered ones with it, and surface — don't write it into the story body as deferred. The point of Phase 6 is to keep dispatch unblocked, and that contract holds even when a question surfaces late.

### Phase 7 — Create the parent Issue and draft sub-issues

This is the actual Linear work. Two stages.

#### 7a. Parent Issue

Use `save_issue` to create or update the parent Issue. Required fields:

- `team`: `AlphaMind`
- `project`: the section name from Phase 1 (e.g., `Risk guardrails`)
- `title`: the feature name verbatim (e.g., `Breach behavior`)
- `labels`: `["Feature"]`
- `description`: see template below

If a matching parent Issue already exists, pass its `id` to update rather than create. **Do not** overwrite an existing populated description without preserving content the operator wrote — diff and merge mentally; if there's any doubt, surface to the operator before saving.

Parent Issue description template (Markdown — fill the bracketed sections; remove sections that genuinely don't apply, but do not skip a section just because writing it is hard):

```markdown
[One-sentence purpose of the feature, drawn from its design doc.]

## Design and architecture

* [Design doc 1](docs/design/<path>.md)
* [Design doc 2](docs/design/<path>.md) — [if multiple]
* [Architecture cross-ref](docs/architecture/<path>.md) — [if applicable]

## Cross-feature dependencies

[Hard gates first. For each: which sub-issue here is gated by which upstream story, and what shape the upstream contract takes. Use Linear `blockedBy` links between sub-issues for hard gates — those are wired in Phase 7b.]

* **Story <NN> (this work tree)** depends on **<sibling-feature> story <NN> (ALP-XXX)** for [contract description]. Until that lands, [stub strategy or "block dispatch"].
* [Repeat for each hard gate.]

[Then coordination notes — one-way dependencies that don't block dispatch but require coordinated edits when both work trees converge.]

* **Coordination:** [downstream feature] currently inlines [type/contract] this feature owns. After story <NN> here lands, those inline declarations should become imports in a coordinated edit when [downstream feature] dispatches.

## Pre-resolved configuration decisions

[Include this section iff Phase 6 produced resolved decisions. Omit entirely otherwise. Use bold-text paragraphs, NOT bullets — the Linear renderer drops bullet lists that follow a colon-ending paragraph or a heading-then-prose stanza, but bold-text paragraphs survive.]

The following choices were settled at drafting time and baked into the relevant stories. The orchestrator does not need to surface them at dispatch.

**(A) <Decision name>.** <Decision in one sentence>. <One-or-two-sentence rationale>. <Story number that applies it>.

**(B) <Decision name>.** <Decision in one sentence>. <One-or-two-sentence rationale>. <Story number that applies it>.

[etc.]

## Dependency graph

```
01
02
 ↓
 03
 ↓
 ┌────┬────┬────┐
04a  04b  04c  04d         (parallel: <one-line description per story>)
 ↓    ↓    ↓    ↓
 ...
```

[ASCII graph showing the wave structure. Use the same conventions as the breach-behavior ORCHESTRATOR.md.]

**ASCII-graph rendering constraints (Linear).** Linear renders ASCII graphs as plain code blocks, but the code-block container can wrap on narrower viewports. Keep the graph readable by:

- Capping line width at ~60 characters before any trailing arrow / box-drawing character. Pad short lines with spaces so column-anchored arrows stay aligned.
- Avoiding right-edge box-drawing characters (`│`, `┐`, `┘`) past column 60 — they wrap and the visual mapping breaks.
- For wide work trees (5+ parallel stories at one position), prefer multi-line stanzas with `(parallel: ...)` annotations on a separate line below rather than a single very wide horizontal fan-out.
- Re-fetch the parent Issue after saving and skim the rendered graph; if any line wrapped, narrow the graph and re-save.

## Notes for the orchestrator

[Feature-specific guidance for the agent that picks this up via `/orchestrate`. DO NOT restate `/orchestrate`'s built-in behavior — only what's specific to this feature. Examples:]

* **Sibling work-tree gates:** before dispatching story <NN>, verify <sibling-feature>'s stories <NN> and <NN> are `Done` via `mcp__linear-server__list_issues(parentId="<sibling parent ID>", state="Done")` and confirm the expected sub-issue identifiers appear.
* **Model selection nuances:** [if this feature has stories where the default Sonnet/Opus split needs adjustment — e.g., "story 02's YAML loader shape wants judgment; default to Opus despite mechanical surface".]
* **Architectural invariants the orchestrator must enforce:** [purity rules, no-I/O constraints, no-magic-numbers constraints, types-must-not-duplicate-upstream constraints — anything that a subagent might violate that the orchestrator should catch on verification.]
* **Cumulative-state primitives:** [if any stories produce state that other stories consume in non-obvious ways.]
* **Surfacing conditions:** [conditions under which the orchestrator should pause and ask the operator rather than improvise — typically schema drift between this work tree's typed inputs and a sibling's actual produced shape.]
```

After saving the parent Issue, capture its identifier (e.g., `ALP-212`) — you'll use it as `parentId` for every sub-issue.

#### 7b. Sub-issues, one at a time

For each user story in dependency order:

1. **Compose the description** following the User Story sub-issue template below.
2. **Call `save_issue`** with: `team="AlphaMind"`, `parentId=<parent ID>`, `title=<NN — Title>`, `labels=["Feature"]`, `state="Todo"`, `description=<the composed body>`, and (if this story has any same-work-tree predecessors that have already been created) `blockedBy=[<predecessor sub-issue IDs>]`.
3. **For cross-feature hard gates** identified in Phase 2, also include the relevant upstream sub-issue IDs in `blockedBy`.
4. **Capture the returned identifier** so subsequent stories that depend on this one can reference it.

Work methodically — one story per `save_issue` call, verifying each lands before drafting the next. Do not batch.

When you re-fetch a description you just wrote, the Linear renderer will have auto-converted naked issue references like `ALP-XXX` into `<issue id="...">ALP-XXX</issue>` tags. This is cosmetic and harmless — the rendered display is unchanged — but it means the round-tripped body is not byte-identical to what you sent. Don't chase the diff.

After all sub-issues are created, do a final pass:

- Verify every `blockedBy` edge from your dependency graph is wired (re-run `get_issue(id, includeRelations=true)` on a few stories and spot-check).
- Update `docs/project-tracker.md`: change the feature's status from `_requirements pending_` to `_stories drafted_`. (This is a single-line edit; do it inline.)

### User Story sub-issue template

The description body — Markdown, no frontmatter (status tracking lives in the file mirror, not the Linear description):

```markdown
# <NN — Title>

## Goal

[2–4 sentences naming what concrete deliverable this story produces and where it sits in the feature. Reference the typed inputs/outputs by name. Mention which downstream consumers depend on this story's output, by name. The goal is not a restatement of the title — it's the precise shape of "done".]

## Reading

[Bullet list of files the agent must read. For each, name the file path and a one-clause summary of what's in it that this story needs. Order: design docs first, then config models, then upstream/downstream code references, then sibling-story specs the agent must align with. Be specific about which sections — "§ Escalation model" rather than just naming the file. Aim for 4–8 entries; more if the story straddles multiple modules.]

* `docs/design/<feature>.md` § <Section> — <what to look for>
* `src/alphamind/<package>/<module>.py` — <what's already there>
* `ALP-XXX` (<sibling story title>) — <which interface this story aligns with>
* [...]

## Depends on

[Bullet list of sub-issue IDs this story depends on, with one-clause reason for each. Both same-work-tree predecessors AND cross-feature gates appear here. The orchestrator reads this list to sequence dispatch.]

* <NN> (<sibling-feature>) — <why>
* <NN> (this work tree) — <why>

## Scope

In scope, all under `<src path>`. Tests at `<test path>`.

### 1. <First named deliverable>

[Function signature, type definition, or behavior description. Be precise — include parameter names, types, return shape, and edge-case semantics. Quote the design doc where possible. If a public function, include its docstring shape.]

### 2. <Second named deliverable>

[...]

### Out of scope

[Things adjacent to this story that another story owns, or that aren't this story's responsibility. Name the owning story when applicable. This prevents scope creep and clarifies hand-offs.]

## Acceptance criteria

[Atomic, testable outcomes. Each is one observable fact: a function exhibits documented behavior, a file exists with named content, a test produces a documented output. Phrase each as a checkbox. Avoid umbrella criteria like "the module works" — split into specific behaviors.]

* [ ] `<function>` returns `<value>` when given `<input>`.
* [ ] `<type>` is exposed from `<module>.types` and is importable.
* [ ] `tests/<path>/test_<name>.py` exists and passes under `uv run pytest -n auto`.
* [ ] [...]

## Verification

[How the orchestrator confirms this story is done. Typically: run the test suite, spot-check a documented behavior, lint clean. Mention any acceptance criterion that is verified by inspection rather than by automated test.]
```

### Quality bar for individual stories

Each story passes the **fresh-agent test**: a competent engineer who has read no design docs and only the story's own description can implement it. They must follow the Reading list, but they should not have to ask clarifying questions. If a story leaves an agent guessing about an interface contract, scope boundary, or dependency mechanic, the story is underspecified — fix it before moving on.

Each acceptance criterion passes the **atomicity test**: it asserts one observable outcome, and a graders can mark it pass/fail unambiguously by running one command or reading one file.

## Linear specifics

### Project mapping

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

If the operator names a feature whose section doesn't appear in this table, list `mcp__linear-server__list_projects(team="AlphaMind")` and surface the mismatch — don't create a new project unilaterally.

### Status, labels, team

- **Team:** `AlphaMind` (key `ALP`).
- **Sub-issue state on creation:** `Todo` (Linear status type `unstarted`, name `Todo`). Pass `state="Todo"` to `save_issue`.
- **Label on parent Issue and sub-issues:** `Feature`. Existing parent Issues like ALP-212 use this — match it.

### `blockedBy` mechanics

`save_issue` accepts `blockedBy` as an array of issue IDs/identifiers (`["ALP-229", "ALP-230"]`). It is **append-only** — calling `save_issue` again with a different `blockedBy` list does not remove previously-set blockers; use `removeBlockedBy` for that. This means you can:

- Set `blockedBy` correctly on creation if all blockers already exist.
- For sub-issues that depend on later-created cross-feature stories (rare, since you draft *this* feature's tree, not siblings'), set `blockedBy` for in-tree predecessors at creation, then re-call `save_issue(id=...)` later with cross-feature additions.

If a `blockedBy` link fails (e.g., the upstream issue doesn't exist), surface the gap rather than silently dropping it — the dependency is real and the operator needs to know it can't be wired in Linear.

## Anti-patterns to avoid

- **Restating `/orchestrate` content in the parent Issue's "Notes for the orchestrator" section.** The orchestrator skill already covers worktree dispatch, status-frontmatter discipline, model-selection defaults, verification commands, and the report-back contract. Only write feature-specific exceptions, gates, and invariants here.
- **Inventing typed records or component names.** Per `feedback_no_inventing_component_names`, every typed value object in a story's Scope section either mirrors an existing upstream record or is named by the design doc. If you find yourself naming a new record without a design-doc precedent, stop — re-read or surface to the operator.
- **Numeric thresholds in story bodies.** Per `feedback_avoid_numeric_anchors`, don't bake `70/85/95`, `60%`, `30 minutes` etc. into acceptance criteria. Reference the configuration source (`config/<feature>.yaml`, `BreachBehaviorConfig`, etc.) and let the verification test load the value.
- **Decision trails in story descriptions.** Per `feedback_no_decision_trails`, state contracts positively. Don't write "this story does NOT cover X" unless X is genuinely a likely-but-wrong reading; if X is obviously someone else's job, just don't mention it.
- **Stories that bundle "and" of two algorithmic concerns.** Re-split. The dependency graph is cheaper to maintain than ambiguous scope.
- **Omitting the final e2e verification + runbook story.** Every feature work tree ends with the story that ships `scripts/verify_<feature>.py`, `scripts/RUNBOOK_<feature>.md`, and the central `scripts/RUNBOOK_end_to_end_verification.md` insertion. Skip only with explicit operator sign-off after raising it in Phase 6.
- **Acceptance criteria that test "the system works".** Replace with criteria that test *observable behaviors* of *named functions* with *named inputs*.
- **Skipping the operator-confirmation step in Phase 1** when the feature's status isn't `_requirements pending_`. Drafting stories on top of an in-progress work tree without confirming intent corrupts that tree.
- **"Surface to operator" gates for drafting-time decisions.** Per Phase 6: if a decision can be made without running code (yaml value, encoding choice, pipeline cadence, scope boundary, stub strategy), settle it during drafting. Writing "the orchestrator should surface this before dispatch" turns the operator's review into N small interruptions during dispatch instead of one batched session before drafting — and the orchestrator typically lacks the context the drafter had to recommend a default.

## When you're done

Report back to the operator in this shape (concise; one short paragraph):

- The parent Issue identifier and URL.
- The number of sub-issues created and the identifier range (e.g., "10 stories, ALP-280 through ALP-289").
- The dependency-graph shape in one line ("01 → 02 → 03 → 4-way parallel 04* → 3-way parallel 05* → 06 → 07").
- Cross-feature `blockedBy` count and which sibling work trees they touch.
- Any unresolved gaps surfaced during drafting (missing upstream stories, ambiguous design-doc sections, etc.) — these become the operator's follow-ups.
- The single-line `docs/project-tracker.md` status update.

Then stop. The operator drives next steps from there (typically: dispatch via `/orchestrate`).

## Self-improvement

While executing, note moments where this skill let you down: a step that was ambiguous, an edge case the procedure didn't anticipate, a Linear/MCP gotcha you hit and worked around, guidance that turned out wrong.

Don't fix the skill mid-flight — interrupting the drafting flow to edit the procedure costs more than it saves. Keep working notes mentally, and at the end — *after* reporting the work tree to the operator — propose specific edits in this shape:

- **Where:** the section/heading in this SKILL.md to change.
- **What:** the concrete edit (added bullet, replaced sentence, new subsection).
- **Why:** what went wrong without it.

Skip silently if nothing came up. The bar is "would have saved a step" or "would have prevented a mistake", not "could be marginally smoother". The operator decides what to apply.
