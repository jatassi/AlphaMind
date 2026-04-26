# Breach behavior

What happens when a constraint is hit or breached. Different rules have different severity profiles and appropriate responses. This document defines the escalation model, forced reduction policy, drawdown halt logic, margin cascade handling, and the per-rule classification of engine action vs. PM deferral.

---

## Escalation model

Every guardrail rule has four proximity zones. The zone determines what information is surfaced and what mechanical actions are available. Zones are defined as percentages of the limit consumed.

| Zone | Threshold | Meaning | Action |
|------|-----------|---------|--------|
| **Normal** | 0–70% of limit | Comfortable headroom | Headroom reported in guardrail state headers. No special treatment. |
| **Warning** | 70–85% of limit | Approaching capacity | Headroom highlighted in guardrail state headers with a `⚠ warning` flag. Analyst should avoid recommendations that would push into the critical zone. PM sees the flag alongside proposals. |
| **Critical** | 85–95% of limit | Near breach | Headroom highlighted with `🔴 critical` flag. Analyst guardrail validation tool returns advisory warnings on proposals that would consume remaining headroom. PM receives explicit notification that this rule is near breach. |
| **Hard block** | 95–100%+ of limit | At or beyond limit | Engine rejects any command that would increase exposure in the breaching direction. Existing positions may trigger forced reduction depending on rule classification (see below). |

**Threshold rationale:** The 70/85/95 breakpoints are chosen to give progressive warning at useful decision points. At 70%, the analyst has room for 1–2 more typical-sized positions before concern. At 85%, one more max-sized position would breach. At 95%, even a small position would breach. The 95% hard-block threshold (rather than 100%) provides a buffer against estimation error — delta computations, correlation estimates, and market prices all have lag, so blocking at 95% prevents the rare case where a command is approved at 99% and market movement pushes it over 100% before execution completes.

**Per-rule threshold overrides:** The 70/85/95 defaults are appropriate for most rules. Two exceptions:

- **Daily drawdown:** Uses 60/80/90 thresholds. Drawdown is the hardest constraint, and the consequences of hitting it (halt mode) are severe enough to warrant earlier warning. At 60% consumed (1.5% of the 2.5% limit), the PM should already be restricting new entries to only high-conviction opportunities.
- **Cumulative drawdown:** Uses 50/70/85 thresholds. The cumulative limit affects system behavior for days or weeks, so earlier warning gives the PM more time to adjust strategy.

---

## Forced reduction policy

When market movement causes a guardrail breach on existing positions — not from a new command, but from the market moving against the portfolio — the system must decide whether to reduce exposure mechanically or defer to the PM.

### Per-rule breach response classification

Each rule is classified as **immediate engine action** or **deferred to strategist → PM** based on two criteria: (a) how quickly the breach can compound if left unaddressed, and (b) whether agent judgment (strategist for remedy proposals, PM for cross-constraint validation and execution) adds value to the response.

| Rule | Between-invocation response | Rationale |
|------|----------------------------|-----------|
| **Daily drawdown (hard block)** | **Immediate: halt mode** | Drawdown compounds in real time. Waiting 2 hours for the next invocation while the portfolio bleeds is unacceptable. See [halt mode](#drawdown-halt-mode) below. |
| **Cumulative drawdown** | **Immediate: progressive reduction** | Same reasoning — cumulative drawdown is a survival constraint. Engine begins reducing risk immediately. |
| **Position-level max loss** | **Immediate: close position** | A position at 30% loss (equity) or 80% loss (options) has exhausted its risk budget. The thesis is almost certainly wrong. No PM judgment adds value — close immediately. |
| **Sector concentration** | **Deferred to strategist → PM** | Sector breaches from market movement (a sector rallies and existing positions grow beyond the limit) are not immediately dangerous — the positions are *winning*. The strategist proposes which position to trim based on thesis strength and target proximity; the PM reviews and executes. |
| **Net long/short exposure** | **Deferred to strategist → PM** | Same reasoning as sector concentration. Directional exposure growing because positions are profitable is not an emergency. Strategist proposes remedy; PM reviews cross-constraint impact and executes. |
| **Gross exposure** | **Deferred to strategist → PM** | Same reasoning. Gross exposure breaches from market movement are the result of winning positions growing. Strategist proposes remedy; PM executes. |
| **Options delta exposure** | **Deferred to strategist → PM** | Delta changes as the underlying moves (gamma effect). A breach may self-correct if the underlying reverses. Strategist judgment on thesis and gamma dynamics proposes remedy; PM validates against cross-constraint interactions and executes. |
| **Total short exposure** | **Immediate if > 110% of limit** / **Deferred if ≤ 110%** | Short exposure breaches are more dangerous than long exposure breaches because short squeezes can accelerate losses. A small overage (≤ 110% of limit) is deferred; a significant overage triggers immediate partial reduction of the most liquid short position. |
| **Single short max size** | **Immediate: partial close** | A single short growing beyond its size cap (from the stock declining — the short is winning but oversized) should be trimmed to lock in profits and reduce squeeze risk. Engine trims to 95% of limit. |

### Position selection logic for forced reductions

When the engine must close or trim a position to cure a breach, it applies deterministic selection criteria. The criteria vary by breach type:

**Drawdown breaches (daily and cumulative):**
1. Select positions with the largest unrealized loss (these are contributing most to the drawdown)
2. Among tied positions, prefer the most liquid (highest ADV relative to position size) for fastest execution
3. Close full positions rather than partial — partial closes leave residual risk and orphaned theses

**Position-level max loss:**
1. Close the specific position that breached — no selection logic needed

**Short exposure breaches (when immediate action triggered):**
1. Select the largest short position in the most liquid name
2. Trim to 95% of the applicable limit (per-position or aggregate)

**Single short max size:**
1. Trim the breaching position to 95% of the per-position limit

### Secondary breach checking

Before executing any forced reduction, the engine verifies the CLOSE or partial CLOSE wouldn't create a new breach in a different rule. The most common secondary breach scenario: closing a short position that was providing directional balance could push net long exposure beyond its limit.

**If a secondary breach would result:**
1. Log the conflict with both the primary breach (the one being cured) and the secondary breach (the one the cure would create)
2. Select an alternative position if one exists that cures the primary breach without creating a secondary breach
3. If no clean cure exists, execute the forced reduction anyway — the primary breach takes priority, and the secondary breach is flagged for the PM at the next invocation. The rationale: a known, flagged, secondary breach is better than an unaddressed primary breach that's actively compounding

---

## Drawdown halt mode

The most severe mechanical response. Triggered when daily drawdown hits 100% of the limit (2.5% of opening equity at default settings) or cumulative drawdown hits 100% of its limit (8% from high-water mark).

### Daily drawdown halt

**Trigger:** Portfolio equity declines 2.5% or more from the day's opening equity, detected by the continuous monitor.

**Mechanical response:**
1. **No new positions.** The engine rejects all OPEN and ADD commands for the remainder of the trading day.
2. **No new shorts.** Even CLOSE commands that would create new short exposure are blocked (edge case: closing a long when the system is net short).
3. **Existing bracket stops remain active.** Take-profit and invalidation stops continue to execute normally. The system doesn't freeze existing risk management — it only blocks new risk.
4. **Pending orders are not cancelled.** Limit orders placed before the halt may still fill. The PM can cancel them at the next invocation if desired, but the engine doesn't mechanically cancel because: (a) a limit buy at a lower price represents defined-risk entry at a known level, and (b) automatic cancellation could interfere with bracket stops on existing positions.
5. **Halt lifts at the next day's open.** The daily drawdown counter resets at market open (9:30 ET). If cumulative drawdown is also in breach, the cumulative drawdown response (below) remains active.

### Agent behavior during halt mode

The pipeline continues to run during halt, but each agent receives modified instructions via its guardrail state header (see [state-delivery.md](state-delivery.md#halt-mode-header-modifications)). The upstream pipeline (data ingestion, distillation, research agents) runs normally — its output informs how agents reason about existing positions during the halt.

**Analyst — watchlist mode.** The analyst does not generate trade proposals during halt. Its OPEN/ADD proposals cannot execute, so generating them wastes tokens. Instead, the analyst produces a compact **watchlist** — opportunities worth tracking for post-halt consideration. The watchlist captures the ticker, thesis summary, and estimated conviction, but omits the full proposal structure (sizing, entry parameters, bracket configuration). This preserves the analytical signal at a fraction of the token cost. The watchlist is stored in the activity log and surfaced to the analyst at the first post-halt invocation.

**Strategist — defensive posture mode.** The strategist's role becomes more focused during halt: instead of balancing additions and reductions, it shifts entirely toward defensive position management. It still receives the full distillation and research output, which informs its reasoning about current holdings. Its position assessments should emphasize: which positions have deteriorating theses in the current market environment, which stops should be tightened, and which positions should be closed to reduce exposure and limit further drawdown. The strategist's recommendations during halt carry higher weight because the PM's only available actions are risk-reducing.

**PM — risk reduction mode.** The PM retains the ability to issue CLOSE commands (reducing risk) and ADJUST commands (tightening stops on existing positions). No OPEN, ADD, or risk-increasing actions are available. The PM's context explicitly states the restricted action space and the halt reason. The PM should prioritize: (a) reviewing and potentially cancelling pending limit orders that were placed before the halt, (b) executing the strategist's defensive recommendations, and (c) tightening stops on positions that are moving against their thesis in the current session.

### Cumulative drawdown response

**Trigger:** Portfolio equity declines 8% or more from the all-time high-water mark, detected by the continuous monitor.

**Progressive response (not a binary halt):**

| Drawdown depth | Response |
|----------------|----------|
| 8% (100% of limit) | Max position size reduced to 3% (from 5%). Max gross exposure reduced to 80% (from 120%). Existing positions with unrealized loss > 10% flagged for PM review. |
| 10% (125% of limit) | Max position size reduced to 2%. Max gross exposure reduced to 60%. Existing positions with unrealized loss > 15% flagged for PM review. |
| 12% (150% of limit) | **Full halt.** No new positions. Strategist and PM enter survival mode — strategist proposes orderly position reductions (1–2 per invocation to avoid market impact), PM reviews and executes. If the strategist/PM fail to reduce positions within 2 invocations of entering tier 3, the engine falls back to closing positions by largest unrealized loss first. |

**Subjective measures are PM/strategist judgment, not mechanical gates.** Conviction, thesis strength, and thesis status are subjective agent assessments — they reflect interpretation, not objective portfolio metrics. Guardrails must be based on objective, measurable quantities (position size, exposure, drawdown). Earlier versions of this design used conviction-level minimums as mechanical gates in tiers 1–2 and thesis status as an engine sorting criterion in tier 3. Both have been removed. The strategist and PM are the appropriate layers for applying subjective judgment: the strategist uses thesis strength to propose which positions to trim or close, and the PM exercises judgment about whether a trade's conviction justifies entry during drawdown. When the engine must act autonomously (the tier 3 fallback), it uses objective criteria only — largest unrealized loss, then most liquid.

**Recovery:** As the portfolio recovers (equity rises), the system exits each tier's restrictions when drawdown depth drops below the tier's threshold. Recovery thresholds use the same percentages — no hysteresis. The reasoning: if the portfolio has recovered to 7% drawdown, it should return to full operating capacity. Hysteresis would delay recovery without adding safety.

**Rationale for progressive rather than binary response:** A binary halt at 8% would prevent the system from recovering — it can't generate returns if it can't trade. The progressive model constrains the system increasingly tightly as losses deepen, preserving some ability to recover while ensuring that truly severe drawdowns trigger a full defensive posture.

---

## Margin call cascade handling

Margin calls create a special category of forced action because they're externally imposed (by the broker) rather than internally triggered (by guardrails). The margin call response interacts with other guardrails.

**Margin call priority:** Margin calls take absolute priority over all other guardrail rules. If a margin call requires liquidation, the engine liquidates regardless of the impact on other constraints. The reasoning: a margin call is a hard external constraint — failure to meet it results in broker-initiated forced liquidation at worse prices and terms.

**Cascade detection:** After executing a margin call liquidation, the engine re-evaluates all guardrail rules against the post-liquidation portfolio state. If the liquidation created a new breach (e.g., closing a short to meet margin causes a net long exposure breach), the new breach is handled according to its own classification (immediate or deferred).

**Cascade logging:** Each step of a margin cascade is logged as a separate command envelope with `engine_guardrail` provenance and a `margin_cascade` sub-type. The full cascade sequence is linked by a shared `cascade_id` so the activity log shows the chain of events.

**Margin call position selection:**
1. Prioritize positions with the worst risk/reward ratio at current price (closest to invalidation, farthest from target)
2. Among tied positions, prefer the most liquid
3. Prefer full closes over partial — partial closes may not satisfy the margin requirement and leave residual risk

---

## Emergency invocation trigger

The continuous monitor normally operates between scheduled pipeline invocations (every ~2 hours), taking only mechanical actions (halt mode, forced closes). But certain market events are severe enough that waiting for the next scheduled invocation is unacceptable — the strategist and PM need to start reasoning about the response immediately.

### Trigger conditions

The continuous monitor requests an emergency pipeline invocation when any of the following objective thresholds are met:

| Trigger | Threshold | Rationale |
|---------|-----------|-----------|
| **Regime jump** | Regime classification skips a level (e.g., low-vol → elevated, normal → crisis, low-vol → crisis) | A regime skip indicates a sudden, severe market shift. The strategist and PM need to begin resolving the resulting breaches immediately, not 2 hours from now. |
| **Multi-rule breach** | 3 or more deferred rules simultaneously enter breach between invocations | A single deferred breach can wait; multiple simultaneous breaches indicate a coordinated market move that requires a coordinated agent response. |
| **Daily drawdown velocity** | Daily drawdown crosses 60% of limit within 30 minutes of the last check | Rapid drawdown acceleration suggests the situation is deteriorating faster than the scheduled cadence can respond to. The agents should evaluate defensive actions before the halt triggers. |
| **Margin call** | Broker issues a margin call | After the engine handles the immediate liquidation mechanically, an emergency invocation lets the agents assess the post-liquidation state and take further defensive action. |

**All triggers are objective and measurable.** No subjective assessment (thesis quality, sentiment, analyst opinion) can trigger an emergency invocation.

### Cooldown

Emergency invocations are subject to a **minimum 30-minute cooldown** between triggers. If multiple trigger conditions fire within the cooldown window, they are coalesced into the already-scheduled or in-progress emergency invocation. The cooldown prevents runaway invocations during sustained volatility where multiple triggers could fire in rapid succession.

**Exception:** Margin calls override the cooldown. A margin call always triggers an emergency invocation (or attaches to one already in progress) because the broker's deadline is external and non-negotiable.

### Relationship to scheduled invocations

An emergency invocation **resets the scheduled cadence.** The next scheduled invocation fires at the normal interval (e.g., 2 hours) after the emergency invocation completes. This prevents a scenario where an emergency invocation fires 15 minutes before a scheduled one, causing two near-simultaneous invocations with minimal new information between them.

**An emergency trigger fired while a rolling invocation is in progress interrupts and replaces it.** The scheduler preserves its `max_instances=1` constraint — at most one pipeline invocation ever runs. When an emergency trigger arrives mid-rolling-invocation, the rolling invocation is cancelled, its in-flight state is discarded per [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md) (partial outputs persisted as diagnostic, never replayed), and the emergency invocation runs in its place. The rolling run was reasoning against a pre-emergency market state that the trigger itself says no longer holds; the emergency invocation's fresh context window and current-data snapshot are precisely the point. Execution-layer state (committed commands, activity log, position records) is preserved — only the interrupted rolling invocation's in-memory reasoning state is discarded. The continuous monitor's between-invocation authority (protective CLOSE via engine-originated envelopes, greeks refresh, halt-mode activation) is unaffected by the interruption.

### Agent context for emergency invocations

Agents receive a modified context header indicating the emergency trigger:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** EMERGENCY INVOCATION — trigger: {trigger_type} **
Trigger detail: {e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)"}
Time since last invocation: {minutes}m (normal cadence: ~120m)

[...normal guardrail state header follows...]
```

The emergency flag serves two purposes: it signals urgency (the strategist and PM should prioritize breach resolution over new opportunity evaluation), and it provides context about *why* the invocation was triggered, which informs the agents' reasoning about the appropriate response.

### Implementation note

The emergency invocation trigger is a guardrail-layer capability that requires execution-layer support. The continuous monitor (execution layer) detects the trigger conditions; the pipeline scheduler (execution layer) handles the actual invocation dispatch. The guardrail layer defines the trigger thresholds and the agent context modifications. See the execution layer architecture for the scheduling and dispatch contract.

---

## Hard rejection semantics

When the engine's T3 guardrail check rejects a command during normal pipeline execution (not between invocations), the rejection is returned synchronously to the PM. The rejection payload includes:

- **Rule(s) breached:** which specific guardrail(s) blocked the command
- **Current state:** current value for each breaching rule (e.g., "sector tech delta-adjusted exposure: 23.7%")
- **Limit:** the active limit value (e.g., "sector tech limit: 25.0%")
- **Overage:** how much the command would exceed the limit (e.g., "proposed addition would push to 28.2%, overage: 3.2%")
- **Suggested modification:** a mechanical suggestion for making the command compliant (e.g., "reduce position size by 42% to fit within sector limit"). This is a starting point for the PM, not a binding instruction
- **Headroom after hypothetical compliance:** what the headroom would be if the PM adopts the suggested modification

See [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the PM's synchronous feedback handling contract.

---

## Traceability for engine-originated actions

All commands in the system — whether originated by the PM or by the engine — are wrapped in **command envelopes** that provide full traceability. The envelope model is defined in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). When the continuous monitor detects a breach and issues a protective CLOSE, it generates an engine-originated envelope (`engine_guardrail` provenance) containing:

- A guardrail trigger record (rule breached, breach details, trigger timestamp, regime at time of breach)
- The position selection logic (which position was selected and why)
- The CLOSE command with close rationale type `risk_management` / sub-type `engine_guardrail`

This envelope is stored in the activity log with the same structure as PM-originated envelopes. The thesis resolution carries the `engine_guardrail` provenance, allowing the feedback loop to segment engine-originated closures from PM-originated ones in outcome analysis. The PM and strategist see engine-originated envelopes in their input bundles at the next invocation (via the activity log delivered as part of [portfolio state §5](../01-data-layer/internal/portfolio-state.md), with prominence cues in their respective guardrail state headers), maintaining full awareness of between-invocation actions.

**Constraint on engine autonomy:** The engine may only issue CLOSE commands via this path — never OPEN, ADD, ADJUST, or CANCEL. All constructive actions require PM judgment.

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — defines the constraints whose breach behavior this document specifies
- [Regime adaptation](regime-adaptation.md) — regime transitions can create immediate breaches if limits tighten around existing positions
- [Execution layer / architecture](../05-execution-layer/architecture.md) — the continuous monitor implements breach detection and forced reduction logic
- [Execution layer / OMS commands](../05-execution-layer/oms-commands.md) — defines the command origins model (PM-originated vs. engine-originated)
- [Execution layer / position model](../05-execution-layer/position-model.md) — margin calls originate from the position model's margin tracking
- [Portfolio manager / command envelopes](../04-decision-layer/portfolio-manager.md) — defines the envelope structure for both PM-originated and engine-originated commands
