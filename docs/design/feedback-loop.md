# Feedback loop

How AlphaMind gets better month over month. The feedback loop bridges the structured reasoning artifacts the system produces (analyst conviction labels, strategist status classifications, PM verdicts with criterion pass/fail and identified anti-patterns, synthesizer citations) to the outcomes those artifacts produced (thesis resolutions, realized P/L, position closures), so the operator can refine prompt and configuration quality over time.

The operator is the agent of all changes — the system does not self-tune, aligning with the uniformly-fail-closed posture elsewhere. The feedback loop's job is to make those decisions as well-informed as possible.

---

## Purpose and scope

**In scope:** performance tuning. Detecting where reasoning artifacts diverge from outcomes, surfacing what to tune, validating whether tunes worked.

**Out of scope:** operational health, alerting, live dashboards. Those belong to the [command center](command-center.md). The feedback loop assumes the system is technically functioning and asks whether it is *getting better*.

The unit of analysis is the **reasoning artifact paired with its outcome trail** — a conviction-4 label paired with the trade's resolution category, a `sunk_cost_persistence` tag paired with the held position's eventual realized P/L, a synthesizer citation chain paired with whether the cited findings ended up in validated thesis components. Every such pairing is a *prediction* the system made; every prediction is retrospectively scoreable.

---

## The three jobs

The loop supports the operator (working with Claude as a consultant) in three distinct cognitive tasks. Each gets different mechanisms and cadences.

| Job | What it answers | Primary mechanism |
|---|---|---|
| **Discovery** | Where are reasoning artifacts diverging from outcomes? | Deterministic analytics over the activity log + provenance fields, with a periodic LLM-driven retrospective for emergent patterns the deterministic layer can't surface |
| **Prescription** | What specific change should we consider? | Operator + Claude, working from Discovery evidence to a concrete prompt or configuration edit |
| **Validation** | Did the last change actually help? | Pre/post comparison on a pre-registered metric over a pre-defined window, with explicit uncertainty bands and confounder controls |

Most quant feedback loops only do Discovery; most LLM-app evals only do operational monitoring. The leverage and design difficulty sit in Prescription and Validation — where teams fool themselves with confirmation bias and where most "we tried X and it worked" claims fail to replicate.

---

## Architecture

### Deterministic analytics is the spine

A continuous analytics layer aggregates activity log, agent call records, thesis records, position records, and counterfactual replay records into queryable views. Feeds Discovery (the weekly digest, command center dashboards), makes Validation possible (reproducible pre/post metrics), and powers operator-driven ad-hoc investigation.

Most metrics in the inventory below derive directly from this layer's queries, with no additional infrastructure beyond the [provenance fields](05-execution-layer/state-persistence.md) and [counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) already specified.

### LLM retrospective is a periodic supplement

A periodic offline retrospective — calendar-anchored, not continuous — reads N invocations end-to-end and produces a structured report on patterns the deterministic layer can't surface. It's the mechanism for finding new things to instrument: when the retrospective consistently surfaces the same pattern across multiple sessions, that pattern becomes a candidate for promotion to a deterministic metric.

The retrospective runs interactively in a Claude Code session (operator and Claude review together), not as an unattended batch report. The act of reviewing IS the work; unattended reports tend to be skimmed.

### Why this split

Validation requires reproducible metrics. LLM retrospective output is qualitative and cross-session noisy — useful for Discovery, unusable for Validation. Deterministic analytics is always-on, cheap, and produces stable comparison surfaces; LLM retrospective is token-expensive and periodic, affording depth but not frequency.

---

## Cadence

Three tiers, mapped to what becomes observable in each window.

| Tier | Window | What's observable | What the operator does |
|---|---|---|---|
| Daily | per-invocation, per-day | Structural failures, distillation threshold firing rates, obvious rationale-quality regressions — binary or near-binary signal | Glance at the dashboard if a daily-tier alert fires; otherwise nothing |
| Weekly | week-over-week | Process metrics with small but meaningful samples — PM rejection patterns by criterion, anti-pattern raw counts, criterion pass/fail distributions, status transition counts | Read the weekly digest before the trading week opens; ad-hoc deep-dive if anything surfaces |
| Monthly / quarterly | month- or quarter-over-quarter | Outcome metrics with statistical power — conviction calibration, status calibration, anti-pattern detector accuracy, P/L attribution | Sit with Claude in a `/feedback-review` session for monthly; `/feedback-retrospective` for quarterly LLM-driven open-ended review |

Outcome-tier metrics need real resolved-thesis volume before they're meaningful — patience is structural. The operator may *want* to look at outcome metrics weekly; the metric should *show* a posterior band wide enough that no honest reading triggers a change. Time-dilation strategies (counterfactual replay) accelerate observability on the rejected/held path but do not manufacture forward outcomes for trades the system actually took.

---

## Process metrics vs. outcome metrics

A load-bearing distinction throughout the inventory.

**Process metrics (P)** — high frequency, low noise. Move enough week-over-week to support iteration on prompt-discipline tweaks. Examples: PM rejection rate per criterion, anti-pattern frequency, conviction distribution, status transition counts. The right surface for fast adjustment of discipline issues.

**Outcome metrics (O)** — low frequency, high noise. Need months of resolved-thesis volume before they support inference. Examples: conviction calibration, status calibration, realized P/L vs. expected. Anchor high-stakes signal-criteria revisions.

Discipline: weight process metrics for fast-iteration tweaks; weight outcome metrics for high-stakes structural changes. Display them differently so the operator doesn't unconsciously read short-window outcome noise as actionable signal. Goodhart awareness throughout — a metric prominently displayed becomes a target. Phrase metrics as diagnostics, not scorecards, and prefer Bayesian posterior bands over point estimates for everything outcome-tier.

---

## Metric inventory

Organized to mirror the system structure: one section per layer with per-agent metrics, then cross-cutting metrics, then **citation-chain metrics** that span layers.

For each metric: name, P/O type, what it measures, computation hint, meaningful window.

### Data layer

Deterministic data fetchers, no LLMs. Metrics here are about whether the inputs to downstream agents are high-quality.

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Source success rate per provider | P | Pulls completing within freshness SLA | Per-provider success ratio per invocation | Weekly |
| Failover frequency | P | How often we used a backup vendor | Count of failover-triggered invocations per category | Weekly |
| Calibration state distribution | P | Fraction of distillation thresholds in bootstrap mode | From per-invocation calibration state snapshot | Weekly |
| Data gap incidence | P | Invocations where a critical category was missing | Count of `staleness_flag = true` | Weekly |

### Distillation layer

Programmatic anomaly detection, regime classification, lead-lag computation. No LLMs. Metrics are signals for adjusting Class A thresholds.

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Anomaly firing rate per type | P | Are anomaly thresholds calibrated? | Per-type emissions vs. baseline expectation | Weekly |
| Regime classification stability | P | Are regime boundaries calibrated? | Number of regime changes per week | Weekly |
| Regime classification confidence | P | Indicator agreement at each classification | From distillation output | Daily |
| Lead-lag observation frequency | P | Per-pair, are we firing the right number of flags? | Per-pair counts vs. expected | Monthly |
| Anomaly downstream citation rate | O | Are anomalies actually informing decisions? | Anomalies cited by analysis-layer agents / total emitted | Monthly |
| Distillation trigger × outcome correlation | O | When threshold X fires, do resulting positions outperform? | Cross-tab firing × position outcome | Monthly |

Class B (rolling baselines) self-tunes; Class A (operator-set thresholds) needs feedback. Most metrics above target Class A threshold tuning.

### Analysis layer

Three sector domain researchers (tech-semis, financials, energy), portfolio analyst, qualitative researcher, adaptive researcher, synthesizer. All LLMs producing prose with structured reference IDs.

#### Per-agent metrics — applies to each domain researcher, portfolio analyst, qualitative, adaptive

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Findings per invocation | P | Output volume | Distinct reference IDs emitted | Weekly |
| Coverage breadth | P | Are all in-scope tickers/topics getting attention? | Distinct tickers cited / universe size (domain); topic coverage (qualitative) | Weekly |
| Citation rate by synthesizer | O | Is this agent's output making it into the synthesis? | Refs cited by synthesizer / total refs emitted | Monthly |
| Citation rate by decision layer | O | Reaching decision agents directly? | Refs retrieved by analyst/strategist/PM / total refs emitted | Monthly |
| Signal validation rate | O | When this agent's findings feed theses, do those theses validate? | Per-component validation rate, conditioned on cited source brief | Quarterly |
| Anomaly catch rate | O | Did this agent address anomalies the distillation layer flagged in its sector? | Anomalies addressed / anomalies in sector | Monthly |

**Cross-agent comparison within layer:** the same metrics side-by-side per researcher — particularly signal validation rate compared across the three sector researchers, and finding density per token (output volume / context tokens consumed). Detects "researcher X is producing low-signal content compared to peers."

#### Synthesizer-specific metrics

The synthesizer has no structured output, just prose with embedded references. Quality is measurable through the reference-ID structure:

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Upstream citation coverage | P | What fraction of upstream findings got cited? | Distinct upstream refs cited / total upstream refs available | Weekly |
| Synthesizer recall | O | When the synthesizer drops a finding, does it later turn out to matter? | Refs not cited by synthesizer but retrieved directly by decision agents / total uncited refs | Monthly |
| Contradiction surfacing rate | P | Is it flagging contradictions in upstream briefs? | Manual or LLM-judged classification of synthesis paragraphs | Monthly |
| Brief length vs. upstream length | P | Compression ratio | Synthesis tokens / sum of upstream tokens | Weekly |

Synthesizer recall measures "is the synthesizer discarding key details?" — high recall failure means signal is being left on the table that decision agents have to retrieve themselves.

#### Adaptive researcher specific

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Tool budget utilization | P | Spending its budget? | Calls used / 25-call budget | Weekly |
| Per-tool value | O | Which tools yield findings cited downstream? | Tool call counts × downstream citation rate | Monthly |
| Investigation depth × citation rate | O | Does deeper investigation produce more useful output? | Calls per finding × downstream citation rate | Monthly |

### Decision layer

Analyst, strategist, PM, proposal pre-processor.

#### Analyst

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Proposals per invocation | P | Output volume; inclusion threshold calibration | Recommendation count per invocation | Weekly |
| Conviction distribution | P | Use of conviction scale | Histogram per week | Weekly |
| Inaction rate | P | How often correctly produces zero proposals | Zero-proposal invocations / total | Weekly |
| PM approval rate | P | Fraction PM approves (any verdict) | (approve + approve_with_modification) / total | Weekly |
| PM modification rate | P | Modified rather than approved as-is | approve_with_modification / total | Weekly |
| Per-criterion fail rate | P | Which PM criteria the analyst trips most | From `pm_decision.evaluation` | Weekly |
| Anti-pattern frequency by name | P | Which behavioral failure modes are firing | Counts per anti-pattern in `anti_patterns_identified` | Weekly |
| Conviction calibration | O | Do higher convictions actually outperform? | Per conviction level: thesis resolution distribution | Quarterly |
| Validation rate by source brief | O | Which source briefs feed validated theses? | Cited brief × thesis resolution outcome cross-tab | Quarterly |

#### Strategist

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Status distribution | P | How statuses are distributed | Histogram per week | Weekly |
| Action distribution | P | Which actions strategist recommends | Histogram (hold/reduce/close/adjust/add) per week | Weekly |
| Hold-on-non-on-track rate | P | How often strategist holds at-risk or stale | Counts per status | Weekly |
| Status transition matrix | P | How often each transition happens | From `prior_status` → `status` pairs | Weekly |
| PM approval rate | P | Fraction PM approves | Same shape as analyst | Weekly |
| Anti-pattern frequency by name | P | Same | Same | Weekly |
| Status calibration | O | Do statuses have distinguishable forward outcomes? | Per status: forward N-day P/L distribution | Quarterly |
| Hold rationale validation rate | O | When strategist holds, does it work out? | Held vs. closed position outcome distributions | Quarterly |
| Action effectiveness | O | Did the recommended action improve the outcome? | Compare actuals to harness counterfactuals | Quarterly |

#### Portfolio manager

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Verdict distribution | P | Approves vs. modifies vs. rejects | Histogram per source agent per week | Weekly |
| Modification category distribution | P | Why PM modifies | Histogram per `adjustment_category` | Weekly |
| Anti-pattern detection frequency | P | Which patterns PM flags most | Counts per pattern per week | Weekly |
| Per-criterion fail rate | P | Which evaluation criteria PM fails proposals on | Per criterion per source agent | Weekly |
| PM rejection accuracy | O | When PM rejects, would the trade have been profitable? | Counterfactual P/L distribution from [counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) | Quarterly |
| Modification effectiveness | O | Do PM-modified trades outperform PM-unmodified? | Compare actual modified-form outcomes to counterfactual original-form outcomes | Quarterly |
| Anti-pattern detector accuracy | O | When PM tags X, does the position resolve consistent with X being the issue? | Per pattern: forward outcomes of tagged positions | Quarterly |
| Sizing modification effectiveness | O | When PM sizes down, do trades outperform? | Modified-sizing vs. as-proposed at conviction band | Quarterly |

#### Proposal pre-processor

Deterministic but worth tracking:

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Conflict detection accuracy | P | Are flagged conflicts real? | Manual sample audit | Quarterly |
| Combined-set computation freshness | P | Projections matching engine's actual cumulative tracking? | Diff at submission time | Daily |

### Execution layer

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Slippage estimate accuracy | O | Paper harness vs. realized live execution drag | Compare `live_execution_estimate` to actual fills (post go-live) | Monthly |
| Fill quality | O | Did we get our intended price? | Slippage distribution per order type | Weekly |
| Bracket trigger accuracy | O | Did stops fire at the intended price? | Bracket leg trigger price vs. actual fill | Monthly |
| Command abandonment rate | P | How often do submissions fail? | `command_abandoned` events / total commands | Weekly |
| Engine-originated CLOSE frequency | P | How often does the monitor have to intervene? | Count per week | Weekly |
| Guardrail rejection rate | P | How often does the engine reject a PM command? | Count per week (should be near zero by design) | Weekly |

### Cross-cutting metrics

System-wide signals that span layers.

#### Outcome surface

| Metric | What it measures | Window |
|---|---|---|
| Daily P/L | Bottom line | Daily visible / weekly meaningful |
| Win rate | Fraction of closed positions profitable | Monthly |
| Profit factor | Sum of wins / sum of losses | Monthly |
| Drawdown | Current and max | Monthly |
| Thesis resolution distribution | Validated / profitable-but-wrong / invalidated-stopped / invalidated-wrong-on-exit | Monthly |
| P/L per resolved thesis | Average P/L per resolved position | Monthly |
| P/L per token spent | Cost efficiency | Monthly |

#### Conditioning surface

The same outcome metrics, sliced by:

- **Regime** — outcomes per regime (low-vol / normal / elevated / crisis). The single most important confounder.
- **Sector** — outcomes by sector
- **Conviction level** — outcomes per analyst conviction
- **Strategist status at last classification** — outcomes per status
- **Anti-pattern tag** — outcomes per tagged pattern (becomes pattern accuracy)
- **Time-of-day** — outcomes per invocation type (pre-open vs. intraday vs. pre-close vs. off-hours)
- **Active prompt versions** — outcomes per prompt version (the pre/post comparison surface for Validation)
- **Active model version** — outcomes per Claude model version (model-update confounder)

These are queries over existing data, conditioned on the [invocation provenance fields](05-execution-layer/state-persistence.md).

#### Cost surface

| Metric | P/O | What it measures | Computation | Window |
|---|---|---|---|---|
| Token cost per invocation | P | Aggregate spend | Sum across `agent_calls` | Weekly |
| Token cost per agent | P | Where the budget goes | `agent_calls` grouped by agent name | Weekly |
| Latency budget headroom | P | Wall-clock vs. budget | `wall_clock_ms` vs. `agents.yaml` budget | Weekly |
| Cache hit rate per agent | P | Prompt caching effectiveness | `cache_read_tokens / (cache_read + input)` | Weekly |
| Failure overhead | P | Tokens spent on retries | Sum of `attempt_number > 1` tokens / total | Weekly |

---

## Citation-chain metrics — the cross-layer flagship

Citation-chain metrics span layers and answer cross-layer questions the per-layer metrics cannot. Fully computable from the existing reference ID taxonomy and persisted agent outputs.

The reference ID hierarchy:

| Source | Prefix |
|---|---|
| Tech & semis researcher | `SA-TECH-N` |
| Financials researcher | `SA-FIN-N` |
| Energy researcher | `SA-ENERGY-N` |
| Qualitative researcher | `QR-N` |
| Adaptive researcher | `AR-N` |
| Correlation/regime brief | `CR-N` |
| Analyst recommendation | `REC-N` |
| Strategist assessment | `SA-N`, `SA-ORD-N` |
| PM envelope | `ENV-*` |

Citation chain analysis traces the flow:

```
upstream finding (SA-TECH-N, QR-N, AR-N, CR-N, etc.)
  → cited in synthesizer brief? (yes/no)
    → cited in analyst recommendation (REC-N) or strategist assessment (SA-N)? (yes/no)
      → became a thesis component?
        → resolved as validated/wrong/inconclusive?
```

Each arrow is a measurement point.

| Metric | Computation | What it tells us |
|---|---|---|
| Synthesizer citation rate per source | Refs from source X cited in synthesis / total refs from X | Which upstream agents the synthesizer values |
| Synthesizer recall | Refs not cited in synthesis but retrieved by decision agents directly / total uncited refs | What the synthesizer is wrongly discarding |
| Decision-layer citation rate per source | Refs from X cited in decision-layer narratives / total refs from X | Which sources actually feed decisions |
| Signal survival rate per source | Refs from X that ended in a validated thesis component / total refs from X | Which sources produce predictive signals |
| Per-source validation rate | Among theses citing X: validated count / total | Whether a source's signals tend to be right |

**This surfaces low-signal upstream agents:** if one sector researcher's signal survival rate is materially lower than the others over a month, that researcher is the candidate for prompt revision. Combine with synthesizer citation rate per source — if the synthesizer is also down-citing that researcher, it's correctly detecting low signal. If citing at parity but downstream validation is low, the synthesizer is being misled by a noisy upstream.

**This surfaces synthesizer drop errors:** synthesizer recall counts dropped findings that decision agents had to retrieve themselves.

These metrics require parsing reference IDs across persisted agent outputs (the `agent_calls.output_artifact_ref` files) and joining to thesis component records — a small reference-ID parsing library is the right unit of work.

---

## Counterfactual replay

PM rejections and modifications produce no observable forward outcomes by default — the trades were never taken. Without observability on these decisions, PM evaluation quality is unmeasurable.

The [counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) closes the gap by deterministically simulating PM-rejected and PM-modified-away proposals against historical underlying price data, producing a hypothetical realized P/L per replay. Records land in the `counterfactual_replays` entity (per [state-persistence.md](05-execution-layer/state-persistence.md)) and join to the originating PM envelope.

Unlocks: PM rejection accuracy, modification effectiveness, anti-pattern detector accuracy, sizing modification effectiveness.

The engine is daily batch (with on-demand override via the command center or `/feedback-review` skill). Equity and single-leg option proposals are both replayed (option proposals via underlying-bar trigger evaluation with Black-Scholes pricing at entry and exit only — see [counterfactual-replay-engine.md § Option proposals](05-execution-layer/counterfactual-replay-engine.md#option-proposals)); multi-leg strategies are recorded as unevaluable. Low-confidence replays are persisted but excluded from aggregated metrics at query time.

---

## Confounder management

Biggest confounders that corrupt feedback-loop inference:

- **Market / volatility regime shifts** — calibration appears to drift but regime moved
- **Anthropic model updates** — Claude can drift in behavior between releases without our prompts changing
- **Other concurrent prompt edits** — if two prompts change in the same window, neither change is attributable
- **Data source quality changes** — Polygon shape change, source brief outage
- **Random variance** — most short-window movements

Structural support in place (per [state-persistence.md](05-execution-layer/state-persistence.md) provenance fields):

- Active regime, profile, mode, overlays, and resolved config hash recorded per invocation
- Active model ID recorded per agent call
- Active prompt path, git SHA, and content hash recorded per agent call
- Active SDK versions and pip-freeze hash recorded per process lifetime
- Data source freshness and calibration state snapshot recorded per invocation

Discipline using that structure:

- **Track the relevant context alongside every metric.** Aggregations are conditioned on regime, prompt version, and model version by default — never displayed as unconditional aggregates that smear across confounders.
- **One-change-at-a-time.** At most one prompt edit per evaluation window. If multiple edits are needed, they ship serially with one window's wait between, not as a batch.
- **Pre-registration.** When a change is made, the operator and Claude write down the expected direction and rough magnitude of impact *before* observing post-change data. Defeats post-hoc rationalization.
- **Hold-out periods.** When in doubt, wait another window.
- **Backtest as sanity check** for regime-sensitive changes — if a change holds across multiple historical regimes, that's stronger evidence than forward observation alone. The [distillation replay harness](02-distillation-layer/replay-harness.md) re-runs the deterministic layer against archived inputs under a candidate `config/distillation.yaml` and emits a per-regime flag-rate report consumed at the `/feedback-validate` REGISTER step.

---

## Skills

Three skills are the primary interface for operator-and-Claude interactive review:

| Skill | Purpose | Reads | Outputs |
|---|---|---|---|
| `/feedback-review` | Main weekly or monthly review session — operator and Claude review the period together with retrieval over the activity log, agent outputs, counterfactual replays, and thesis records | Window-bounded slice of activity log, agent_calls, thesis records, counterfactual replays, conditioning provenance | Interactive findings surfaced on the dashboard; potentially a saved review note |
| `/feedback-validate` | Pre/post comparison for a specific prompt edit. Forces pre-registered expected impact and active regime/model context into the analysis so it is structurally impossible to unconsciously rationalize | Pre-edit window and post-edit window data, conditioned on the edited prompt's version field | Validation verdict (improved / no change / degraded) with posterior bands; saved record of the validation attempt |
| `/feedback-retrospective` | Quarterly open-ended LLM-driven deep review. Reads a large window end-to-end, surfaces patterns the deterministic metrics didn't catch, proposes candidates for promotion to deterministic metrics | Quarterly window of all relevant data | Structured retrospective report; promotion candidates for new metrics |

### Dashboard as shared canvas

All three skills run against the [command center's review-session surface](command-center.md#review-sessions). The integration model mirrors the Claude Code IDE pattern: Claude has *passive awareness* of what the operator is currently viewing and selecting; each prompt turn, the skill calls `GET /review-sessions/{id}/state` to inject the operator's current dashboard context (current view, what they have highlighted or selected, recent navigation history) into Claude's context. Claude reasons about that state plus the operator's message plus the underlying analytics, then issues `POST /review-sessions/{id}/control` calls to highlight metrics, navigate the dashboard, annotate chart points, or pin items for comparison. The operator continues using the dashboard normally and responds verbally in the chat.

Asymmetric by design: Claude writes via tool calls and reads via tool calls; the operator drives the dashboard themselves. Dashboard-as-shared-canvas — both parties act on the same surface; neither's actions automatically trigger the other.

The v1 affordance vocabulary, session lifecycle endpoints, and session state shape are in [command-center.md § Review sessions](command-center.md#review-sessions).

---

## Dashboard and digest curation

The [command center's Quality and feedback view group](command-center.md) is the rendering surface, serving both self-review (operator browses alone) and session mode (operator + Claude reviewing together via the [Skills](#skills)).

### Curation heuristic

The dashboard surfaces anything the operator might review on their own outside a Claude session, plus visual history of key metrics so trajectory is gaugeable at a glance. The full metric inventory is the universe; the dashboard renders a curated subset, with everything else accessible via ad-hoc query.

### Surface composition

A **weekly digest** — single scrollable view designed for ~5-minute consumption Sunday morning before the trading week opens. Live by default (always reflects current data when opened) and snapshotted weekly to a `weekly_digest_snapshots` table on a fixed schedule (8am ET Sunday) so historical week-to-week comparison is queryable. Generated deterministically — no LLM tokens. Thresholds for notable-shift flags are operator-tunable in `config/digest.yaml`.

#### Section 1 — Headline outcomes

Four large card values (no trends):

- **P/L last 7 days** — sum of realized P/L from positions closed in the trailing 7 days; in paper mode, rendered both raw and live-adjusted
- **Win rate last 7 days** — fraction of positions closed at profit in the trailing 7 days
- **Current drawdown** — from the portfolio summary entity
- **Trades closed last 7 days** — count

#### Section 2 — Process pulse

Six metrics, each with a week-over-week delta:

- **PM rejection rate** — count(reject verdicts this week) / count(all envelopes this week)
- **PM modification rate** — count(approve_with_modification) / count(all envelopes this week)
- **Analyst inaction rate** — fraction of analyst invocations that produced zero proposals this week
- **Strategist hold-on-non-on-track rate** — count(hold actions on at-risk or stale positions) / count(at-risk + stale assessments) this week
- **Anti-pattern occurrences** — counts per canonical anti-pattern name (`sunk_cost_persistence`, `rationalized_continuation`, `thesis_contradiction_suppression`, `engine_originated_closure_signal`, `conviction_inflation`), shown together as a small bar group
- **Guardrail rejection count** — count of `guardrail_rejection` activity log events this week (expected near-zero by design — any non-zero week is itself the signal)

#### Section 3 — Trajectory

Six sparklines over the trailing 8–12 weeks. Sparklines show direction; current absolute value is displayed alongside.

- **Weekly P/L** — line, raw and live-adjusted overlay in paper mode
- **Win rate** — line with 80% credible band; "needs N more observations" annotation when weekly trade count is below the configured sample threshold
- **Cost per resolved thesis** — line, in dollars
- **Conviction calibration spread** — line with 80% credible band; spread = win-rate(conviction ≥4) − win-rate(conviction ≤2)
- **Status calibration spread** — line with 80% credible band; spread = forward-5-day adverse-outcome rate(at-risk classifications) − same(on-track classifications)
- **PM rejection accuracy** — line with 80% credible band; sourced from `counterfactual_replays` filtered to evaluated + medium/high confidence

Outcome-tier sparklines (the four with credible bands) carry insufficient-sample annotations until the underlying resolved-thesis counts cross threshold.

#### Section 4 — Validation status

One row per active validation (registered via `/feedback-validate`, no joining outcome record yet):

- Edited artifact + pre-registered expected direction
- Watched metric current reading with 80% credible band
- Days-of-data-so-far / total window length
- Days remaining until evaluation due
- Quick-jump link to the validation evaluation view

Empty state: "No active validations."

#### Section 5 — Notable shifts

Auto-flagged items, each row links to the relevant deeper view. Default thresholds (operator-tunable in [`config/digest.yaml`](configuration-management.md#digestyaml)):

- **Anti-pattern spike** (`anti_pattern_spike`) — fires when any canonical pattern's weekly count exceeds `multiplier_vs_baseline` × the prior `baseline_window_weeks` average AND `min_occurrences_this_week` floor met
- **Regime change** (`regime_change`) — fires on any classification change from the prior week
- **Sector underperforming** (`sector_underperform`) — fires when a sector's rolling P/L over `baseline_window_weeks` falls below (median of other sectors − `median_offset_sigma` × σ)
- **Citation chain shift** (`citation_chain_shift`) — fires when synthesizer citation rate per source changes by more than `delta_pp_threshold` percentage points from the prior `baseline_window_weeks` average
- **Source signal survival drop** (`source_signal_survival_drop`) — fires when a per-source signal survival rate drops by more than `delta_pp_threshold` percentage points from the prior `baseline_window_weeks` average
- **Validation reaching window end** (`validation_window_end`) — fires when an active validation is within `days_before_due` days of evaluation-due, or overdue
- **Validation superseded** (`validation_superseded`) — fires when an active validation was auto-marked superseded during the week (regime transition, model version change, or concurrent edit on the watched artifact); row carries the supersession reason and a re-register link to `/feedback-validate` REGISTER seeded with the prior registration's fields

Empty state: "Nothing crossed a notable-shift threshold this week."

#### Section 6 — Open validation queue

Counterfactual replays produced this week, broken out:

- Total replays attempted
- By `replay_status`: evaluated / unevaluable
- For unevaluable: by `unevaluable_reason` (unsupported_instrument / unsupported_bracket_type / data_missing / corporate_action_in_window)
- For evaluated: by `confidence` (high / medium / low)

Renders as a small stacked bar plus per-bucket counts. The operator scans this for a high `data_missing` rate (data layer issue) or `low` confidence rate (data quality issue).

---

Roughly 25 deterministic numbers across six sections plus variable rows in Sections 4 and 5. Process metrics dominate; outcome metrics in Section 3 carry explicit credible bands and sample-threshold annotations. Pull-only.

A **monthly view** — outcome-tier trajectory curves with a global conditioning control bar (regime, sector, conviction band, prompt version, model version, period), calibration-first layout, citation-chain and anti-pattern accuracy panels below. Layout in [command-center.md § Monthly view](command-center.md#monthly-view); panels render before resolved-thesis volume crosses the configured minimum, with credible bands faded and a sample-size banner overlaying the panel until the read becomes meaningful.

An **ad-hoc query surface** — for everything not on the curated views. A hybrid form-to-SQL surface keyed to an entity catalog (agent calls, activity log, PM envelopes, theses, counterfactual replays, invocations, validations, retrospective decisions); the form composes to canonical SQL the operator can drop into and edit, saved views persist as SQL strings, results render as a typed table with row-expansion to the underlying record, and CSV export is one click. Read-only by construction. Specified in [command-center.md § Ad-hoc query surface](command-center.md#ad-hoc-query-surface).

### Session mode overlay

When a [Skill](#skills) opens a [review session](command-center.md#review-sessions), the same dashboard pages — including the weekly digest — gain highlighting / annotation / view-control affordances. Claude highlights a metric Claude wants the operator to look at; the operator highlights one they want Claude to discuss; both parties read the same surface.

Session-mode affordances are dimmed when no session is active so self-review stays uncluttered. The digest is a natural starting page for `/feedback-review` — Claude uses it as entry point and drills into specific items via `navigate_to_view`.

---

## Validation methodology

Validation is operationalized through the [`/feedback-validate` skill](#skills): pre-registration of expected impact captured at change time as a contract, criteria frozen at registration, posterior bands rather than point estimates, confounder conditioning mandatory, "inconclusive" as a first-class verdict, one change per validation window. The skill's REGISTER and EVALUATE flows enforce these; the [validation evaluation view](command-center.md#validation-evaluation-view) is the dashboard surface EVALUATE walks through.

### Mid-window supersession

A registered validation is a contract over a specific conditioning context. When that context shifts materially during the post-edit window, the contract is structurally broken — the post-edit data the operator agreed to evaluate is no longer comparable to the pre-edit baseline. The system auto-marks such validations `superseded`; the operator either re-registers with a fresh post-shift window or accepts the supersession as the resolution.

Triggers — any of these landing during the post-edit window:

- **Regime transition.** The active regime label at any invocation during the post-edit window differs from the regime at registration, sourced from the distillation layer's per-invocation regime classification (the same field surfaced in the [conditioning surface](#conditioning-surface)).
- **Anthropic model version change.** The active model ID on any agent call during the post-edit window differs from the model ID at registration, sourced from the `agent_calls` provenance fields per [state-persistence.md](05-execution-layer/state-persistence.md).
- **Concurrent prompt edit on the watched artifact.** A new commit to the registered artifact's path (the validation's `edited_artifact`) lands in the artifact's git ancestry between registration and evaluation due. Detected by walking the artifact's git log over the post-edit window.

When a trigger fires, the validation record's `superseded_at` and `superseded_reason` fields are written; the supersession surfaces as a [notable-shift row](#section-5--notable-shifts) in the next weekly digest so the operator sees it without polling the validation queue. Supersession is permanent for that validation; re-registration is a new validation entity.

Confounders detected at evaluation time that didn't trigger automatic supersession during the window remain `inconclusive` material per the EVALUATE walk's confounder-conditioning step. Supersession handles structural breaks; `inconclusive` handles residual noise the structural test didn't catch — layered defense.

The supersession detector runs against active validations on each pipeline invocation (the natural cadence at which new conditioning context lands) and on each commit to a watched artifact's path. Producer is the command-center backend, which already owns the `validations` table per [command-center.md § Persistence boundary](command-center.md#persistence-boundary).

### Rollback evidence protocol

EVALUATE produces a verdict; the verdict shape determines what evidence is sufficient to roll back the change. Layered defense — structural threshold first, confounder check second, retrospective context for everything ambiguous — produces a `rollback_status` value carried on the validation outcome record per [state-persistence.md § Validation outcomes](05-execution-layer/state-persistence.md). Superseded validations do not reach EVALUATE and produce no outcome, so the protocol applies only to validations whose post-edit window completed in its registered conditioning context.

| `rollback_status` | Triggers when | Operator action |
|---|---|---|
| `mandatory_clean_failure` | Verdict is `degraded`, the post-edit window crossed the pre-registered failure criterion, and no confounder from [Confounder management](#confounder-management) (regime distribution mismatch, model version straddle, concurrent edits in window) was flagged on the outcome | Roll back the edit; the same EVALUATE session pre-fills a paired post-rollback validation through the existing REGISTER flow |
| `optional_pending_retrospective` | Verdict is `degraded` with a confounder flagged, OR `no_change` when the registration's expected direction was `improved`, OR `inconclusive` when the most recent prior outcome on the same `edited_artifact` was also `inconclusive` | Rollback decision deferred to the next [`/feedback-retrospective`](../../.claude/skills/feedback-retrospective/SKILL.md), where the entry surfaces in the report's Suggested follow-ups section and the operator's accept/reject decision lands as a `decision_type: follow_up` in the [retrospective decisions ledger](05-execution-layer/state-persistence.md) |
| `not_applicable` | Any verdict shape not covered by the rows above | None |

**Mandatory clean failure → paired post-rollback validation.** The pre-registered failure criterion crossing on a clean window is the operator's own contract firing. The skill pre-fills a paired registration in the same EVALUATE session: same `edited_artifact`, same `watched_metric_ids`, same `window_length_days`, `expected_direction = improved` (restore-to-pre-edit-baseline), success criterion auto-derived as "metric returns to within posterior band of the pre-failed-edit baseline." Operator confirms or adjusts before commit. A revert is itself a change to the artifact and goes through the same validation discipline as the edit it reverts.

**Optional pending retrospective.** Single-window verdicts with confounders, or with shapes that fall short of the strictest threshold, carry no rollback obligation but accumulate in the [retrospective view](command-center.md#retrospective-view)'s decision rail. The retrospective is the cadence at which patterns become legible; rollback decisions in this band deserve that evidence base. Two consecutive `inconclusive` outcomes on the same artifact also land here — once the second window failed to distinguish from noise, the question is whether the artifact's current shape is worth keeping at all, which is a retrospective question.

The protocol is calibrated, not pessimistic: structural rollback only fires on the operator's own pre-registered criterion, the system never rolls back unilaterally, and the retrospective path absorbs the noise that single-window verdicts cannot resolve.

---

## Pending

Items waiting on operating the system.

### Waiting on operating the system

- **Skill prompt iteration.** The three skill drafts cover orchestration logic and discipline; behavioral specifics refine once the skills are evaluable against real sessions. Normal skill iteration, not a design gap.

---

## Dependencies

- [State persistence](05-execution-layer/state-persistence.md) — defines the activity log, agent calls, process lifetimes, invocation provenance, and counterfactual replays entities that supply substantially all feedback-loop data
- [Counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) — populates the counterfactual replays entity, unlocking PM accuracy and modification effectiveness metrics
- [Configuration management](configuration-management.md) — defines the resolved-config snapshot and prompt versioning surface that Validation depends on
- [Portfolio manager](04-decision-layer/portfolio-manager.md) — defines the PM envelope structure (verdicts, criterion pass/fail, modifications, anti_patterns_identified) that supplies most decision-layer process metrics
- [Analyst](04-decision-layer/analyst.md), [Strategist](04-decision-layer/strategist.md) — define the structured outputs whose calibration the loop measures
- [Synthesizer](03-analysis-layer/synthesizer.md) — defines the citation discipline that powers citation-chain metrics
- [Threshold calibration](02-distillation-layer/threshold-calibration.md) — defines the Class A thresholds that distillation-layer metrics target for tuning
- [Command center](command-center.md) — renders the dashboard and digest surface; hosts the on-demand triggers for the feedback-review and counterfactual-replay engines
- [Cost and rate-limit modeling](cost-and-rate-limit-modeling.md) — bounds how aggressively the LLM retrospective can be run
