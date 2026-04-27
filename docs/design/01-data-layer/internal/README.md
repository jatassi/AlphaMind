# Internal data — portfolio state

The system's internal memory. External data describes the world; portfolio state describes *what the system itself is doing* — positions held, performance, justifying theses, available capital.

Portfolio state is the only data category where the system is both producer and consumer. The [execution layer](../../05-execution-layer/README.md) writes it as it executes trades, tracks P/L, and records thesis outcomes; the data layer reads it back at invocation start. Multiple agents consume it differently — the [synthesizer](../../03-analysis-layer/synthesizer.md) via lightweight tools, the [analyst](../../04-decision-layer/analyst.md), the [strategist](../../04-decision-layer/strategist.md), and the [portfolio manager](../../04-decision-layer/portfolio-manager.md).

*Freshness and source of truth:* The OMS maintains portfolio state as a real-time database populated from (a) Alpaca's `trade_updates` websocket for fills and order-state changes, and (b) periodic reconciliation queries against Alpaca's `account`, `positions`, `orders`, and `activities` endpoints. When local state disagrees with Alpaca, Alpaca is authoritative and the OMS logs the delta. The data layer reads the current OMS snapshot at invocation time. The only residual freshness concern is mark-to-market pricing on open positions, which depends on quant category 1 freshness.

---

## Document structure

| Document | Description |
|----------|-------------|
| [Portfolio state](portfolio-state.md) | Categories 1–6: position inventory, P/L tracking, thesis registry, capital/capacity, activity log, thesis quality trends — direct reads from the OMS database |
| [Derived metrics](../../02-distillation-layer/internal.md) | Categories 7–11: exposure analysis, P/L attribution, thesis dependency mapping, capital efficiency, system health — computed by the [distillation layer](../../02-distillation-layer/README.md) using raw state + market data |

Boundary: if the OMS stores it or it's a direct aggregation, it's [portfolio state](portfolio-state.md). If it requires market-data cross-reference or LLM judgment, it's [derived metrics](../../02-distillation-layer/internal.md).

**Data schemas:** Authoritative model definitions for positions, theses, and orders live in the [execution layer](../../05-execution-layer/README.md) — [position-model.md](../../05-execution-layer/position-model.md), [thesis-model.md](../../05-execution-layer/thesis-model.md), and [orders-and-brackets.md](../../05-execution-layer/orders-and-brackets.md) — tightly coupled with execution behavior. The portfolio state document here describes these items from the consumer's perspective.

---

*Ingestion frequency summary:*

| Category | Cadence | Source |
|----------|---------|--------|
| 1. Position inventory | Every invocation (snapshot) | OMS database, reconciled against Alpaca `GET /v2/positions` |
| 2. P/L and performance tracking | Every invocation (computed from positions + market data) | OMS database + quant 1a. In paper mode, the [paper-evaluation harness](../../05-execution-layer/paper-evaluation-harness.md) also delivers live-adjusted P/L alongside raw |
| 3. Thesis registry | Every invocation (active theses) + rolling window of resolutions | OMS thesis database (system-internal — no Alpaca counterpart) |
| 4. Capital and capacity | Every invocation (computed from positions + risk limits) | OMS database + Alpaca `GET /v2/account` (cash, buying power, margin, day-trade count) + risk guardrail configuration |
| 5. Activity log | Every invocation (events since last invocation) | OMS event log, populated from Alpaca `trade_updates` websocket and Alpaca `account/activities` for EOD fee/dividend/corporate-action events |
| 6. Thesis quality trends | Every invocation (trailing metrics, lightweight compute) | OMS historical records |
| 7–8. Exposure and attribution | 7 every invocation; 8 daily (pre-close) | Computed from categories 1–2 + market data |
| 9. Thesis dependency | Every invocation | Computed from category 3 + qualitative data (requires LLM) |
| 10. Capital efficiency | Daily (pre-close) | Computed from categories 1, 4 |
| 11. System health | 11a daily, 11b weekly | Computed from categories 2, OMS process logs, broker-adapter connection health |

*Consumer map:*

| Consumer | Categories received | Purpose |
|----------|-------------------|---------|
| [Synthesizer](../../03-analysis-layer/synthesizer.md) | 1a, 3a (summary), 1b (via tools) | On-demand portfolio context: current positions, active thesis summaries, exposure snapshot — queried via lightweight tools when market signals are relevant to existing positions |
| [Analyst](../../04-decision-layer/analyst.md) | 1a, 3a, 4a, 4b (summary) | Knows existing positions, active theses, and pending orders to avoid duplicate or conflicting recommendations; knows available capital for sizing |
| [Strategist](../../04-decision-layer/strategist.md) | All (1–11) | Thesis status classification, position action recommendations, cross-position dynamics, portfolio-level observations. Primary consumer of full thesis records and derived metrics |
| [Portfolio manager](../../04-decision-layer/portfolio-manager.md) | All (1–11) | Risk management, position sizing, thesis approval/rejection, drawdown management, capital allocation |

*Cross-reference map — where portfolio state meets market data:*

| Portfolio state category | Market data counterpart | Relationship |
|------------------------|----------------------|-------------|
| 1a (current holdings) | Quant 1a (price structure) | Mark-to-market pricing; current price vs. entry determines P/L |
| 1b (sector exposure) | Quant 7b (cross-sector rotation) | Portfolio sector weights vs. sector momentum |
| 2a (position P/L) | Quant 1a, 1e (price and relative performance) | P/L computation requires current prices and sector returns |
| 2c (drawdown tracking) | Quant 11f (volatility regime) | Drawdown limits should be regime-aware |
| 3a (active thesis details) | Qualitative 1–6, Quant 1–8 | Thesis validity checked against the signals that generated it |
| 3b (thesis status) | Quant 3a (implied vol), Qualitative 4 (earnings) | Catalyst timing from options and event data drives thesis status |
| 4b (pending orders) | Quant 1a (current price) | Order proximity to fill depends on current price vs. limit price |
| 4c (risk budget) | Quant 7a (intra-sector correlation) | Correlation limits require live correlation data |
| 4d (active risk params) | Quant 11f (volatility regime) | Regime classification determines which parameter set is active |
| 6c (performance attribution) | Quant 11f (volatility regime), Quant 7b (sector rotation) | Attributing P/L to market regime and sector dynamics |
| 7a (beta-adjusted exposure) | Quant 1f (beta) | Position values × betas = risk-adjusted exposure |
| 7b (correlation profile) | Quant 7a, 7d, 7e, 7g (correlation data) | Position pairs × trailing correlations = portfolio correlation structure |
| 8a–8b (P/L attribution) | Quant 1a (SPY return), 7b (sector ETF returns), 1f (beta) | P/L decomposed into market + sector + alpha |
| 9a–9b (thesis dependency) | Qualitative 1–6 (catalyst data) | Thesis narratives checked for shared catalysts and narrative overlap |
