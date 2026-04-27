# API failure handling

Deterministic policy for handling external data provider failures (rate limits, timeouts, 500s, deprecated endpoints, auth issues), defined per data category.

## Design principle — no degraded decisions

The system decides on 4–72h horizons. Decisions built on stale or missing data are worse than decisions skipped. This spec favors aborting the invocation over proceeding with degraded inputs, except for supplementary categories whose absence doesn't materially change the decision surface.

Most API failure scenarios skip the invocation. The pipeline runs 2–4 times per market day; one skip means a 2–6 hour wait for the next trigger — operationally acceptable on a 4–72h horizon, since most theses survive a missed cycle intact.

No category uses stale data. If a freshness window is exceeded, the category is treated as failed. The narrow cases where stale data retains value (static-between-events data such as fundamentals) were judged not worth the complexity of event-gap verification.

## Criticality tiers

Every external data category is assigned to one of three tiers.

**Critical** — retry aggressively (multiple attempts, provider failover where configured); abort on persistent failure. Portfolio state, live quotes, current yields, commodities.

**Important** — retry briefly (limited attempts, no failover); abort on failure. Order flow, options flow, fundamentals, news, earnings commentary, regulatory calendar, sector catalysts, corporate actions.

**Optional** — retry briefly; on failure, skip the category, log for ops, and proceed with the category absent from the distillation payload. Short selling, social sentiment, prediction markets.

Critical and Important produce identical runtime outcomes on failure (abort). The distinction survives in retry aggressiveness (Critical gets more attempts and cross-provider failover) and alerting severity.

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

The Q1 freshness window applies to live market data — current session ticks, current-forming bars, derived indicators. Settled historical bars are static; failure to pull a specific historical observation is handled as a missing value at the observation level, not a category-level freshness failure. Same live-vs-settled distinction applies to Q6: current yields are time-sensitive, historical yield curves are static.

Two **derived** quantitative categories — computed from other inputs rather than independently ingested — inherit freshness from their sources:

- Q7. Cross-asset and correlation — inherits from Q1 and Q6.
- Q11. Volatility regime — inherits from Q1 and Q3.

### Non-obvious classifications

Three categories that might appear decision-relevant are Optional because their failure does not compromise decision quality at this horizon:

- **Q4 short selling.** Native refresh rates are slow — short interest bi-monthly (FINRA settlement cycle), utilization and borrow costs daily, only daily short volume provides same-day signal. Adds color to squeeze theses but isn't load-bearing for most trades. Aborting for a category with its own native latency is disproportionate.
- **Qual 2 social sentiment.** Noisy supplementary signal; most valuable at extremes and structurally contrarian there. Absence for one invocation does not meaningfully degrade analyst reasoning.
- **Qual 3 prediction markets.** Delta-over-level design principle — rate of change in odds matters more than absolute level (see [qualitative.md](external/qualitative.md) §3). One missed delta is partially recovered by the next invocation's trajectory signal. Aborting for a specialized signal informing only monetary-policy, regulatory, and geopolitical thesis types is disproportionate.

**Q8 commodities** is unconditionally Critical rather than context-dependent on whether an energy-sector trade is a candidate. This avoids coupling the data layer to downstream candidate selection; EIA and Polygon are reliable enough that this should rarely trigger aborts.

## Retry semantics

Retry shape per tier:

- **Critical** — multiple retries with exponential backoff, plus provider failover where a fallback is configured.
- **Important** — limited retries with exponential backoff, no failover.
- **Optional** — single retry on brief delay.

Specific retry counts, backoff intervals, and failover configurations are tuned during testing rather than pre-committed here.

## Abort semantics

"Abort" cancels the current pipeline invocation entirely — no analyst, strategist, portfolio manager, or OMS commands. The system waits for the next scheduled trigger. With 2–4 invocations per market day, one abort means a 2–6 hour delay; thesis horizons (4–72 hours) absorb this without structural damage.

### Continuous monitor interaction

The [continuous monitor](../05-execution-layer/architecture.md#4-continuous-monitor) is a separate component whose safety authority does not depend on the LLM pipeline. During an API outage that aborts the pipeline:

- Outages in qualitative (Qual 1, 4, 5, 6) or LLM-only quant categories (Q2, Q3, Q5, Q7, Q11, Q12): the monitor operates normally — detects guardrail breaches from live quote data and issues protective CLOSE commands via engine-originated command envelopes.
- Outages in Q1 live price/volume — the monitor's own dependency: the monitor is also degraded. Breach detection on market-movement conditions cannot function without live quotes; the monitor retains authority over portfolio-state conditions (margin cascades, broker-side fill reports) but loses market-movement coverage until quotes recover.

This asymmetry is a physical consequence of shared upstream dependency. A sustained Q1 outage is the highest-severity alerting condition in the system.

### Emergency invocations

When the continuous monitor triggers an emergency invocation (regime jump, multi-rule breach, margin call — see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md)), the same tier rules apply. If fresh Critical-tier data is unavailable, the invocation aborts and the monitor handles the breach mechanically via its own response authority rather than forcing a degraded LLM decision.

## Alerting

Alerting severity follows the tier mapping — Critical warrants immediate notification, Important warrants review, Optional warrants trend logging. Specific alerting infrastructure is scoped to the Phase 4 monitoring design.

---

*Cross-references:*

- Data category definitions: [external/quantitative.md](external/quantitative.md), [external/qualitative.md](external/qualitative.md), [internal/portfolio-state.md](internal/portfolio-state.md)
- Continuous monitor: [05-execution-layer/architecture.md](../05-execution-layer/architecture.md)
- Engine-originated command envelopes: [04-decision-layer/portfolio-manager.md](../04-decision-layer/portfolio-manager.md)
- Emergency invocation triggers: [06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
