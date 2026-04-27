# Analyst

Operates in a fresh context window. **Mandate:** given current conditions, identify the highest-conviction asymmetric setups with a 4–72 hour time horizon. Focus is exclusively new trade opportunities; existing-position management is the [strategist's](strategist.md) job, run in parallel.

---

## Inputs

The analyst's input bundle is delivered at invocation start. Source documents in parentheses are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Synthesizer brief | [synthesizer.md](../03-analysis-layer/synthesizer.md) | Prose synthesis with embedded `[SA-*]`, `[QR-*]`, `[AR-*]`, `[CR-*]` references — the primary market context for opportunity identification |
| Analyst guardrail state header | [state-delivery.md — Analyst guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) | Formatted text block at the top of the prompt: regime label, available capital, per-sector delta-adjusted headroom, directional/gross headroom, per-position size limits, options/short feature flags, hard blocks |
| Held positions block | [state-delivery.md — Analyst guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) (held-positions section) | Slim per-position dedup line (ticker, direction, % of portfolio, sector). Used by the [non-overlap filter](#non-overlap-filter) — the analyst does not receive thesis records or P/L for held positions |
| Abandoned openings block | [state-delivery.md — Analyst guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) (abandoned-openings section) | Prior-invocation OPEN commands that failed to reach the broker — surfaced for re-evaluation on current grounds, not as a retry obligation. See [Abandoned openings from prior invocation](#abandoned-openings-from-prior-invocation) |

Tools available during reasoning:

| Tool | Source | Use |
|---|---|---|
| Source-brief retrieval | [decision-layer overview — Information flow](README.md#information-flow) | Pull a section of an analysis brief by reference ID from the synthesizer's retrieval store. See [Source brief retrieval](#source-brief-retrieval) |
| Guardrail validation tool | [state-delivery.md — Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) | Deterministic pre-submission check. Tracks cumulative impact across multiple proposals within the invocation. See [Pre-submission guardrail validation](#pre-submission-guardrail-validation) |

The volatility regime label is delivered as the `Regime:` line in the guardrail state header (not as a separate broadcast).

---

## Output

The output is a single document conforming to the [analyst output schema](analyst-output-schema.md) (formal JSON Schema, Draft 2020-12). The schema is the authoritative contract; the field lists below are the readable reference. Each trade recommendation is a structured record with **structured** fields (machine-parseable, consumed by the [proposal pre-processor](proposal-pre-processor.md) and execution layer) and **narrative** fields (free-text reasoning, consumed by the [portfolio manager](portfolio-manager.md) for thesis quality evaluation). Structured fields enable deterministic processing; narrative fields give the PM reasoning context.

Mode is `normal` (recommendations) under standard conditions and `watchlist` (lighter-weight entries, no sizing or bracket detail) under halt mode — see [state-delivery.md — Analyst — watchlist mode](../06-risk-guardrails/state-delivery.md#analyst--watchlist-mode).

**Structured fields:**

- **Recommendation ID:** unique identifier within the invocation (e.g., `REC-1`, `REC-2`)
- **Instrument:** ticker, asset type (equity, option, strategy), direction (long/short). For options: underlying, strike, expiration, contract type. For strategies: full leg specification
- **Underlying:** the root ticker (e.g., `NVDA` whether the instrument is NVDA equity or an NVDA option) — used by the pre-processor for same-name conflict detection and sector classification
- **Sector:** which sector this instrument belongs to (tech, semis, financials, energy)
- **Conviction level:** integer 1–5 per the [conviction scale](#conviction-scale) below
- **Entry order:** order type (market, limit, stop-limit) and price parameters
- **Position size:** quantity (shares or contracts), dollar value, and percentage of portfolio. For defined-risk instruments: premium at risk. For all instruments: estimated delta-adjusted exposure (populated by the guardrail validation tool at pre-submission check time)
- **Target:** target price and target dollar P/L
- **Invalidation legs:** each with type (price, time, event), condition, and order parameters for mechanical enforcement
- **Entry window** (optional): deadline (ISO 8601), decay type (binary/gradual) — see [entry window](#entry-window) below
- **Time expectation:** expected resolution horizon in hours
- **Guardrail validation result:** pass/fail summary from the pre-submission check, including delta-adjusted exposure computed at validation time and per-rule headroom after the trade

**Narrative fields:**

- **Thesis narrative:** the specific reasoning for the trade — which qualitative + quantitative signals support it, how they converge, the causal chain from catalyst to price movement. Includes source references (e.g., `[SA-TECH-3]`, `[QR-2]`)
- **Target rationale:** why this exit level, and what catalyst should drive price there
- **Invalidation rationale:** for each invalidation leg, why this specific condition indicates the thesis is wrong
- **Position size rationale:** how the sizing maps to conviction level and the advisory band, accounting for the instrument's risk profile
- **Entry window rationale** (when entry window is present): why the window exists and what happens after it closes
- **Counterarguments acknowledged:** the strongest case against this trade, and why the analyst proceeds despite it

---

## Conviction scale

Every recommendation includes a conviction level from 1 to 5, defined by signal characteristics — quality and convergence of evidence — not by expected return magnitude. Each level maps to an advisory sizing band expressed as a percentage of portfolio value.

| Level | Label | Signal criteria | Advisory sizing band |
|-------|-------|----------------|---------------------|
| 1 | **speculative** | Single signal source, no corroboration. Thesis is plausible but relies on one data point or an untested pattern. | 0.25–0.75% |
| 2 | **low** | Two signal sources align, but at least one significant unresolved contradiction or uncertainty flagged by the synthesizer. | 0.5–1.5% |
| 3 | **moderate** | Multiple independent signal sources converge. Contradictions exist but are acknowledged and the thesis accounts for them. Catalyst timeline is identifiable but may be imprecise. | 1–3% |
| 4 | **high** | Strong convergence across three or more independent signal types. No unresolved contradictions. Catalyst has a concrete, near-term timeline. | 2–4% |
| 5 | **maximum** | Overwhelming signal convergence, hard catalyst within 24 hours, defined and asymmetric risk/reward, no credible counter-thesis. Should appear infrequently — if more than ~10% of recommendations are level 5, the scale is being applied too loosely. | 3–5% |

**Bands overlap intentionally.** A moderate-conviction trade with defined risk (long options, max loss = premium) may justify the upper end of its band; a moderate-conviction trade with open-ended risk (short equity) should sit at the lower end. The overlap expresses risk-profile nuance within a conviction level.

**Bands are advisory.** The [portfolio manager](portfolio-manager.md) retains full sizing discretion. Deviations are captured via the command envelope's modification mechanism — the `conviction_disagreement` adjustment category tracks cases where the PM disagrees with the analyst's conviction-to-size mapping, and the feedback loop assesses whether overrides improve or worsen outcomes.

**Sizing bands express capital at risk, not notional.** For defined-risk instruments (long options, max loss = premium), the band refers to premium at risk. For open-ended instruments (equity), the band refers to notional position value. The size rationale must make this mapping explicit — dollar amount, percentage of portfolio, and how the band was applied given the instrument's risk profile.

**Calibration target:** A well-calibrated analyst exhibits higher thesis validation rates at higher conviction levels. If level-4/5 trades don't meaningfully outperform level-2 trades, signal criteria need tightening or scale application has drifted. See [thesis quality trends](../01-data-layer/internal/portfolio-state.md) (category 6).

---

## Entry window

The optional `entry_window` field communicates when the entry should be executed and how edge decays over time, giving the [portfolio manager](portfolio-manager.md) concrete information for prioritization across multiple recommendations.

**Field structure:**

- **`deadline`** — ISO 8601 timestamp; the analyst's assessment of when the setup's edge meaningfully degrades.
- **`decay_type`** — how edge degrades:
  - `binary` — setup exists before the deadline and not after. Typical for event-driven trades with a hard catalyst timestamp (earnings, FOMC, data releases). Near the deadline, execute or abandon.
  - `gradual` — edge erodes progressively. Typical for technical setups, mean-reversion trades, or information edges that leak over time. Earlier entry captures more edge; the trade may remain viable near the deadline at reduced attractiveness.
- **`rationale`** — why the window exists and what happens after it closes.

**Include when:** the setup has a meaningful time constraint — a catalyst with a known timestamp, an options structure with accelerating theta, a technical level weakening as participants front-run. Omit when the thesis has no inherent urgency (e.g., a structural sector rotation playing out over weeks).

**Entry window is advisory.** The PM evaluates holistically; a passed deadline may still be viable if conditions haven't changed.

---

## Opportunity ranking and prioritization

The analyst does not rank or compare proposals. Each recommendation is self-contained and stands on its own merits. Cross-opportunity prioritization — which trade to execute first, which to cut under capital pressure — is the [portfolio manager's](portfolio-manager.md) job, performed with full portfolio context the analyst does not have.

The analyst's contribution in this area is limited to three behaviors: an inclusion threshold, a non-overlap filter, and a conventional presentation order.

### Inclusion threshold

Only propose setups the analyst would commit capital to if acting alone. The strongest ranking signal the analyst produces is what it chose to exclude.

- Conviction 1 setups should appear rarely — only when asymmetric payoff justifies a small position even on single-signal evidence.
- Conviction 2 setups should carry a clear acknowledgment of contradictions and a specific reason the analyst believes the signal despite them.
- On quiet days, zero or one recommendation is valid and preferable to padding.

**No numeric cap on count.** The number of recommendations per invocation emerges from this threshold and tracks signal availability — zero or one on quiet days, many on exceptional days. A fixed or soft cap would anchor the analyst toward a number rather than signal, and forced truncation would require cross-opportunity ranking the analyst is not positioned to do. Genuine stress conditions (regime jumps, margin events) trigger [emergency invocations or halt mode](../06-risk-guardrails/state-delivery.md#halt-mode-header-modifications), which constrain generation upstream. Calibration drift is monitored across invocations via the feedback loop, not prevented per-invocation.

The [conviction scale calibration target](#conviction-scale) defines the expected distribution across invocations.

### Non-overlap filter

Filter proposals before finalizing to avoid presenting structurally redundant or collectively non-viable sets. The [proposal pre-processor](proposal-pre-processor.md) flags analyst-vs-strategist same-underlying conflicts for the PM as annotations but does not suppress them — the analyst's self-filter is what prevents the noise from reaching the PM in the first place.

- **Same-underlying duplicates (intra-invocation).** If two proposals target the same underlying in the same direction (equity long plus call long on NVDA, two different call strikes on the same name), pick the best expression and drop the others. These are variants of the same bet, not independent opportunities.
- **Held-book duplicates.** If a candidate's underlying appears in the analyst's [`Held positions` block](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) with the same direction at meaningful size, drop the candidate. The [strategist](strategist.md) owns add/hold/reduce for existing positions and will evaluate whether the current thesis supports increasing exposure — re-proposing from the analyst side creates redundant work and noise for the PM. Exception: an opposite-direction thesis on a held name (a new short on a held long, or vice versa) is genuine new information and should be surfaced; the strategist's thesis-status assessment will reconcile it against the existing thesis.
- **Collectively breaching headroom.** When the [guardrail validation tool's](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) cumulative check fails across multiple proposals, raise the inclusion threshold until the remaining set fits, rather than revising sizes indefinitely to squeeze everything in.

Catalyst overlap (e.g., two different underlyings riding the same macro catalyst) is not inherently a filter trigger — proposals may share a catalyst and still be independent opportunities — but the analyst should weigh reduced signal independence when considering inclusion.

### Presentation order

Recommendations are presented conviction descending, with entry window urgency as tiebreaker (binary decay with nearest deadline first, then gradual, then no window), and risk asymmetry as secondary tiebreaker (defined-risk before open-ended). This is a readability convention so the portfolio manager can scan high-signal setups first — not an expression of the analyst's execution preference. Anti-ranking discipline (each proposal stands on its own merits; do not cross-compare) is enforced at the system prompt — see [`prompts/decision/analyst.md`](../../prompts/decision/analyst.md).

---

## Abandoned openings from prior invocation

OPEN commands approved by the PM in the prior invocation but abandoned at broker submission (per the Phase 2 write-path policy in [state-persistence.md](../05-execution-layer/state-persistence.md) and [broker-adapter.md](../05-execution-layer/broker-adapter.md)) are surfaced in the analyst guardrail state header's `Abandoned openings` block — see [state-delivery.md](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) for format and purpose.

Each entry is a prompt to re-evaluate the original thesis on current signals, not a retry obligation. If the thesis is re-expressed as a new recommendation (new `REC-n`), the [Conviction scale](#conviction-scale) and [Inclusion threshold](#inclusion-threshold) apply unchanged — prior PM approval is not evidence that elevates conviction or relaxes the threshold, and the new narrative must cite current synthesizer references rather than copy forward the prior invocation's reasoning. If current signals no longer support the thesis, the entry lapses; no "decline" artifact is required. Never fabricate a reference to a prior-invocation source; only cite references present in the current synthesizer brief.

---

## Pre-submission guardrail validation

Before finalizing recommendations, the analyst validates each proposal against the current guardrail state. This ensures that only guardrail-compliant proposals reach the portfolio manager — the PM evaluates thesis quality and portfolio coherence, not guardrail feasibility.

**Two-layer approach:**

1. **Headroom context (pre-loaded).** The analyst's [guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) includes current guardrail headroom at the start of its window — available capital, per-sector delta-adjusted exposure room, directional and gross exposure room, per-position size limits under the active regime. The analyst uses this to self-constrain during initial proposal generation, avoiding obviously infeasible recommendations (e.g., a large new semi position when semi sector exposure is near its limit). This is the primary mechanism — most proposals should be compliant on the first pass because the analyst is sizing with awareness of constraints.

2. **Guardrail validation tool (deterministic check).** After drafting each recommendation, the analyst calls a guardrail validation tool that deterministically checks the proposal against all applicable rules. The tool accepts the proposed instrument, direction, and size, computes delta-adjusted exposure for options/strategies via the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) (the same primitives the engine's T3 check uses), and returns a pass/fail result per rule with current headroom and projected headroom after the trade.

**On validation failure:** The analyst revises the proposal — reducing size, choosing a different instrument (e.g., a lower-delta strike), or dropping the recommendation — and re-validates. This is an expected part of the workflow, not an exceptional case. The validation feedback is specific enough to guide revision: "semi sector delta-adjusted exposure would be 22.3%, limit is 20.0% — reduce by approximately 15% or choose a lower-delta instrument."

**On validation success:** The proposal is finalized and included in the analyst's output. The portfolio manager can trust that all received proposals were guardrail-compliant at the time the analyst checked them.

**Important caveat:** Portfolio state may shift between the analyst's validation and the PM's command execution (due to market movement, fills resolving, or regime changes). The execution layer performs a final authoritative guardrail check on every OMS command. If a previously-compliant proposal is rejected at execution time, the PM receives synchronous feedback and can adjust — see [portfolio-manager.md](portfolio-manager.md) and [oms-commands.md](../05-execution-layer/oms-commands.md).

**Multi-proposal cumulative impact:** When the analyst produces multiple recommendations in a single invocation, the guardrail validation tool tracks cumulative impact across proposals. The second proposal's headroom check accounts for the first proposal's projected impact. This prevents the analyst from producing five individually-compliant proposals that collectively breach a limit.

---

## Source brief retrieval

The synthesizer output contains typed source references (e.g., `[SA-TECH-3]`, `[QR-4]`). The analyst has access to a retrieval tool that accepts these reference IDs and returns the corresponding section from the original research brief.

**Expected workflow:**
1. Read the synthesizer brief in full
2. Form initial views on opportunities, risks, and thesis candidates
3. For findings needing deeper investigation — especially flagged contradictions or areas of uncertainty — retrieve the underlying source material by reference ID
4. Incorporate retrieved detail into thesis construction where it strengthens the case

The retrieval tool is optional. On quiet days with broad consensus, the synthesis alone may be sufficient. On volatile days with many flagged contradictions, the analyst should expect to retrieve more source material to inform its recommendations.
