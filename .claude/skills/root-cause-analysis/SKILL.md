---
name: root-cause-analysis
description: Use to investigate a reported AlphaMind production bug and rewrite its Linear issue to be implementation-ready — driving past the reported symptom to the true root cause on an irrefutable chain of production-sourced evidence, then specifying the fix scope, acceptance criteria, a file:line reading list, and any one-time prod data-cleanup. 
---

# Root-cause a prod bug report into an implementation-ready issue

The operator points this skill at one Linear issue that reports a production bug. That
issue is almost always a **hypothesis** written from a symptom — a plausible story that
has not been confirmed against production. Discover the **true root cause** and rewrite
the issue so an implementer can pick it up cold with zero open questions.

**The deliverable is the implementation-ready issue itself** (plus documented manual
cleanup steps if prod is left dirty). This skill does **not** implement the fix and does
**not** run the cleanup — that's `/implement-issue` and a human on the prod box.
Investigation and authoring only; no code changes, no worktree.

Two companion files hold the mechanics:

- **`production-evidence.md`** — where production evidence lives and how to query it (prod
  DB, live Alpaca, archived invocation artifacts, installed library, git history).
- **`issue-house-style.md`** — the implementation-ready issue body template, the
  prod-cleanup discipline, and the Linear renderer/relation gotchas.

Read `docs/agents/linear.md` before touching Linear tools.

## The bar: "implementation-ready"

An issue is implementation-ready when:

- **0 open decisions** — every fork is resolved, by evidence or by an explicit operator
  call you obtained.
- **0 scope ambiguities** — the fix is concrete enough that two implementers would build
  the same thing.
- **Root cause confirmed irrefutably with production data** — not "likely", not "appears
  to"; every load-bearing claim cites a primary production source.
- **Self-contained** — a reading list of every relevant file (file:line), so the
  implementer never has to rediscover the surface area.
- **Matches house style** — see `issue-house-style.md`.

The only acceptable open item is a genuine scope decision you've surfaced to the operator
(Phase 4).

## The core principle

> The investigation must arrive at the **true root cause of the reported issue — not the
> symptom** — on an **irrefutable chain of production-sourced evidence.**

Everything below serves that sentence.

## The investigative stance

### 1. The reported cause is a hypothesis, not a fact — expect to refute it

Treat the issue's title and body as a starting lead, not ground truth. Reports are written
from a symptom plus a guess, and the central claim is frequently wrong. Your first
question on reading one is "what would prove or disprove this?", not "how do I fix it?".
When you refute the stated cause, the rewrite opens with a "**Corrected from the original
report:**" callout stating what the report got wrong and the true picture.

### 2. Confirm OR refute every load-bearing claim against a primary source

For each claim the root cause rests on, run the query that confirms or refutes it against a
**primary** source — the prod DB, live Alpaca, an archived invocation artifact, the
installed library, the code at file:line — never against the report's own narrative or a
secondary summary. If you write "the fill was never captured", you have queried the fills
table *and* the broker and can paste the row (or its absence). "Appears to / likely /
should" are banned from the final issue — replace each with the row, the order, or the
line of code.

### 3. Drill to the mechanism — don't stop at the first plausible explanation

Keep asking "but *why*" until you bottom out at a **structural code-level defect** you can
point to at file:line and that explains **every** observed symptom. A restated symptom is
not a root cause: "the commit was lost" is a symptom; "the broker submit happens *before
and outside* the transaction that writes the order row, and that commit has no retry
protection against the concurrent-writer race" is the mechanism. If any observed fact is
still unexplained, you are not done.

### 4. You cannot scope the fix until the root cause is conclusive

Scope is the *last* step, not the first. A fix written before the mechanism is nailed
fixes a guess. Don't propose edits until rule 3 holds.

### 5. Symptom vs disease — state both, fix the disease

Name the observable symptom *and* the underlying disease. The issue fixes the disease; it
may note a defensive symptom-level guard as an explicitly-descoped follow-up, never as the
fix. A symptom-scoped fix papers over the disease and tends to reintroduce it elsewhere.

### 6. Separate distinct defect classes — do NOT bundle

Evidence often surfaces a second anomaly that looks similar but has a **different** root
cause the proposed fix wouldn't touch. Flag it for separate triage in a "**Separate
observation — do NOT bundle**" section; never fold a different root cause into the issue
you're sharpening.

### 7. By-design steady state is not a bug — verify before proposing cleanup

Before calling a production row an orphan, confirm it isn't the correct by-design state.
Query the **whole class**, not just the reported instance — that's what distinguishes
healthy from broken, and tells you whether cleanup is one row or systemic.

### 8. Be skeptical of your own intermediate conclusions

Re-derive your own load-bearing claims as adversarially as you challenge the report's.
Verify a cited issue number, commit, or file against git log and memory before finalizing.
An RCA that's wrong in a cited fact is worse than none — it sends the implementer down a
false trail with false confidence.

### 9. Explain why it escaped tests

If it's a real bug in covered code, find why existing tests didn't catch it — that's part
of the root cause and it shapes the new test design (the classic case: a test that mocks
the very seam where the failure occurs). Capture it in a "Why it escaped tests" note.

## Procedure

### Phase 0 — Read the report as a hypothesis

`get_issue(id="ALP-XXX", includeRelations=true)` and read the body and comments
(`list_comments`). Extract the reported symptom, the reported cause, and the proposed fix.
Write down — for yourself — the list of **load-bearing claims** the report makes; each is
now a thing to confirm or refute. Note the `relatedTo`/`blockedBy` graph; a related issue
is often the actual mechanism.

### Phase 1 — Reproduce the symptom from production

Find the symptom in prod data first, pinning the exact entities (position id, order ids,
invocation id, fill id) the rest of the investigation hangs on. If you can't find the
symptom in prod, that itself is a finding — surface it. See `production-evidence.md`.

### Phase 2 — Build the evidence chain (confirm/refute each claim)

Go down the Phase-0 claim list. For each, run the primary-source query that confirms or
refutes it. Cross-check sources against each other and against the clock (UTC vs Mountain
Time). Expect to **refute** central claims — that's the normal outcome. When a claim is
refuted, the correct fact takes its place in the chain. See `production-evidence.md`.

### Phase 3 — Drill to the structural root cause

Apply rule 3: keep asking "but why" until every observed fact is explained by a defect
cited at file:line. Confirm timing corroboration via git log. Confirm by-design vs bug for
the whole class (rule 7). Identify any second, distinct defect class (rule 6). Re-derive
your own claims skeptically (rule 8). You are done only when the mechanism accounts for
**everything** — the symptom, why recovery didn't self-heal it, and why tests didn't catch
it.

### Phase 4 — Operator decision gate (genuine scope forks only)

Resolve everything you can from data + code yourself. When a **genuine** scope decision
remains — one the evidence cannot settle because it's a design trade-off (e.g. a broad
structural rewrite vs a narrow targeted guard; whether a sibling operation shares the
defect and belongs in scope) — surface it to the operator with the options and your
recommendation, and wait, *before* writing the fix scope. Don't invent a decision the data
already settles; don't unilaterally pick where it's a real fork.

### Phase 5 — Rewrite the issue to implementation-ready house style

Rewrite the **entire** issue body via `save_issue` using the template in
`issue-house-style.md`. Open the root cause with the "**Corrected from the original
report:**" callout if you refuted the stated cause; anchor every claim at file:line; mark
any superseded symptom-level approach as such with one line on why; rewrite the title if
the old one encodes the wrong cause.

### Phase 6 — Production data-cleanup steps (only if prod is left dirty)

If the bug left production inconsistent, include a one-time cleanup section per the
discipline in `issue-house-style.md`. If the class self-heals after the fix, or the
reported instance is healthy by-design, say so explicitly ("**No bulk cleanup needed**")
— don't invent cleanup for healthy state.

### Phase 7 — Verify the rendered issue, then report

Re-fetch with `get_issue` and confirm the renderer didn't drop content (see
`issue-house-style.md`). Then give the operator a tight summary: what the report claimed,
what you confirmed/refuted with which production evidence, the true root cause in plain
English, the fix in one paragraph, and whether cleanup is needed. State plainly that no
code was changed.

## What not to do

- **Don't propose a fix before the mechanism is nailed** (rule 4) — a guess at the cause
  yields a guess at the fix.
- **Don't trust the report's narrative as evidence** — only primary production sources
  count.
- **Don't leave "likely / appears to / probably" in the final issue** — either you
  confirmed it (cite the source) or it isn't in the root cause.
- **Don't bundle a second, distinct root cause** (rule 6) — separate triage.
- **Don't propose cleanup for by-design steady state** (rule 7), and never re-apply an
  already-applied side effect.
- **Don't implement the fix or run the cleanup** — hand off to `/implement-issue` and the
  operator.
- **Don't change code or enter a worktree** — investigation and authoring only.
