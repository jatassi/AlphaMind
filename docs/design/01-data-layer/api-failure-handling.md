# API failure handling

Deterministic policy for handling external data provider failures (rate limits, timeouts, 500s, deprecated endpoints, auth issues), defined per data category.

## Design principle — no degraded decisions

The system decides on 4–72h horizons. Decisions built on stale or missing data are worse than decisions skipped. This spec favors aborting the invocation over proceeding with degraded inputs, except for supplementary categories whose absence doesn't materially change the decision surface.

Most failure scenarios skip the invocation. The pipeline runs 2–4 times per market day; one skip means a 2–6 hour wait for the next trigger — operationally acceptable, since most theses survive a missed cycle.

No category uses stale data. If a freshness window is exceeded, the category is treated as failed.

## Criticality tiers

Every external data category is assigned to one of three tiers.

**Critical** — retry aggressively (multiple attempts, provider failover where configured); abort on persistent failure. Portfolio state, live quotes, current yields, commodities.

**Important** — retry briefly (limited attempts, no failover); abort on failure. Order flow, options flow, fundamentals, news, earnings commentary, regulatory calendar, sector catalysts, corporate actions.

**Optional** — retry briefly; on failure, skip the category, log for ops, and proceed with the category absent from the distillation payload. Short selling, social sentiment, prediction markets.

Critical and Important produce identical runtime outcomes on failure (abort); they differ in retry aggressiveness and alerting severity.

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

The Q1 freshness window applies to live market data — current session ticks, current-forming bars, derived indicators. Settled historical bars are static; failure to pull a specific historical observation is a missing value at the observation level, not a category-level freshness failure. Same distinction applies to Q6: current yields are time-sensitive, historical yield curves are static.

Two **derived** categories — computed from other inputs rather than independently ingested — inherit freshness from their sources:

- Q7. Cross-asset and correlation — inherits from Q1 and Q6.
- Q11. Volatility regime — inherits from Q1 and Q3.

### Non-obvious classifications

Three categories that might appear decision-relevant are Optional because their absence does not compromise decision quality at this horizon:

- **Q4 short selling.** Native refresh rates are slow — short interest bi-monthly (FINRA settlement cycle), utilization and borrow costs daily, only daily short volume provides same-day signal. Adds color to squeeze theses but isn't load-bearing for most trades.
- **Qual 2 social sentiment.** Noisy supplementary signal; most valuable at extremes and structurally contrarian there. Absence for one invocation does not meaningfully degrade analyst reasoning.
- **Qual 3 prediction markets.** Delta-over-level design principle — rate of change in odds matters more than absolute level (see [qualitative.md](external/qualitative.md) §3). One missed delta is partially recovered by the next invocation's trajectory signal.

**Q8 commodities** is unconditionally Critical rather than context-dependent on whether an energy-sector trade is a candidate — avoids coupling the data layer to downstream candidate selection. EIA and Polygon are reliable enough that this should rarely trigger aborts.

## Retry semantics

- **Critical** — 3 attempts with exponential backoff (1.0s, 2.0s sleeps), plus provider failover where a fallback is configured.
- **Important** — 2 attempts with exponential backoff (1.0s sleep), no failover.
- **Optional** — 2 attempts with a flat 0.5s delay, no failover.

The schedule is pinned in `_common.py`; see [data-sources.md § Retry-per-tier](collector/data-sources.md#retry-per-tier) for the canonical table. Revisions follow the [threshold-calibration](../02-distillation-layer/threshold-calibration.md) discipline, driven by `collection_runs.error_summary` history.

## Abort semantics

"Abort" cancels the current pipeline invocation entirely — no analyst, strategist, portfolio manager, or OMS commands. The system waits for the next scheduled trigger; thesis horizons (4–72 hours) absorb the 2–6 hour delay without structural damage.

### Continuous monitor interaction

The [continuous monitor](../05-execution-layer/architecture.md#4-continuous-monitor) is a separate component whose safety authority does not depend on the LLM pipeline. During an API outage that aborts the pipeline:

- Outages in qualitative (Qual 1, 4, 5, 6) or LLM-only quant categories (Q2, Q3, Q5, Q7, Q11, Q12): the monitor operates normally — detects guardrail breaches from live quote data and issues protective CLOSE commands via engine-originated command envelopes.
- Outages in Q1 live price/volume — the monitor's own dependency: the monitor is also degraded. Breach detection on market-movement conditions cannot function without live quotes; the monitor retains authority over portfolio-state conditions (margin cascades, broker-side fill reports) but loses market-movement coverage until quotes recover.

A sustained Q1 outage is the highest-severity alerting condition in the system.

### Emergency invocations

When the continuous monitor triggers an emergency invocation (regime jump, multi-rule breach, margin call — see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md)), the same tier rules apply. If fresh Critical-tier data is unavailable, the invocation aborts and the monitor handles the breach mechanically via its own response authority.

## Alerting

Severity follows the tier mapping — Critical-tier failures map to Critical alerts, Important-tier failures map to Important alerts, Optional-tier failures map to Operational alerts. Routing, debounce, and notification channels are owned by the command-center alert registry; default rules covering Critical-tier API failure, Important-tier API failure, schedule miss on a critical category, and Optional-tier data category skipped land in [`config/alerts.yaml`](../command-center.md#alerting).

---

*Cross-references:*

- Data category definitions: [external/quantitative.md](external/quantitative.md), [external/qualitative.md](external/qualitative.md), [internal/portfolio-state.md](internal/portfolio-state.md)
- Continuous monitor: [05-execution-layer/architecture.md](../05-execution-layer/architecture.md)
- Engine-originated command envelopes: [04-decision-layer/portfolio-manager.md](../04-decision-layer/portfolio-manager.md)
- Emergency invocation triggers: [06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
