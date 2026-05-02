---
status: done
completed_date: 2026-04-28
commit_id: f51f43ed2b86450eafda4ea2f65f2d05a396b4e1
---

# 08e — Q12 corporate actions signals

## Goal

Implement the deterministic computations from `external.md § 2 From corporate actions (quant 12)`: event novelty detection (unusual scheduling — e.g., a company that hasn't held an investor day in two years suddenly schedules one), and ETF flow vs. single-name flow divergence (ETF outflows paired with single-name institutional buying). Plus the routine corporate-action-driven anomaly detection that supports the analysis layer's understanding of the catalyst landscape.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From corporate actions (quant 12) — authoritative scope: event novelty detection, ETF flow vs. single-name flow divergence
- `docs/design/01-data-layer/external/quantitative.md` §§ 12d, 12f — underlying scheduling pattern and ETF/single-name flow data
- `docs/design/01-data-layer/collector/storage.md` § `event_calendar`, § `corporate_actions`, § `etf_membership` — the tables this story reads
- Stories 04, 05, 06, 07 — framework primitives

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/q12_corporate_actions.py` —

- **Event novelty detection** (per `quant 12d`): scan `event_calendar` for scheduled events whose pattern is unusual relative to the issuing company's history. Two flag types:
  - `unusual_event_cadence`: an event of type `investor_day` / `conference` / `product_launch` scheduled by a ticker that hasn't held one in ≥ 2 years (look back across `event_calendar` rows for the same `ticker` × `event_type` with `status` ∈ {`completed`, `scheduled`}).
  - `event_clustering`: ≥ 3 events of `event_type` ∈ {`investor_day`, `conference`, `product_launch`, `regulatory_decision`} scheduled by universe names within the same sector inside a 30-day forward window. The cluster itself is the signal — sector-level event density rises before regime shifts.
- **ETF flow vs. single-name flow divergence** (per `quant 12f`): cross-reference ETF membership weights with universe ticker flow patterns. For each sector ETF (XLK / SMH / XLF / XLE) and its top-10-weight constituents:
  - Compute ETF net flow proxy: change in `etf_membership.weight_pct` over the trailing 5-day window for the ETF as a whole, plus volume comparison vs. its 20-day baseline. Distillation already maintains the volume baseline via story 07; this computation reads it.
  - Compute single-name institutional-flow proxy: aggregate of options BTO flow (from story 08b's persisted output if available, or recompute from `options_contract_snapshots`) plus block-trade activity from the order-flow categories. Q2 microstructure is deferred — fall back to options-flow alone for v1.
  - Emit `etf_vs_single_name_divergence` flag when ETF outflows (proxy: ETF volume below 1σ vs. baseline AND price weakness) coincide with single-name institutional buying (BTO call flow > 1σ on ≥ 2 of the top-10 constituents). The combination suggests "someone using the ETF sell as cover to accumulate individual names."
- **Recent corporate-action awareness**: emit a per-ticker block listing corporate actions from `corporate_actions` whose `ex_date` falls within ±5 trading days of the current invocation. Pure pass-through; the analysis layer reads it for thesis context (a ticker about to go ex-dividend tomorrow is in a different state than the same ticker mid-quarter).
- **Output assembly**: emit `OutputBlock` instances:
  - `q12.event_novelty` per detection (unusual cadence or clustering); audience = sector audience for the affected ticker.
  - `q12.etf_vs_single_name_divergence` per detection; audience = the ETF's sector audience.
  - `q12.recent_corporate_actions` per ticker with relevant actions in window; audience = ticker's sector audience.
- Unit tests:
  - `unusual_event_cadence` fires when prior-event lookback finds the prior matching event was > 2 years ago; suppressed when one was held within 2 years.
  - `event_clustering` fires at exactly 3 events in 30 days within a sector; suppressed at 2.
  - `etf_vs_single_name_divergence` fires when both legs are present (ETF weakness + single-name BTO on top-10); suppressed when only one leg fires.
  - Recent corporate actions block correctly populates entries for ex-dates within ±5 trading days; empty when no actions are imminent.

Out of scope:
- The mechanical OMS-side corporate action processing (lives in [`corporate-actions.md`](../../../design/05-execution-layer/corporate-actions.md) and is owned by the execution layer, not distillation).
- Symbol-change-aware ticker history reconciliation (covered by `corporate_actions` table population in collector).
- The `event_calendar.status` lifecycle (collector concern; this story reads, doesn't update).

## Notes

The "2 years" lookback for unusual event cadence is documented in `quant 12d` as the example threshold ("a company that hasn't held an investor day in two years"). Encode as a named constant in the source — `UNUSUAL_EVENT_CADENCE_LOOKBACK_DAYS = 730` — not a Class A config knob; the threshold is definitional.

The ETF flow proxy is intentionally a coarse approximation. Full ETF flow requires creation/redemption data the data layer doesn't currently collect. The "weight_pct change + volume" proxy is the simplest signal that captures relative drawdown without a new data source. Document the simplification in source comments and tag the output with `attribution_method = "weight_volume_proxy"` so downstream readers know its limit.

For the single-name institutional-flow proxy, the dependency on story 08b's outputs is real but loose: this story can either (a) read the persisted Q3 output blocks if they're written before this story runs, or (b) recompute the Q3 BTO flow on the fly. Recommendation: recompute on the fly. The dependency-graph cleanliness is worth the small redundancy. Coordinate with the orchestrator (story 12) to ensure either ordering works.

The recent-corporate-actions pass-through block is a small but meaningful addition to the design doc's spec — the design names "event novelty" and "ETF/single-name divergence" but the analyst and strategist need ex-date awareness as routine context. Including it here keeps the corporate-actions concerns in one story; if the design doc is later updated, this implementation aligns.

## Acceptance criteria

- [ ] `unusual_event_cadence` correctly fires when prior-event lookback finds the most recent matching event > 2 years ago.
- [ ] `event_clustering` fires at exactly 3 events in 30 days within a sector.
- [ ] `etf_vs_single_name_divergence` requires both legs (ETF weakness + single-name BTO).
- [ ] Recent corporate-actions block populates entries for ex-dates within ±5 trading days.
- [ ] All outputs carry the appropriate sector audience.
- [ ] `attribution_method` field documents the ETF flow proxy simplification.
- [ ] Named constants used for the 2-year lookback and 30-day clustering window.
- [ ] Unit tests cover all detection conditions with fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
