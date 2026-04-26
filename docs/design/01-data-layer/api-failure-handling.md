# API failure handling

When external data providers fail — rate limits, timeouts, 500s, deprecated endpoints, authentication issues — the pipeline needs a deterministic policy for how to proceed. This document defines that policy per data category.

## Design principle — no degraded decisions

The system makes capital allocation decisions on short horizons (4–72 hours). Decisions built on stale or missing data are worse than decisions skipped. This spec favors aborting the invocation over proceeding with degraded inputs, except for supplementary categories whose absence doesn't materially change the decision surface.

The practical consequence: most API failure scenarios result in the invocation being skipped. The pipeline runs 2–4 times per market day; skipping one invocation means waiting 2–6 hours for the next trigger. For a 4–72h horizon, this is operationally acceptable — most theses survive a single missed cycle intact.

No category uses stale data. If a freshness window is exceeded, the category is treated as failed. This is deliberate: stale data from most categories is worthless at this time horizon, and the narrow cases where stale data retains value (static-between-events data such as fundamentals) were judged not worth the complexity of event-gap verification.

## Criticality tiers

Every external data category is assigned to one of three tiers.

**Critical** — retry aggressively (multiple attempts, provider failover where available); abort the invocation on persistent failure. Portfolio state, live quotes, current yields, commodities.

**Important** — retry briefly (limited attempts, no failover); abort the invocation on failure. Order flow, options flow, fundamentals, news, earnings commentary, regulatory calendar, sector catalysts, corporate actions.

**Optional** — retry briefly; on failure, skip the category, log for ops, and proceed with the rest of the pipeline with that category absent from the distillation payload. Short selling, social sentiment, prediction markets.

Critical and Important tiers produce identical runtime outcomes on failure (abort). The distinction survives in retry aggressiveness (Critical gets more attempts and cross-provider failover; Important fails fast) and alerting severity.

## Category assignments

| Category | Tier | Freshness window |
|---|---|---|
| Portfolio state (internal) | Critical | <5 min |
| Q1. Price and volume | Critical | <5 min |
| Q2. Order flow and microstructure | Important | <30 min |
| Q3. Derivatives and options | Important | <30 min |
| Q4. Short selling and lending | Optional | — |
| Q5. Fundamental and earnings | Important | <1 day |
| Q6. Macro and rates | Critical | <1 h |
| Q8. Commodities | Critical | <1 h |
| Q12. Corporate actions and structural flows | Important | <1 day |
| Qual 1. News and financial media | Important | <1 h |
| Qual 2. Social and alternative sentiment | Optional | — |
| Qual 3. Prediction markets | Optional | — |
| Qual 4. Earnings and management commentary | Important | event + 1 h |
| Qual 5. Regulatory, policy, geopolitical | Important | <1 day |
| Qual 6. Sector-specific qualitative catalysts | Important | <4 h |

The Q1 freshness window applies to live market data — current session ticks, current-forming bars, and derived indicators computed on them. Settled historical bars from prior sessions are static and do not refresh; failure to pull a specific historical observation is handled as a missing value at the observation level, not a category-level freshness failure. The same live-vs-settled distinction applies to Q6: current yields are time-sensitive, historical yield curves are static.

Two quantitative categories are **derived** — computed from other inputs rather than independently ingested — and inherit freshness from their sources:

- Q7. Cross-asset and correlation — inherits from Q1 and Q6.
- Q11. Volatility regime — inherits from Q1 and Q3.

### Non-obvious classifications

Three categories that might appear decision-relevant are Optional because their failure does not compromise decision quality at this time horizon:

- **Q4 short selling.** Native refresh rates are naturally slow — short interest is bi-monthly (FINRA settlement cycle), utilization and borrow costs are daily, only daily short volume provides same-day signal. The category adds color to squeeze theses but is not load-bearing for the majority of trades. Aborting for a temporarily unavailable category with its own native latency is disproportionate.
- **Qual 2 social sentiment.** Noisy supplementary signal; most valuable at extremes and structurally contrarian at them. Absence for one invocation does not meaningfully degrade analyst reasoning.
- **Qual 3 prediction markets.** The design principle for this category is delta-over-level — the rate of change in odds is what matters, not the absolute level (see [qualitative.md](external/qualitative.md) §3). Skipping one invocation causes one missed delta; the next invocation's multi-cycle trajectory signal partially recovers. Aborting for a specialized signal that only informs monetary-policy, regulatory, and geopolitical thesis types is disproportionate.

**Q8 commodities** is unconditionally Critical rather than context-dependent on whether an energy-sector trade is a candidate this invocation. This avoids coupling the data layer to downstream candidate-selection logic; providers (EIA, Polygon for oil futures) are reliable enough that this should rarely trigger aborts in practice.

## Retry semantics

Retry shape per tier:

- **Critical** — multiple retries with exponential backoff, plus provider failover where a fallback provider is configured for the category.
- **Important** — limited retries with exponential backoff, no failover.
- **Optional** — single retry on brief delay.

Specific retry counts, backoff intervals, and provider failover configurations are implementation parameters, to be tuned based on observed provider reliability during testing rather than pre-committed here.

## Abort semantics

"Abort" means cancelling the current pipeline invocation entirely. No analyst runs, no strategist, no portfolio manager, no OMS commands issued. The system waits for the next scheduled trigger.

For a system with 2–4 scheduled invocations per market day, one aborted invocation means a 2–6 hour delay to the next decision cycle. Thesis-level time horizons (4–72 hours) absorb this delay without structural damage.

### Continuous monitor interaction

The [continuous monitor](../05-execution-layer/architecture.md#4-continuous-monitor) is a separate system component whose safety authority does not depend on the LLM pipeline running. During an API outage that aborts the pipeline:

- If the outage is in the qualitative research layer (Qual 1, 4, 5, 6) or quant categories that only feed the LLM layer (Q2, Q3, Q5, Q7, Q11, Q12), the continuous monitor operates normally: it can detect guardrail breaches on existing positions from live quote data and issue protective CLOSE commands via engine-originated command envelopes.
- If the outage is in Q1 live price/volume — the monitor's own data dependency — the monitor is also degraded. Breach detection on market-movement-driven conditions cannot function without live quotes; the monitor retains authority over portfolio-state-driven conditions (margin cascades, broker-side fill reports) but loses market-movement coverage until quote data recovers.

This asymmetry is a physical consequence of both components depending on the same upstream data source, not a design choice. Operationally, a sustained Q1 outage is the highest-severity alerting condition in the system.

### Emergency invocations

When the continuous monitor triggers an emergency pipeline invocation (regime jump, multi-rule breach, margin call — see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md)), the same tier rules apply. If the emergency invocation cannot obtain fresh Critical-tier data, it aborts — and the continuous monitor handles the breach mechanically via its own response authority rather than forcing a degraded LLM decision on top of a market condition already flagged as abnormal.

## Alerting

Alerting severity follows the tier mapping — Critical failures warrant immediate human notification, Important failures warrant review, Optional failures warrant trend logging. Specific alerting infrastructure is scoped to the Phase 4 monitoring and alerting design and not defined here.

---

*Cross-references:*

- Data category definitions: [external/quantitative.md](external/quantitative.md), [external/qualitative.md](external/qualitative.md), [internal/portfolio-state.md](internal/portfolio-state.md)
- Continuous monitor: [05-execution-layer/architecture.md](../05-execution-layer/architecture.md)
- Engine-originated command envelopes: [04-decision-layer/portfolio-manager.md](../04-decision-layer/portfolio-manager.md)
- Emergency invocation triggers: [06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
