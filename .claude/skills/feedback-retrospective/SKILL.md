---
name: feedback-retrospective
description: Use to conduct an open-ended quarterly LLM-driven deep retrospective over AlphaMind's recent operating history — Claude reads many invocations end-to-end, surfaces patterns the deterministic metrics did not catch, and proposes candidates for promotion to deterministic metrics. Triggers on `/feedback-retrospective` and operator phrases like "let's do the quarterly retrospective", "deep dive on Q3", "what patterns have we missed?", "open-ended review of the last few months", "find things our metrics don't catch". Open-ended by design: the operator does not bring a specific question — Claude reads the operating record, finds patterns, and proposes things worth investigating. Long-form (typically a multi-hour session); produces a saved retrospective report; calendar-anchored cadence (typically once per quarter). For routine performance review use /feedback-review; for evaluating a specific edit use /feedback-validate.
---

# Feedback retrospective session

The quarterly open-ended job from
[`feedback-loop.md` § Skills](../../../docs/design/feedback-loop.md#skills): read a large
window of AlphaMind's operating history end-to-end and surface patterns the deterministic
metrics could not catch on their own. The output is a structured retrospective report —
promotion candidates (patterns worth instrumenting as new deterministic metrics) and
follow-up actions (validations to register, pending rollback decisions to resolve).

Read [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md)
first — it defines the headless CLI surface, the review discipline, the rollback-evidence
protocol, the MetricId catalog pointer, and the dashboard affordances deferred to
[ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686). The metric inventory, the
confounder list, and the framing for what the LLM retrospective is meant to find that the
deterministic layer cannot live in
[`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md); read it on first
invocation, particularly the metric inventory and the citation-chain section.

This is the most expensive feedback-loop activity: it consumes substantial tokens reading
raw agent outputs across the window. The calendar anchor (typically once per quarter) keeps
the cost contained.

This skill runs headlessly. The command-center dashboard and its review-session shared
canvas — including the retrospective view — are deferred to
[ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686). The report is saved as
markdown the operator reads; decisions are captured through the CLI. When that surface
lands, the same report renders on the dashboard and the deferred affordances ground it
visually.

---

## 1. The phases

Four phases, in order. Phases 1–3 are Claude-only; Phase 4 is interactive. Each of Phases
1, 3, and 4 drives one subcommand of the retrospective CLI
(`python -m alphamind.feedback_loop.retrospective.cli`, ALP-890):

| Phase | Subcommand | Drives |
|---|---|---|
| 1 — Data ingestion | `ingest` | the in-window data set |
| 3 — Report rendering | `save-report` | persist the rendered markdown |
| 4 — Walkthrough | `capture-decision` | record each operator decision |

Pass `--db-path` against the production DB read-only per the
[environment policy](../../../CLAUDE.md); omit it to fall back to the configured production
path (the CLI warns when it does). Exit codes: `0` success, `1` argument error, `2`
infrastructure error.

### Phase 1 — Data ingestion (Claude-only, no operator interaction)

Default window: the prior calendar quarter; the operator can override. Run:

```
python -m alphamind.feedback_loop.retrospective.cli ingest \
    --start <ISO-8601> --end <ISO-8601> [--db-path <path>]
```

`--start`/`--end` are tz-aware ISO-8601 (a naive datetime exits 1). The command prints the
in-window counts you read in this phase: `agent_calls`, `thesis_resolutions`, `pm_decisions`,
`validations`, `replays`, and `pending_rollbacks` — the
`rollback_status = optional_pending_retrospective` outcomes not yet resolved by a prior
`decision_type: follow_up`, per
[`feedback-loop-skills.md` § Rollback evidence protocol](../../../docs/agents/feedback-loop-skills.md#rollback-evidence-protocol).
Under the `pending_rollbacks` count it then prints one line per pending rollback —
`<item_identifier>\t<edited_artifact>`. The `item_identifier` is canonical: copy it verbatim
into Phase 4's `capture-decision --item-identifier`. Do not reconstruct it.

This is heavy and will consume substantial tokens. Confirm at the start that the operator is
OK with a several-minute pause, then proceed without further interaction until Phase 2
completes.

### Phase 2 — Pattern surfacing (Claude-only)

Read the ingested data to find patterns the deterministic metrics did not surface — the
purpose this skill exists for. Look across cross-agent interactions, regime-conditioned
behavior shifts, repeating thesis-failure narratives, synthesizer drop patterns, PM
modification stickiness, adaptive-researcher productivity, and prompt-edit drift; the metric
inventory and citation-chain section of
[`feedback-loop.md`](../../../docs/design/feedback-loop.md#metric-inventory) bound what the
deterministic layer already covers, so aim past it.

Some categories yield nothing. "Noticed but no clear pattern" is a valid finding — the
absence of pattern in a place you looked is itself information.

### Phase 3 — Report rendering

Render the report as markdown, then persist it:

```
python -m alphamind.feedback_loop.retrospective.cli save-report \
    --start <ISO-8601> --end <ISO-8601> --markdown-file <path> \
    [--session-id <id>] [--db-path <path>] [--data-root <path>]
```

The command writes the markdown to `data/retrospective_reports/{report_id}/report.md`,
writes the metadata row, and prints the new `report_id`. Carry that `report_id` into Phase 4.

Structure the report with these sections:

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
- Pending rollback decisions: one bullet per `pending_rollbacks` entry from Phase 1 — the
  failed validation's edited artifact, watched metric, verdict, the rule that produced the
  deferred status, and a recommended accept/reject framing. Carry each entry's
  `item_identifier` (printed by `ingest`) verbatim so Phase 4's capture routes through the
  `decision_type: follow_up` mechanism.
- Validations to register (for changes the operator might consider)
- Investigations to schedule
- Open questions for the next retrospective
```

### Phase 4 — Walkthrough (operator + Claude interactive)

Walk the operator through the saved `report.md` top-to-bottom — they read the markdown; the
dashboard retrospective view (`scroll_to_section` / `highlight_decision_row` and the
review-session canvas) is deferred to
[ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686), catalogued in
[`feedback-loop-skills.md` § Deferred dashboard affordances](../../../docs/agents/feedback-loop-skills.md#deferred-dashboard-affordances-alp-686).

Capture each decision as the operator makes it:

```
python -m alphamind.feedback_loop.retrospective.cli capture-decision \
    --report-id <id> --item-identifier <id> \
    --decision-type {promotion_candidate,follow_up} \
    --verdict {accepted,rejected} --rationale <text> \
    [--linked-validation-id <id>] [--db-path <path>]
```

The command writes a `retrospective_decisions` row and prints the `decision_id`. Map the
report sections onto its arguments:

- **Promotion candidates** → `--decision-type promotion_candidate`. An `accepted` verdict
  means the operator implements the new metric separately; a `rejected` verdict records the
  rationale.
- **Suggested follow-ups** → `--decision-type follow_up`. When an accepted follow-up converts
  to a validation registration, hand off to [`/feedback-validate`](../feedback-validate/SKILL.md)
  REGISTER and pass the resulting validation id back as `--linked-validation-id`.
- **Pending rollback decisions** (the `pending_rollbacks` items, each passed with its
  verbatim `item_identifier` from Phase 1) → `--decision-type follow_up`. An `accepted`
  verdict means the operator will execute the rollback; route the paired post-rollback
  registration through
  [`/feedback-validate`](../feedback-validate/SKILL.md) and pass its validation id back as
  `--linked-validation-id`. The rule that produced each deferred status is fixed by the
  [rollback evidence protocol](../../../docs/agents/feedback-loop-skills.md#rollback-evidence-protocol)
  — no new judgment is introduced when resolving it here.

The saved report persists; there is no session state to tear down.

---

## 2. Discipline you maintain throughout

Apply the shared review discipline from
[`feedback-loop-skills.md` § Review discipline](../../../docs/agents/feedback-loop-skills.md#review-discipline)
— confounder conditioning, sample-size honesty, Goodhart framing, no auto-prescription.
Three applications are specific to an open-ended retrospective:

- **Open-endedness.** The patterns Claude finds are the patterns Claude finds. If a category
  yields nothing, write "nothing notable in this category" and move on. Inventing patterns to
  fill space wastes operator attention and pollutes the historical record.
- **Promotion candidates, not prescriptions.** The retrospective surfaces what to MEASURE; the
  operator decides what to CHANGE in subsequent sessions (typically `/feedback-validate` for a
  proposed change). The "Promotion candidates" section proposes new metrics; it does not
  direct edits.
- **"Noticed but unexplained" is a valid finding.** Some patterns resist explanation in the
  available data. Record them honestly — they become hypotheses for the next quarter.

---

## 3. Anti-patterns specific to running a retrospective

- **Filling sections to look thorough.** If the citation-chain analysis produced no notable
  shifts, write "no notable shifts" — do not pad with restated metric definitions.
- **Rebranding the existing metrics.** The retrospective adds value by surfacing what the
  metrics didn't catch. If "Patterns observed" reads like the digest's weekly view, re-read
  the section and find the things behind the numbers.
- **Drowning the operator in detail.** The Phase 4 walkthrough should stay navigable in one
  sitting. If the report is too long to walk through, the report is too long. Cut.
- **Confounder hand-waving.** "PM rejection rate of analyst proposals declined 8pp" is useless
  without the regime distribution, the active prompt versions, and any concurrent edits over
  the window.

---

## 4. Cross-references

- [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md) —
  shared reference: headless CLI surface, review discipline, rollback-evidence protocol,
  MetricId catalog pointer, deferred dashboard affordances
- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — full metric
  inventory, confounder list, citation-chain methodology, and the framing for what the LLM
  retrospective finds that the deterministic layer cannot
- [`feedback-review`](../feedback-review/SKILL.md) — sibling skill for routine performance
  review
- [`feedback-validate`](../feedback-validate/SKILL.md) — sibling skill for validation
  registration; Phase 4 hands off to it for follow-ups and pending rollback decisions
