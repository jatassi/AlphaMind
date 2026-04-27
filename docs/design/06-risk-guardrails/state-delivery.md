# Guardrail state delivery

How current guardrail state is packaged for each consumer. Granularity varies: analyst gets a summary; PM gets detailed headroom and proximity; the engine layer operates on the authoritative state.

This doc owns the **guardrail state header** — a formatted text block bounded by `=== GUARDRAIL STATE ===` markers that opens each agent's prompt. One component of the agent's input bundle (the *context package*), alongside the synthesizer brief, portfolio state, and proposal pre-processor bundle. See [analyst.md](../04-decision-layer/analyst.md), [strategist.md](../04-decision-layer/strategist.md), and [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the full per-agent input contracts.

---

## Design principles

**Snapshot consistency.** All headers in an invocation use the same snapshot, computed once at Phase 1 start (after fill collection). Analyst, strategist, PM, and validation tool operate on the same numbers; the execution layer's final check catches drift.

**Token efficiency.** Structured text blocks, not raw JSON. Field names abbreviated, zero-headroom rules highlighted, normal-headroom rules compressed.

**Progressive detail.** Analyst gets the least; strategist gets position-level; PM gets the most.

**Feature-flag aware.** Disabled features (e.g., options and shorts in the primary portfolio — see [dual portfolio profiles](rules-and-limits.md#dual-portfolio-profiles)) omit corresponding sections entirely. Those fields don't exist in the primary analyst's header, naturally preventing proposals in those categories.

---

## Analyst guardrail state header

**Purpose:** Constraint context for self-filtering — don't propose what can't execute, don't re-propose trades already in the book.

**Token budget:** 250–500 tokens (175–300 for the primary portfolio with features disabled).

**Format:** Structured text block at the top of the analyst's context window, before the synthesizer output.

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

**Excluded from the analyst header:** P/L trajectories, drawdown state, full thesis records, breach history, position-level max-loss proximity, activity log. The held-positions block is intentionally thin (ticker + direction + size + sector) — enough for dedup, not enough to invite reasoning about existing thesis health (strategist's domain).

**Held positions section:** One compact line per position for dedup — the analyst doesn't re-propose trades already in the book; the strategist owns add/hold/reduce. Opposite-direction proposals on held names (e.g., short on a held long) are new information; the strategist's thesis-status assessment reconciles them. On the primary portfolio with shorts disabled, entries are always `long`; the direction column is retained for format consistency.

**Hard block section:** When any rule is at ≥95% consumption, the constraint is called out in a "do NOT recommend" block. Engine will reject proposals in the blocked direction regardless of thesis quality. Disabled features appear as permanent hard blocks.

**Abandoned openings section:** OPEN commands the PM approved in the prior invocation but that failed to reach the broker within the retry window (per [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md)). Scoped to the prior invocation only — older abandonments are stale. Each entry prompts evaluation against current market data; if the thesis holds, a new `REC-n` with fresh narrative may be produced. The inclusion threshold and fresh-grounds reasoning requirements in [analyst.md](../04-decision-layer/analyst.md) apply unchanged. Exists so legitimately missed opportunities are explicitly reconsidered, not as a retry obligation.

---

## Strategist guardrail state header

**Purpose:** Constraint context for reasoning about which existing positions to trim, close, or add to.

**Token budget:** 300–600 tokens (200–400 for the primary portfolio).

**Format:** Structured text block opening with the same `=== GUARDRAIL STATE ===` envelope. Differs from the analyst header: held-positions dedup block omitted (strategist reasons via the position-level constraint proximity block); additional position-level and drawdown sections present.

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

**Why position-level detail:** To recommend intelligently, the strategist needs to know which positions are nearest constraint boundaries, which sectors have room vs. capacity, and which positions contribute to drawdown.

**Abandoned position actions section:** ADD, ADJUST, CLOSE, CANCEL commands the PM approved in the prior invocation but failed at broker submission (per [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md)). Scoped to the prior invocation only. Each entry prompts evaluation against current data; if intent holds, a new `SA-n` or `SA-ORD-n` may be produced. Action-decision rigor in [strategist.md](../04-decision-layer/strategist.md) applies unchanged. A CLOSE abandonment deserves particular attention — an intended risk-reducing exit that didn't execute is the most operationally consequential failure mode. The abandoned-openings section is portfolio-awareness context; the analyst owns re-proposing those.

**Strategist remedy responsibility:** When regime-transition breaches are present, the strategist proposes specific remedies for each breaching position — trim to compliance (with quantity), close, or hold with thesis-based rationale. Position-level thesis context (target proximity, conviction, catalyst timing) makes the strategist the right agent to decide *which* positions to reduce; the PM handles cross-constraint interactions and execution.

---

## Portfolio manager guardrail state header

**Purpose:** Full constraint visibility for final sizing and cross-proposal evaluation.

**Token budget:** 400–800 tokens (300–500 for the primary portfolio).

**Format:** Structured text block, same envelope. Differs from the strategist header: held-positions dedup block omitted (position-level constraint proximity block carries strictly more information; PM doesn't propose new entries needing dedup); abandoned-action sections omitted (originating agent owns re-evaluation); PM-specific fields added.

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

Dependency risk flag:                       [omitted if < 3 concurrent positions]
  Max catalyst-failure exposure: {value}% / {limit}% [{zone}]
  Effective independent thesis count: {n}
  Worst shared catalyst: "{label}" — positions: {POS-A, POS-B, ...}

Hard blocks (do NOT issue commands violating):
  {list of any rules at ≥95% consumption, with specific constraint}
  [if options_enabled: false] "Options: DISABLED for this portfolio"
  [if short_selling_enabled: false] "Short selling: DISABLED for this portfolio"
===
```

**Cross-constraint impact summary:** Shows how approving one proposal affects headroom for others. Pre-computed by the proposal pre-processor over the combined analyst + strategist set; PM modifications are validated separately via the guardrail tool.

**Strategist remedy proposals:** When breaches are present, the strategist's per-position remedies (trim with quantity, close, or hold with rationale) arrive in the pre-processor bundle as per-position assessments with `remedy_flag` set. PM reviews against the cross-constraint summary — closing a short might create a net long breach visible in the cross-constraint view. PM adjusts, reorders, or overrides before executing.

**Recent engine-originated actions:** Prominence cue for activity-log events the PM should not miss. Full activity log in [portfolio state §5](../01-data-layer/internal/portfolio-state.md); this section spotlights.

---

## Halt-mode header modifications

When daily drawdown halt or cumulative drawdown full halt (tier 3, 12%+) is active, each agent's header reflects the restricted mode. Upstream pipeline runs normally; its output feeds modified agent contexts. See [breach-behavior.md](breach-behavior.md#agent-behavior-during-halt-mode) for behavioral contracts.

### Analyst — watchlist mode

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: WATCHLIST ONLY — do not generate trade proposals

Capital:
  New positions: BLOCKED (halt active)
  ...
```

Generates a compact watchlist for post-halt — ticker, thesis summary, estimated conviction. Omits sizing, entry parameters, bracket config.

### Strategist — defensive posture mode

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions

Position-level constraint proximity:
  ...
```

Strategist receives full distillation and research output to assess whether current theses hold under halt conditions. Position assessments emphasize thesis deterioration, stop-tightening candidates, and closes for positions with weakened risk/reward.

### PM — risk reduction mode

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
Available actions: CLOSE, ADJUST, CANCEL only
Blocked actions: OPEN, ADD

Pending orders review:
  {list of any limit orders placed before halt, with current distance from fill price}
  ...
```

PM restricted to reviewing/cancelling pre-halt pending orders, executing defensive recommendations, tightening stops on deteriorating positions. Cross-constraint impact summary remains, scoped to risk-reducing actions.

---

## Emergency invocation header

When the continuous monitor triggers an emergency invocation (see [breach-behavior.md](breach-behavior.md#emergency-invocation-trigger)), all headers include an emergency block before the normal state:

```
=== GUARDRAIL STATE (invocation {id}, {timestamp}) ===
** EMERGENCY INVOCATION — trigger: {trigger_type} **
Trigger detail: {e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)"}
Time since last invocation: {minutes}m (normal cadence: ~120m)
```

The flag shifts priorities toward breach resolution: analyst minimizes new proposal generation; strategist prioritizes regime-transition remedies and defensive assessment; PM prioritizes executing recommendations and addressing engine-originated actions.

Emergency invocations and halt mode are independent states that can co-occur. When both are active, agents receive both header blocks; halt-mode restrictions (no OPEN/ADD) take precedence.

---

## Guardrail validation tool

Deterministic tool callable by analyst, strategist, and PM during reasoning. Primary mechanism for ensuring proposals are compliant before execution.

### Input contract

```
validate_guardrail(
  instrument: {ticker, asset_type, direction, [options params if applicable]},
  size: {quantity, dollar_value, [premium_at_risk if options]},
  action: "OPEN" | "ADD" | "CLOSE" | "ADJUST"
)
```

For the primary portfolio, immediately returns FAIL with reason `feature_disabled` for any instrument with `asset_type: option` or `direction: short`.

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

**Cumulative tracking:** State persists across calls within a single agent invocation. When the analyst validates proposal #2, headroom accounts for proposal #1's projected impact — preventing N individually-compliant proposals that collectively breach.

**State reset:** Tracking resets at agent-invocation boundaries (analyst → strategist → PM each start fresh). The proposal pre-processor handles cross-agent cumulative analysis.

**Shared math:** Per-rule projection, delta-adjusted exposure, regime parameter resolution, and feature-flag early-exit live in the [guardrail-evaluation library](guardrail-evaluation.md). The `per_rule[]` shape is the library's canonical output. A proposal passing the tool also passes T3, barring state drift.

**Failure guidance:** On FAIL, output includes the minimum adjustment for compliance (e.g., "reduce size by 15%" or "switch to a lower-delta strike").

---

## Portfolio state ingestion payload

### Source of truth

Guardrail state is computed by the enforcement layer at the start of Phase 1 (after fill collection), using OMS position data, market prices, and active regime parameters.

### Integration with portfolio state categories

| Portfolio state category | Guardrail data delivered |
|---|---|
| **4c: Risk budget consumption** | Per-rule current value, limit, headroom, and zone (normal/warning/critical/blocked) for all T1/T2/T3 rules. Authoritative read interface for guardrail state. |
| **4d: Active risk parameter set** | Active regime label, all current limit values, parameter change flag (true if any parameter changed since last invocation), and transition state (stable / tightening / loosening with invocations remaining). |

### Freshness guarantee

State is computed once per invocation at Phase 1 start. All downstream consumers see the same snapshot. The execution layer's final check at submission closes the freshness gap against drifted live state.

---

## Cross-constraint impact visibility

The system shows how curing one breach affects other constraints, not just per-rule headroom in isolation.

### Where this surfaces

**PM state header:** Cross-constraint impact summary shows projected state across all rules if all pending proposals are approved. Primary cross-constraint mechanism.

**Validation tool:** Each check returns projected per-rule headroom after the proposed trade, including cumulative impact of prior proposals. PM sees, e.g., that a tech long would leave sector headroom at 1.5% while pushing gross into warning.

**Breach response context:** When the engine reports a secondary breach from a forced reduction (see [breach-behavior.md](breach-behavior.md)), the primary/secondary conflict surfaces explicitly.

### Cross-constraint scenarios

1. **Sector trim → directional breach:** Closing a short in a breaching sector could push net long beyond limit.
2. **Drawdown response → concentration breach:** Closing the biggest loser may over-concentrate remaining sectors. The engine's secondary check catches it; the PM sees the tradeoff.
3. **Regime tightening → multiple simultaneous breaches:** When 3–4 rules tighten at once, the PM sees aggregate overage and plans a coordinated response.
4. **Options expiration → exposure cliff:** Expirations or closes can drop delta-adjusted exposure suddenly, changing directional balance. PM sees post-expiration projected state. (Full-system portfolio only.)

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
