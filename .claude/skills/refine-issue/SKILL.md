---
name: refine-issue
description: Use to sharpen a single underspecified Linear issue into an implementation-ready spec — the issue an `/implement-issue` run needs but doesn't yet have. Triggers on `/refine-issue ALP-XXX` and operator phrases like "refine ALP-525", "sharpen ALP-525", "flesh out ALP-525", "make ALP-525 implementation-ready", "ALP-525 needs more detail before implementing", and — for production bugs — "root cause ALP-525", "investigate the bug in ALP-525", "rewrite ALP-525 from the symptom". Auto-detects whether the issue is a production bug (drives past the symptom to the true root cause on an irrefutable chain of production-sourced evidence) or an underspecified spec (gathers design + code context and fills the gaps), then rewrites the whole issue body to the shared implementation-ready standard. Authoring only — no code changes, no worktree; hands off to `/implement-issue` to build, and to `/draft-user-stories` if the issue is really a multi-story feature. Do NOT use to implement, or for a parent feature Issue.
---

# Refine a single Linear issue into an implementation-ready spec

The operator points this skill at one Linear issue (e.g. `ALP-525`) that isn't yet ready to
implement. You rewrite it so an implementer — `/implement-issue`, or a dispatched
`story-implementer` under `/orchestrate` — can pick it up cold with zero open questions.

**The deliverable is the implementation-ready issue itself** (plus, for a production bug,
documented manual prod-cleanup steps if prod is left dirty). This skill does **not**
implement the fix and does **not** run cleanup — that's `/implement-issue` and a human on the
prod box. **Investigation and authoring only: no code changes, no worktree.**

"Implementation-ready" is defined once, in **`docs/agents/implementation-ready-issue.md`** —
the bar, the common body sections (Reading / Scope / Acceptance criteria / Verification), the
writing rules, and the Linear render mechanics. Read it; everything below produces an issue
that meets it. Read `docs/agents/linear.md` before touching Linear tools.

## Two modes

An issue arrives as one of two kinds, and this skill auto-detects which:

- **Bug mode** — the issue reports a production defect. The reported cause is a *hypothesis*;
  you drive past the symptom to the true root cause on an irrefutable chain of
  production-sourced evidence, then scope the fix. This is the full rigor of the
  investigative stance in **`bug-mode.md`** (read it when in bug mode), backed by the
  evidence sources in **`production-evidence.md`**.
- **Spec mode** — the issue names work to do (a feature story, a refactor, a chore) but is
  underspecified: vague scope, missing or umbrella acceptance criteria, no reading list, open
  design decisions. You drive past the vague framing to a complete specification grounded in the
  as-built code (and, where a design assumption rests on real data, in production). This is the
  full rigor of the specifying stance in **`spec-mode.md`** (read it when in spec mode); it draws
  on the same **`production-evidence.md`** sources as bug mode for validating ground-truth data.

**Triage signals (auto, with fallback).** Bug mode when the issue carries a `Bug` label,
reports a production symptom, or names production entities (position / order / invocation /
fill ids), or the operator says "root cause / investigate". Spec mode otherwise. When the
signals conflict or the issue is genuinely ambiguous, ask the operator which mode before
investing.

**Too-big screen.** If the issue is really a multi-story feature — several distinct
deliverables that each want their own issue — stop and tell the operator to run
`/draft-user-stories` on it. This skill sharpens one issue; it does not decompose. (Filing a
separate spin-off for a genuinely-distinct *second* defect found mid-investigation is fine —
that's flagging a separate concern, not decomposing the in-scope work.)

## Procedure

### Phase 0 — Resolve the issue

`get_issue(id="ALP-XXX", includeRelations=true)` and read the body and comments
(`list_comments`). Capture the reported symptom / intent, the proposed approach, current
status, labels, and the `relatedTo` / `blockedBy` graph (a related issue is often the actual
mechanism). Do not enter a worktree; you will not touch code.

### Phase 1 — Read as a hypothesis, then triage

Read the body as a *starting lead, not ground truth* — for a bug, the reported cause is
frequently wrong; for a spec, the proposed approach is a draft you may discard. List the **load-bearing claims** (bug) or the **open decisions and gaps** (spec)
the rewrite must resolve. Triage bug vs spec per the signals above; run the too-big screen.
If ambiguous, ask.

### Phase 2 — Establish ground truth

**Bug mode** — follow the investigative stance in `bug-mode.md`: reproduce the symptom from
production, confirm or refute every load-bearing claim against a primary source, and drill to
the structural defect at `file:line` that explains every observed fact.

**Spec mode** — follow the specifying stance in `spec-mode.md`: challenge the framing (right
change / place / size; reuse before construction), ground every claim in a primary source — the
as-built code for contracts, production for data assumptions — pin every interface to a concrete
name, and map the gaps the rewrite must fill.

### Phase 3 — Operator decision gate (genuine forks only)

Resolve everything you can from evidence + code yourself. Surface to the operator only a
**genuine** decision the evidence cannot settle — a design trade-off (a broad structural fix
vs a narrow targeted guard; whether a sibling operation shares the defect and belongs in
scope; a config value with no design-doc default). Give options + a recommendation, and wait,
*before* writing the scope — **except** when the fork is scope-shaped (how much to change: a
broad structural fix vs a narrow targeted guard), which you frame as the ladder in
`docs/agents/operator-decisions.md` and surface with **no** recommendation. Don't
invent a decision the data already settles; don't unilaterally pick where it's a real fork.
(This is the drafting-time vs dispatch-time cut from the standard.)

### Phase 4 — Rewrite the whole issue to the standard

Rewrite the **entire** body via `save_issue`, conforming to
`docs/agents/implementation-ready-issue.md`. Add the mode-specific sections per the companion:

- **Bug mode** (`bug-mode.md`): a "**Corrected from the original report:**" callout if you
  refuted the cause, then **Evidence**, **Root cause**, **Why it escaped tests**. Rewrite the
  title if it encodes the wrong cause; flag a distinct second defect as "**Separate observation
  — do NOT bundle**".
- **Spec mode** (`spec-mode.md`): **Goal** and **Depends on** before the common sections, plus
  **Out of scope** where a reader would assume something is in.

**Operational runbook impact.** Assess whether the work changes production operational behavior
— a service, the schedule, a port, an env var, a migration / bootstrap step, a CLI flag, a
monitoring surface, or a new failure mode / gotcha. If so, add an acceptance criterion that the
same change updates `scripts/RUNBOOK_production.md` (its Living-document rule: the runbook moves
with the behavior). Skip for work with no operator-visible prod-runtime effect (pure internal
logic, analysis / decision-layer changes, test-only work).

Leave status at `Todo`.

### Phase 5 — Production data cleanup (bug mode only)

If the bug left production inconsistent, include the one-time manual prod-box cleanup recipe
per the discipline in `bug-mode.md`. If the class self-heals after the fix, or the reported
instance is healthy by-design, say so explicitly ("**No bulk cleanup needed**"). Never invent
cleanup for healthy state; never re-apply an already-applied side effect. Spec mode has no
cleanup section.

### Phase 6 — Verify the render, then report

Re-fetch with `get_issue` and confirm the renderer didn't drop content (see the render
hazards in the standard). Then give the operator a tight summary:

- **Bug:** what the report claimed, what you confirmed / refuted with which production
  evidence, the true root cause in plain English, the fix in one paragraph, whether cleanup is
  needed.
- **Spec:** what was underspecified, what context you gathered, the gaps you filled (decisions
  pinned, criteria written, reading list assembled), and any fork you resolved with the
  operator.

State plainly that no code was changed.

## Boundaries

- Investigation and authoring only — **no code changes, no worktree, no tests, no PR.** Hand
  off to `/implement-issue` (build) or `/draft-user-stories` (if it's really a feature).
- Operate on an **existing** issue the operator names — don't create one from a blank prompt.
- Rewrite only this one issue's **description** (and `title`, and `state` only if needed).
  Don't touch sibling issues. The one exception is filing a separate spin-off for a distinct
  second defect (bug mode).
- Don't leave "likely / appears to / probably" in a bug root cause — confirm it (cite the
  source) or it isn't the root cause.
- Don't propose a fix before the mechanism is nailed (bug mode); don't write scope before the
  gaps are mapped (spec mode).

## Self-improvement

Note where the skill let you down — an ambiguous step, an unanticipated edge case, a Linear /
MCP gotcha. Don't fix it mid-flight; after reporting, propose edits as **Where** / **What** /
**Why** (the event that exposed the gap). Bar: "would have saved a step" or "prevented a
mistake"; skip silently otherwise.
