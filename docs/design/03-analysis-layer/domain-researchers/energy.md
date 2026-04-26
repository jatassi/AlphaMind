# Energy researcher agent (~15 tickers)

Part of the [domain researcher](../README.md) layer — multiple LLM agents scoped by sector, not by data type. Sector scoping ensures each agent develops domain-specific pattern recognition. See [design-decisions.md](../../design-decisions.md) for the rationale on sector-scoped vs. data-type-scoped agents.

Sibling researchers: [Tech & semis](tech-semis.md) · [Financials](financials.md)

The agent's mandate is to surface intra-sector conditions, anomalies, and thesis candidates for ~15 energy names. Specialized in commodity-price linkage — maps oil/gas price movements to individual stock impact based on business mix (E&P vs. midstream vs. services). Key signals: inventory reports (EIA weekly), OPEC decisions, geopolitical events affecting supply, refining margins, LNG shipping rates.

---

## Inputs

The agent's input bundle is delivered at invocation start with the structure below. Source documents in parentheses are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Distillation output for energy tickers | [distillation external.md](../../02-distillation-layer/external.md) §Output format | Per-ticker indicators, anomaly flags, and divergence detections scoped to the ~15 energy names in the universe; includes the commodity-relevant outputs from distillation §8 (industrial metals divergence, crack spread vs. energy stock divergence, DXY-commodity correlation regime) this sector specifically depends on |
| Sector-specific qualitative inputs | [qualitative.md §6b](../../01-data-layer/external/qualitative.md) | OPEC rhetoric and compliance, inventory and supply narrative, weather and seasonal patterns, pipeline and infrastructure developments |
| Volatility regime label | [distillation external.md §4](../../02-distillation-layer/external.md) | Universal context broadcast — current regime classification plus the regime-transition flag |

---

## Output

The agent emits a single sector brief per invocation conforming to the [domain researcher output contract](tech-semis.md#domain-researcher-output-contract) shared by all sector researchers. This sector uses reference prefix `SA-ENERGY` for the synthesizer's typed reference system.

**Token budget:** 300–600 tokens total (smaller universe than tech/semis). Dense enough to be complete, compact enough to fit within the synthesizer's context alongside all other briefs.
