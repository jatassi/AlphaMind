# Breach behavior

Defines the escalation model, forced reduction policy, drawdown halt logic, margin cascade handling, and per-rule engine-vs-PM-deferral classification.

---

## Escalation model

Every rule has four proximity zones, defined as percentages of the limit consumed. The zone determines what information is surfaced and what mechanical actions are available.

| Zone | Threshold | Meaning | Action |
|------|-----------|---------|--------|
| **Normal** | 0–70% of limit | Comfortable headroom | Headroom reported in guardrail state headers. |
| **Warning** | 70–85% of limit | Approaching capacity | Headroom flagged `⚠ warning`. Analyst should avoid pushing into the critical zone. |
| **Critical** | 85–95% of limit | Near breach | Headroom flagged `🔴 critical`. Validation tool returns advisory warnings on proposals consuming remaining headroom. PM receives explicit near-breach notification. |
| **Hard block** | 95–100%+ of limit | At or beyond limit | Engine rejects any command increasing exposure in the breaching direction. Existing positions may trigger forced reduction per rule classification (below). |

**Threshold rationale:** 70/85/95 gives progressive warning at useful decision points: at 70%, room for 1–2 more typical positions; at 85%, one more max-sized position breaches; at 95%, even a small position breaches. The 95% hard block (rather than 100%) buffers against estimation error in delta, correlation, and price lag — preventing the case where a command approved at 99% pushes over 100% before execution completes.

**Per-rule overrides:** Two exceptions to 70/85/95:

- **Daily drawdown:** 60/80/90. Drawdown is the hardest constraint and triggers halt mode; earlier warning lets the PM restrict new entries to high-conviction opportunities at 60% consumed (1.5% of the 2.5% limit).
- **Cumulative drawdown:** 50/70/85. The cumulative limit affects system behavior for days or weeks; earlier warning gives time to adjust strategy.

---

## Forced reduction policy

When market movement causes a breach on existing positions, the system either reduces exposure mechanically or defers to the PM.

### Per-rule breach response classification

Each rule is classified as **immediate engine action** or **deferred to strategist → PM** based on (a) how quickly the breach compounds if unaddressed, and (b) whether agent judgment adds value (strategist for remedy proposals, PM for cross-constraint validation).

| Rule | Between-invocation response | Rationale |
|------|----------------------------|-----------|
| **Daily drawdown (hard block)** | **Immediate: halt mode** | Drawdown compounds in real time; waiting for the next invocation is unacceptable. See [halt mode](#drawdown-halt-mode). |
| **Cumulative drawdown** | **Immediate: progressive reduction** | Survival constraint. Engine begins reducing risk immediately. |
| **Position-level max loss** | **Immediate: close position** | At 30% loss (equity) or 80% loss (options), the risk budget is exhausted and the thesis is almost certainly wrong. |
| **Sector concentration** | **Deferred to strategist → PM** | Market-movement breaches mean positions are *winning* — not immediately dangerous. Strategist proposes which to trim based on thesis strength and target proximity. |
| **Net long/short exposure** | **Deferred to strategist → PM** | Same reasoning — directional exposure growing from profitable positions is not an emergency. |
| **Gross exposure** | **Deferred to strategist → PM** | Same reasoning. |
| **Options delta exposure** | **Deferred to strategist → PM** | Delta changes via gamma as the underlying moves; the breach may self-correct on reversal. Strategist judgment on thesis and gamma dynamics proposes remedy. |
| **Total short exposure** | **Immediate if > 110% of limit** / **Deferred if ≤ 110%** | Short squeezes accelerate losses. Small overage (≤ 110%) is deferred; significant overage triggers immediate partial reduction of the most liquid short. |
| **Single short max size** | **Immediate: partial close** | A short growing past its cap (winning but oversized) is trimmed to 95% of limit to lock profits and reduce squeeze risk. |

### Position selection logic for forced reductions

Deterministic selection criteria, varying by breach type.

**Drawdown breaches (daily and cumulative):**
1. Largest unrealized loss (largest contributor to drawdown)
2. Tiebreaker: most liquid (highest ADV relative to position size)
3. Full closes — partial leaves residual risk and orphaned theses

**Position-level max loss:** Close the specific breaching position.

**Short exposure breaches (when immediate action triggered):**
1. Largest short position in the most liquid name
2. Trim to 95% of the applicable limit

**Single short max size:** Trim the breaching position to 95% of the per-position limit.

### Secondary breach checking

Before any forced reduction, the engine verifies the action wouldn't breach a different rule. Most common case: closing a short providing directional balance could push net long beyond its limit.

**If a secondary breach would result:**
1. Log the conflict with both the primary and secondary breach
2. Select an alternative position that cures the primary without creating a secondary breach
3. If no clean cure exists, execute anyway — the primary takes priority, and the secondary is flagged for the PM at the next invocation. A known, flagged secondary breach is better than an unaddressed primary breach that's actively compounding.

---

## Drawdown halt mode

The most severe mechanical response. Triggered at 100% of the daily limit (2.5% of opening equity) or the cumulative limit (8% from high-water mark).

### Daily drawdown halt

**Trigger:** Portfolio equity declines 2.5%+ from day's opening equity, detected by the continuous monitor.

**Mechanical response:**
1. **No new positions.** Engine rejects all OPEN and ADD commands for the rest of the trading day.
2. **No new shorts.** CLOSE commands creating new short exposure are blocked (edge case: closing a long when net short).
3. **Existing bracket stops remain active.** Take-profits and invalidation stops continue normally — the system blocks new risk, not existing risk management.
4. **Pending orders are not cancelled.** Limit orders placed before halt may still fill. The PM can cancel at the next invocation. Engine does not auto-cancel because (a) a limit buy at a lower price is defined-risk at a known level, and (b) auto-cancellation could interfere with bracket stops on existing positions.
5. **Halt lifts at next day's open.** Daily counter resets at 9:30 ET. If cumulative drawdown is also breached, that response (below) remains active.

### Agent behavior during halt mode

The pipeline continues running during halt; each agent receives modified instructions via its guardrail state header (see [state-delivery.md](state-delivery.md#halt-mode-header-modifications)). The upstream pipeline (data ingestion, distillation, research) runs normally and informs reasoning about existing positions.

**Analyst — watchlist mode.** Generates no trade proposals — OPEN/ADD cannot execute, so generation wastes tokens. Produces a compact watchlist (ticker, thesis summary, estimated conviction) for post-halt consideration, omitting sizing, entry parameters, and bracket config. Stored in the activity log and surfaced at the first post-halt invocation.

**Strategist — defensive posture mode.** Receives full distillation and research output. Position assessments emphasize: deteriorating theses in the current environment, stops to tighten, and positions to close to limit further drawdown. Recommendations carry higher weight because the PM's only available actions are risk-reducing.

**PM — risk reduction mode.** Retains CLOSE and ADJUST (tightening stops) authority. No OPEN, ADD, or risk-increasing actions. Context states the restricted action space and halt reason. Priorities: (a) review and possibly cancel pre-halt pending limit orders, (b) execute the strategist's defensive recommendations, (c) tighten stops on positions moving against thesis.

### Cumulative drawdown response

**Trigger:** Portfolio equity declines 8%+ from the all-time high-water mark.

**Progressive response (not binary halt):**

| Drawdown depth | Response |
|----------------|----------|
| 8% (100% of limit) | Max position size reduced to 3% (from 5%). Max gross exposure reduced to 80% (from 120%). Existing positions with unrealized loss > 10% flagged for PM review. |
| 10% (125% of limit) | Max position size reduced to 2%. Max gross exposure reduced to 60%. Existing positions with unrealized loss > 15% flagged for PM review. |
| 12% (150% of limit) | **Full halt.** No new positions. Strategist and PM enter survival mode — strategist proposes orderly position reductions (1–2 per invocation to avoid market impact), PM reviews and executes. If the strategist/PM fail to reduce positions within 2 invocations of entering tier 3, the engine falls back to closing positions by largest unrealized loss first. |

**Subjective measures are PM/strategist judgment, not mechanical gates.** Conviction, thesis strength, and thesis status are agent interpretations, not objective portfolio metrics. Guardrails are based on measurable quantities (position size, exposure, drawdown). The strategist uses thesis strength to propose which positions to trim; the PM exercises judgment on whether conviction justifies entry during drawdown. The tier 3 engine fallback uses objective criteria only — largest unrealized loss, then most liquid.

**Recovery:** Each tier's restrictions exit when drawdown depth drops below its threshold. No hysteresis — same percentages on entry and exit. If the portfolio has recovered to 7% drawdown, it returns to full operating capacity; delaying recovery doesn't add safety.

**Rationale for progressive vs. binary response:** A binary halt at 8% prevents the system from recovering — it can't generate returns if it can't trade. The progressive model constrains tighter as losses deepen while preserving recovery ability.

---

## Margin call cascade handling

Margin calls are externally imposed by the broker rather than internally triggered.

**Margin call priority:** Absolute priority over all guardrail rules. If liquidation is required, the engine liquidates regardless of impact on other constraints — failure to meet the call results in broker-initiated forced liquidation at worse prices.

**Cascade detection:** After liquidation, the engine re-evaluates all rules against post-liquidation state. New breaches (e.g., closing a short causes a net long breach) are handled per their own classification.

**Cascade logging:** Each step is logged as a command envelope with `engine_guardrail` provenance and `margin_cascade` sub-type. A shared `cascade_id` links the chain.

**Margin call position selection:**
1. Worst risk/reward ratio at current price (closest to invalidation, farthest from target)
2. Tiebreaker: most liquid
3. Full closes — partial may not satisfy the requirement and leaves residual risk

---

## Emergency invocation trigger

The continuous monitor normally takes only mechanical actions between scheduled invocations. Certain events are severe enough that waiting for the next scheduled run is unacceptable.

### Trigger conditions

| Trigger | Threshold | Rationale |
|---------|-----------|-----------|
| **Regime jump** | Classification skips a level (e.g., low-vol → elevated, normal → crisis, low-vol → crisis) | A regime skip indicates a sudden severe shift requiring immediate strategist/PM reasoning, not 2 hours from now. |
| **Multi-rule breach** | 3+ deferred rules simultaneously breach between invocations | Multiple simultaneous breaches indicate a coordinated market move requiring a coordinated agent response. |
| **Daily drawdown velocity** | Daily drawdown crosses 60% of limit within 30 minutes of last check | Rapid acceleration suggests the situation is outpacing the scheduled cadence; agents should evaluate defensive actions before halt triggers. |
| **Margin call** | Broker issues a margin call | After mechanical liquidation, agents assess post-liquidation state and take further defensive action. |

All triggers are objective and measurable; no subjective assessment can trigger an emergency invocation.

### Cooldown

**Minimum 30-minute cooldown** between triggers. Multiple conditions firing within the window coalesce into the in-progress emergency invocation. Prevents runaway invocations during sustained volatility.

**Exception:** Margin calls override the cooldown — the broker's deadline is external and non-negotiable.

### Relationship to scheduled invocations

An emergency invocation **resets the scheduled cadence.** The next scheduled invocation fires at the normal interval after the emergency completes, preventing near-simultaneous runs with minimal new information between them.

**An emergency trigger fired during a rolling invocation interrupts and replaces it.** The scheduler preserves `max_instances=1`. The rolling invocation is cancelled, its in-flight state discarded per [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md), and the emergency runs in its place. The rolling run was reasoning against a pre-emergency state the trigger says no longer holds; the emergency's fresh context and current-data snapshot are the point. Execution-layer state (committed commands, activity log, position records) is preserved — only in-memory reasoning state is discarded. Continuous monitor authority (protective CLOSE, greeks refresh, halt activation) is unaffected.

### Agent context for emergency invocations

Agents receive a modified context header:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** EMERGENCY INVOCATION — trigger: {trigger_type} **
Trigger detail: {e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)"}
Time since last invocation: {minutes}m (normal cadence: ~120m)

[...normal guardrail state header follows...]
```

The flag signals urgency (prioritize breach resolution over new opportunity evaluation) and provides the trigger context that informs the appropriate response.

### Implementation note

The continuous monitor (execution layer) detects trigger conditions; the scheduler dispatches the invocation. The guardrail layer defines thresholds and agent context modifications. See the execution layer architecture for the scheduling contract.

---

## Hard rejection semantics

When the T3 check rejects a command during normal pipeline execution, the rejection returns synchronously to the PM. Payload:

- **Rule(s) breached:** which guardrail(s) blocked the command
- **Current state:** current value for each breaching rule (e.g., "sector tech delta-adjusted exposure: 23.7%")
- **Limit:** the active limit (e.g., "sector tech limit: 25.0%")
- **Overage:** how much the command would exceed the limit (e.g., "would push to 28.2%, overage: 3.2%")
- **Suggested modification:** a mechanical compliance suggestion (e.g., "reduce size by 42%"). Starting point, not binding.
- **Headroom after hypothetical compliance:** headroom if the PM adopts the suggestion

See [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the PM's synchronous feedback handling.

---

## Traceability for engine-originated actions

All commands are wrapped in **command envelopes** for traceability. Envelope model in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). When the continuous monitor issues a protective CLOSE, the engine-originated envelope (`engine_guardrail` provenance) contains:

- A guardrail trigger record (rule breached, breach details, trigger timestamp, regime at time of breach)
- Position selection logic (which position, why)
- The CLOSE command with close rationale type `risk_management` / sub-type `engine_guardrail`

Stored in the activity log with the same structure as PM-originated envelopes. Thesis resolution carries `engine_guardrail` provenance so the feedback loop can segment engine-originated closures in outcome analysis. The PM and strategist see these envelopes in their next input bundle (via the activity log in [portfolio state §5](../01-data-layer/internal/portfolio-state.md), with prominence cues in their guardrail state headers).

**Engine autonomy constraint:** Engine may only issue CLOSE — never OPEN, ADD, ADJUST, or CANCEL. All constructive actions require PM judgment.

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — defines the constraints whose breach behavior this document specifies
- [Regime adaptation](regime-adaptation.md) — regime transitions can create immediate breaches if limits tighten around existing positions
- [Execution layer / architecture](../05-execution-layer/architecture.md) — the continuous monitor implements breach detection and forced reduction logic
- [Execution layer / OMS commands](../05-execution-layer/oms-commands.md) — defines the command origins model (PM-originated vs. engine-originated)
- [Execution layer / position model](../05-execution-layer/position-model.md) — margin calls originate from the position model's margin tracking
- [Portfolio manager / command envelopes](../04-decision-layer/portfolio-manager.md) — defines the envelope structure for both PM-originated and engine-originated commands
