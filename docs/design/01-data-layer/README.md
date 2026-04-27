# Data layer

Foundation of the AlphaMind pipeline. Owns all raw data that downstream layers will factor into LLM decisions. No computation, no interpretation — collection and structured storage only.

Two sources:

| Source | Description | Document |
|--------|-------------|----------|
| [External](external/) | Market data from outside the system — prices, flows, options, macro, news, sentiment, prediction markets | [Quantitative](external/quantitative.md) · [Qualitative](external/qualitative.md) |
| [Internal](internal/) | The system's own state — positions, theses, orders, P/L, capital, activity history | [Portfolio state](internal/portfolio-state.md) |

External data describes the world; internal data describes what the system is doing in that world. Both feed into the [distillation layer](../02-distillation-layer/README.md).

---

## External data

Two dimensions of market information, collected via APIs at each pipeline invocation:

- **[Quantitative data](external/quantitative.md)** — 12 categories of structured market data: price/volume, order flow, derivatives, short selling, fundamentals, macro/rates, cross-asset correlations, commodities, crypto, alternative data, corporate actions, sentiment metrics.
- **[Qualitative data](external/qualitative.md)** — Unstructured, interpretive, forward-looking information: news/financial media, social sentiment, prediction markets, macro/geopolitical calendar, earnings transcripts, sector-specific narratives.

The ticker universe scoping external data collection is defined in [asset-universe.md](../asset-universe.md).

## Internal data

The system's internal memory — positions, performance, theses, available capital. See [internal/README.md](internal/README.md) for the full specification including ingestion frequency, consumer map, and cross-reference map.

Portfolio state is the only data category where the system is both producer and consumer. The [execution layer](../05-execution-layer/README.md) writes it; the data layer reads it back at invocation start. The execution layer's own source of truth is reconciled against Alpaca's account endpoints and `trade_updates` websocket — see [broker-adapter.md § Account state queries](../05-execution-layer/broker-adapter.md).

**Data schemas:** Authoritative data model definitions for positions, theses, and orders live in the execution layer ([position-model.md](../05-execution-layer/position-model.md), [thesis-model.md](../05-execution-layer/thesis-model.md), [orders-and-brackets.md](../05-execution-layer/orders-and-brackets.md)) because they are tightly coupled with execution behavior. The internal data documents here describe what downstream layers consume, not how it is produced.

**API failure handling:** External API failures are handled per tier (Critical / Important / Optional) with fresh-or-abort semantics — no stale data propagates. See [api-failure-handling.md](api-failure-handling.md).

**Collection process:** External data lands in the database via a long-running collector process supervised by NSSM. See [collector/README.md](collector/README.md) for storage schema, vendor adapters, runner/scheduler, lifecycle, and operator workflow.
