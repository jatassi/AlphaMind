# Cost and rate-limit modeling

How AlphaMind's LLM workload fits inside a single $100/month Claude Max 5x subscription consumed via the Claude Agent SDK on the operator's trading machine. Establishes per-invocation token budget, weekly throughput envelope, per-invocation latency budget, and operating posture when usage approaches caps.

Pre-paper-trading: numbers are best estimates from agent specs and Anthropic's published behavior; [Calibration discipline](#calibration-discipline) names the instrumentation that refines them once the system runs.

---

## Scope

**In scope.**
- Subscription envelope (Max 5x plan parameters, usage windows, at-cap behavior, authentication posture).
- Per-invocation LLM workload (agents involved, token volume, tool-use loop overhead, failure-path retry overhead).
- Headroom analysis converting workload into utilization against the subscription envelope, surfacing the binding constraint.
- Latency budget per invocation against scheduler cadence and emergency-invocation latency target.
- Operating posture when workload approaches or hits a cap, including manual operator levers via the command center.
- Monitoring surface tracking utilization and approach-to-cap.

**Out of scope** (owned elsewhere):

| Concern | Authoritative spec |
|---|---|
| Per-agent token budget ranges | [06-risk-guardrails/state-delivery.md](06-risk-guardrails/state-delivery.md), per-agent docs in [04-decision-layer](04-decision-layer/) and [03-analysis-layer](03-analysis-layer/) |
| Agent inventory, model assignments, orchestration pattern | [architecture/llm-integration.md](../architecture/llm-integration.md) |
| Pipeline schedule and invocation cadence | [architecture/infrastructure.md § Scheduling](../architecture/infrastructure.md#scheduling) |
| Failure-path semantics (retry, abort, cap-hit handling) | [llm-agent-failure-handling.md](llm-agent-failure-handling.md) |
| Adaptive-researcher tool-use bounds | [03-analysis-layer/adaptive-research.md](03-analysis-layer/adaptive-research.md) |
| Per-trigger agent roster and budget envelope | [configuration-management.md § run_types/](configuration-management.md#run_typestriggeryaml) |
| Continuous monitor scope, emergency invocation triggers | [05-execution-layer/architecture.md § Continuous monitor](05-execution-layer/architecture.md), [06-risk-guardrails/breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) |
| Operator-action surface (pause scheduler, switch profile, halt mode) | [command-center.md § Operator actions](command-center.md#operator-actions) |

---

## Subscription envelope

### Plan

A single **Claude Max 5x** subscription, $100/month. Owned and used exclusively by the operator on hardware the operator owns. No rerouting, no product offered to other users, no claude.ai login surfaced to anyone else.

This is the personal-use posture [Anthropic's Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance) accommodates with *"Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK"*. The third-party-developer prohibitions on the same page (*"to route requests through Free, Pro, or Max plan credentials on behalf of their users"*) do not apply to a single-operator setup.

### Authentication

OAuth token generated via `claude setup-token`, set in the pipeline process environment as `CLAUDE_CODE_OAUTH_TOKEN`. The Claude Agent SDK for Python picks this up automatically; no API key configured. The subscription does not fall back to pay-per-token API billing when caps are reached — at-cap behavior is a hard block until the window rolls.

### Models available

Max 5x provides full access to the current Claude lineup. The agent specs assign:

- **Opus** to the three decision-layer agents (analyst, strategist, portfolio manager), where high-stakes judgment justifies the slower, more capable model.
- **Sonnet** to the five analysis-layer agents (three sector analysts, qualitative researcher, adaptive researcher) and the synthesizer.
- **Haiku** is unused. Model assignments are configurable per-agent in `config/agents.yaml`; switching the analysis layer to Haiku is a manual lever under sustained cap pressure (see [Cap-approach response](#cap-approach-response)).

### Usage caps

Anthropic publishes the *structure* of Max plan caps but not the numeric thresholds. Three windows enforce simultaneously:

| Window | Structure | Max 5x range (community-measured, late 2026) |
|---|---|---|
| 5-hour rolling session | All models combined; rolls 5 hours after first message in the session | ~225 messages, very roughly |
| Weekly all-models | Resets 7 days after the first message of the week (rolling, not calendar) | Coarse umbrella; in practice the model-specific weekly caps bite first |
| Weekly Sonnet | Sonnet-only sub-cap | ~140–280 Sonnet-hours |
| Weekly Opus | Opus-only sub-cap | ~15–35 Opus-hours |

"Opus-hours" / "Sonnet-hours" is calibrated by Anthropic against an "ordinary" user issuing roughly one message every five minutes (≈12 messages per hour of plan-time). A token-heavier or tool-heavier message consumes cap faster. The numeric ranges in the table are community measurements; treat them as order-of-magnitude, not contract.

Two consequences for AlphaMind:

1. **The weekly Opus cap is the binding constraint.** The decision-layer trio runs every invocation; the analysis layer runs on Sonnet which has roughly 10× the weekly headroom.
2. **Token-heavy calls (PM, synthesizer) consume cap faster than the 12-msg/hour calibration suggests.** Headroom estimates need an empirical deflation factor measured during paper trading.

### At-cap behavior

When any cap is exceeded, the SDK receives an HTTP error. The pipeline process treats this as the [llm-agent-failure-handling.md § Model API error](llm-agent-failure-handling.md) specifies: three exponential-backoff retries, then abort. The continuous monitor is unaffected (it does not invoke LLMs) and retains its protective authority over open positions.

No automatic model downgrade and no fallback to API billing. Operator awareness via the command-center alert surface; manual response options under [Cap-hit response](#cap-hit-response).

### Residual technical-enforcement risk

Anthropic deployed a server-side classifier in early 2026 rejecting OAuth tokens used in patterns associated with reseller or aggregator workloads. Personal-use SDK consumption from a single machine is not the target, but heuristics aren't perfect. If the classifier rejects an AlphaMind request, it surfaces as a credential error, which the pipeline treats as a model-API-error abort. Mitigation: the API-key escape hatch (see [API-key escape hatch](#api-key-escape-hatch)).

---

## Workload model

### Invocation cadence

Per [architecture/infrastructure.md § Scheduling](../architecture/infrastructure.md#scheduling):

Under the Tier B schedule (ALP-745), every trigger fires at a distinct minute so no two scheduled runs share a minute-truncated `as_of`:

| Trigger | Times (US/Eastern) | Per week |
|---|---|---|
| Pre-open anchored | 09:00 Mon–Fri | 5 |
| Market-hours rolling | 13:00 Mon–Fri (single mid-day read) | 5 |
| Pre-close anchored | 15:30 Mon–Fri | 5 |
| Weekend anchored | Sun 18:00 | 1 |

Net scheduled invocations per week: **~16 normal market week** (~3 per trading weekday plus the Sunday run). `off_hours_rolling` and `weekend_saturday` remain valid run types for manual / emergency use but are no longer scheduled — real-time risk on open positions is owned by the continuous monitor, not by an intraday pipeline cadence.

Emergency invocations triggered by the continuous monitor add a variable tail: **0–2 in a quiet week, 4–8 in a stress week, occasionally more in a crisis week**. Each runs the same agent surface as a scheduled invocation, with the analyst in `watchlist` mode and the strategist in `defensive_posture` mode, both producing smaller outputs than normal mode.

### Per-invocation agent surface

Per [architecture/llm-integration.md § Agent inventory](../architecture/llm-integration.md#agent-inventory), the agent inventory is nine LLM agents in the same orchestration shape, filtered per firing trigger by the [`run_types/` overlay](configuration-management.md#run_typestriggeryaml):

| Agent | Layer | Model | Parallelism |
|---|---|---|---|
| Tech/semis analyst | Analysis | Sonnet | Parallel group |
| Financials analyst | Analysis | Sonnet | Parallel group |
| Energy analyst | Analysis | Sonnet | Parallel group |
| Qualitative researcher | Analysis | Sonnet | Parallel group |
| Adaptive researcher | Analysis | Sonnet | Sequential after parallel group; omitted on off-hours rolling and weekend-Saturday triggers |
| Synthesizer | Analysis | Sonnet | Sequential after adaptive |
| Analyst | Decision | Opus | Parallel pair |
| Strategist | Decision | Opus | Parallel pair |
| Portfolio manager | Decision | Opus | Sequential after parallel pair |

The proposal pre-processor is deterministic. The continuous monitor process does not invoke LLMs.

### Per-invocation token aggregate

Token volumes per agent are documented at the agent-spec level. Aggregated for the cost model, using midpoints of declared ranges:

**Sonnet calls per invocation, primary profile ($1,500), normal day:**

| Agent | Input | Output |
|---|---|---|
| Three sector analysts | ~9,000 | ~1,800 |
| Qualitative researcher (incl. tool returns) | ~7,500 | ~500 |
| Adaptive researcher (incl. tool returns) | ~2,500 | ~600 |
| Synthesizer | ~10,000 | ~1,500 |
| **Sonnet subtotal** | **~29,000** | **~4,400** |

**Opus calls per invocation, primary profile, normal day:**

| Agent | Input | Output |
|---|---|---|
| Analyst | ~3,000 | ~1,000 |
| Strategist | ~2,000 | ~1,500 |
| Portfolio manager | ~6,000 | ~3,000 |
| **Opus subtotal** | **~11,000** | **~5,500** |

**Per-invocation total: ~49,900 tokens (~33K Sonnet, ~16.5K Opus).** Under Tier B every scheduled trigger (pre-open, market-hours rolling, pre-close, weekend-Sunday) carries the adaptive researcher, so the full ~49,900-token surface applies to all ~16 scheduled invocations per normal week. (The off-hours-rolling and weekend-Saturday overlays still omit the adaptive researcher and drop ~3,100 Sonnet tokens, but those triggers are unscheduled under Tier B and contribute only on manual / emergency use.) The weekly Opus envelope — the binding constraint — is unaffected by run-type composition since the decision-layer trio fires on every trigger; the move from ~28 to ~16 scheduled invocations per week lowers total weekly token consumption proportionally.

Full-system profile ($100K, 6–15 positions, options/shorts enabled) increases strategist and PM input/output proportional to position count and adds the options/shorts sections to the analyst's guardrail header. Per-invocation total scales to **~65,000–80,000 tokens** in normal operation, with the increase concentrated in Opus.

Volatile / catalyst day adds adaptive-researcher tool-budget consumption (up to the 25-call / 4,000-token cumulative budget per [adaptive-research.md](03-analysis-layer/adaptive-research.md)) and may add an emergency invocation. Per-invocation token total in such cases pushes toward **~75K–90K** for primary profile, **~95K–110K** for full-system.

### Failure-path overhead

Per [llm-agent-failure-handling.md](llm-agent-failure-handling.md):

| Failure mode | Retry shape | Approximate token overhead per failed call |
|---|---|---|
| Malformed output (schema fail) | One same-context corrective retry | ~0.8× original input + ~0.3× original output |
| Model API error (429, 5xx) | Three retries with exponential backoff | Up to 3× original (each retry is a fresh call) |
| Tool-use error (idempotent tools only) | One retry | Negligible (tool result is a small fraction of the call) |
| Timeout | One retry with doubled budget | Up to 2× original |
| Context overflow | No retry, immediate abort | Zero retry overhead; entire invocation aborts |

Malformed-output retries are the dominant overhead in steady state (estimated 1–5% of calls during prompt iteration, dropping toward <1% as prompts stabilize). Model-API-error retries spike during Anthropic incidents and cap approach. The cost model adds **+10% to weekly token volume** as an envelope for failure-path overhead in normal operation, **+20%** in stress weeks.

---

## Headroom analysis

### Weekly Opus utilization (binding constraint)

Per scheduled invocation: 3 Opus calls (analyst, strategist, PM).

| Scenario | Scheduled | Emergency | Retries | Opus calls/week | Vs. 15-Opus-hour cap (~180 msg) | Vs. 35-Opus-hour cap (~420 msg) |
|---|---|---|---|---|---|---|
| Quiet week, primary | 16 × 3 = 48 | 0 | +5% = +2 | ~50 | 28% | 12% |
| Normal week, primary | 16 × 3 = 48 | 4 × 3 = 12 | +10% = +6 | ~66 | 37% | 16% |
| Stress week, full-system | 16 × 3 = 48 | 8 × 3 = 24 | +20% = +14 | ~86 | 48% | 20% |
| Crisis week, full-system | 16 × 3 = 48 | 16 × 3 = 48 | +20% = +19 | ~115 | 64% | 27% |

The cap-numerator uses the 12-message-per-Opus-hour calibration baseline. AlphaMind's PM call (~9K tokens combined) is meaningfully heavier than that baseline; effective cap may be tighter than message count suggests. Treat percentages as "best-case headroom from published structure"; expect actual paper-trading measurements to land at the higher end of utilization.

**Read:** Normal weeks consume 16–37% of the weekly Opus cap depending on which end of the published range Anthropic enforces. Stress weeks push 20–48%. A crisis week with large sustained breaches reaches 27–64% — still within cap under the Tier B cadence, where the schedule contributes ~48 of the weekly Opus calls and emergencies drive the rest. The operating posture remains built around the crisis tail.

### Weekly Sonnet utilization

Per scheduled invocation: 6 Sonnet calls. Stress and crisis weeks approximately double the call count via emergency invocations and adaptive-researcher tool turns.

| Scenario | Sonnet calls/week | Vs. 140-Sonnet-hour cap (~1,680 msg) | Vs. 280-Sonnet-hour cap (~3,360 msg) |
|---|---|---|---|
| Normal week | 16 × 6 + ~10 emergency + ~20 retries = ~126 | 8% | 4% |
| Crisis week | 16 × 6 + ~42 emergency + ~50 retries = ~188 | 11% | 6% |

Sonnet has comfortable headroom in every realistic scenario.

### 5-hour rolling window utilization

Under Tier B no 5-hour window holds more than two scheduled invocations (e.g., 09:00 pre-open and 13:00 mid-day, or 13:00 mid-day and 15:30 pre-close). Worst case ~2 scheduled + a possible emergency = ~3 invocations × ~10 LLM calls = ~30 calls.

Against a community-measured ~225-message Max 5x window allowance: **~13% utilization at peak.** No real risk of hitting the 5-hour cap from scheduled cadence alone.

### Combined all-models weekly cap

Anthropic enforces an umbrella all-models weekly cap in addition to per-model sub-caps. The numeric value is not published, but per-model caps are roughly half the umbrella cap. Opus + Sonnet combined (normal week ~126 Sonnet + ~66 Opus = ~192 calls) sits well below either per-model cap and clears the umbrella by a wider margin.

### What happens if the cap estimates are wrong

Community measurements are wide and Anthropic does not commit to them. Realistic cap-exhaustion scenarios:

- **Anthropic tightens caps without notice** (e.g., the off-peak bonus removed in March 2026). Mitigation: monitor Anthropic announcements; operator reacts via [Manual operator levers](#manual-operator-levers).
- **The Opus token-weight discount is harsher than the message-count baseline.** If AlphaMind's heavier messages count for, say, 2× a baseline message, the effective Opus cap shrinks by half. Mitigation: instrument actual usage in paper trading, refine the model.
- **Sustained crisis triggers many emergency invocations.** Mitigation: profile downgrade to model-light agent assignments, pause scheduler temporarily, accept reduced trading frequency.

---

## Latency model

### Per-invocation wall-clock budget

The pipeline runs sequentially across data → distillation → analysis → decision → execution. Latency is dominated by the analysis layer's sequential synthesizer and the decision layer's sequential PM. Best estimate per invocation:

| Phase | Approximate duration |
|---|---|
| Data layer (parallel API fetches, with cache hits) | 3–8s |
| Distillation layer (CPU-bound) | 1–2s |
| Analysis layer parallel group (Sonnet) | 8–15s (slowest of the five) |
| Adaptive research (Sonnet, 0–25 tool calls) | 5–60s |
| Synthesizer (Sonnet, 10K input) | 8–15s |
| Analyst + strategist parallel pair (Opus) | 15–30s (slower of the two) |
| PM (Opus) | 15–30s |
| Execution layer (broker submission) | 1–3s |
| **Per-invocation total, normal** | **~60–110s** |
| **Per-invocation total, with full adaptive budget consumed** | **~120–180s** |

### Catch-up budget

Under Tier B the tightest scheduler spacing is the 13:00↔15:30 gap (2.5 hours; the 09:00↔13:00 gap is wider). Even a worst-case 3-minute invocation consumes ~2% of the inter-trigger budget. APScheduler's `max_instances=1` prevents pile-up by queuing at most one fire per trigger. The 30-minute deduplication window suppresses a rolling fire only if an emergency or slow run completed within the lookback — under Tier B no anchored trigger sits within 30 minutes of the mid-day rolling slot, so the window is a backstop rather than a load-bearing collision guard.

### Emergency invocation latency target

Per [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger), the target is **command submission within ~30 seconds of the trigger**. The decision-layer Opus trio is the bottleneck:

- Analyst in `watchlist` mode: ~10s (lighter output than normal)
- Strategist in `defensive_posture` mode: ~15s (restricted action enum, smaller output)
- PM evaluating defensive-posture envelopes: ~10s (smaller envelope set)

Emergency-mode latency lands at **~25–35 seconds** for the decision layer, plus the analysis layer upstream. End-to-end emergency latency is dominated by the analysis layer (~30s) when triggers fire mid-day. The continuous monitor's protective authority covers the pre-decision window.

### Tool-use loop latency

The adaptive researcher's 25-call / 4,000-token budget is the longest-running tool loop. Each external tool call is in the 1–3 second range; 25 calls in series saturates at ~60s wall-time. Sonnet model turns between tool calls add 5–10s total. The qualitative researcher's bounded ~5–15 tool calls consume 10–30s end-to-end. Both fit inside the per-invocation latency budget.

---

## Operating posture

### Normal-day operation

The system runs unattended. The pipeline consumes ~25–60% of the weekly Opus cap (best estimate, normal week, primary profile) and ~10% of the weekly Sonnet cap. The operator interacts via the command center for routine review, not intervention.

### Cap-approach response

The command center's monitoring surface tracks running utilization across the three windows (5-hour, weekly Opus, weekly Sonnet) and surfaces an alert when sustained utilization crosses thresholds:

| Threshold | Alert severity | Recommended operator response |
|---|---|---|
| Weekly Opus > 60% sustained over 24h | Warning | No action; monitor |
| Weekly Opus > 80% with 3+ days remaining in window | Critical | Consider pausing scheduler for off-hours rolling triggers, or switching to a Sonnet-decision-layer profile |
| Weekly Sonnet > 70% sustained | Warning | Investigate adaptive-researcher activity; check for runaway tool loops |
| 5-hour window > 90% in any single window | Critical | Pause scheduler until window rolls |

Thresholds are operator-tunable in the command center alert registry. Starting values; refinement happens during paper trading once actual utilization is measured.

### Cap-hit response

When the cap is exceeded mid-invocation, the pipeline aborts per the standard fail-closed semantics. Subsequent scheduled triggers abort on retry exhaustion. The continuous monitor remains active, protecting open positions via engine-originated CLOSE commands.

The operator's options:

1. **Wait for the window to roll.** 5-hour window hit: at most 5 hours from the first message of the session. Weekly cap hit: up to 7 days.
2. **Pause the scheduler.** "Pause scheduler" stops new triggers from firing, conserving cap during the wait. Per [command-center.md § Operator actions](command-center.md#operator-actions), single-click with audit-log capture.
3. **Switch to a lighter profile.** The primary profile uses fewer tokens than full-system. "Switch profile" triggers a configuration reload at the next invocation.
4. **Reassign the decision layer to Sonnet.** Edit `config/agents.yaml` to switch analyst/strategist/PM model from Opus to Sonnet. A degraded-quality fallback the operator may accept temporarily.

### Manual operator levers

Cap-related operator actions from the command center:

| Action | Effect | Reversibility |
|---|---|---|
| Pause scheduler | New triggers do not fire; in-flight invocation completes | Resume restores normal cadence |
| Switch profile | Next invocation loads the new profile | Switching back is symmetric |
| Edit `config/agents.yaml` model assignments | Next invocation uses the new models | Edit back; takes effect at next invocation |
| Toggle halt mode | Per [breach-behavior.md § Halt mode](06-risk-guardrails/breach-behavior.md), engages the defensive_posture operating mode and disables `add` actions | Exit halt mode via the same action |

None of these levers consume cap. All are safe to invoke during a cap-exhausted period.

### API-key escape hatch

If Anthropic tightens caps materially, or the technical-enforcement classifier rejects the OAuth token persistently: switch the SDK to API-key authentication. Provision an API key from Anthropic Console, set `ANTHROPIC_API_KEY` in the pipeline process environment, unset `CLAUDE_CODE_OAUTH_TOKEN`. No code change required — the SDK picks the appropriate credential automatically.

API-key billing consumes the same model endpoints under per-token pricing (Opus ~$5/$25 per million input/output tokens, Sonnet ~$3/$15, current as of 2026-04). With prompt caching (synthesizer brief and stable system prompts cache well), the modeled workload's pay-per-token cost lands at roughly **$150–$450/month** depending on profile and volatility — comparable to or modestly above the $100/month Max subscription, in exchange for removing the cap entirely.

The escape hatch is documented but not provisioned by default.

---

## Monitoring

### What is tracked

The pipeline records per-invocation usage metrics into the `invocations` table (per [infrastructure.md § Layer 1: Structured metrics](../architecture/infrastructure.md#layer-1-structured-metrics-sqlite)):

| Field | Granularity | Source |
|---|---|---|
| Per-call input tokens | Per agent per invocation | SDK response metadata |
| Per-call output tokens | Per agent per invocation | SDK response metadata |
| Per-call wall-clock | Per agent per invocation | Pipeline timing |
| Failure-mode tag | Per agent per invocation | Failure-handling layer |
| Retry count | Per agent per invocation | Failure-handling layer |
| Estimated cap consumption | Rolling per window (5h, weekly Opus, weekly Sonnet) | Computed from per-call counts using the calibration baseline |

Cap-consumption estimates initially use the 12-message-per-hour Anthropic baseline, refined to a token-weight-adjusted multiplier once paper-trading data accumulates.

### Where surfaced

The command center's [Risk & guardrails view group](command-center.md) gains a Throughput panel showing:

- Current 5-hour window consumption (live)
- Current week Opus consumption (live, with reset countdown)
- Current week Sonnet consumption (live, with reset countdown)
- Per-invocation cost trend (last 7 days)
- Per-agent cost decomposition (last 7 days)
- Recent retry-rate trend (last 7 days)

### Alert conditions

Alerts route to the command center's standard notification channels (in-app banner + Discord webhook), per [command-center.md § Alerting](command-center.md). The default rule set adds:

| Rule | Condition | Severity |
|---|---|---|
| `throughput.opus_weekly_warning` | Estimated Opus cap utilization > 60% sustained 24h | Warning |
| `throughput.opus_weekly_critical` | Estimated Opus cap utilization > 80% with > 72h remaining in the week | Critical |
| `throughput.sonnet_weekly_warning` | Estimated Sonnet cap utilization > 70% sustained 24h | Warning |
| `throughput.session_critical` | 5-hour window utilization > 90% in current window | Critical |
| `throughput.cap_hit` | Pipeline invocation aborted with rate-limit error class | Critical |
| `throughput.retry_rate_warning` | Retry rate > 10% over rolling 24h | Warning |

Starting values; refinement via the operator-driven Class A review pattern used for distillation thresholds.

---

## Calibration discipline

### Numeric values are estimates

Three classes of number:

1. **Anthropic-published structure** — three windows (5-hour, weekly Sonnet, weekly Opus), authentication mechanism, at-cap behavior. Stable.
2. **Anthropic-unpublished cap thresholds** — actual numeric values of the windows. Community-measured and approximate; ranges given here are best estimates as of late 2026.
3. **AlphaMind-side estimates** — per-invocation token volumes, latency budgets, retry rates. Derived from agent specs and engineering judgment, not measured.

The cost model's conclusions (binding constraint is weekly Opus; normal-week utilization sits ~16–37% of cap under the Tier B cadence; latency budget is comfortable against scheduler cadence) are robust to wide uncertainty in numeric values. The alert-registry threshold values are starting points that need calibration.

### Re-evaluation triggers

Per the operator-driven Class A review pattern from [02-distillation-layer/threshold-calibration.md](02-distillation-layer/threshold-calibration.md), the cost model is reviewed under the following triggers:

- Anthropic announces a Max plan pricing or limit change
- Anthropic deploys new models the agents may switch to
- Sustained alert firing on `throughput.opus_weekly_warning` or `throughput.retry_rate_warning`
- Any single instance of `throughput.cap_hit`
- Profile change or addition
- Major workload change (new agent, agent budget revision, scheduler cadence change)

Default review cadence in absence of triggers: **monthly during paper trading, quarterly once live.**

### Refinement during paper trading

Three measurements drive refinement:

1. **Actual per-invocation token volume by agent and profile.** Compared against estimates in [Per-invocation token aggregate](#per-invocation-token-aggregate). Gap informs headroom recalibration.
2. **Actual cap consumption rate.** Tracked across the first ~3 weeks, compared against community-measured ranges. Ratio of estimated-to-actual utilization gives the empirical token-weight multiplier.
3. **Actual retry-rate.** Per agent per failure mode, compared against the +10% / +20% envelopes.

The first month of paper trading gathers enough data to refine alert thresholds and update this doc with measured values.

---

## Cross-doc impact

Changes landed alongside this doc:

- [architecture/llm-integration.md](../architecture/llm-integration.md) — subscription tier corrected to $100/month Max 5x; pay-per-token comparison updated for prompt-caching savings; cross-reference added.
- [command-center.md § Operator actions](command-center.md#operator-actions) — references existing actions (pause scheduler, switch profile, toggle halt mode) without adding new ones.
- [command-center.md § Alerting](command-center.md) — adds throughput rule names to the default rule set.

Open follow-ups:

- The [Feedback loop design](../project-tracker.md#phase-4--maturation-before-live-transition), when it lands, will likely add a per-agent cost-per-quality-unit metric depending on the per-call token tracking specified here.
- Once paper-trading measurements are available, this doc is updated with measured values and alert thresholds are revised.
