# Analysis layer

LLM agents interpret outputs from the [data](../01-data-layer/README.md) and [distillation](../02-distillation-layer/README.md) layers into briefs for the [decision layer](../04-decision-layer/README.md).

| Component | Document | Description |
|-----------|----------|-------------|
| Domain researchers | [domain-researchers/](domain-researchers/) | Three LLM agents scoped by sector — [tech/semis](domain-researchers/tech-semis.md), [financials](domain-researchers/financials.md), [energy](domain-researchers/energy.md) — each produces a structured brief from distillation output |
| Baseline qualitative research | [qualitative-research.md](qualitative-research.md) | Always-on sweep: headlines, macro calendar, sentiment, prediction markets, portfolio catalyst proximity — fixed scope every invocation |
| Adaptive research | [adaptive-research.md](adaptive-research.md) | Anomaly-driven investigation: LLM triages distillation and domain-researcher flags, generates research questions, runs bounded agentic search — the system's "curiosity" layer |
| Synthesizer | [synthesizer.md](synthesizer.md) | Reads all briefs and produces the unified market snapshot with typed source references — primary input for [decision layer](../04-decision-layer/README.md) agents. Lightweight portfolio-state tools for on-demand context |

*Sequencing:* Domain researchers and qualitative baseline run in parallel (independent inputs). Adaptive research follows domain researchers (reads their anomaly flags alongside distillation's). Synthesizer runs last — needs all briefs to find intersections and contradictions.

*Upstream relationship:* [Data layer](../01-data-layer/README.md) defines *what raw data is collected*; [distillation](../02-distillation-layer/README.md) defines *what is computed*; this layer defines *what LLM agents interpret*. Portfolio state (raw and derived metrics specified in the [data](../01-data-layer/internal/README.md) and [distillation](../02-distillation-layer/internal.md) layers) is consumed directly by decision-layer agents via tools; the [synthesizer](synthesizer.md) has lightweight portfolio-state tools for on-demand context during cross-domain synthesis.

**Universal context broadcast:** Every analysis-layer agent (and downstream — trader, PM) receives the volatility regime classification label from [distillation external.md §4](../02-distillation-layer/external.md). This regime flag changes how each agent interprets its data and decides.
