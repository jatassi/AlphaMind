---
status: in_progress
completed_date:
commit_id:
---

# 08a — Q1 price/volume indicators and anomalies

## Goal

Implement the deterministic price/volume computations from `external.md § 2 From price and volume (quant 1)`: multi-timeframe technical indicators, volume profile, gap analysis with per-ticker fill probability, relative performance against sector ETF and SPY, trend state and per-name volatility regime. Plus the anomaly detections that key off these computations: volume σ exceedance and price-move ATR multiple.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From price and volume (quant 1) — authoritative scope of the indicators
- `docs/design/02-distillation-layer/external.md` § 3 Anomaly detection — Price and volume bullets
- `docs/design/02-distillation-layer/threshold-calibration.md` § Anomaly detection thresholds — `volume_anomaly_sigma`, `price_move_atr_multiple`
- `docs/design/01-data-layer/external/quantitative.md` §§ 1a–1g — the underlying quant 1 categories that feed each computation
- `docs/design/01-data-layer/collector/storage.md` § `ohlcv_bars` — table the indicators read from (paired `adj_*` / `unadj_*` columns, multi-timeframe rows)
- Stories 04, 05, 06, 07 — the framework primitives this story composes

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/q1_price_volume.py` (or split across that module + helpers if it gets large) —

- **Multi-timeframe technical indicators** (per `quant 1c`): for each ticker × timeframe ∈ {`15min`, `1h`, `4h`, `1d`, `1w`}, compute:
  - RSI (14-period) — return current value plus a `divergence_flag` when the indicator at this timeframe disagrees with the indicator at the next-higher timeframe (e.g., 4h RSI bearish divergence vs. daily RSI healthy).
  - MACD (12/26/9) — return signal-line crossover state and histogram momentum direction.
  - Stochastic (14/3/3) — return %K, %D, and crossover state.
  - Bollinger bands (20/2σ) — return position within bands (0–1 normalized) and band width.
  - Keltner channels (20/2 ATR) — return position and width.
  - Moving averages — slope and crossover state for the 20 / 50 / 200 EMA pairs.
  - ADX (14-period) — return current value (trend strength, direction-agnostic).
  - ATR (14-period) — return absolute value and the per-name expansion/compression regime (compression when current ATR is below the 60-day rolling mean by ≥ 1σ; expansion when above by ≥ 1σ).
- **Multi-timeframe divergence flags**: explicit rollup field listing every indicator/timeframe pair whose divergence flag fires this invocation. `external.md` calls these "Among the most actionable outputs for sector researchers" — surface them prominently in the output payload.
- **Volume profile** (per `quant 1b`): per ticker × session × multi-day window —
  - Value area (price range capturing ~70% of volume).
  - Point of control (single most-traded price level).
  - High-volume nodes and low-volume nodes (price ranges).
  - Developing vs. settled profile classification (compare current session profile to the trailing 5-session pool).
- **Gap analysis** (per `quant 1d`):
  - Overnight gap = `today_open - prior_session_close`, expressed in absolute terms and as `gap / atr_14d` ratio.
  - Classification: full vs. partial (full = gap larger than entire prior bar's range), with-trend vs. counter-trend.
  - Gap-fill probability lookup: read from the per-ticker gap-event history (story 07's `distillation_event_history` for `event_kind = 'gap'`); use the per-ticker rate when calibrated, otherwise the sector-pooled fallback from story 04.
  - On detecting a new gap at the start of the session, append a fresh row to `distillation_event_history` with `outcome = NULL` (story 07's refresh entry point owns the write; this story passes the detected event through).
- **Relative performance** (per `quant 1e`): per ticker —
  - Rolling ratio of ticker price vs. its sector ETF (XLK / SMH / XLF / XLE per `sector_classification.sector_etf`) and vs. SPY, over 5-day and 20-day windows.
  - Intra-sector ranking: position in the sector's daily performance distribution (percentile).
  - Relative-strength regime change flag: leader↔laggard transition over the multi-day windows.
- **Trend state and regime** (per `quant 1f`): per ticker —
  - Per-timeframe trend state ∈ {`trending_up`, `trending_down`, `range_bound`}.
  - Composite multi-timeframe trend score (weighted by timeframe per the design's "structure of price matters" principle — daily and 4h dominate; 15min provides directional confirmation).
  - 52-week range percentile.
  - Distance from the 20 / 50 / 200 EMA in ATR units.
  - Per-name volatility regime ∈ {`low_vol_compression`, `high_vol_expansion`, `transitional`} based on Bollinger band width vs. its 60-day baseline. (Distinct from the universal market-volatility regime in story 09.)
- **Anomaly detections** — emit one `AnomalyFlag` per detection on the corresponding `OutputBlock`:
  - `volume_anomaly`: today's volume exceeds `volume_anomaly_sigma` (default 2.5) σ above the 20-day trailing mean (from `distillation_ticker_baseline` for `kind = 'volume'`). Severity defaults to `investigate_now`; downgrade to `investigate_if_persists` when the calibration state is `bootstrap`.
  - `price_move_anomaly`: today's daily-bar move exceeds `price_move_atr_multiple` (default 1.5) × ATR. Severity defaults to `investigate_now`.
- **Output assembly**: each indicator group emits its own `OutputBlock` with the appropriate `block_id` (`q1.technicals`, `q1.volume_profile`, `q1.gap`, `q1.relative_performance`, `q1.trend_state`, `q1.divergence_flags`) and `audience` set to the ticker's sector audience (`SECTOR_TECH_SEMIS` / `SECTOR_FINANCIALS` / `SECTOR_ENERGY`) per `sector_classification`.
- Unit tests:
  - One canonical per-indicator test against a hand-constructed fixture bar series with known expected values.
  - Multi-timeframe divergence flag fires on the documented condition (4h RSI <30 while daily RSI >50) and stays silent otherwise.
  - Gap detection writes a new event row with `outcome = NULL`; gap-fill probability lookup falls back to sector-pooled when the per-ticker history is below the minimum.
  - Volume anomaly fires at 2.5σ exactly (boundary case); does not fire at 2.49σ.
  - Price-move anomaly fires at 1.5×ATR exactly; does not fire at 1.49×ATR.
  - Anomaly severity downgrades to `investigate_if_persists` when calibration is `bootstrap`.
  - Outputs are tagged with the correct sector audience based on the ticker's `sector_classification` row.

Out of scope:
- Sentiment-based news-price divergence anomaly (story 08f — qualitative).
- Lead-lag and cross-asset (story 08d — Q7).
- Universal volatility regime (story 09).
- The orchestrator that calls this story's entry points (story 12).
- Q2 order flow (deferred per [storage.md § Deferred categories](../../../design/01-data-layer/collector/storage.md#deferred-categories)).

## Notes

The "five timeframes" set is exactly the `ohlcv_bars.timeframe` enum: `15min` / `1h` / `4h` / `1d` / `1w`. Read paired `adj_*` columns for indicator computation (corporate-action-adjusted history), unadjusted only for volume calculations where adjustment would distort the share count.

The volume profile's "value area" computation is well-established as a Market Profile concept — implement the standard algorithm (sort price levels by volume descending, accumulate until ≥70% of session volume). Document the 70% threshold in a named constant; it's a settled convention, not a tunable.

The "developing vs. settled profile" classification compares the current session's profile shape to a trailing pool — a session whose value area substantially overlaps the recent multi-session pool is "settled"; one shifting outside the pool is "developing." Define overlap quantitatively (e.g., overlap ≥ 60% of the multi-session value area is settled).

For relative-strength regime change, define "leader" / "laggard" by intra-sector quartile: top quartile is leader, bottom quartile is laggard. A leader→laggard transition over 5 trading days is the regime-change signal — emit it as a flag, not just a numeric output.

ATR computation reuses `compute_atr` from story 06's normalization library. The 60-day ATR baseline (used for the per-name volatility regime) reads from `distillation_ticker_baseline` for `kind = 'atr'` (60-day window — this is a separate baseline from the 14-day raw ATR; the refresh primitive in story 07 should already handle it via the `atr_baseline_days = 14` config but the volatility-regime baseline uses the wider window — coordinate with story 07 if the kind needs to support multiple windows).

`distillation_ticker_baseline.kind` should distinguish between the two ATR uses if the wider 60-day window is also persisted; otherwise compute it on the fly from the underlying bars and only persist the 14-day ATR. Either approach works — pick one and document it in the code.

The bootstrap-mode severity downgrade is important: a 2.5σ anomaly against a sector-pooled denominator (because per-ticker has < 30 observations) is genuinely weaker evidence than the same anomaly against per-ticker history. Surfacing this through severity (rather than swallowing the flag) keeps downstream consumers free to act on it while letting them weight conviction appropriately.

## Acceptance criteria

- [ ] All five timeframes covered for each technical indicator listed above.
- [ ] Multi-timeframe divergence flag computed per pairwise adjacent-timeframe comparison; rollup field lists every firing pair.
- [ ] Volume profile computation produces value area (70% capture), POC, and high/low-volume nodes.
- [ ] Developing vs. settled classification defined quantitatively (named overlap threshold).
- [ ] Gap detection writes a new event-history row with `outcome = NULL` on first sight; does not duplicate.
- [ ] Gap-fill probability lookup uses per-ticker rate when calibrated; sector-pooled fallback when bootstrap.
- [ ] Relative performance covers vs. sector ETF and vs. SPY at 5-day and 20-day windows.
- [ ] Intra-sector ranking returns percentile and leader/laggard classification.
- [ ] Relative-strength regime-change flag fires on the documented quartile transition.
- [ ] Trend state computed per timeframe with a composite multi-timeframe score.
- [ ] Per-name volatility regime ∈ {`low_vol_compression`, `high_vol_expansion`, `transitional`}.
- [ ] `volume_anomaly` fires at exactly the threshold; not below.
- [ ] `price_move_anomaly` fires at exactly the threshold; not below.
- [ ] Anomaly severity downgrades to `investigate_if_persists` under bootstrap calibration.
- [ ] Each indicator group emits an `OutputBlock` with the correct `block_id` and sector-scoped `audience`.
- [ ] Unit tests cover all of the above against hand-constructed fixtures with known expected values.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
