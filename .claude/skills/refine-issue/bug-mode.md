# Bug mode: root-cause a prod bug into an implementation-ready issue

When `refine-issue` triages an issue as a **production bug**, this file governs. The issue is
almost always a **hypothesis** written from a symptom — a plausible story that has not been
confirmed against production. Discover the **true root cause** and rewrite the issue so an
implementer can pick it up cold with zero open questions.

The generic bar, the common Reading / Scope / Acceptance criteria / Verification sections, the
writing rules, and the Linear render mechanics live in `docs/agents/implementation-ready-issue.md`.
This file adds the bug-specific investigative stance, the bug-specific body sections, and the
prod-cleanup discipline. Evidence sources and queries live in `production-evidence.md`.

## The core principle

> The investigation must arrive at the **true root cause of the reported issue — not the
> symptom** — on an **irrefutable chain of production-sourced evidence.**

## The investigative stance

### 1. The reported cause is a hypothesis, not a fact — expect to refute it

Treat the issue's title and body as a starting lead, not ground truth. Reports are written
from a symptom plus a guess, and the central claim is frequently wrong. Your first question on
reading one is "what would prove or disprove this?", not "how do I fix it?". When you refute
the stated cause, the rewrite opens with a "**Corrected from the original report:**" callout
stating what the report got wrong and the true picture.

### 2. Confirm OR refute every load-bearing claim against a primary source

For each claim the root cause rests on, run the query that confirms or refutes it against a
**primary** source — the prod DB, live Alpaca, an archived invocation artifact, the installed
library, the code at file:line — never against the report's own narrative or a secondary
summary. If you write "the fill was never captured", you have queried the fills table *and*
the broker and can paste the row (or its absence). "Appears to / likely / should" are banned
from the final issue — replace each with the row, the order, or the line of code.

### 3. Drill to the mechanism — don't stop at the first plausible explanation

Keep asking "but *why*" until you bottom out at a **structural code-level defect** you can
point to at file:line and that explains **every** observed symptom. A restated symptom is not
a root cause: "the commit was lost" is a symptom; "the broker submit happens *before and
outside* the transaction that writes the order row, and that commit has no retry protection
against the concurrent-writer race" is the mechanism. If any observed fact is still
unexplained, you are not done.

### 4. You cannot scope the fix until the root cause is conclusive

Scope is the *last* step, not the first. A fix written before the mechanism is nailed fixes a
guess. Don't propose edits until rule 3 holds.

### 5. Symptom vs disease — state both, fix the disease

Name the observable symptom *and* the underlying disease. The issue fixes the disease; it may
note a defensive symptom-level guard as an explicitly-descoped follow-up, never as the fix. A
symptom-scoped fix papers over the disease and tends to reintroduce it elsewhere.

### 6. Separate distinct defect classes — do NOT bundle

Evidence often surfaces a second anomaly that looks similar but has a **different** root cause
the proposed fix wouldn't touch. Flag it for separate triage in a "**Separate observation —
do NOT bundle**" section; never fold a different root cause into the issue you're sharpening.

### 7. By-design steady state is not a bug — verify before proposing cleanup

Before calling a production row an orphan, confirm it isn't the correct by-design state. Query
the **whole class**, not just the reported instance — that's what distinguishes healthy from
broken, and tells you whether cleanup is one row or systemic.

### 8. Be skeptical of your own intermediate conclusions

Re-derive your own load-bearing claims as adversarially as you challenge the report's. Verify a
cited issue number, commit, or file against git log and memory before finalizing. An
investigation that's wrong in a cited fact is worse than none — it sends the implementer down a
false trail with false confidence.

### 9. Explain why it escaped tests

If it's a real bug in covered code, find why existing tests didn't catch it — that's part of
the root cause and it shapes the new test design (the classic case: a test that mocks the very
seam where the failure occurs). Capture it in a "Why it escaped tests" note.

## Bug-mode body sections

These wrap around the common sections from the standard. Order:

```markdown
## Summary            (or ## Symptom for narrower bugs)
<the observable symptom in prose. If you refuted the report, lead with a
 "Corrected from the original report:" paragraph stating what it got wrong and the
 true picture.>

## Evidence (prod DB + live Alpaca — <entity ids>)
<the irrefutable chain: specific rows, order ids, fill ids, exact prices/qtys/timestamps,
 the invocation that produced it, and timing corroboration. Each bullet is a fact with
 its source. No "appears to / likely".>

## Root cause   (or: Root cause (confirmed))
<numbered structural mechanism, each step anchored at file:line, explaining every
 symptom. Call out a secondary integrity gap as "note, not the fix target" if found.>

## Why it escaped tests
<which existing test should have caught it and why it didn't — informs the new tests>

## Scope — <one-line characterization of the clean fix>
<lettered bold-label steps (A)…(G): concrete edits at file:line. Note what's
 Descoped / superseded and why. Flag traps (e.g. a migration CHECK-constraint built from
 the live enum when widening a status vocabulary).>

## Acceptance criteria
<atomic, testable bullets — each fails for a distinct reason; mirror the Scope steps>

## Verification
<integration/unit tests to write, and the authoritative gate (CI full suite on Windows)>

## One-time prod data cleanup (manual, Windows prod box; `source .env`)
<only if prod is dirty — see "Prod-cleanup discipline"; else "No bulk cleanup needed"
 with a one-line why>

## Separate observation — do NOT bundle
<only if a distinct second defect class surfaced — flag it for its own issue>

## Related
<linked issues with the CAUSAL relationship spelled out, not just the id>
```

The Scope / Acceptance criteria / Verification mechanics (bold run-in labels under one heading,
atomic criteria, the CI gate) are the standard's — this just names where the bug-specific
sections sit relative to them.

## Prod-cleanup discipline

When the bug stranded production in a dirty state, the issue carries a precise, manual,
prod-box cleanup recipe. What makes it safe:

- **Find what's already been side-effected and DON'T redo it.** The signature hazard: a
  reconciliation or recovery pass may have *already* applied part of the effect (e.g. already
  credited cash), so re-running the normal path double-counts. State the hazard explicitly
  ("Cash hazard: `cash_ledger` already matches Alpaca and already includes the proceeds; the
  repair must book PnL + history **only**").
- **Each step is concrete and idempotent-minded:** exact table, exact row key (the real ids),
  exact target values (the real prices/qtys/PnL), and exactly which fields change vs stay.
- **Repair to a consistent end-state, not to "looks closed".** Book honest PnL from the real
  fill price rather than a reconcile estimate (reconcile has no per-position exit price).
- **End with a cleanup verification checklist** — the exact post-state to confirm (status,
  realized PnL, share_count, no stranded fill, cash unchanged and equal to Alpaca, next
  reconciliation emits no alert).
- A **new table the fix introduces needs an Alembic migration** — but cleanup of *existing*
  rows is a manual prod-box operation, never a migration.
