# Financials researcher agent (~15 tickers)

Part of the [domain researcher](../README.md) layer — multiple LLM agents scoped by sector, not by data type. Sector scoping ensures each agent develops domain-specific pattern recognition. See [design-decisions.md](../../design-decisions.md) for the rationale on sector-scoped vs. data-type-scoped agents.

Sibling researchers: [Tech & semis](tech-semis.md) · [Energy](energy.md)

The agent's mandate is to surface intra-sector conditions, anomalies, and thesis candidates for ~15 banks, payments, and fintech names. Specialized in rate sensitivity analysis — maps macro data releases (CPI, jobs, FOMC) to expected sector impact. Key signals: yield curve movements, credit spread changes, loan growth data, M&A activity in the sector.

---

## Inputs

The agent's input bundle is delivered at invocation start with the structure below. Source documents in parentheses are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Distillation output for financials tickers | [distillation external.md](../../02-distillation-layer/external.md) §Output format | Per-ticker indicators, anomaly flags, and divergence detections scoped to the ~15 financials names in the universe; includes the rate-environment-relevant macro/rates outputs (yield curve regime, credit spread changes, dollar-move attribution) from distillation §6 that this sector specifically depends on |
| Sector-specific qualitative inputs | [qualitative.md §6c](../../01-data-layer/external/qualitative.md) | Credit conditions narrative, rate environment commentary, M&A and deal pipeline, regulatory posture shifts, consumer and payment trends, crypto/digital assets narrative |
| Volatility regime label | [distillation external.md §4](../../02-distillation-layer/external.md) | Universal context broadcast — current regime classification plus the regime-transition flag |

---

## Output

The agent emits a single sector brief per invocation conforming to the [domain researcher output contract](tech-semis.md#domain-researcher-output-contract) shared by all sector researchers. This sector uses reference prefix `SA-FIN` for the synthesizer's typed reference system.

**Token budget:** 300–600 tokens total (smaller universe than tech/semis). Dense enough to be complete, compact enough to fit within the synthesizer's context alongside all other briefs.
