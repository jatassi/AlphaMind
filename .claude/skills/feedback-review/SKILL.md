---
name: feedback-review
description: Use to conduct an AlphaMind feedback review with the operator — weekly process-metric review, monthly outcome-metric review, or ad-hoc deep-dive into specific agents, metrics, or behaviors. Triggers on `/feedback-review` and operator phrases like "review this week", "let's review last month's performance", "how are we trending", or any operator request to look at AlphaMind agent calibration trends, PM rejection patterns, anti-pattern frequencies, citation chain metrics, conviction or status calibration, or system trajectory month-over-month. Runs headlessly: you read the analytics through the feedback-loop digest CLI and emit findings as markdown the operator reads. Not for live operational health (the command center handles that directly), not for evaluating a specific prompt edit's pre-registered impact (use /feedback-validate), not for open-ended quarterly pattern discovery (use /feedback-retrospective).
---

# Feedback review session

Walk the operator through AlphaMind's recent performance, surface what is worth attention,
apply discipline so weak signals are not overread, and help the operator decide what — if
anything — needs adjustment. You decide nothing on the operator's behalf; you surface
evidence and frame it honestly.

Read [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md)
first — it defines the headless CLI surface this skill drives, the MetricId catalog pointer,
the review discipline, and the dashboard affordances deferred to
[ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686). The metric inventory and the
substance of the discipline live in
[`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md); read it on first
invocation — the shape of a review depends on knowing what is available to look at.

This skill runs headlessly. The command-center dashboard and its review-session shared
canvas are deferred to [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686); read
analytics through the CLI and emit findings as markdown. When that surface lands, the same
findings render on the dashboard and the deferred affordances ground them visually.

---

## 1. Pick the cadence

Three cadences. If the operator's opening message leaves the intent unclear, ask once: *"this
week's process pulse, last month's outcome calibration, or something specific you want to
drill into?"* Then pull the data and walk it top-to-bottom.

### Weekly review (default)

Run `python -m alphamind.feedback_loop.digest.cli digest` (optionally `--week <ISO-DATE>` for
a past week). The JSON `WeeklyDigest` already curates the weekly surface; walk its sections in
order:

- **Notable shifts first.** The digest's flagged shifts — anti-pattern spike, regime change,
  sector underperformance, a validation reaching its window. Highest-leverage because each is
  a *change*. Name each and ask whether the operator had already noticed.
- **Process pulse second.** PM rejection rate, anti-pattern frequencies by name, modification
  rate, guardrail rejection count — each with its week-over-week delta. Process metrics move
  enough week-over-week to be diagnostic. Surface the meaningful deltas and discuss what they
  suggest.
- **Trajectory third.** The 8–12-week sparklines for P/L, win rate, cost per resolved thesis,
  conviction calibration, status calibration (the last two with posterior bands). Ask whether
  the trajectory matches the operator's expectation.
- **Validation status if active.** Active prompt edits in their validation windows with their
  pre-registered expected impact. Flag any nearing window-end so the operator can take it to
  `/feedback-validate`; the go/no-go decision belongs there.
- **Headline outcomes last.** P/L, win rate, drawdown — noisy at the weekly horizon. Mention
  briefly; do not over-weight.

### Monthly review

Outcome-tier metrics dominate; conditioning slices are the primary lens. Pull each via
`metric <metric_id> --window <weeks>` (one contiguous month), reading the `metric_id`s from
`metrics-list`. Lead with:

- **Citation chain metrics.** Per-source signal survival rate, synthesizer recall,
  decision-layer citation rate per source — the highest-leverage diagnostics for whether an
  analysis-layer agent produces predictive content and whether the synthesizer drops signal.
- **Conviction calibration** (analyst) and **status calibration** (strategist), both with
  posterior bands; both need months of data, so honor the `insufficient_sample` flag.
- **Anti-pattern detector accuracy.** For each canonical anti-pattern defined in
  [portfolio-manager.md](../../../docs/design/04-decision-layer/portfolio-manager.md), read
  the forward-outcome distribution of tagged positions — a detector firing on noise signals
  the PM prompt's pattern definition needs revisiting.
- **PM rejection accuracy** from the counterfactual replay engine — when PM rejected a
  proposal, what would the trade have done; same for modifications. Gated on that engine: if
  its metric is absent from `metrics-list`, name it as not-yet-available and move on.

### Ad-hoc deep-dive

The operator brings a specific question — "why did energy underperform this month?", "is the
strategist holding too many at-risk positions?", "is the synthesizer down-weighting
tech-semis findings?". Pull the relevant `metric`(s) over the window the question implies,
conditioned on the slice it names, and walk the data with them. The discipline below applies
unchanged.

---

## 2. Discipline

Apply the shared review discipline from
[`feedback-loop-skills.md` § Review discipline](../../../docs/agents/feedback-loop-skills.md#review-discipline):
process-vs-outcome weighting, confounder conditioning, sample-size honesty, Goodhart framing,
no auto-prescription. These are what make a review trustworthy rather than misleading; honor
them on every metric you surface.

Two applications worth stating concretely here:

- **Read the JSON honestly.** Each `MetricResult` carries `value`, `posterior_band`,
  `sample_size`, and `insufficient_sample`. When `insufficient_sample` is true or the band
  straddles "no change," report the reading as not-yet-actionable — echo the "needs more
  observations" framing rather than reading a point estimate as a trend.
- **Condition before attributing.** When an unconditioned aggregate looks notable, re-pull the
  metric conditioned on regime (or the relevant slice) before claiming a cause. An
  unconditioned number is a question, not a finding.

---

## 3. Emitting findings

The operator reads your markdown — there is no shared dashboard to point at yet. Keep it
scannable:

- One short section per cadence area (notable shifts, process pulse, trajectory, …), each
  leading with the metric and its delta, then one sentence on what it suggests.
- Quote the number with its posterior band and sample size when outcome-tier; quote the
  week-over-week delta when process-tier.
- Flag every confounder you did not control and every insufficient sample inline, next to the
  reading it qualifies — not in a trailing caveats block the operator skims past.
- Close with what is worth the operator's attention, framed as questions or hypotheses, not
  directives. If the operator asks "what would you do?", offer a recommendation with its
  caveats and the validation it would need.

When the [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686) shared canvas lands,
these findings additionally ground on the dashboard via the deferred affordances catalogued in
[`feedback-loop-skills.md` § Deferred dashboard affordances](../../../docs/agents/feedback-loop-skills.md#deferred-dashboard-affordances-alp-686);
until then, the markdown is the surface.

---

## 4. Anti-patterns specific to running a review

- **Reading short-window outcome metrics as signal.** A win-rate move over a dozen trades is
  variance. Treat it as such or leave it out.
- **Unconditioned aggregate as evidence.** "Conviction-4 trades performed worse this month"
  without checking the regime, the analyst prompt version, or the sample size. Re-pull
  conditioned before claiming the cause.
- **Auto-prescription.** Surfacing "tighten the strategist's at-risk criteria" instead of the
  evidence that raises the question. Surface the evidence; let the operator decide.
- **Pretending to know.** With little resolved-thesis volume, "we do not have enough resolved
  theses citing this source to score signal survival rate yet" is the correct answer.
- **Goodhart-style framing.** "Anti-pattern frequency for `sunk_cost_persistence` should be
  lower" teaches avoidance of the language, not the behavior. Say what the frequency suggests.
- **Walls of text.** One sentence per metric is usually enough. The operator wants the signal,
  not the prose.

---

## 5. Cross-references

- [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md) —
  shared reference: headless CLI surface, MetricId catalog pointer, review discipline,
  rollback pointer, deferred dashboard affordances
- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — full metric
  inventory across all layers; the canonical source for what is measurable
- [`docs/design/04-decision-layer/portfolio-manager.md`](../../../docs/design/04-decision-layer/portfolio-manager.md) —
  the canonical anti-pattern strings and PM evaluation criteria the review surfaces
- [`docs/design/05-execution-layer/counterfactual-replay-engine.md`](../../../docs/design/05-execution-layer/counterfactual-replay-engine.md) —
  engine behind PM rejection accuracy and modification effectiveness
- [`feedback-validate`](../feedback-validate/SKILL.md) — sibling skill for evaluating a
  specific prompt edit's pre-registered impact
- [`feedback-retrospective`](../feedback-retrospective/SKILL.md) — sibling skill for
  open-ended quarterly pattern discovery
