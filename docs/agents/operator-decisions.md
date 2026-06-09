# Framing an operator decision

Shared reference for the issue-authoring and -auditing skills — `refine-issue`,
`draft-user-stories`, `audit-user-stories` — on how to frame a genuine fork the agent surfaces
to the operator after resolving everything it can from evidence and code itself.

A fork is either **categorical** — orthogonal choices with no scope spectrum (named-constant
vs yaml-loaded, per-invocation vs cron, empty-tuple vs fake-record stub) — or **scope-shaped**.
Give a categorical fork a short option list and a recommended default. Frame a scope-shaped
fork as the ladder below.

## Surfacing a scope-shaped fork

A fork is **scope-shaped** when the goal is fixed and the only question is *how much to change
to reach it* — a correctness-versus-blast-radius tradeoff (the canonical case: a broad
structural fix vs a narrow targeted guard).

The ladder is two fixed end-posts with one or two rungs between them:

- **The Principled Choice** — maximally correct, efficient, and maintainable *regardless of
  blast radius*: the design that's right given unbounded scope, addressing the root cause's
  root cause. It may mean restructuring a whole module or subsystem. The asymptote, stated
  honestly even when plainly unaffordable.
- **The Minimal Choice** — the fewest lines that satisfy the stated intent and nothing else:
  the immediate bug fixed or the design intent met, no adjacent improvement.
- **One or two intermediate rungs**, each labelled by *what it concretely changes* — not a
  manufactured name like "Balanced". Add the second only when it marks a genuinely distinct
  point on the spectrum; never pad the ladder to reach it.

Characterize every rung by **blast radius** — the `Surgical → Local → Module → Subsystem →
System-wide` scale from `python-architecture` — never by time; time anchors rot and mislead.
The Minimal end-post sits at `Surgical`/`Local`, the Principled end-post at
`Subsystem`/`System-wide`.

**Give no recommendation.** This is the one exception to these skills' standing "options + a
recommendation" rule, and it holds only for scope-shaped forks. How much blast radius to
accept is the operator's call; the agent's job is the honest, calibrated spectrum, not a
steer. Present the rungs in spectrum order, with no rung tagged "(Recommended)". The end-posts
exist to bracket the real decision space, so cutting back to a smaller rung is a visible,
deliberate choice rather than an unexamined default. When the fork is surfaced through
`AskUserQuestion`, its four-option cap is why the ladder tops out at four rungs — "Other"
remains the operator's escape if none land.
