# The implementation-ready issue

The contract between the skills that **author** Linear issues (`draft-user-stories`,
`refine-issue`) and the skills that **consume** them (`implement-issue`, `orchestrate`).
An issue an authoring skill produces must meet the bar below; a consuming skill may assume
it. Read `docs/agents/linear.md` for the Linear API mechanics this references.

## The bar

An issue is **implementation-ready** when a competent engineer who has read no design docs
— only the issue body and the files in its Reading list — can implement it without asking a
clarifying question. Concretely, four conditions:

1. **Zero open design decisions.** No "e.g. X / Y", no "(or Z if cleaner)". The concrete
   `module.py`, the concrete type/union and its fields, the concrete function name and
   signature, the concrete config key are pinned. If the author can settle it from the
   design docs + the code, it is settled in the issue — not left to the implementer.

2. **Zero scope ambiguities.** File ownership is crisp: the body states which files the
   work touches, and — in a multi-issue tree — one writer owns each file. No "appropriate",
   "as needed", or "a sensible default" standing in for a value or a boundary; every edge
   case the body names has a stated outcome.

3. **Zero hedging.** The contract is stated positively and singularly. The one legitimate
   exception is a genuine **dispatch-time** discovery — schema drift, a third-party
   behavior the docs don't pin, an algorithm case found only in implementation — named as a
   **surfacing condition**, not a fork.

4. **Self-contained, with a Reading list.** The body plus its Reading list is everything the
   implementer needs. Shared vocabulary is coherent across a tree: a type / seam / module /
   config key pinned in one issue is referenced by that exact name everywhere, and each
   consumer's Reading list points at the issue that defines it.

**The drafting-time vs dispatch-time cut** governs conditions 1 and 3. A question is
*drafting-time* if it can be answered without running code — config values, encoding choices
(named constant vs yaml-loaded), naming, scope boundaries, default behaviors, stub
strategies. Settle these in the issue. A question is *dispatch-time* only if it requires
implementation discovery — leave it as a named surfacing condition and trust the consumer to
escalate. The cut line: if it can be answered without running code, it may not remain open.

## The common body sections

Every implementation-ready issue — bug fix or greenfield story — carries these four.
Authoring skills wrap type-specific sections around them: a greenfield story adds a **Goal**
and **Depends on**; a bug fix adds **Evidence**, **Root cause**, and **Why it escaped
tests**. Those type-specific sections live in the authoring skill, not here.

### Reading

The files the implementer must read, each with a one-clause note on what it provides and
which section matters. Order: design docs → config models → upstream/downstream code →
sibling-issue specs to align with. Anchor at `file:line` wherever it tightens the pointer. A
reading list the implementer can follow without rediscovering the surface area is the
difference between ready and not.

### Scope

The concrete deliverables, named. For a multi-part scope, use `**(A) Label.**` bold run-in
labels under a **single** `## Scope` heading — not a run of `## Scope — Part A` / `## Scope —
Part B` headings (the renderer drops them; see Linear mechanics). Each deliverable names the
function signature / type definition / behavior precisely — parameter names, types, return
shape, edge-case semantics — quoting the design doc where possible. State what's out of
scope only when a reader would plausibly assume it's in.

### Acceptance criteria

Atomic, testable outcomes — one observable fact each, phrased as checkboxes. Each must fail
for a reason no other criterion fails for. A criterion is atomic when a grader can mark it
pass/fail by running one command or reading one file. Avoid umbrella criteria ("the module
works") — split into named behaviors of named functions with named inputs.

### Verification

How the consumer confirms done: the test suite to run, the behavior to spot-check, the lint
gate. Name any criterion verified by inspection rather than by automated test. The
authoritative full-suite gate is CI on Windows (CLAUDE.md "Testing"), never a local full run.

## Writing rules

- **Structural / quality criteria over numeric anchors.** Don't bake `70/85/95`, `60%`, `30
  minutes` into acceptance criteria — an implementer (or an LLM agent) reads a number as a
  target. Reference the configuration source (`config/<feature>.yaml`, the config model) and
  let the verification test load the value.
- **State contracts positively.** Don't document a prohibition already implied by the
  positive statement — it propagates into redundant defensive checks at implementation time.
  Write "this issue does NOT cover X" only when X is a likely-but-wrong reading; if X is
  obviously someone else's job, just don't mention it.
- **Anchor every load-bearing claim at `file:line`.**
- **Don't invent component names.** Every typed value object either mirrors an existing
  upstream record or is named by a design doc. Naming a new record with no design-doc
  precedent is a stop-and-surface, not a drafting liberty.
- **Editorial discipline — write the final shape, not the drafting process.** "Wait, this is
  a third method…", "Actually, on second thought…", and internal contradictions survive
  Linear's render and clutter the body for the agent picking it up. Converge to one positive
  statement before saving.

## Linear mechanics

The issue-body-specific subset of `docs/agents/linear.md` — read that for the full API detail.

- **Re-fetch and verify after every `save_issue`.** The renderer silently drops content; the
  round-trip is the only proof it rendered.
- **Renderer hazards:** a bullet list directly after a colon-ending line (keeps only the
  first — use a heading or inline prose); sub-bullets nested under a numbered item (keeps
  only the first); long runs of same-prefix sub-headings interleaved with code (drops
  intermediate ones — use `**(A) Label.**` bold labels under one heading). A bold prefix
  containing inline code (`` **(A) `code` …** ``) truncates at the first backtick — keep
  backticks out of the bold span.
- **Relation fields are append-only.** A new `blockedBy` / `relatedTo` *adds*, never replaces
  — use `removeBlockedBy` / `removeRelatedTo` to clear; pass add + remove in one call to
  swap. After a description-only re-save, never re-send `blockedBy`.
- **Naked `ALP-XXX` references** round-trip to `<issue id="…">ALP-XXX</issue>` tags —
  cosmetic, don't chase the diff.
- **Status names are case-sensitive** (`Todo`, not `todo`). An authoring skill leaves status
  at `Todo` unless told otherwise — it makes an issue ready, it doesn't start it.
- **Active-issue cap** (free tier, ~250): `uv run python scripts/check_linear_cap.py` for
  the count; mind it when an authoring run creates new issues or files spin-offs.
