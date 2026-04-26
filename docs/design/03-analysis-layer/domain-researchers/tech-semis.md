# Tech & semiconductors researcher agent (~35 tickers)

Part of the [domain researcher](../README.md) layer — multiple LLM agents scoped by sector, not by data type. Sector scoping ensures each agent develops domain-specific pattern recognition. See [design-decisions.md](../../design-decisions.md) for the rationale on sector-scoped vs. data-type-scoped agents.

Sibling researchers: [Financials](financials.md) · [Energy](energy.md)

The agent's mandate is to surface intra-sector conditions, anomalies, and thesis candidates for ~35 tech and semiconductor names. These sectors are grouped because of heavy cross-correlation (AI narrative moves both), but the agent should distinguish between mega-cap macro sensitivity and growth-name narrative sensitivity. Key signals: earnings sentiment shifts, AI infrastructure spending signals, supply chain data (TSMC → NVDA chain), export control / regulatory developments.

---

## Inputs

The agent's input bundle is delivered at invocation start with the structure below. Source documents in parentheses are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Distillation output for tech & semis tickers | [distillation external.md](../../02-distillation-layer/external.md) §Output format | Per-ticker indicators (multi-timeframe technicals, volume profile, gap analysis, relative performance, trend state, options flow classification, short-selling estimates, fundamentals scorecard), anomaly flags, and divergence detections — all scoped to the ~35 tech and semiconductor names in the universe |
| Sector-specific qualitative inputs | [qualitative.md §6a](../../01-data-layer/external/qualitative.md) | AI narrative and spending signals, supply chain intelligence, product cycle dynamics, competitive dynamics, export controls |
| Volatility regime label | [distillation external.md §4](../../02-distillation-layer/external.md) | Universal context broadcast — current regime classification (low-vol compression, vol expansion, crisis/spike, vol normalization) plus the regime-transition flag |

---

## Output

The agent emits a single sector brief per invocation conforming to the [domain researcher output contract](#domain-researcher-output-contract) shared by all sector researchers. Structural validation is by hand-written validator (see [llm-output-validation.md](../../testing/llm-output-validation.md)); the prose contract below is the validator's source. This sector uses reference prefix `SA-TECH` for the synthesizer's typed reference system.

**Token budget:** 400–800 tokens total. Dense enough to be complete, compact enough to fit within the synthesizer's context alongside all other briefs.

---

### Domain researcher output contract

This schema is shared across all domain researcher agents (tech-semis, financials, energy). The [synthesizer](../synthesizer.md) consumes these briefs and assigns typed reference IDs. The schema ensures consistent structure so reference IDs are stable and retrieval works correctly.

```
SECTOR BRIEF: {sector_label}
Invocation: {invocation_id}
Signal quality: {HIGH | MODERATE | LOW | DEGRADED}
  [If DEGRADED: reason — e.g., "missing options flow data due to API failure"]

=== KEY FINDINGS ===
[SA-{SECTOR}-1] {one-sentence finding}
  Tickers: {affected tickers}
  Signal type: {price_action | flow | options | fundamental | sentiment | technical | cross_asset}
  Strength: {strong | moderate | weak}
  Detail: {2–3 sentence elaboration with specific data points}

[SA-{SECTOR}-2] ...
  ...

[SA-{SECTOR}-N] ...
  (3–5 findings per brief. Each finding MUST have a unique sequential index.
   The index is the synthesizer's reference handle — e.g., [SA-TECH-3].)

=== FLAGGED ANOMALIES ===
[SA-{SECTOR}-ANOM-1] {anomaly description}
  Anomaly type: {volume | price_flow_divergence | correlation_break | options_skew | other}
  Tickers: {affected tickers}
  Severity: {investigate_now | investigate_if_persists | note_for_context}
  Suggested question: {a specific research question for adaptive research}

[SA-{SECTOR}-ANOM-M] ...
  (0–3 anomalies per brief. Only flag genuinely unusual observations,
   not routine market movements.)

=== THESIS CANDIDATES ===
[SA-{SECTOR}-TC-1]
  Ticker: {primary ticker}
  Direction: {long | short}
  Setup type: {catalyst | mean_reversion | momentum | divergence | event}
  Catalyst/driver: {1 sentence — what makes this actionable now}
  Time horizon: {hours estimate, e.g., "4–24h" or "24–72h"}
  Conviction sketch: {low | moderate | high} with 1-sentence justification
  Key risk: {primary risk to the thesis}

[SA-{SECTOR}-TC-P] ...
  (0–3 thesis candidates per brief. These are preliminary sketches,
   not full trade recommendations — the analyst develops them further.)
```

**Schema design rationale:**

**Sequential indexing per section.** Key findings use `SA-{SECTOR}-N`, anomalies use `SA-{SECTOR}-ANOM-N`, thesis candidates use `SA-{SECTOR}-TC-N`. The prefix variation avoids index collision across sections — `[SA-TECH-3]` is always the third key finding, never an anomaly or thesis candidate.

**Signal type taxonomy.** A fixed set of signal types (`price_action`, `flow`, `options`, `fundamental`, `sentiment`, `technical`, `cross_asset`) enables the synthesizer to identify cross-domain intersections mechanically — e.g., a `flow` signal in tech and a `sentiment` signal in qualitative research both pointing to the same name.

**Anomaly severity levels.** Three levels rather than a binary flag: `investigate_now` goes directly to the adaptive research triage queue, `investigate_if_persists` is noted for re-check at next invocation, `note_for_context` is informational only.

**Thesis candidate structure.** Deliberately lean — just enough for the synthesizer to cross-reference across sectors and for the analyst to decide whether to develop further. The full thesis structure (with bracket legs, invalidation conditions, sizing) is the analyst's job per [analyst.md](../../04-decision-layer/analyst.md).

**Degraded signal quality flag.** When the researcher's input data is incomplete (API failure, stale data), the `DEGRADED` flag with reason tells downstream consumers to weight this brief's findings lower. The synthesizer surfaces this to the analyst as a confidence qualifier.
