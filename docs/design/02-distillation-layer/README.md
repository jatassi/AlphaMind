# Distillation layer

The bridge between raw data and LLM-consumable signals. Everything in this layer is deterministic — no LLMs, no interpretation. Its job is to present data in the most token-efficient way possible so that downstream analysis agents can focus on pattern recognition and judgment rather than data wrangling.

The distillation layer transforms both external and internal data:

| Source | Input | Output | Document |
|--------|-------|--------|----------|
| External | Raw market data from [data layer external](../01-data-layer/external/) | Normalized indicators, anomaly flags, regime classifications, composites | [external.md](external.md) |
| Internal | Raw portfolio state from [data layer internal](../01-data-layer/internal/) | Exposure analysis, P/L attribution, thesis dependencies, capital efficiency, system health | [internal.md](internal.md) |

The threshold values that gate every anomaly flag, regime boundary, and persistence baseline are specified in [threshold-calibration.md](threshold-calibration.md) — initial values, the bootstrap policy for cold-start operation, the update process, and validation invariants.

**The boundary:** If it's a deterministic computation that transforms raw data into a more useful form for LLM consumption, it belongs here. If it requires LLM judgment, it belongs in the [analysis layer](../03-analysis-layer/README.md).

---

## External distillation

Transforms raw quantitative and qualitative data into LLM-ready signals. See [external.md](external.md) for the full spec. Key operations:

- **Normalization:** Unit standardization, time alignment, volatility normalization, extended-hours discounting, macro surprise framing
- **Technical indicators & derived metrics:** Computed from each data category (price/volume, order flow, derivatives, short, fundamental, macro, cross-asset, commodities, corporate actions, qualitative)
- **Anomaly detection:** Statistical outlier flagging across price, flow, divergence, short, fundamental, and macro dimensions
- **Persistent state & composites:** Rolling baselines, composite signals, prediction market state tracking, volatility regime classification

## Internal distillation

Transforms raw portfolio state into derived metrics that require cross-referencing with market data. See [internal.md](internal.md) for the full spec. Five categories:

- **Exposure analysis** (categories 7a–7b): Beta-adjusted exposure, position correlation profiles
- **P/L attribution** (categories 8a–8b): Market vs. sector vs. alpha decomposition
- **Thesis dependency mapping** (categories 9a–9b): Shared catalyst identification, dependency-adjusted concentration (LLM-intensive)
- **Capital efficiency** (categories 10a–10b): Utilization, turnover, execution efficiency
- **System health diagnostics** (categories 11a–11b): Rolling Sharpe/Sortino, execution quality metrics

---

## Universal context broadcast

The volatility regime classification produced by external distillation (see [external.md](external.md), section 4) is broadcast to every agent in the [analysis layer](../03-analysis-layer/README.md) and [decision layer](../04-decision-layer/README.md). This is not optional context — it's a regime flag that changes how every agent interprets its data and makes decisions.
