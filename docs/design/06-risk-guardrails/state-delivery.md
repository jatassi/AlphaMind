# Guardrail state delivery

How current guardrail state is packaged and delivered to each consuming layer. Each consumer needs different granularity — the analyst needs a summary of what's available; the portfolio manager needs detailed headroom and proximity data; the engine layer operates directly on the authoritative state.

This doc owns the **guardrail state header** delivered to each agent — a formatted text block, bounded by `=== GUARDRAIL STATE ===` markers, that opens the agent's prompt. It is one component of the agent's full input bundle (which the agent docs refer to as the agent's *context package*), alongside the synthesizer brief, portfolio state, and the proposal pre-processor bundle. This doc does not describe those other inputs — see [analyst.md](../04-decision-layer/analyst.md), [strategist.md](../04-decision-layer/strategist.md), and [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the full per-agent input contracts.

---

## Design principles

**Snapshot consistency.** All guardrail state headers within a single invocation are generated from the same guardrail state snapshot, computed once at the start of Phase 1 (after fill collection). The analyst, strategist, PM, and guardrail validation tool all operate on the same numbers. If state drifts during the invocation (market movement, fill resolution), the execution layer's final authoritative check catches it.

**Token efficiency.** Headers are formatted as structured text blocks, not raw JSON. Field names are abbreviated, zero-headroom rules are highlighted, and normal-headroom rules are compressed. The target is to convey complete guardrail state in the minimum token budget without losing critical information.

**Progressive detail.** The analyst gets the least detail (it needs to know what's available, not why). The strategist gets position-level detail (it needs to reason about which positions to recommend trimming). The PM gets the most detail (it makes final sizing decisions and sees the full constraint picture).

**Feature-flag aware.** When features are disabled (e.g., options and shorts in the primary portfolio — see [dual portfolio profiles](rules-and-limits.md#dual-portfolio-profiles)), the headers omit the corresponding sections entirely. The analyst for the $1,500 primary portfolio doesn't see options headroom or short exposure data — those fields don't exist in its header, which naturally prevents it from generating proposals in those categories.

---

## Analyst guardrail state header

**Purpose:** Give the analyst enough constraint context to self-filter recommendations — don't propose what can't be executed, and don't re-propose trades already expressed in the book.

**Token budget target:** 250–500 tokens (175–300 for the primary portfolio with features disabled).

**Format:** A structured text block at the top of the analyst's context window, before the synthesizer output.

**Fields:**

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
Regime: {label} [CHANGED since last invocation | unchanged]

Capital:
  Available for new positions: ${amount} ({pct}% of portfolio)
  Per-position max size: ${amount} ({pct}% of portfolio, {regime} regime)

Sector headroom (delta-adjusted):
  Tech:       {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Semis:      {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Financials: {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Energy:     {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]

Directional headroom:
  Net long:  {current}% / {limit}% — room: {remaining}%
  Net short: {current}% / {limit}% — room: {remaining}%
  Gross:     {current}% / {limit}% — room: {remaining}%

Options headroom:                          [omitted if options_enabled: false]
  Delta exposure: {current}% / {limit}% — room: {remaining}%
  Theta:          {current}% / {limit}%/day
  Vega:           {current}% / {limit}%/pt

Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce):
  {TICKER}  {direction}  {pct}%  {sector}
  ...
  [or "None" if the book is empty]

Abandoned openings from prior invocation (decide on current grounds whether to re-propose):
  {ENV-REC-n}  {direction} {TICKER} {asset_type}  {size}%  — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no OPEN commands were abandoned in the prior invocation]

Hard blocks (do NOT recommend):
  {list of any rules at ≥95% consumption, with specific constraint}
  e.g., "Semis sector at 24.2% / 25.0% limit — no new semi longs"
  [if options_enabled: false] "Options: DISABLED for this portfolio"
  [if short_selling_enabled: false] "Short selling: DISABLED for this portfolio"
===
```

**What's excluded from the analyst header:** P/L trajectories, drawdown state, full thesis records, breach history, position-level max loss proximity, and activity log. The held-positions block is the only position-level data the analyst sees — and it is intentionally thin (ticker + direction + size + sector) to enable dedup against candidate proposals without inviting the analyst to reason about existing thesis health. That reasoning is the strategist's domain.

**Held positions section:** One compact line per open position — ticker, direction, % of portfolio, sector. Purpose is dedup: the analyst should not re-propose a trade already expressed in the book at meaningful size, because the strategist is the agent responsible for add/hold/reduce on existing positions and will evaluate whether the thesis supports increasing exposure. Opposite-direction proposals (e.g., a new short thesis on a held long) are new information and should be surfaced — the strategist's thesis-status assessment will reconcile them against the existing thesis. The block intentionally excludes thesis summaries, P/L, and entry rationale to keep the analyst in its opportunity-identification role rather than reasoning about existing thesis health. On the primary portfolio with short selling disabled, entries will always be `long`; the direction column is retained for format consistency with the full-system portfolio.

**Hard block section:** When any rule is at ≥95% consumption, the constraint is called out explicitly in a "do NOT recommend" block. This is the strongest self-constraint signal — the analyst should not produce proposals in the blocked direction regardless of thesis quality, because the engine will reject them. Disabled features are also listed here as permanent hard blocks so the analyst's instruction set is unambiguous.

**Abandoned openings section:** OPEN commands that were approved by the PM in the prior invocation but failed to reach the broker within the retry window (per [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md)) are surfaced to the analyst here. Scoped to the prior invocation only — older abandonments are stale by construction and are not re-surfaced. The analyst's responsibility is not to mechanically re-propose these: each entry is a prompt to evaluate, on current market data, whether the original thesis still holds. If it does and the setup is still attractive, a new recommendation (new `REC-n`, new thesis narrative grounded in current signals) may be produced; if not, the entry lapses. The analyst treats these like any other candidate opportunity — the inclusion threshold and fresh-grounds reasoning requirements in [analyst.md](../04-decision-layer/analyst.md) apply unchanged. The section exists so that legitimately missed opportunities are explicitly reconsidered rather than silently disappearing, not to create a retry obligation.

---

## Strategist guardrail state header

**Purpose:** Give the strategist guardrail context for reasoning about which existing positions to recommend trimming, closing, or adding to.

**Token budget target:** 300–600 tokens (200–400 for the primary portfolio).

**Format:** Structured text block opening with the same `=== GUARDRAIL STATE ===` envelope as the analyst header, with the strategist-specific field set below. Differences from the analyst header: the held-positions dedup block is omitted (the strategist reasons about positions through the position-level constraint proximity block), and additional position-level and drawdown sections are present.

**Fields:**

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
Regime: {label} [CHANGED since last invocation | unchanged]

Capital:
  Available for new positions: ${amount} ({pct}% of portfolio)
  Per-position max size: ${amount} ({pct}% of portfolio, {regime} regime)

Sector headroom (delta-adjusted):
  Tech:       {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Semis:      {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Financials: {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Energy:     {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]

Directional headroom:
  Net long:  {current}% / {limit}% — room: {remaining}%
  Net short: {current}% / {limit}% — room: {remaining}%
  Gross:     {current}% / {limit}% — room: {remaining}%

Options headroom:                          [omitted if options_enabled: false]
  Delta exposure: {current}% / {limit}% — room: {remaining}%
  Theta:          {current}% / {limit}%/day
  Vega:           {current}% / {limit}%/pt

Position-level constraint proximity:
  POS-NVDA-001: 4.2% of portfolio (max 5.0%) — P/L: -18% of cost (max loss: -30%) [⚠ WARNING]
  POS-AMD-002:  2.1% of portfolio (max 5.0%) — P/L: +5% of cost
  POS-JPM-003:  3.8% of portfolio (max 5.0%) — P/L: -2% of cost
  ...

Sector exposure breakdown (per position):
  Tech (18.3% / 25.0%):
    POS-NVDA-001: 4.2% (delta-adj)
    POS-AAPL-004: 3.1% (delta-adj)
    POS-MSFT-005: 2.8% (delta-adj) [options, delta 0.45]   [omitted if options_enabled: false]
    ...
  Semis (12.1% / 25.0%):
    ...

Drawdown state:
  Daily:      {current}% / {limit}% [{zone}]
  Cumulative: {current}% / {limit}% [{zone}]
  [If cumulative drawdown response active: current tier and restrictions in effect]

Regime-transition breaches (if any):
  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%
  ...

Abandoned openings from prior invocation (portfolio awareness; analyst owns re-evaluation):
  {ENV-REC-n}  {direction} {TICKER} {asset_type}  {size}%  — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no OPEN commands were abandoned in the prior invocation]

Abandoned position actions from prior invocation (decide on current grounds whether to re-propose):
  {ENV-SA-n}: {ADD|ADJUST|CLOSE} on {POS-ID} — abandoned at {timestamp} ({failure_reason})
  {ENV-SA-ORD-n}: {CANCEL|modify} on {ORD-ID} — abandoned at {timestamp} ({failure_reason})
  ...
  [or "None" if no position-action commands were abandoned in the prior invocation]

Hard blocks (do NOT recommend):
  {list of any rules at ≥95% consumption, with specific constraint}
  [if options_enabled: false] "Options: DISABLED for this portfolio"
  [if short_selling_enabled: false] "Short selling: DISABLED for this portfolio"
===
```

**Why the strategist needs position-level detail:** The strategist's job is to recommend actions on existing positions. To recommend intelligently, it needs to know which positions are nearest their constraint boundaries, which sectors have room for additions vs. are at capacity, and which positions are contributing to any drawdown.

**Abandoned position actions section:** ADD, ADJUST, CLOSE, and CANCEL commands that were approved by the PM in the prior invocation but failed to reach the broker within the retry window (per [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md)) are surfaced to the strategist here. Scoped to the prior invocation only; older abandonments are stale by construction. The strategist's responsibility is not to mechanically re-propose: each entry is a prompt to evaluate, on current market data and current position state, whether the original intent still holds. If it does, a new per-position assessment (new `SA-n`) or pending-order assessment (new `SA-ORD-n`) may be produced; if not, the entry lapses. The usual action-decision rigor in [strategist.md](../04-decision-layer/strategist.md) applies unchanged. A CLOSE abandonment deserves particular attention — an intended risk-reducing exit that did not execute is the most operationally consequential failure mode; the strategist should prioritize re-evaluating it against current position and market state. The abandoned-openings section is also surfaced as portfolio-awareness context; responsibility for re-proposing those lies with the analyst.

**Strategist remedy responsibility:** When regime-transition breaches are present, the strategist is responsible for proposing specific remedies for each breaching position — trim to compliance (with quantity), close entirely, or hold with thesis-based rationale. These remedy proposals are passed to the PM for review and execution. The strategist's position-level thesis context (target proximity, conviction, catalyst timing) makes it the right agent to reason about *which* positions to reduce, while the PM handles the cross-constraint interactions and final execution decisions.

---

## Portfolio manager guardrail state header

**Purpose:** Full constraint visibility for final sizing decisions and cross-proposal evaluation.

**Token budget target:** 400–800 tokens (300–500 for the primary portfolio).

**Format:** Structured text block opening with the same `=== GUARDRAIL STATE ===` envelope as the analyst and strategist headers, with the PM-specific field set below. Differences from the strategist header: the slim held-positions dedup block is omitted (the position-level constraint proximity block carries strictly more information and the PM does not propose new entries that would need dedup); abandoned-action sections are omitted (the originating agent owns re-evaluation, not the PM); and the PM-specific fields below are added.

**Fields:**

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
Regime: {label} [CHANGED since last invocation | unchanged]

Capital:
  Available for new positions: ${amount} ({pct}% of portfolio)
  Per-position max size: ${amount} ({pct}% of portfolio, {regime} regime)

Sector headroom (delta-adjusted):
  Tech:       {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Semis:      {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Financials: {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]
  Energy:     {current}% / {limit}% — room: {remaining}% [NORMAL|⚠ WARNING|🔴 CRITICAL|BLOCKED]

Directional headroom:
  Net long:  {current}% / {limit}% — room: {remaining}%
  Net short: {current}% / {limit}% — room: {remaining}%
  Gross:     {current}% / {limit}% — room: {remaining}%

Options headroom:                          [omitted if options_enabled: false]
  Delta exposure: {current}% / {limit}% — room: {remaining}%
  Theta:          {current}% / {limit}%/day
  Vega:           {current}% / {limit}%/pt

Position-level constraint proximity:
  POS-NVDA-001: 4.2% of portfolio (max 5.0%) — P/L: -18% of cost (max loss: -30%) [⚠ WARNING]
  POS-AMD-002:  2.1% of portfolio (max 5.0%) — P/L: +5% of cost
  ...

Sector exposure breakdown (per position):
  Tech (18.3% / 25.0%):
    POS-NVDA-001: 4.2% (delta-adj)
    POS-AAPL-004: 3.1% (delta-adj)
    ...
  Semis (12.1% / 25.0%):
    ...

Cross-constraint impact summary:
  If all pending proposals are approved as-sized:
    Sector tech: 18.3% → 22.1% (within limit)
    Net long:    42.0% → 48.5% (within limit)
    Gross:       78.0% → 84.5% (within limit)
    Capital:     ${available} → ${remaining}
  [Flagged constraints: any rule that would enter WARNING or CRITICAL zone]

Guardrail validation tool available:
  Call validate_guardrail(instrument, direction, size) to check any proposed modification.
  Tool tracks cumulative impact across multiple checks within this invocation.

Drawdown context:
  Daily P/L:     {current}% ({zone})
  Daily limit:   {limit}% — headroom: {remaining}%
  Cumulative:    {current}% from HWM ({zone})
  [If in reduced mode: current restrictions and tier]

Regime-transition breaches (if any):
  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%
  ...

Recent engine-originated actions (since last invocation):
  {timestamp}: Engine closed POS-XYZ-001 — reason: position-level max loss (-31% of cost)
  {timestamp}: Engine trimmed POS-ABC-002 — reason: single short size limit (3.2% > 3.0%)
  ...
  [or "None" if no engine-originated actions]

Active regime overrides:
  [If pre-event tightening active: event name, adjusted limits, expiration]
  [If stress overlay active: trigger, adjusted limits]
  [or "None" if no overrides active]

Correlation state:                         [omitted if < 3 concurrent positions]
  Portfolio weighted avg correlation: {value} / {limit} [{zone}]
  Highest pairwise: POS-X ↔ POS-Y = {value}

Hard blocks (do NOT issue commands violating):
  {list of any rules at ≥95% consumption, with specific constraint}
  [if options_enabled: false] "Options: DISABLED for this portfolio"
  [if short_selling_enabled: false] "Short selling: DISABLED for this portfolio"
===
```

**Cross-constraint impact summary:** This is the Phase 0 gap item — showing how approving one proposal affects headroom for subsequent proposals. The PM needs this to make sequencing and prioritization decisions. The summary is pre-computed by the proposal pre-processor based on the combined set of analyst + strategist proposals, not the PM's modifications (which are validated via the guardrail tool as the PM makes them).

**Strategist remedy proposals:** When regime-transition breaches or market-movement breaches are present, the strategist's proposed remedies for each breaching position (trim with quantity, close, or hold with rationale) arrive in the proposal pre-processor bundle as integrated per-position assessments with `remedy_flag` set. The PM reviews these against the cross-constraint impact summary above — for example, a strategist recommendation to close a short position might create a net long exposure breach visible in the cross-constraint view. The PM adjusts, reorders, or overrides the strategist's proposals as needed before executing via command envelopes.

**Recent engine-originated actions:** A guardrail-state spotlight on activity-log events the PM should not miss. The full activity log is delivered as part of [portfolio state §5](../01-data-layer/internal/portfolio-state.md) — this section is a redundant prominence cue, not the PM's only access to between-invocation events.

---

## Halt-mode header modifications

When daily drawdown halt or cumulative drawdown full halt (tier 3, 12%+) is active, each agent's guardrail state header is modified to reflect the restricted operating mode. The upstream pipeline (data ingestion, distillation, research agents) runs normally — its output feeds into the modified agent contexts. See [breach-behavior.md](breach-behavior.md#agent-behavior-during-halt-mode) for the behavioral contracts.

### Analyst — watchlist mode

The analyst's guardrail state header changes to indicate halt:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: WATCHLIST ONLY — do not generate trade proposals

Capital:
  New positions: BLOCKED (halt active)
  ...
```

The analyst's instruction set shifts from "generate proposals for opportunities the system should act on" to "generate a compact watchlist of opportunities worth revisiting post-halt." The watchlist format is lighter than a full proposal — ticker, thesis summary, estimated conviction — omitting sizing, entry parameters, and bracket configuration. This preserves analytical signal at reduced token cost.

### Strategist — defensive posture mode

The strategist's guardrail state header adds the halt flag, and the instruction framing shifts:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions

Position-level constraint proximity:
  ...
```

The strategist still receives the full distillation and research output, which informs its reasoning about whether current holdings' theses are intact under the market conditions that triggered the halt. Its position assessments during halt emphasize: thesis deterioration signals, stop-tightening candidates, and close recommendations for positions with weakened risk/reward.

### PM — risk reduction mode

The PM's guardrail state header makes the restricted action space explicit:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Available actions: CLOSE, ADJUST, CANCEL only
Blocked actions: OPEN, ADD

Pending orders review:
  {list of any limit orders placed before halt, with current distance from fill price}
  ...
```

The PM's instruction set restricts to: reviewing and cancelling pre-halt pending orders, executing the strategist's defensive recommendations, and tightening stops on deteriorating positions. The cross-constraint impact summary is still provided but only for risk-reducing actions.

---

## Emergency invocation header

When the continuous monitor triggers an emergency pipeline invocation (see [breach-behavior.md](breach-behavior.md#emergency-invocation-trigger)), all agent guardrail state headers include an emergency header block before the normal guardrail state:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** EMERGENCY INVOCATION — trigger: {trigger_type} **
Trigger detail: {e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)"}
Time since last invocation: {minutes}m (normal cadence: ~120m)
```

The emergency flag shifts agent priorities toward breach resolution. The analyst should minimize new proposal generation and focus on reassessing existing theses in light of the triggering event. The strategist should prioritize regime-transition breach remedies and defensive position assessment. The PM should prioritize executing the strategist's recommendations and addressing any engine-originated actions that occurred between invocations.

Note: Emergency invocations and halt mode are independent states that can co-occur. If an emergency invocation fires *and* halt mode is active, agents receive both the emergency header and the halt-mode context modifications. The halt-mode restrictions (no OPEN/ADD) take precedence over normal emergency behavior.

---

## Guardrail validation tool

A deterministic tool callable by the analyst, strategist, and portfolio manager during their reasoning. This is the primary mechanism for ensuring proposals are guardrail-compliant before they reach execution.

### Input contract

```
validate_guardrail(
  instrument: {ticker, asset_type, direction, [options params if applicable]},
  size: {quantity, dollar_value, [premium_at_risk if options]},
  action: "OPEN" | "ADD" | "CLOSE" | "ADJUST"
)
```

For the primary portfolio, the tool immediately returns FAIL with reason `feature_disabled` for any instrument with `asset_type: option` or `direction: short`, without performing further validation.

### Output contract

```
{
  overall: "PASS" | "FAIL",
  per_rule: [
    {
      rule: "sector_concentration",
      status: "PASS" | "FAIL" | "WARNING",
      current: 18.3,
      limit: 25.0,
      projected_after: 21.5,
      headroom_remaining: 3.5,
      unit: "% of portfolio (delta-adjusted)"
    },
    ...
  ],
  delta_adjusted_exposure: {computed delta-adj value for this instrument},
  greeks: {delta, gamma, theta, vega},           // options/strategies only
  cumulative_impact_note: "This is proposal #N in this invocation. Cumulative impact of proposals #1–{N-1} is included in headroom calculations.",
  failure_guidance: "Reduce size by 15% to pass sector concentration"   // on FAIL only
}
```

### Behavioral contract

**Cumulative tracking:** The tool maintains state across multiple calls within a single agent invocation. When the analyst validates proposal #2, the headroom already accounts for proposal #1's projected impact. This prevents the common failure of N individually-compliant proposals that collectively breach a limit.

**State reset:** Cumulative tracking resets at the boundary between agent invocations (analyst → strategist → PM each start fresh). The proposal pre-processor handles cross-agent cumulative analysis.

**Shared math:** The tool's per-rule projection, delta-adjusted exposure computation, regime parameter resolution, and feature-flag early-exit are all the [guardrail-evaluation library](guardrail-evaluation.md). The `per_rule[]` item shape above is the library's canonical output object. A proposal passing the tool will also pass the engine's T3 check, barring state drift.

**Failure guidance:** On FAIL, the output includes a suggestion for the minimum adjustment needed to achieve compliance (e.g., "reduce size by 15% to pass sector concentration" or "switch to a lower-delta strike to reduce delta-adjusted exposure").

---

## Portfolio state ingestion payload

How guardrail state flows into the portfolio state specification at each invocation.

### Source of truth

The guardrail state is computed by the guardrail enforcement layer at the start of Phase 1 (after fill collection), using current position data from the OMS database, current market prices from the data pipeline, and the active regime parameters.

### Integration with portfolio state categories

| Portfolio state category | Guardrail data delivered |
|---|---|
| **4c: Risk budget consumption** | Per-rule current value, limit, headroom, and zone (normal/warning/critical/blocked) for all T1/T2/T3 rules. This is the authoritative read interface for guardrail state. |
| **4d: Active risk parameter set** | Active regime label, all current limit values under the active regime, parameter change flag (boolean — true if any parameter changed since last invocation), and the transition state (stable / tightening / loosening with invocations remaining). |

### Freshness guarantee

Guardrail state is computed once per invocation, at the start of Phase 1. All downstream consumers — guardrail state headers, validation tool, analysis pipeline — see the same snapshot. The execution layer performs a final authoritative check at command submission time against live state (which may have drifted), closing the freshness gap.

---

## Cross-constraint impact visibility

A key gap identified in Phase 0: the system must show how curing one guardrail breach affects other constraints, not just per-rule headroom in isolation.

### Where this surfaces

**In the PM guardrail state header:** The cross-constraint impact summary (above) shows the projected state across all rules if all pending proposals are approved. This is the primary cross-constraint visibility mechanism.

**In the guardrail validation tool:** Each validation check returns per-rule projected headroom after the proposed trade, including the cumulative impact of prior proposals. The PM can see, for example, that approving a tech long would leave sector headroom at 1.5% while pushing gross exposure into the warning zone.

**In breach response context:** When the engine reports a secondary breach that would result from a forced reduction (see [breach-behavior.md](breach-behavior.md)), the conflict between the primary breach and the secondary constraint is surfaced explicitly.

### Cross-constraint scenarios the system must handle

1. **Sector trim → directional exposure breach:** Closing a short in a breaching sector could push net long exposure beyond its limit. The PM needs to see this before deciding how to cure the sector breach.
2. **Drawdown response → concentration breach:** Closing the biggest losing position (to cure a drawdown breach) might leave the portfolio over-concentrated in remaining sectors. The engine's secondary breach check catches this; the PM sees the tradeoff.
3. **Regime tightening → multiple simultaneous breaches:** When 3–4 rules tighten simultaneously, the PM needs to see the aggregate overage and plan a coordinated response, not address each breach in isolation.
4. **Options expiration → exposure cliff:** When options positions expire or are closed, delta-adjusted exposure can drop suddenly, potentially creating room for new positions but also changing the directional exposure balance. The PM needs to see the post-expiration projected state. (Full-system portfolio only.)

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — defines what's being reported, including dual portfolio profiles and feature flags
- [Regime adaptation](regime-adaptation.md) — the active regime and parameter set are part of the delivered state
- [Breach behavior](breach-behavior.md) — escalation zones and breach flags are delivered in guardrail state headers
- [Analyst](../04-decision-layer/analyst.md) — consumer of the analyst guardrail state header and the guardrail validation tool
- [Strategist](../04-decision-layer/strategist.md) — consumer of the strategist guardrail state header and the guardrail validation tool
- [Portfolio manager](../04-decision-layer/portfolio-manager.md) — consumer of the PM guardrail state header and the guardrail validation tool; receives synchronous rejection feedback from the execution layer
- [Portfolio state](../01-data-layer/internal/README.md) — the read interface that surfaces guardrail data to the broader system
- [Guardrail-evaluation library](guardrail-evaluation.md) — provides the deterministic math (Black-Scholes greeks, conservative buffer, per-rule projection, regime parameter resolution, feature-flag early-exit) that backs the validation tool
