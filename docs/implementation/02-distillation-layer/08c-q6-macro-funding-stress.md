---
status: in_progress
completed_date:
commit_id:
---

# 08c — Q6 macro indicators and funding-stress composite

## Goal

Implement the deterministic macro computations from `external.md § 2 From macro and rates (quant 6)`: yield curve regime classification, inflation regime classification, dollar move attribution, the funding-stress composite (4-component), and the market-wide liquidity composite. Plus the macro surprise anomaly and the funding-stress composite alert.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From macro and rates (quant 6) — authoritative scope
- `docs/design/02-distillation-layer/external.md` § 3 Anomaly detection — Macro bullets
- `docs/design/02-distillation-layer/threshold-calibration.md` § Anomaly detection thresholds — `macro_surprise_percentile`, `funding_stress_component_alert_count`, `funding_stress_component_percentile`, `market_liquidity_alert_percentile`
- `docs/design/01-data-layer/external/quantitative.md` §§ 6a–6g — underlying quant 6 categories
- `docs/design/01-data-layer/collector/storage.md` § `macro_observations`, § `treasury_auctions` — the tables this story reads
- Stories 04, 05, 06, 07 — framework primitives

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/q6_macro.py` —

- **Yield curve regime** (per `quant 6a`): from FRED-sourced treasury yields in `macro_observations` (series `DGS3MO`, `DGS2`, `DGS5`, `DGS10`, `DGS30`):
  - Compute the 2s10s, 3m10y, and 5s30s spreads.
  - Classify as one of `normal_upward_sloping`, `flat`, `inverted`, `steepening`, `flattening`. Steepening / flattening are transition labels — fired when the trailing 5-day change in the 2s10s spread exceeds 25 bps.
  - Emit the regime label plus a `regime_transition` flag when the label changed since the prior invocation.
- **Inflation regime** (per `quant 6c`): from FRED inflation series (`T10YIE` 10y breakeven, `CPIAUCSL` headline CPI, `PCEPI` PCE) and recent macro-release surprises:
  - Classify as one of `hot`, `cooling`, `stable`, `deflation_risk`. Use a rule table: `hot` = trailing 3-month breakeven trending up by ≥ 30bps AND CPI surprises positive in the last 3 releases; `cooling` = breakeven trending down ≥ 30bps AND surprises negative; `stable` = neither; `deflation_risk` = breakeven < 1.5% sustained over 30 days.
  - Emit the label plus a transition flag.
- **Dollar move attribution** (per `quant 6f`): for each significant DXY move (≥ 0.5% session change) classify the driver:
  - `rate_differential_driven`: USD move correlates with same-day shift in 10-year Treasury yield differential vs. G10 average (proxy: shift in `DGS10` minus shift in synthetic G10 yield from a small predefined component set).
  - `risk_sentiment_driven`: USD move correlates with same-day equity move (SPY) in the typical risk-off direction (USD up, SPY down).
  - `trade_flow_driven`: residual when neither correlation is dominant.
  - Use a simple discriminant: whichever single-factor correlation has the largest |coefficient| over the trailing 20 days.
- **Funding-stress composite** (per `quant 6e`): four components from `macro_observations`:
  - SOFR-OIS spread (series `SOFRINDEX` minus a synthesized OIS reference).
  - Repo-Treasury spread.
  - Term repo premium.
  - MMF flow direction (from FRED `WMMFP` weekly money-market fund total).
  - Per-component: compute the trailing 60-day percentile (uses `distillation_composite_state` for `composite_kind = 'funding_stress'`'s `component_breakdown_json` history, OR computes against recent `macro_observations` rows directly — pick the same source story 07's `refresh_composite_state` uses).
  - Composite alert: `alert_active = True` when ≥ `funding_stress_component_alert_count` (default 2) of the 4 components are above `funding_stress_component_percentile` (default 90th). Emit `funding_stress_alert` flag.
- **Market-wide liquidity composite** (the partner of funding stress, fed by quant 7c inputs but homed in this story per `external.md`'s grouping under "Persistent state and composites"):
  - Aggregate composite computed from sector-ETF spread averages, average universe spreads, and other liquidity inputs (per the available data — keep the formula simple and document it in the source).
  - Compute the trailing 60-day percentile via `distillation_composite_state` for `composite_kind = 'market_liquidity'`.
  - `alert_active = True` when in the bottom 10% (`market_liquidity_alert_percentile = 10`).
- **Macro surprise anomaly** (per `external.md § 3 Macro` + threshold `macro_surprise_percentile = 90`): for each scheduled macro release in `event_calendar` whose actual value has landed (cross-reference `event_calendar.status = 'completed'` with a recent `macro_observations` row for the corresponding series), compute the surprise via story 06's `macro_surprise_zscore` against the trailing 24-month surprise distribution. Flag when the |z-score| corresponds to top-10%-of-distribution (the percentile threshold). Emit `macro_surprise_anomaly` with magnitude = the z-score and severity `investigate_now`.
- **Output assembly**: emit `OutputBlock` instances with `audience = UNIVERSAL_BROADCAST` (macro is universal context, not sector-scoped):
  - `q6.yield_curve_regime`
  - `q6.inflation_regime`
  - `q6.dollar_attribution`
  - `q6.funding_stress`
  - `q6.market_liquidity`
  - `q6.macro_surprise_anomaly` (per detection)
- Unit tests:
  - Yield curve regime correctly classifies under each of the five conditions.
  - Yield curve transition flag fires only when the label changes between invocations.
  - Inflation regime correctly classifies each of the four conditions.
  - Dollar attribution correctly assigns the dominant-correlation label.
  - Funding-stress composite alert fires at exactly 2 / 4 components above the percentile; suppressed at 1.
  - Market-liquidity alert fires at exactly the 10th percentile threshold.
  - Macro surprise anomaly fires at the 90th-percentile boundary; suppressed below.
  - All outputs carry `audience = UNIVERSAL_BROADCAST`.

Out of scope:
- The four-tier volatility regime classification (story 09 owns the composite VIX/VVIX/term-structure regime; this story's "yield curve regime" and "inflation regime" are macro-thematic regimes, not the universal volatility regime).
- Treasury-auction-specific signals (`auction_yield_bp`, `tail_bp`, `bid_to_cover` in `treasury_auctions`) — out of v1 scope per `external.md` (no dedicated section).
- Per-sector macro impact mapping (rate cuts → financials, etc.) — that's analyst-layer interpretation.

## Notes

The yield curve and inflation regime computations are deliberately rule-based, not statistical. The thresholds (25 bps for steepening detection, 30 bps for inflation breakeven trend) are documented in the source as `# regime classification cutoff per <design-doc-citation>` and not exposed as Class A config — they're definitional, not tuning surfaces, and tightening them risks producing regime labels the analysts and PM aren't trained to read. If they prove unstable in paper trading, surface them as Class A in a future story rather than tuning them in code.

The funding-stress composite's four components require some synthesis because `macro_observations` doesn't carry a pre-computed SOFR-OIS or repo-Treasury spread series — those are derived. The collector populates the underlying series; this story computes the spread inline. Document the series-pair construction in the source so a reader can verify the spread arithmetic against FRED.

The dollar-move attribution heuristic is a simplification — full attribution would require a multi-factor model. The "largest |correlation coefficient| over trailing 20 days" approach is the simplest version that produces a non-arbitrary label; document the simplification.

The market-liquidity composite's exact formula is partially under-specified in `external.md` ("aggregate spread, volume vs. trailing average, composite liquidity score" per § Breadth and market internals). Pick a reasonable composite — e.g., normalized average of (sector-ETF spread percentiles + universe-average spread percentiles + universe-average volume percentile inverted) — document the formula choice in the source. The exact composition can be tuned later; the Class A `market_liquidity_alert_percentile` lever is the operator's primary tuning surface.

`UNIVERSAL_BROADCAST` audience is appropriate for these blocks because every analysis-layer agent reads macro context. The synthesizer also consumes them via the correlation/regime brief, but that's covered by story 11b's assembly logic — this story just tags blocks as universally targeted.

## Acceptance criteria

- [ ] Yield curve regime classification produces one of the five documented labels.
- [ ] Yield curve transition flag fires only on label change between invocations.
- [ ] Inflation regime classification produces one of the four documented labels.
- [ ] Dollar attribution produces one of the three documented labels based on dominant correlation.
- [ ] Funding-stress composite computes per-component percentiles and an alert flag at the documented threshold.
- [ ] Funding-stress alert fires at exactly 2/4 components above percentile; not at 1/4.
- [ ] Market-liquidity composite computes its percentile and alert flag.
- [ ] Market-liquidity alert fires at exactly the 10th-percentile boundary.
- [ ] Macro surprise anomaly uses story 06's `macro_surprise_zscore` and fires at the documented threshold.
- [ ] All outputs carry `audience = UNIVERSAL_BROADCAST`.
- [ ] Unit tests cover all classification and threshold conditions with hand-constructed fixtures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
