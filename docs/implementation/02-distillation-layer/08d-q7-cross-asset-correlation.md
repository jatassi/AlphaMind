---
status: done
completed_date: 2026-04-28
commit_id: f4f0e9388ec6fd0ae82832bbccabb9e2b108c64e
---

# 08d — Q7 cross-asset and correlation computations

## Goal

Implement the deterministic cross-asset and correlation computations from `external.md § 2 From cross-asset and correlation (quant 7)`: intra-sector pairwise correlation matrices, cross-sector rotation classification with narrative tagging, breadth/market internals, intermarket regime signals, lead-lag pair tracking with overdue flagging, and correlation-regime change detection with the narrative-lag indicator. Plus the divergence anomalies that key off these computations.

This is the layer's heaviest derived computation — Q7 has no raw data of its own, all signals are derived from Q1–Q6 and Q8 inputs.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From cross-asset and correlation (quant 7) — authoritative scope (intra-sector correlation, cross-sector rotation, breadth, intermarket, lead-lag, correlation regime change, narrative lag)
- `docs/design/02-distillation-layer/external.md` § 3 Anomaly detection — Divergence bullets
- `docs/design/02-distillation-layer/threshold-calibration.md` § Lead-lag and narrative-lag — `lead_lag_*_max_days`, `lead_lag_overdue_lead_sigma`, `narrative_lag_correlation_shift_sigma`, `narrative_lag_media_silence_hours`
- `docs/design/02-distillation-layer/threshold-calibration.md` § Persistence and percentile windows — `correlation_short_days = 20`, `correlation_long_days = 60`
- `docs/design/01-data-layer/external/quantitative.md` §§ 7a–7g — underlying quant 7 categories
- Stories 04, 05, 06, 07 — framework primitives. The `distillation_pair_lag` table from story 03 holds lead-lag state.

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/q7_cross_asset.py` —

- **Intra-sector correlation** (per `quant 7a`):
  - Compute 20-day and 60-day rolling pairwise correlation matrices within each sector (tech, semis, financials, energy) from daily-bar log returns in `ohlcv_bars` (timeframe = `1d`).
  - Persist per-pair correlation summaries in `distillation_ticker_baseline` if extending the table fits the schema; otherwise keep in-memory per invocation. The matrices are used downstream by stories 08b and 11b — coordinate on persistence vs. recompute.
  - Divergence detection: per pair within a sector, flag when current 20-day correlation deviates from its 60-day baseline by ≥ 1.5σ over the trailing 60-day variance distribution. Magnitude and duration both reported.
  - Historical divergence resolution lookup: per pair, persist resolved-divergence outcomes (when a flagged divergence "resolved" — corr returned to within 1σ of baseline). Use `distillation_event_history` with `event_kind = 'correlation_divergence'` (extending the schema's `event_kind` CHECK constraint — coordinate with story 03 to add the value).
- **Cross-sector rotation** (per `quant 7b`):
  - Compute rolling sector-ETF (XLK / SMH / XLF / XLE) relative performance ratios over 5-day and 20-day windows.
  - Rotation velocity classification: `slow_regime_shift` (gradual rotation over 5+ days; cross-sector ranking changes one or two positions) vs. `sharp_intraday_event_driven` (single-session rotation moving multiple sectors past each other).
  - Rotation narrative classification: tag the dominant driver as one of `rate_driven` (financials vs. tech), `growth_driven` (cyclicals vs. defensives — proxy: energy vs. tech), `risk_appetite_driven` (high-beta vs. low-beta — proxy: small-cap IWM vs. SPY). Use the same dominant-correlation discriminant pattern from story 08c's dollar attribution: whichever single-factor correlation explains the most of the rotation over the trailing 5 days.
- **Breadth and market internals** (per `quant 7c`):
  - Percentage of universe names above 20 / 50 / 200-day EMA (read from `ohlcv_bars`).
  - Advance/decline within each sector (count of universe sector members up vs. down on the day).
  - Equal-weight vs. cap-weight performance comparison (compute equal-weight portfolio return across universe and compare to SPY).
  - Market-wide aggregate spread, volume vs. trailing average, composite liquidity score. (Note: the aggregate spread and composite liquidity score also feed story 08c's market-liquidity composite — share the underlying primitive.)
- **Intermarket regime signals** (per `quant 7d`):
  - Stocks vs. bonds (SPY/TLT) correlation regime: positive (inflation environment) vs. negative (growth environment). Computed against the trailing 60-day window with a shift-detection flag.
  - Gold vs. real yields (GLD vs. `DFII10` from FRED): divergence detection.
  - Oil vs. energy stock beta (use crude futures proxy from EIA / WTI series in `macro_observations` vs. XLE): stability monitoring (rolling 60-day beta; flag when beta moves > 0.5 from 60-day mean).
  - VIX vs. SPY: divergence flagging (VIX rising on flat/rising market).
  - Per-relationship correlation regime stability: rolling stdev of trailing correlation estimates; emit when the stability drops by > 50% over 20 days.
- **Lead-lag relationships** (per `quant 7f`, threshold `lead_lag_overdue_lead_sigma = 1.5`):
  - For each named pair from `threshold-calibration.md § Lead-lag` (`funding_to_credit`, `credit_to_equity`, `semis_to_tech`, `financials_to_market`, `commodity_to_energy_equity`):
    - Read trailing pair-event history from `distillation_pair_lag`.
    - Detect: has the lead asset moved ≥ `lead_lag_overdue_lead_sigma` (1.5σ) within the pair's `_max_days` window without the lag tracking? Emit `overdue_lag_flag` finding with magnitude = the lead's z-score and the elapsed days since the lead move.
    - Lead-lag regime-shift detection: flag when the normal leader/follower inverts (the named "lag" asset moves first, the "lead" asset follows).
- **Correlation regime change detection** (per `quant 7g`, threshold `narrative_lag_correlation_shift_sigma = 1.5`):
  - Rolling correlation stability: stdev of trailing 60-day correlation estimates per pair.
  - Correlation breakdown: pairwise or sector-level correlation exceeding historical norms for rate of change — emit `correlation_breakdown_flag` when the pair correlation moves by ≥ `narrative_lag_correlation_shift_sigma` (1.5σ) relative to its trailing 60-day variance.
  - Dispersion shift: sudden increase in cross-stock dispersion (universe-wide stdev of single-day returns). Emit `dispersion_shift_flag` when current daily dispersion is > 1.5σ above the trailing 20-day mean.
  - **Narrative lag indicator**: cross-reference the correlation-breakdown detection with news article volume in `news_articles` over the prior `narrative_lag_media_silence_hours = 12` window. If the correlation has shifted but media coverage is silent, emit `narrative_lag_flag` (early-stage regime transition signal).
- **Output assembly**: emit `OutputBlock` instances:
  - Most Q7 outputs go to the `CORRELATION_REGIME_BRIEF` audience (the synthesizer's `CR` brief). Per the design: "Synthesizer agent (via correlation brief): Cross-asset correlation, lead-lag, regime outputs — category 7 computations."
  - Universal cross-cutting items (breadth, intermarket regime) also carry `UNIVERSAL_BROADCAST` so domain researchers see them in their input bundles.
  - Block IDs: `q7.intra_sector_correlation` (per sector), `q7.cross_sector_rotation`, `q7.breadth_internals`, `q7.intermarket_regime` (per relationship), `q7.lead_lag` (per pair), `q7.correlation_breakdown` (per detection), `q7.narrative_lag` (per detection).
- Unit tests:
  - Intra-sector correlation matrix produces expected values against a fixture of 4 tickers × 60 days of synthetic returns.
  - Pair correlation divergence fires at exactly 1.5σ; suppressed below.
  - Cross-sector rotation classification produces both velocity labels under documented input conditions.
  - Rotation narrative classification produces all three driver labels under appropriate fixtures.
  - Breadth metrics aggregate correctly across a fixture universe.
  - Stocks-vs-bonds regime label flips correctly when the correlation sign changes across the 60-day baseline.
  - Lead-lag overdue flag fires when the lead has moved ≥ 1.5σ and the lag has not tracked within `_max_days`; suppressed when the lag has tracked.
  - Lead-lag inversion flag fires when the lag asset moves first.
  - Correlation breakdown fires at exactly 1.5σ; suppressed below.
  - Dispersion shift fires correctly.
  - Narrative lag flag requires both the correlation breakdown AND zero qualifying news articles in the trailing 12 hours.
  - All Q7 outputs default to `CORRELATION_REGIME_BRIEF` audience; intermarket and breadth carry `UNIVERSAL_BROADCAST` as well.

Out of scope:
- Per-position-correlation computations (those are in `internal.md` portfolio-state derived metrics, separate layer).
- Sector ETF options flow (story 08b — Q3 owns ETF IV divergence).
- Universal volatility regime (story 09).
- Q8 commodity-specific divergences (story 08e cluster — though `external.md § 2 commodities` divergences are intentionally minimal in v1; bundled in 08e).

## Notes

The intra-sector correlation matrix's persistence question (`distillation_ticker_baseline` extension vs. in-memory) is real: the matrix is O(N²) per sector, and recomputing every invocation is cheap (a few dozen pairs × 60 days of returns each). Recommendation: compute fresh per invocation, persist only the per-pair summary (mean, stdev, last value) the lead-lag and divergence detections need. This avoids a schema change.

For the historical divergence resolution lookup, extending `distillation_event_history.event_kind` requires either modifying the story 03 CHECK constraint at migration time (adding `correlation_divergence` to the allowed set) or generalizing the constraint. Coordinate with story 03 — if 03 has already shipped, write a follow-up Alembic migration here that extends the constraint.

The cross-sector rotation narrative tagging (`rate_driven` / `growth_driven` / `risk_appetite_driven`) reuses the dominant-correlation discriminant from story 08c. Extract that into a shared helper if both stories implement it.

The narrative lag indicator's "media silence" check needs a meaningful filter on `news_articles` — count only articles whose tickers overlap with the universe and whose `topic_tags` intersect with `["macro_data", "regulatory", "geopolitical", "sector_rotation"]` (the regime-relevant tag subset). Otherwise routine company news drowns the signal.

The intermarket regime computations require non-equity series. SPY/TLT comes from `ohlcv_bars` for the ETFs (universe `benchmarks` block in `assets.yaml` includes both). GLD vs. real yields requires `DFII10` from FRED in `macro_observations`. Oil vs. energy stocks requires WTI from EIA. All series are populated by the collector — verify the series_id convention by reading recent rows.

This story is the largest of the 08* set. If implementation reveals it's too large for one PR, split per major group (intra-sector, cross-sector, intermarket, lead-lag, correlation-regime) — but keep them in one user story for planning purposes since they share the underlying ohlcv_bars / macro_observations read patterns.

## Acceptance criteria

- [ ] Intra-sector pairwise correlation matrices computed at 20-day and 60-day windows for all four sectors.
- [ ] Pair correlation divergence detection fires at exactly the 1.5σ threshold; suppressed below.
- [ ] Cross-sector rotation velocity labels (`slow_regime_shift` vs. `sharp_intraday_event_driven`) produced correctly.
- [ ] Rotation narrative classification produces `rate_driven` / `growth_driven` / `risk_appetite_driven` correctly.
- [ ] Breadth metrics (% above EMA, advance/decline, equal-vs-cap) aggregated correctly.
- [ ] Stocks-vs-bonds regime correctly identifies positive (inflation) vs. negative (growth) windows.
- [ ] Gold-vs-real-yields divergence detection fires.
- [ ] Oil-vs-energy beta stability monitoring fires when beta drifts > 0.5 from 60-day mean.
- [ ] VIX-vs-SPY divergence flag fires.
- [ ] Lead-lag overdue flag fires for each named pair under the documented condition; respects per-pair `_max_days` bounds and 1.5σ minimum lead size.
- [ ] Lead-lag regime-shift / inversion detection fires when the lag asset moves first.
- [ ] Correlation breakdown fires at exactly 1.5σ relative to trailing 60-day variance.
- [ ] Dispersion shift detection fires correctly.
- [ ] Narrative lag flag requires both correlation breakdown AND `narrative_lag_media_silence_hours` of qualifying-tag-filtered media silence.
- [ ] Q7 outputs default to `CORRELATION_REGIME_BRIEF` audience; intermarket and breadth carry `UNIVERSAL_BROADCAST`.
- [ ] Unit tests cover all of the above against fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
