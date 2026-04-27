# Distillation layer

Deterministic transforms — no LLMs, no interpretation — that present data in token-efficient form so downstream analysis agents focus on judgment, not data wrangling.

| Source | Input | Output | Document |
|--------|-------|--------|----------|
| External | Raw market data from [data layer external](../01-data-layer/external/) | Normalized indicators, anomaly flags, regime classifications, composites | [external.md](external.md) |
| Internal | Raw portfolio state from [data layer internal](../01-data-layer/internal/) | Exposure analysis, P/L attribution, thesis dependencies, capital efficiency, system health | [internal.md](internal.md) |

Threshold values gating every anomaly flag, regime boundary, and persistence baseline live in [threshold-calibration.md](threshold-calibration.md) — initial values, cold-start bootstrap policy, update process, validation invariants.

**The boundary:** Deterministic computations that reshape raw data for LLM consumption belong here. Anything requiring LLM judgment belongs in the [analysis layer](../03-analysis-layer/README.md).

---

## External distillation

Raw quantitative and qualitative data into LLM-ready signals. See [external.md](external.md). Key operations:

- **Normalization:** Unit standardization, time alignment, volatility normalization, extended-hours discounting, macro surprise framing
- **Technical indicators & derived metrics:** Computed from each data category (price/volume, order flow, derivatives, short, fundamental, macro, cross-asset, commodities, corporate actions, qualitative)
- **Anomaly detection:** Statistical outlier flagging across price, flow, divergence, short, fundamental, and macro dimensions
- **Persistent state & composites:** Rolling baselines, composite signals, prediction market state tracking, volatility regime classification

## Internal distillation

Raw portfolio state into derived metrics requiring cross-reference with market data. See [internal.md](internal.md). Five categories:

- **Exposure analysis** (categories 7a–7b): Beta-adjusted exposure, position correlation profiles
- **P/L attribution** (categories 8a–8b): Market vs. sector vs. alpha decomposition
- **Thesis dependency mapping** (categories 9a–9b): Shared catalyst identification, dependency-adjusted concentration (LLM-intensive)
- **Capital efficiency** (categories 10a–10b): Utilization, turnover, execution efficiency
- **System health diagnostics** (categories 11a–11b): Rolling Sharpe/Sortino, execution quality metrics

---

## Universal context broadcast

The volatility regime classification produced by external distillation ([external.md](external.md) §4) is broadcast to every agent in the [analysis layer](../03-analysis-layer/README.md) and [decision layer](../04-decision-layer/README.md) as a regime flag that changes how each agent interprets its data and decides.
