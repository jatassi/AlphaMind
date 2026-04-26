---
name: feedback-retrospective
description: Use to conduct an open-ended quarterly LLM-driven deep retrospective over AlphaMind's recent operating history — Claude reads many invocations end-to-end, surfaces patterns the deterministic metrics did not catch, and proposes candidates for promotion to deterministic metrics. Triggers on `/feedback-retrospective` and operator phrases like "let's do the quarterly retrospective", "deep dive on Q3", "what patterns have we missed?", "open-ended review of the last few months", "find things our metrics don't catch". Open-ended by design: the operator does not bring a specific question — Claude reads the operating record, finds patterns, and proposes things worth investigating. Long-form (typically a multi-hour session); produces a saved retrospective report; calendar-anchored cadence (typically once per quarter). For routine performance review use /feedback-review; for evaluating a specific edit use /feedback-validate.
---

# Feedback retrospective session

A long-form, calendar-anchored retrospective in which Claude reads a large window of AlphaMind's operating history end-to-end and surfaces patterns the deterministic metrics could not catch on their own. The output: a structured retrospective report, plus a list of promotion candidates (patterns worth instrumenting as new deterministic metrics) and follow-up actions (validations to register, prompt edits to consider).

This is the most expensive feedback-loop activity: it consumes substantial tokens reading raw agent outputs across the window. Calendar-anchored (typically once per quarter) keeps the cost contained.

The discipline: open-ended pattern discovery, conditioning context required for every claim, promotion candidates over prescriptions, "noticed but unexplained" as a valid finding.

The full design context is in [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — read it on first invocation, particularly the metric inventory and the citation-chain section.

This skill assumes the AlphaMind dashboard exposes the review-session tools (used for the walkthrough phase) and the analytics-query surface (used for the data-ingestion phase), plus the Chrome browser automation tools for opening the dashboard URL.

---

## 1. The phases

The session has four distinct phases. Run them in order.

### Phase 1 — Data ingestion (Claude-only, no operator interaction)

Claude reads the operating record for the window. Default window: the prior calendar quarter. Operator can override.

The ingestion pulls:
- All invocation records in the window with their provenance (active regime, prompt versions, model versions, mode/overlay activations)
- All thesis resolutions with component-level outcomes
- All PM decision envelopes with verdicts, criterion pass/fail, modifications, identified anti-patterns
- All counterfactual replays with verdicts and confidence
- All anti-pattern occurrences across all PM envelopes
- Cost and latency aggregates per agent

This is heavy. It will consume substantial tokens. Confirm at the start that the operator is OK with a several-minute pause, then proceed without further interaction until Phase 2 completes.

### Phase 2 — Pattern surfacing (Claude-only)

Read the ingested data with the goal of finding patterns the deterministic metrics did not surface. Categories worth looking for:

- **Cross-agent interaction patterns.** When agent X said Y, agent Z usually responded with W — and the combination is associated with a particular outcome distribution.
- **Regime-conditioned behavior shifts.** An agent's reasoning style or output shape changed in a particular regime in a way no single metric flagged.
- **Repeating thesis-failure narratives.** Many invalidated theses share a structural failure not caught by any anti-pattern detector.
- **Synthesizer drop patterns.** Categories of upstream findings the synthesizer systematically deprioritizes that turn out to have predictive value.
- **PM modification stickiness.** Modifications PM applies to certain proposal shapes that turn out to be systematic improvements (or systematic over-corrections).
- **Adaptive researcher productivity patterns.** Tool budget allocation patterns that correlate with downstream citation rates.
- **Prompt edit drift.** Behavior changes that don't trace to a specific edit but accumulated across multiple ones.

Some categories will be empty. "Noticed but no clear pattern" is a valid finding to record — the absence of pattern in a place you looked is itself information.

### Phase 3 — Report rendering

Produce a structured report with these sections:

```
# Quarterly retrospective: {window}

## Period summary
- P/L, win rate, drawdown, key headline outcomes
- Operating cadence (invocations, agent calls, cost)
- Active prompt versions, model versions, regime distribution

## Patterns observed
- {pattern_1}: what was noticed, evidence, conditioning context, confidence
- {pattern_2}: ...

## Anti-pattern trends
- Frequency of each canonical anti-pattern over the window
- Detector accuracy (forward outcomes of tagged positions)
- Notable shifts since the prior retrospective

## Citation-chain trends
- Per-source signal survival rate
- Synthesizer recall
- Notable shifts

## Calibration assessment
- Conviction calibration (analyst), with posterior bands
- Status calibration (strategist), with posterior bands
- PM rejection accuracy
- Notable shifts

## Promotion candidates
- {candidate_1}: pattern Claude noticed that should be instrumented as a new deterministic metric
  - What it would measure
  - How to compute it
  - Why it would be worth tracking
- {candidate_2}: ...

## Suggested follow-ups
- Validations to register (for changes the operator might consider)
- Investigations to schedule
- Open questions for the next retrospective
```

Save the report to a `retrospective_reports` table (or equivalent persistence; spec TBD) and render it in a dedicated dashboard view.

### Phase 4 — Walkthrough (operator + Claude interactive)

Open a review session via `create_review_session()` and `mcp__claude-in-chrome__tabs_create_mcp(url=dashboard_url)`. Navigate the operator to the rendered retrospective report and walk through it top-to-bottom using the standard affordances (highlight, navigate, annotate, pin).

Capture decisions in real-time:
- Promotion candidates accepted → record as new metric proposals (operator implements separately)
- Promotion candidates rejected → record with brief rationale
- Suggested follow-ups accepted → some may convert to validation registrations (handoff to `/feedback-validate`)

End the session via `end_review_session()`. The report itself persists; the session state is transient.

---

## 2. Discipline you maintain throughout

**Open-endedness.** This is not a checklist exercise. The patterns Claude finds are the patterns Claude finds. If a category yields nothing, write "nothing notable in this category" and move on. Inventing patterns to fill space wastes operator attention and pollutes the historical retrospective record.

**Conditioning context for every claim.** Every pattern observation must name the conditioning under which it holds: which regime(s), which prompt versions, which model versions, which sectors. Unconditioned patterns are usually noise.

**Promotion candidates, not prescriptions.** Phase 3's "Promotion candidates" section proposes new metrics worth instrumenting. The retrospective surfaces what to MEASURE; the operator decides what to CHANGE in subsequent sessions (typically `/feedback-validate` for proposed changes).

**"Noticed but unexplained" is a valid finding.** Some patterns will resist explanation in the available data. Record them honestly. They become hypotheses for the next quarter.

**Sample-size honesty.** If a pattern is based on three data points, say so. A small sample is a hint, not a signal.

---

## 3. Using the dashboard affordances

Phase 4 uses the same review-session affordances as `/feedback-review`. The retrospective report itself is rendered as a dedicated view; navigate to it as the starting point.

Phases 1–3 are Claude-only and do not use the dashboard. They produce the report which Phase 4 walks through.

---

## 4. Anti-patterns

**Filling sections to look thorough.** If the citation-chain analysis produced no notable shifts, write "no notable shifts" — do not pad with restating the metric definitions.

**Rebrand of the dashboard's existing metrics.** The retrospective adds value by surfacing things the metrics didn't catch. If the report's "Patterns observed" section reads like the dashboard's monthly view, you've done it wrong — re-read the section with fresh eyes and find the things behind the numbers.

**Prescriptive language.** "The strategist should tighten its at-risk criteria." Replace with: "Strategist `at-risk` classifications in elevated-vol regimes had 38% forward invalidation rate vs. 62% in normal-vol; worth investigating whether the criteria need regime-conditioning." Findings, not prescriptions.

**Drowning the operator in detail.** The Phase 4 walkthrough should be navigable in 60–90 minutes. If the report is too long to walk through, the report is too long. Cut.

**Confounder hand-waving.** "PM rejection rate of analyst proposals declined 8pp this quarter" is useless without: what was the regime distribution, which prompt versions were active, were there other concurrent edits.

---

## 5. Cross-references

- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — full metric inventory, confounder list, citation-chain methodology, the framing for what the LLM retrospective is meant to find that the deterministic layer cannot
- [`docs/design/command-center.md § Review sessions`](../../../docs/design/command-center.md#review-sessions) — session API surface used in Phase 4
- [`docs/design/05-execution-layer/state-persistence.md`](../../../docs/design/05-execution-layer/state-persistence.md) — agent_calls, counterfactual_replays, activity log entries, thesis records that Phase 1 ingests
- [`feedback-review`](../feedback-review/SKILL.md) — sibling skill for routine review
- [`feedback-validate`](../feedback-validate/SKILL.md) — sibling skill for validation registration; Phase 4 may hand off to it for follow-ups
