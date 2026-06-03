# House style: the implementation-ready issue

Used by the `root-cause-analysis` skill (Phases 5–7). How to write the rewritten Linear
issue so it renders correctly and reads as implementation-ready.

## Body template

Sections in this order. For a multi-part Scope, use `**(A) Label.**`-style bold run-in
labels under a **single** heading — **not** a run of `## Scope — Part A`, `## Scope — Part
B` headings (the renderer drops them; see "Renderer hazards" below).

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

Writing rules:

- **Prefer structural/quality acceptance criteria over numeric caps, soft targets, or
  ranking labels** — they read as targets to an implementer.
- **State contracts positively.** Don't document prohibitions already implied by the
  positive statement; they propagate into redundant defensive checks at implementation
  time.
- **Anchor every claim at file:line.** A reading list the implementer can follow without
  rediscovering the surface area is the difference between ready and not.

## Prod-cleanup discipline

When the bug stranded production in a dirty state, the issue carries a precise, manual,
prod-box cleanup recipe. What makes it safe:

- **Find what's already been side-effected and DON'T redo it.** The signature hazard: a
  reconciliation or recovery pass may have *already* applied part of the effect (e.g.
  already credited cash), so re-running the normal path double-counts. State the hazard
  explicitly ("Cash hazard: `cash_ledger` already matches Alpaca and already includes the
  proceeds; the repair must book PnL + history **only**").
- **Each step is concrete and idempotent-minded:** exact table, exact row key (the real
  ids), exact target values (the real prices/qtys/PnL), and exactly which fields change vs
  stay.
- **Repair to a consistent end-state, not to "looks closed".** Book honest PnL from the
  real fill price rather than a reconcile estimate (reconcile has no per-position exit
  price).
- **End with a cleanup verification checklist** — the exact post-state to confirm (status,
  realized PnL, share_count, no stranded fill, cash unchanged and equal to Alpaca, next
  reconciliation emits no alert).
- A **new table the fix introduces needs an Alembic migration** — but cleanup of
  *existing* rows is a manual prod-box operation, never a migration.

## Linear mechanics (from `docs/agents/linear.md` — re-read it)

- **Re-fetch and verify after every `save_issue`** — the renderer silently drops content.
- **Renderer hazards:** sub-bullets nested under a numbered item (keeps only the first); a
  bullet list directly after a colon-ending line (keeps only the first — use a heading or
  inline prose); long runs of same-prefix sub-headings interleaved with code (drops
  intermediate ones — use `**(A) Label.**` bold labels under one heading).
- **Relation fields are append-only.** Passing a new `relatedTo` / `blockedBy` *adds*,
  never replaces — use `removeRelatedTo` / `removeBlockedBy` to clear; pass add + remove in
  one call to swap atomically.
- **Status names are case-sensitive** (`Todo`, not `todo`). Leave status `Todo` unless the
  operator says otherwise — this skill makes an issue ready, it doesn't start it.
- Mind the **active-issue cap** (free tier, 250) only if you file a *new* spin-off issue
  for a separate-observation defect; `uv run python scripts/check_linear_cap.py` for the
  count.
