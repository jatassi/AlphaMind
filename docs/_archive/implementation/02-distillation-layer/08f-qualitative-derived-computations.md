---
status: done
completed_date: 2026-04-28
commit_id: 3a40c27011ae5dbe25a16b977ef65704e3837239
---

# 08f — Qualitative-derived deterministic computations

## Goal

Implement the deterministic computations from `external.md § 2 From qualitative data`: news–price divergence detection (cross-referencing ingested directional news classification with recent price action), per-ticker sentiment percentile calibration (trailing distributions per ticker, current readings expressed as per-name percentiles), and prediction-market deltas with cross-platform normalization. These are the three exceptions where qualitative data admits programmatic distillation; everything else qualitative is LLM-interpreted at the analysis layer.

## Reading

- `docs/design/02-distillation-layer/external.md` § 2 From qualitative data — News–price divergence, Sentiment calibration, Prediction market deltas
- `docs/design/02-distillation-layer/external.md` § 4 Persistent state and composites — Prediction market state, Trailing sentiment distributions
- `docs/design/02-distillation-layer/threshold-calibration.md` § Anomaly detection thresholds — `news_price_divergence_window_hours = 12`
- `docs/design/02-distillation-layer/threshold-calibration.md` § Prediction market delta — `prediction_market_delta_pp_threshold = 5.0`, `prediction_market_low_liquidity_volume_min_usd = 10000`
- `docs/design/02-distillation-layer/threshold-calibration.md` § Persistence and percentile windows — `sentiment_baseline_days = 60`, `sentiment_min_observations = 30`, `prediction_market_history_days = 30`
- `docs/design/01-data-layer/external/qualitative.md` §§ 1, 2a, 3 — underlying news, sentiment, prediction-market data
- `docs/design/01-data-layer/collector/storage.md` § `news_articles`, § `news_article_tickers`, § `prediction_market_contracts`, § `prediction_market_snapshots` — tables this story reads
- Stories 04, 05, 06, 07 — framework primitives. Stories 03's `distillation_ticker_baseline` (for `kind = 'sentiment'`) and `distillation_contract_history` are the persistence targets here.

## Depends on

- 04, 05, 06, 07.

## Scope

In scope: under `src/alphamind/distillation/qualitative_derived.py` —

- **News-price divergence detection** (per `external.md § 2 News-price divergence (qualitative 1a)`):
  - For each universe ticker, look back over `news_price_divergence_window_hours = 12` hours of `news_articles` (joined to the ticker via `news_article_tickers`).
  - Aggregate the `vendor_sentiment_label` field across articles in the window — count `positive`, `negative`, `neutral`, `mixed`. Determine a dominant directional classification per ticker when one direction has > 60% of non-neutral articles.
  - Cross-reference with the same window's price action from `ohlcv_bars` (timeframe = `1h`).
  - Emit `news_price_divergence` flag in two directions:
    - `priced_in`: dominant negative news AND price flat or rising (price change > 0 over the window).
    - `hidden_problem`: dominant positive news AND price flat or falling (price change < 0 over the window).
  - Magnitude = the |sign mismatch| — sentiment-direction strength × price-direction strength.
  - Severity = `investigate_now`.
- **Per-ticker sentiment calibration** (per `external.md § 2 Sentiment calibration (qualitative 2a)`):
  - Maintain trailing per-ticker sentiment distributions in `distillation_ticker_baseline` for `kind = 'sentiment'` (per story 07's refresh primitive).
  - On each invocation, compute current sentiment readings (average `news_article_tickers.vendor_sentiment_score` filtered to the ticker over the trailing 4-hour window — Marketaux populates per-(article, ticker) scores; this is a proxy until aggregated social-sentiment ingestion lands; document the proxy choice in source).
  - Express each current reading as a per-ticker percentile against the trailing baseline distribution — e.g., "TSLA current sentiment is in its 88th percentile."
  - Calibration: when `sentiment_min_observations = 30` is not met for a ticker, fall back via `tag_with_fallback` to `universe_pooled_sentiment_distribution` from story 04. Tag the output `BOOTSTRAP`.
  - Per `external.md`: "TSLA's 'neutral' is louder than most names' extremes — per-ticker calibration prevents false flags on high-sentiment-volatility names." Surface the per-ticker percentile, not a universal score.
- **Prediction market deltas** (per `external.md § 2 Prediction market deltas (qualitative 3)`, `external.md § 4 Prediction market state`):
  - Per tracked contract: compute delta since the immediately prior invocation's snapshot from `distillation_contract_history` (story 07's `refresh_contract_history` writes the new row; this story reads the prior to compute the delta).
  - Flag moves > `prediction_market_delta_pp_threshold = 5.0` percentage points since last pull as `prediction_market_delta_anomaly` with severity `investigate_now`.
  - Trailing history per contract: surface the prior 30-day probability series (`prediction_market_history_days = 30`) so analysis-layer agents see trajectory, not just current level.
  - **Cross-platform probability normalization**: when the same outcome trades on multiple platforms (e.g., a "FOMC Jan rate hold" contract on both Polymarket and Kalshi), compute the platform-weighted normalized probability. Weighting = liquidity-proportional (`prediction_market_snapshots.liquidity_usd`). Implement a simple matcher: contracts whose `description` contains the same key phrase tokens (after lowercase and stop-word removal) and same `category` are candidates; emit normalized cross-platform probability when a match is found.
  - **Low-liquidity tagging**: contracts with 24-hour notional volume below `prediction_market_low_liquidity_volume_min_usd = 10,000` get a `low_liquidity = True` field on the output; their deltas are computed but downstream consumers weight them lower.
- **Output assembly**: emit `OutputBlock` instances:
  - `qual.news_price_divergence` per ticker with a divergence; audience = ticker's sector audience.
  - `qual.sentiment_percentile` per ticker with current sentiment data; audience = ticker's sector audience.
  - `qual.prediction_market_delta` per contract with a delta over threshold; audience = `UNIVERSAL_BROADCAST` (prediction markets are cross-cutting context — every analysis agent reads them via the qualitative researcher's input bundle).
  - `qual.prediction_market_normalized` per cross-platform match; audience = `UNIVERSAL_BROADCAST`.
- Unit tests:
  - News-price divergence fires for `priced_in` (negative news + flat/rising price) and `hidden_problem` (positive news + flat/falling price); does not fire when news and price agree.
  - Dominant-direction threshold (60% of non-neutral) gates correctly: 59% does not produce a dominant label, 61% does.
  - Sentiment percentile reflects per-ticker distribution; bootstrap fallback to universe-pooled distribution fires when ticker has < 30 observations.
  - Prediction market delta fires at exactly 5.0pp; suppressed at 4.99pp.
  - Cross-platform normalization correctly weights by liquidity for a fixture pair of contracts.
  - Low-liquidity tag fires at exactly $10,000 notional; suppressed at $10,001.
  - Trailing 30-day history block carries the documented number of points.

Out of scope:
- Social-sentiment ingestion from StockTwits / Reddit / Twitter (deferred per data-layer scope; the proxy uses `news_article_tickers.vendor_sentiment_score` until aggregated social ingestion lands).
- Headline tagging taxonomy and clustering (data-layer concern per [project-tracker.md backlog](../../../project-tracker.md#analysis-layer)).
- Earnings transcript NLP (data-layer concern, deferred).
- LLM-side narrative interpretation (analysis layer's [`qualitative-research.md`](../../../design/03-analysis-layer/qualitative-research.md) owns this).

## Notes

The news-price divergence dominant-direction threshold (60% of non-neutral) is a definitional cutoff, not a Class A tunable. Encode as a named constant. If paper trading reveals the cutoff is wrong, raise it as a future story rather than tuning silently in code.

The sentiment-aggregation proxy (`vendor_sentiment_score` averaged over 4 hours from `news_article_tickers`, filtered to the ticker) is genuinely a stand-in for the eventual aggregated-sentiment vendor pipeline. Document the proxy clearly in source — both that it's a proxy and what would replace it. The percentile-calibration logic itself is identical regardless of input source, so the swap when aggregated sentiment lands is mechanical.

The cross-platform contract matching is the trickiest part. The simple-token-match heuristic catches obvious pairs ("FOMC January rate hold" on Polymarket vs. "Fed Jan rate hold" on Kalshi) but misses semantically equivalent phrasings ("rates unchanged" vs. "no change"). For v1 the heuristic is sufficient; the fallback is to under-match (no normalization performed) rather than over-match (false normalization producing misleading numbers). Document and accept the under-match risk.

The "trailing 30-day history" block on prediction markets is a payload chunk, not a separate output type. Include it inside the `qual.prediction_market_delta` output's `payload` as a list of `(snapshot_ts, yes_probability)` tuples sorted ascending. Bound the count at the configured `prediction_market_history_days` × invocations-per-day to keep payload size predictable.

The `audience = UNIVERSAL_BROADCAST` for prediction-market outputs reflects the design's universal-context treatment: prediction markets are not sector-scoped (Fed decisions affect every sector), so domain researchers and the synthesizer all consume them. The analysis layer's qualitative researcher specifically loads them in-context per [`qualitative-research.md § 3 Prediction market snapshot`](../../../design/03-analysis-layer/qualitative-research.md).

## Acceptance criteria

- [ ] News-price divergence fires for both `priced_in` and `hidden_problem` directions under documented input conditions.
- [ ] Dominant-direction threshold (60% of non-neutral) correctly gates the classification.
- [ ] Per-ticker sentiment percentile reflects the trailing 60-day distribution.
- [ ] Sentiment bootstrap fallback fires at < 30 observations and the output carries calibration state `BOOTSTRAP`.
- [ ] Prediction market delta fires at exactly 5.0pp; suppressed below.
- [ ] Cross-platform normalization correctly weights by liquidity.
- [ ] Low-liquidity tag fires at exactly $10,000 notional.
- [ ] Trailing 30-day history block populates correctly.
- [ ] News-price-divergence and sentiment-percentile outputs carry sector audience; prediction-market outputs carry `UNIVERSAL_BROADCAST`.
- [ ] Sentiment proxy choice (vendor_sentiment_score from news_article_tickers, filtered per ticker) is documented in source.
- [ ] Unit tests cover all of the above with fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
