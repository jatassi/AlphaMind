# Analysis layer

The core intelligence layer and the most architecturally complex stage. LLM agents analyze information produced by the [data](../01-data-layer/README.md) and [distillation](../02-distillation-layer/README.md) layers to prepare briefs for consumption by the [decision layer](../04-decision-layer/README.md).

The analysis layer has four components:

| Component | Document | Description |
|-----------|----------|-------------|
| Domain researchers | [domain-researchers/](domain-researchers/) | Three LLM agents scoped by sector — [tech/semis](domain-researchers/tech-semis.md), [financials](domain-researchers/financials.md), [energy](domain-researchers/energy.md) — each produces a structured brief from distillation output |
| Baseline qualitative research | [qualitative-research.md](qualitative-research.md) | Always-on sweep: headlines, macro calendar, sentiment, prediction markets, portfolio catalyst proximity — runs every invocation with fixed scope |
| Adaptive research | [adaptive-research.md](adaptive-research.md) | Anomaly-driven investigation: LLM triages distillation and domain researcher anomaly flags, generates research questions, executes bounded agentic search — the system's "curiosity" layer |
| Synthesizer | [synthesizer.md](synthesizer.md) | Reads all domain briefs and produces the unified market snapshot with typed source references — the primary input for [decision layer](../04-decision-layer/README.md) agents. Has lightweight portfolio state tools for on-demand portfolio context |

*Sequencing:* Domain researchers and qualitative baseline research can run in parallel (they have independent inputs). Adaptive research runs after domain researchers (it reads their anomaly flags alongside the distillation layer's anomaly flags). The synthesizer runs last — it needs all briefs before it can find intersections and contradictions.

*Relationship to upstream layers:* The [data layer](../01-data-layer/README.md) defines *what raw data is collected*. The [distillation layer](../02-distillation-layer/README.md) defines *what is computed from that raw data*. This layer defines *what the LLM agents interpret from the computed data*. Portfolio state (raw state and derived metrics specified in the [data](../01-data-layer/internal/README.md) and [distillation](../02-distillation-layer/internal.md) layers) is consumed directly by decision-layer agents via tools, and the [synthesizer](synthesizer.md) has lightweight portfolio state tools for on-demand portfolio context during cross-domain synthesis.

**Universal context broadcast:** Every agent in the analysis layer (and downstream — trader, PM) receives the volatility regime classification label from the [distillation layer](../02-distillation-layer/external.md) (section 4). This label is not optional context — it's a regime flag that changes how every agent interprets its data and makes decisions.
