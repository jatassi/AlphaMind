# Energy researcher agent (~15 tickers)

Part of the [domain researcher](../README.md) layer — LLM agents scoped by sector, not by data type. Sector scoping ensures each agent develops domain-specific pattern recognition. See [design-decisions.md](../../design-decisions.md) for the rationale.

Sibling researchers: [Tech & semis](tech-semis.md) · [Financials](financials.md)

Mandate: surface intra-sector conditions, anomalies, and thesis candidates for ~15 energy names. Specialized in commodity-price linkage — maps oil/gas price movements to individual stock impact based on business mix (E&P vs. midstream vs. services). Key signals: inventory reports (EIA weekly), OPEC decisions, geopolitical events affecting supply, refining margins, LNG shipping rates.

---

## Inputs

Input bundle delivered at invocation start. Source documents in parentheses are authoritative for each input.

| Input | Source | Description |
|---|---|---|
| Distillation output for energy tickers | [distillation external.md](../../02-distillation-layer/external.md) §Output format | Per-ticker indicators, anomaly flags, and divergence detections scoped to the ~15 energy names; includes commodity-relevant outputs from distillation §8 (industrial metals divergence, crack spread vs. energy stock divergence, DXY-commodity correlation regime) |
| Sector-specific qualitative inputs | [qualitative.md §6b](../../01-data-layer/external/qualitative.md) | OPEC rhetoric and compliance, inventory and supply narrative, weather and seasonal patterns, pipeline and infrastructure developments |
| Volatility regime label | [distillation external.md §4](../../02-distillation-layer/external.md) | Universal context broadcast — current regime classification plus the regime-transition flag |

---

## Output

Single sector brief per invocation conforming to the [domain researcher output contract](tech-semis.md#domain-researcher-output-contract) shared by all sector researchers. Reference prefix `SA-ENERGY`.

**Token budget:** 300–600 tokens total (smaller universe than tech/semis).
