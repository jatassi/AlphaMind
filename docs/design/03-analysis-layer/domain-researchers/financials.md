# Financials researcher agent (~15 tickers)

Part of the [domain researcher](../README.md) layer — LLM agents scoped by sector, not by data type. Sector scoping ensures each agent develops domain-specific pattern recognition. See [design-decisions.md](../../design-decisions.md) for the rationale.

Sibling researchers: [Tech & semis](tech-semis.md) · [Energy](energy.md)

Mandate: surface intra-sector conditions, anomalies, and thesis candidates for ~15 banks, payments, and fintech names. Specialized in rate sensitivity analysis — maps macro data releases (CPI, jobs, FOMC) to expected sector impact. Key signals: yield curve movements, credit spread changes, loan growth data, M&A activity in the sector.

---

## Inputs

Input bundle delivered at invocation start. Source documents in parentheses are authoritative for each input.

| Input | Source | Description |
|---|---|---|
| Distillation output for financials tickers | [distillation external.md](../../02-distillation-layer/external.md) §Output format | Per-ticker indicators, anomaly flags, and divergence detections scoped to the ~15 financials names; includes rate-environment-relevant macro/rates outputs (yield curve regime, credit spread changes, dollar-move attribution) from distillation §6 |
| Sector-specific qualitative inputs | [qualitative.md §6c](../../01-data-layer/external/qualitative.md) | Credit conditions narrative, rate environment commentary, M&A and deal pipeline, regulatory posture shifts, consumer and payment trends, crypto/digital assets narrative |
| Volatility regime label | [distillation external.md §4](../../02-distillation-layer/external.md) | Universal context broadcast — current regime classification plus the regime-transition flag |

---

## Output

Single sector brief per invocation conforming to the [domain researcher output contract](tech-semis.md#domain-researcher-output-contract) shared by all sector researchers. Reference prefix `SA-FIN`.

**Token budget:** 300–600 tokens total (smaller universe than tech/semis).
