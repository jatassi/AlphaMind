---
name: draft-user-stories
description: Use to draft Linear user stories for an AlphaMind feature. Triggers on `/draft-user-stories <Feature>` and operator phrases like "draft user stories for Breach behavior", "decompose Domain researchers into stories", "write requirements for the Synthesizer feature", "break State persistence into user stories", "set up the work tree for Portfolio manager". The skill gathers context from the feature's design and architecture docs (and up/downstream features), decomposes the work into ordered user-story candidates, creates or updates the parent Linear Issue with design-doc links + cross-feature `blockedBy` + dependency graph + orchestrate-skill instructions, and drafts each user story as a sub-issue using the User Story template — atomic testable acceptance criteria, parallelism-aware naming (1, 2, 3a/3b, 4), `blockedBy` links between stories, all in Todo status. Use this skill whenever an AlphaMind feature needs its implementation work tree drafted, even when the operator says "plan the work for X" or "set up X for implementation" — those phrasings still mean drafting user stories. Do NOT use for one-off bug fixes, refactors, or work outside AlphaMind.
---

# Draft user stories for an AlphaMind feature

The operator names a feature (e.g., "Breach behavior", "Domain researchers", "Portfolio manager"). You produce the Linear work tree for it: one parent Issue (with cross-feature gates, dependency graph, and orchestrator-specific instructions) and an ordered set of sub-issue user stories with `blockedBy` links wired up.

The output of a successful run is a Linear work tree that an agent can pick up via `/orchestrate` and execute end-to-end without further requirements work.

## Hard rule: do not delegate to subagents

You — the conversation thread the operator is in — must do the gathering and drafting yourself. Do not spawn `Agent(...)` calls of any subagent type for any phase of this skill.

**Why:** drafting requires holding the whole design + up/downstream contracts + dependency graph + parallelism shape at once. Fragmented across subagents, that synthesis collapses — each re-reads partial context and produces stories that don't compose. The coherence is the value, and it lives only in the thread that read everything in order.

When the work feels heavy and you reach for `Agent`, read the next file yourself instead; if you genuinely run out of context, surface it and pause. The Explore agent for *finding* a file by name is fine — you may delegate searches, never reading, synthesis, or drafting.

## Inputs

One argument: the feature name (e.g., `Breach behavior`, `Domain researchers`, `Portfolio manager`). If the operator's argument doesn't resolve to a single feature with design docs, surface the candidates and ask which one.

## Procedure

Eight phases. Work through them in order. After each phase, briefly tell the operator what you found / chose so they can redirect early if you're off track.

### Phase 1 — Resolve the feature

Locate the feature's design/architecture docs (search `docs/design/` and `docs/architecture/` by the feature name; use the Explore agent if you can't find them by name). Capture:

- The **section** it lives under (e.g., "Risk guardrails", "Analysis layer") — this maps to the Linear Project (see [Linear specifics](#linear-specifics)). The design docs live under `docs/design/<section>/`, so the section is usually evident from the doc path.
- The **design/architecture doc paths** for the feature — typically one or more files under `docs/design/<section>/` and possibly `docs/architecture/`. Capture every path.

**Then verify the feature's drafting status against the as-built code.** Backlog prose can lag the codebase — a sibling feature often ships typed records ahead of its owning feature being drafted (Portfolio state shipped `PositionRecord`, `ThesisRecord`, etc. under `portfolio_state/records/` long before "Position & thesis model" was drafted, with ~279 import sites already consuming them). Extract the primary records / functions the design doc claims to ship and grep them across `src/` and `tests/`. If substantial hits exist outside the feature's own scaffolded directories, the as-built has diverged — surface it to the operator as a shape question (typical options: already-done / close parent; add the remainders only; move-to-canonical-home refactor) *before* reading the design docs end-to-end. The answer determines whether you do a full Phase 2 + decomposition or a much smaller one.

Then check Linear for an existing parent Issue:

```
list_issues(team="AlphaMind", project="<Section name>", query="<Feature name>")
```

If a parent Issue already exists with a matching title — especially one that already has sub-issues — surface this and confirm with the operator before proceeding (they may want to add stories to an in-progress feature, or they may have named the wrong feature); otherwise you'll **update** it in Phase 7 rather than create. If multiple match, surface them and ask. If none match, you'll create one in Phase 7.

### Phase 2 — Gather context

Read the feature's design docs end-to-end. Then walk the up/downstream graph:

- **Upstream:** features whose contracts this feature consumes. The design doc usually names them; if not, grep for imports / cross-references. Read enough of each upstream design doc to understand the *shape* of the contract this feature relies on (typed records, function signatures, side-effect boundaries) — you do not need to read the upstream feature's own implementation stories.
- **Downstream:** features that consume this feature's output. Same depth.
- **Cross-cutting policies:** anything under `docs/architecture/` referenced by the design — failure-handling policy, asset universe methodology, source-to-target mappings, scenario tests, test plans.
- **Existing Linear sub-issues in sibling work trees:** for each upstream feature, run `list_issues(team="AlphaMind", parentId="<sibling parent ID>", limit=50)` and capture the IDs + short titles. These are the candidates for your cross-feature `blockedBy` links.

The bar for "enough context": you can write each user story's acceptance criteria without having to re-open the design doc. If you can't, keep reading.

**Audit existing code when it exists.** If the feature already has substantial as-built code (the as-built check in Phase 1 surfaced existing implementation, or upstream features ship typed records / modules this feature will consume), invoke `Skill("python-architecture")` in audit mode scoped to the relevant subdivision (the feature's package, or an upstream feature's package whose contract you'll lean on). Treat the audit findings as inputs to decomposition:

- **Load-bearing findings** that touch the feature's surface (primitive obsession in a record this feature consumes, a shallow-module swarm in an upstream the feature extends, a layer violation crossing into the feature's intended scope) become candidate stories or constraints on story scope. A "Money should be a domain primitive" finding upstream becomes either a coordinated-edit note in the parent Issue, a hard gate on the upstream's draft tree, or — if owned by this feature — its own typed-record story.
- **High-yield findings** (mutable defaults, naive datetimes, missing timeouts) that live inside the feature's planned scope fold into the relevant story's scope without becoming a story of their own.
- **Findings outside the feature's scope** that the audit surfaces opportunistically are *not* this feature's job. Surface them to the operator separately as candidate follow-on Linear issues — do not silently absorb them into the work tree.

Skill invocation runs in your thread (no Agent dispatch), so it's compatible with the no-delegation rule. Use the audit's punch list to sanity-check that your Phase 4 candidate stories cover the load-bearing structural work the feature actually needs.

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

**Design mode for new modules.** When the feature introduces a new package or service with non-trivial domain logic (named typed records, Protocols/adapters, a domain core distinct from its I/O shell) rather than purely extending existing modules, invoke `Skill("python-architecture")` in design mode before naming the candidate stories. The brief produces:

- A concrete **package layout** (`<feature>/domain.py`, `<feature>/adapters.py`, etc., or per-component subdirectories per the `feedback_scaffold_per_component_depth` memory).
- **Named typed records and domain primitives** (`PositionId = NewType(...)`, `Quantity = ...`, frozen-dataclass shapes) rather than abstract "use NewType for IDs".
- The **testing seam** (which dependencies get Protocols + in-memory fakes; which get testcontainers / embedded substitutes; which are pure local-substitutable).
- Three or four **hardest-to-reverse decisions** (persistence engine, sync-vs-async, message-bus-or-not) with tradeoffs.

Anchor the Scope sections of subsequent stories to the brief's concrete names — a story's "produces `PositionRecord` with these fields" is far more useful to a dispatched subagent than "introduces a position record". When the brief surfaces a hardest-to-reverse decision the design doc hasn't settled, add it to your Phase 6 open-decisions list. Skill invocation runs in your thread (no Agent dispatch), compatible with the no-delegation rule. Skip design mode when the feature is small, purely additive to an existing module, or already fully specified at the type level by its design doc.

**Operational runbook story.** Assess whether the feature changes production operational behavior — a service, the schedule, a port, an env var, a migration / bootstrap step, a CLI flag, a monitoring surface, or a new failure mode / gotcha. If so, add a final story that updates `scripts/RUNBOOK_production.md` (its Living-document rule keeps the runbook moving with the behavior), `blockedBy` every story whose operational change it documents so it lands last. Omit it for features with no operator-visible prod-runtime effect (pure internal logic, analysis / decision-layer changes, test-only work).

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

**Drafting-time vs. dispatch-time** is the standard's cut: a question is drafting-time if it can be answered without running code (config values, encoding choices, cadence, naming, scope boundaries, stub strategies) — settle it now; it's dispatch-time only if it needs implementation discovery (schema drift, an unpinned third-party behavior, an algorithm case the design didn't name) — leave it as a Surfacing condition in the parent issue for the orchestrator to escalate.

**Procedure:**

1. Compile every open decision from your Phase 2 notes into a single batched message to the operator. Don't pepper them one question at a time — the cognitive cost of context-switching across decisions exceeds the cost of reviewing them in one pass.
2. For each decision, give: a one-line summary of what's open, a two-or-three-line option list, and a recommended default with a one-sentence rationale. The recommendation matters — operators have limited bandwidth and most decisions have a clear "right under current constraints" answer. **Exception — scope-shaped forks** (the goal is fixed and the only question is how much to change: a correctness-vs-blast-radius tradeoff): frame these as the ladder in `docs/agents/operator-decisions.md` and present them with **no** recommendation. A scope-shaped fork therefore can't be baked in with a default — it is always *asked* (see the split below).
3. Wait for the operator's resolutions. Apply each — usually a one-line annotation in your working notes — before moving to Phase 7.
4. Bake every resolved decision into either (a) the relevant story's body as a positive specification (what the story will do), or (b) a "Pre-resolved configuration decisions" section in the parent Issue body (see the template in Phase 7a). Do not write "Surface to operator" guidance for resolved decisions.

**Handling more than 4 decisions — `AskUserQuestion` is capped at 4 questions per call.** Drafting an AlphaMind feature routinely surfaces 5–10 decisions, exceeding the tool's limit. Do NOT split into multiple sequential `AskUserQuestion` calls — that defeats the "single batched exchange" goal in step 1 and forces the operator into a second cognitive context-switch they shouldn't have to pay. Instead, rank your decisions by impact and apply this split:

- **Top 4 — ask via `AskUserQuestion`.** Choose the decisions where (a) story count or shape depends on the answer, (b) cross-tree coordination is at stake (sibling work tree's pre-resolved decisions need updating), (c) the right answer isn't obvious enough to bake in without risk, or (d) the decision is a **scope-shaped fork** — it carries no recommendation to bake in, so it must be asked. These get the tool's structured option list + radio-button affordance.
- **Remainder — bake in with your recommendation; state the bake-in in the same message that surfaces the 4 questions.** Before the `AskUserQuestion` call, send a text message that names every smaller decision, summarizes the options in one sentence each, and states your recommended default. Use the exact framing "I'll bake in with my recommendation in the story bodies; tell me to flip any of these and I'll update". The operator can override any in their `AskUserQuestion` "notes" or in a follow-up message; otherwise you proceed with your defaults captured in the parent Issue's Pre-resolved decisions section.

This preserves the single-exchange contract: the operator reads one combined message (text rationale + tool-rendered 4-question prompt), answers the structured 4, and optionally redirects any of the baked-in ones in the same reply. If the operator flips any baked-in decision, treat the flip as a Phase 6 resolution and apply per step 4 above.

If you'd otherwise need 8+ questions, your decomposition is probably too granular — re-check whether some "decisions" are actually default behaviors with no real second-choice, or whether two questions are facets of the same underlying choice that should merge.

**Common decision categories that surface here:**

- **As-built / design divergences.** The yaml or schema differs from what the design doc names. Recommend keeping or changing; don't punt. **Verify before surfacing:** apparent divergences frequently turn out to be intentional dual taxonomies on closer reading (e.g., a 3-way analysis-side sector enum coexisting with a 4-way risk-side enum). Before treating a divergence as real, grep the term across at least two layers (design docs + as-built code + config) — a single mismatch in one place doesn't establish drift. False alarms cost operator attention.
- **Threshold encoding.** Definitional cutoff (named constant) vs. Class A tunable (yaml-loaded). Default to definitional unless the design doc explicitly names a calibration cadence.
- **Pipeline cadence.** Per-invocation render vs. cron-scheduled producer + on-demand consumer. Default to producer-cadence-named-by-design-doc.
- **Tool / contract scope.** Ship N tools or N+1; defer one for downstream-data-readiness reasons.
- **Stub strategy for missing upstream.** Empty-tuple loader vs. fake-record loader vs. block-the-story. Default to empty-tuple when the consumer prompt handles the empty case gracefully.

If you discover a new decision *during* Phase 7 drafting (you didn't catch it in Phase 2), pause, batch any other late-discovered ones with it, and surface — don't write it into the story body as deferred. The point of Phase 6 is to keep dispatch unblocked, and that contract holds even when a question surfaces late.

### Phase 7 — Create the parent Issue and draft sub-issues

This is the actual Linear work. A pre-flight cap check, then two stages.

#### Pre-flight: Linear free-tier cap check

Linear's free tier caps the workspace at roughly 250 active (non-archived) issues. Hitting the cap mid-drafting leaves the work tree in a half-state — some sub-issues saved, others not, the operator must run the consolidation rollup before you can resume, and your dispatch is paused until both happen. Catch the risk before any writes.

Compute how many issues you're about to create:

- `needed_sub_issues` = number of stories from your Phase 5 sequence.
- Add 1 if the parent Issue doesn't already exist (you'll create it in 7a).
- `needed = needed_sub_issues + (1 if parent absent else 0)`.

Run the cap-check script with that count:

```
uv run python scripts/check_linear_cap.py --needed <needed> --json
```

The script queries the Linear GraphQL API directly (key from the repo-root `.env`), paginates the whole workspace, and prints one JSON line — e.g. `{"active": 243, "cap": 250, "buffer": 7, "needed": 12, "margin": 2, "required": 14, "ok": false}`. It applies a 2-issue safety margin internally (`required = needed + margin`), so you pass only the raw `needed` count. Exit code mirrors `ok`: `0` = clear, `1` = cap risk, `2` = error.

Decision:

- **`ok == true` (exit 0)** — cap risk is low. Note `active` in your working memory and proceed to 7a.
- **`ok == false` (exit 1)** — surface to the operator *before any writes*. Report `active`, `buffer`, `needed`, and `required` from the JSON. Recommend running the consolidation rollup per the `project_linear_consolidation` memory and the `/linear-consolidate` skill — `uv run python scripts/linear_consolidation_candidates.py` ranks which shipped feature would free the most slots, and `scripts/check_linear_cap.py --breakdown` shows where the active issues sit. Ask whether to (a) pause for rollup, (b) proceed accepting that the cap may fire mid-drafting (you will catch the error gracefully and surface the remaining drafts inline as a hand-off), or (c) trim story count if you can identify a fold.
- **exit 2** — the script failed (missing `LINEAR_API_KEY` in `.env`, or a network/API error; the stderr message says which). Surface it to the operator and resolve the cause before proceeding — don't fall back to a manual MCP count.

The cap is occasionally enforced at counts above 250 — Linear's exact threshold depends on workspace age and account state. Treat the check as early-warning, not exact predictor. If a create fails despite a pre-flight `ok == true`, surface the blocker and inline the remaining drafts in the operator hand-off.

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

[Include iff Phase 6 produced resolved decisions; omit otherwise. Use bold-text paragraphs, not bullets, and keep inline code out of the bold prefix — see the standard's render hazards.]

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

After all sub-issues are created, do a final pass:

- Verify every `blockedBy` edge from your dependency graph is wired (re-run `get_issue(id, includeRelations=true)` on a few stories and spot-check).
- **Check for mid-flight commits to `main`.** Drafting takes 30–60 minutes; the codebase can move during that window. Run `git log <starting-sha>..main --oneline` (where `<starting-sha>` is whatever HEAD was at Phase 1 — capture it then if you anticipate a long session) or `git log --oneline -10` and look for any commits that landed since you started reading. For each new commit, run `git show --stat <sha>` and check whether its file changes overlap any path or symbol referenced in your stories' Reading lists, Scope sections, or coordinated-edit blocks. Common overlap patterns: a fix to a file your stories extend (typed-record edits, MCP-server wrappers, harness diagnostic surfaces); a sibling work tree shipping a typed record your story depends on; a refactor that renames a symbol your acceptance criteria mention. If overlap exists, surface to the operator with a one-line summary of each commit's impact and absorb the changes into affected stories *before* finalizing the work tree — Reading-list pointers, Scope deliverables, acceptance criteria, and the parent Issue's Pre-resolved decisions can all need touch-ups. The cost of catching this here is minutes; the cost of catching it after dispatch is a subagent diverging from a stale spec.

### User Story sub-issue template

This template adds the greenfield-story specifics (Goal, Depends on, Out of scope, parallelism-aware naming) around the common Reading / Scope / Acceptance criteria / Verification sections defined in `docs/agents/implementation-ready-issue.md`. The semantics of those common sections, the writing rules (structural-not-numeric criteria, positive contracts, no invented names), and the Linear render mechanics live in the standard.

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

[Editorial discipline (the standard's): write the final shape, not your drafting process — edit out thinking-out-loud residue and internal contradictions before `save_issue`.]

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

### Phase 8 — Implementation-readiness pass

After every sub-issue is created and the Phase 7 final pass (every `blockedBy` wired, mid-flight commits absorbed) is done, take one more adversarial read through the whole tree — the parent and every sub-issue — before reporting. You are re-reading your own drafts, so read against the grain: hunt for the latitude you left yourself, not the intent you remember. A first draft routinely smuggles in a choice you deferred without noticing.

Each issue must clear the bar in `docs/agents/implementation-ready-issue.md`, applied here against the grain to every story. Fix in place with `save_issue` (description-only — never re-send `blockedBy`; it is append-only, per [`blockedBy` mechanics](#blockedby-mechanics)) and re-fetch to confirm the render, exactly as in Phase 7b.

Three **tree-level** checks the standard's single-issue bar doesn't cover — apply them here because this pass sees the whole tree at once:

1. **One writer per file across the tree.** State which files each story owns and hold one writer per file. Two stories editing the same `task.py` / `wiring.py` for different concerns is a conflict you pay at integration — resolve it by ownership (one story owns the file) or a `blockedBy` edge that serialises them.

2. **Tree-wide vocabulary coherence.** A type, seam, module, config key, or error name pinned in its defining story is referenced by that exact name in every consuming story, and each consumer's Reading list points at the defining story. When this pass changes a pinned name in one story, propagate it to every sibling that references it — an inconsistent name across two stories is a scope ambiguity wearing a self-contained costume.

3. **Sibling-rippling decisions are drafting-time.** A type's representation, a module's location, a public name, or an API's shape that ripples into sibling stories must be pinned now, not left to the implementer (the Phase 6 drafting-time-vs-dispatch-time test).

Do this in-thread — the no-delegation rule holds, because coherence across the whole tree is exactly what the pass checks. If the pass surfaces a defect that changes a *decision* rather than its wording (e.g. a foundation story's API shape was underspecified in a way that reshapes its consumers), treat it as a late Phase 6 item: fix the defining story and every consumer in the same pass, then note it in the done-report.

## Linear specifics

### Project mapping

| Section                           | Linear Project          |
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

- **Restating `/orchestrate` content in "Notes for the orchestrator".** That skill already covers worktree dispatch, status discipline, model-selection defaults, verification, and the report-back contract. Write only feature-specific exceptions, gates, and invariants.
- **Stories that bundle "and" of two algorithmic concerns.** Re-split — the dependency graph is cheaper to maintain than ambiguous scope.
- **Drafting on top of a populated parent Issue without confirming intent** (Phase 1) — it corrupts an in-progress tree.

The per-issue writing-rule anti-patterns — invented names, numeric anchors, decision trails, "the system works" criteria, and drafting-time "Surface to operator" gates — are the standard's (`docs/agents/implementation-ready-issue.md`) and Phase 6's.

## When you're done

Report back to the operator in this shape (concise; one short paragraph):

- The parent Issue identifier and URL.
- The number of sub-issues created and the identifier range (e.g., "10 stories, ALP-280 through ALP-289").
- The dependency-graph shape in one line ("01 → 02 → 03 → 4-way parallel 04* → 3-way parallel 05* → 06 → 07").
- Cross-feature `blockedBy` count and which sibling work trees they touch.
- The Phase 8 implementation-readiness result: a one-line tally of what it fixed (open decisions / scope ambiguities / hedges resolved, names propagated), or "clean" if nothing needed changing.
- Any unresolved gaps surfaced during drafting (missing upstream stories, ambiguous design-doc sections, etc.) — these become the operator's follow-ups.

Then stop. The operator drives next steps from there (typically: dispatch via `/orchestrate`).

## Self-improvement

Note where the skill let you down — an ambiguous step, an unanticipated edge case, a Linear/MCP gotcha. Don't fix it mid-flight; after reporting the work tree, propose edits as **Where** / **What** / **Why** (what went wrong without it). Bar: "would have saved a step" or "prevented a mistake"; skip silently otherwise.
