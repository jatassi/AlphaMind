# Programmatic distillation

The bridge between raw API payloads and LLM-consumable signal. This layer is entirely deterministic — no LLM tokens are spent here. It takes the raw data collected by the ingestion layer and produces structured, token-efficient outputs optimized for the analysis pipeline's context windows.

The distillation layer has four responsibilities, applied across all data categories:

1. **Normalize** data into consistent formats and units
2. **Compute** derived metrics and indicators from raw feeds
3. **Detect anomalies** against recent baselines
4. **Maintain state** — rolling baselines, trailing distributions, composites, and regime classifications that persist across invocations

The anomaly flags are the critical output: they become the trigger inputs for the adaptive research layer in the analysis pipeline.

*Relationship to data ingestion docs:* The ingestion docs ([quantitative](../01-data-layer/external/quantitative.md), [qualitative](../01-data-layer/external/qualitative.md)) and [portfolio state](../01-data-layer/internal/portfolio-state.md) define *what* each signal is, *why* it matters, and *what fields* the system needs. This document defines *what the distillation layer computes* from those raw inputs. Cross-references to ingestion categories (e.g., "quant 1a") point to the data source; the computation spec lives here.

*Scope boundary with the analysis layer:* If a computation is deterministic and doesn't require LLM judgment, it belongs here. If it requires cross-referencing with qualitative context or making interpretive calls, it belongs in the analysis pipeline. There is one exception: some portfolio-state-aware computations (beta-adjusted exposure, position correlation matrices) are deterministic but require portfolio state inputs that this layer doesn't currently receive. Those are specified in [portfolio state — derived metrics](../01-data-layer/internal/portfolio-state.md) and should migrate here if the distillation layer's scope expands to include portfolio-aware computation.

---

## 1. Normalization and formatting

Applied to all incoming data. The goal: every downstream agent receives data in the same units, the same time frames, and the same format regardless of which vendor API produced it.

- **Unit standardization:** All moves expressed in both absolute and ATR-relative terms where applicable (a 2% move on TSLA is normal, a 2% move on JPM is a big deal). Dollar values in consistent notation. Percentages and ratios in consistent decimal format
- **Time alignment:** All timestamps normalized to ET. Multi-source data aligned to common time windows so agents can compare across sources without temporal mismatch
- **Per-ticker volatility normalization:** Move magnitudes expressed as multiples of ATR so analyst agents can compare across tickers without adjusting for each name's volatility personality (from quant 1f)
- **Extended-hours confidence discounting:** All extended-hours metrics (quant 1g, 2f) tagged with a reliability weight — moves in thin liquidity often overstate the regular session open. This is a blanket tag, not a per-metric judgment
- **Macro surprise framing:** Macro data points (quant 6a–6g) expressed primarily in terms of deviation from expectations rather than absolute values. For the 4–72 hour horizon, the surprise component is almost always more actionable than the level

---

## 2. Technical indicators and derived metrics

Computed from raw OHLCV and market data feeds. Organized by the ingestion category they derive from.

### From price and volume (quant 1)

**Multi-timeframe technical indicators (quant 1c):**
- Momentum oscillators: RSI (14-period) at 15min, 1hr, 4hr, daily, weekly; MACD (signal line crossovers and histogram momentum); stochastic
- Mean-reversion bands: Bollinger bands (position within bands, band width as vol proxy); Keltner channels
- Trend-following: moving average slopes and crossovers (20/50/200 EMA); ADX (trend strength regardless of direction)
- Volatility: ATR (14-period, absolute and as a normalizer); Bollinger bandwidth; ATR expansion/compression regime
- **Multi-timeframe divergence flags:** Surface explicitly when indicators at different timeframes conflict — e.g., RSI bearish divergence on 4hr while daily RSI is still healthy. These are among the most actionable outputs for sector researchers

**Volume profile (quant 1b):**
- Value area (price range where ~70% of volume transacted), point of control, high-volume and low-volume nodes
- Developing vs. settled profile classification

**Gap analysis (quant 1d):**
- Overnight gaps expressed in both absolute and ATR-relative terms
- Gap classification: full vs. partial, with-trend vs. counter-trend
- Historical gap-fill probability lookup per ticker and gap type

**Relative performance (quant 1e):**
- Rolling ratio of ticker price vs. sector ETF (XLK/XLF/XLE/SMH) and vs. SPY
- Intra-sector ranking: where each name sits in its sector's daily performance distribution
- Relative strength regime change detection: name shifting from sector leader to laggard (or vice versa) over multi-day windows

**Trend state and regime (quant 1f):**
- Per-timeframe trend state classification (trending up, trending down, range-bound) with composite multi-timeframe trend score
- Position in 52-week range (percentile)
- Distance from key moving averages in ATR terms
- Volatility regime per name: low-vol compression (Bollinger squeeze) vs. high-vol expansion

### From order flow (quant 2)

**Trade tape aggregation (quant 2a):**
- Net dollar flow over rolling windows (1hr, 4hr, session)
- Trade size distribution shifts
- Aggressor-side balance (ratio of trades hitting bid vs. lifting ask)
- Volume-weighted flow direction

**Liquidity scoring (quant 2b):**
- Composite liquidity score: spread + depth + fill probability for the system's typical position sizes
- Bid-ask spread trends over rolling windows

**Block and institutional flow (quant 2c):**
- Dark pool prints tagged with venue and, where possible, classified by likely participant type (institutional vs. retail-originated). Venue-level attribution: Sigma X (Goldman) skews institutional, IEX attracts informed flow, BATS dark carries more retail. Not always available (some venues report to FINRA TRF without venue-level granularity), but tagged when it is
- Sustained dark pool buying/selling detection: rolling detection of one-sided dark pool flow above/below VWAP

**Extended-hours flow (quant 2f):**
- Same aggregation as regular-session flow (net dollar flow, aggressor-side balance, trade size distribution) computed separately for pre-market and after-hours
- **Regular-session confirmation flag:** Track whether extended-hours flow direction is confirmed or reversed in the first 30 minutes of regular trading. Maintain historical confirmation rate per ticker to calibrate weight on pre-market signals

### From derivatives and options (quant 3)

**Options flow classification (quant 3b, 3f):**
- Buy-to-open vs. sell-to-open breakdown for puts and calls
- Protective vs. speculative classification: puts on names with large existing long interest flagged as hedging, not bearish conviction

**Cross-ticker options signals (quant 3g, 3h):**
- Pair trade signature detection: simultaneous bullish flow on one name + bearish flow on a correlated peer
- Sector-wide sweep detection: same directional bet appearing across multiple names within a short window
- **ETF IV vs. single-name IV composite divergence:** When IV on a sector ETF spikes but IV on the underlying names hasn't caught up (or vice versa) — signals the sector view hasn't propagated to individual names yet
- **Index hedging vs. sector conviction classification:** When both SPY/QQQ put flow and sector ETF put flow appear simultaneously, distinguish macro hedging from sector-specific concern

### From short selling (quant 4)

**Real-time short interest estimation (quant 4a–4c):**
- Triangulate across core subcategories: use daily short volume (fast) to estimate how short interest (slow) is changing between official bi-monthly FINRA reports, and use borrow cost spikes (fast) to infer utilization shifts before confirmation
- **Directional vs. mechanical classification:** Flag when short volume appears directional (bearish conviction) vs. mechanical (market maker hedging and liquidity provision), using context from order flow data (quant 2)

### From fundamental and earnings (quant 5)

**Expectations vs. reality scorecard (quant 5a–5g):**
- Running per-ticker scorecard tracking the gap between market expectations (consensus estimates, guidance, analyst targets) and incoming reality signals (estimate revisions, reported actuals, insider behavior)
- Updates near-real-time as new estimates and ratings arrive; re-anchors quarterly when actuals land
- Single output per ticker at any given moment: "Is the expectation gap widening, narrowing, or stable?"
- **Revision-price lag detection:** Flag revisions that have landed since the last run but haven't yet been absorbed by price action — timestamps revisions to the hour, not just the day, and cross-references with price movement

### From macro and rates (quant 6)

**Yield curve regime classification (quant 6a):**
- Normal (upward sloping), flat, inverted, steepening, flattening — regime transitions flagged when they occur

**Inflation regime classification (quant 6c):**
- Hot, cooling, stable, deflation risk — based on breakeven trends and data surprise patterns

**Dollar move attribution (quant 6f):**
- Classify each significant dollar move as rate-differential-driven, risk-sentiment-driven, or trade-flow-driven, since the equity implications differ for each

**Funding stress composite (quant 6e):**
- Maintain composite of SOFR spread + repo-treasury spread + term repo premium + MMF flow direction
- Single "funding market health" score readable as a systemic risk early warning
- Elevated readings should trigger the adaptive research layer to investigate the specific source of stress

### From cross-asset and correlation (quant 7)

This is the distillation layer's heaviest derived computation. Category 7 has no raw data sources of its own — everything is computed from categories 1–6 and 8.

**Intra-sector correlation (quant 7a):**
- Rolling pairwise correlation matrices within each sector (20-day and 60-day trailing)
- Automated divergence detection: names breaking from sector group behavior, with magnitude and duration
- Historical divergence resolution lookup per pair

**Cross-sector rotation (quant 7b):**
- Sector ETF relative performance ratios (rolling)
- Rotation velocity classification: slow rotation (regime shift) vs. sharp intraday rotation (event-driven, potentially mean-reverting)
- **Rotation narrative classification:** Tag rotation patterns with likely driver — rate-driven (financials vs. tech), growth-driven (cyclicals vs. defensives), risk-appetite-driven (high-beta vs. low-beta)

**Breadth and market internals (quant 7c):**
- Percentage of universe names above 20/50/200-day EMA
- Advance/decline within each sector
- Equal-weight vs. cap-weight performance comparison
- Market-wide aggregate spread, volume vs. trailing average, composite liquidity score

**Intermarket regime signals (quant 7d):**
- Stocks vs. bonds (SPY/TLT) correlation regime: positive (inflation) vs. negative (growth)
- Gold vs. real yields: divergence detection
- Oil vs. energy stock beta: stability monitoring
- VIX vs. SPY: divergence flagging (VIX rising on flat/rising market)
- Correlation regime stability assessment per relationship

**Lead-lag relationships (quant 7f):**
- **Maintain trailing estimates of lead-lag timing** between key pairs: funding → credit → equity, semis → tech, financials → market, commodity futures → energy stocks
- **Flag when a lead asset has moved but the expected lag response is overdue** — these are high-conviction short-term thesis inputs for the synthesizer
- Detect lead-lag regime shifts: when the normal leader/follower structure inverts

**Correlation regime change detection (quant 7g):**
- Rolling correlation stability (standard deviation of trailing correlation estimates)
- Correlation breakdown detection: major pairwise or sector-level correlation exceeding historical norms for rate of change
- Dispersion shift detection: sudden increase in cross-stock dispersion
- **Narrative lag indicator:** When correlation structure has shifted but financial media narrative hasn't caught up — early-stage regime transition signal

### From commodities (quant 8)

**Divergence flags:**
- Industrial metals vs. equity divergence: conflicting growth signals (quant 8c)
- Crack spread vs. energy stock performance divergence (quant 8e)
- DXY-commodity correlation regime assessment (quant 6f/8a)

### From corporate actions (quant 12)

- **Event novelty detection:** Flag unusual scheduling patterns — e.g., a company that hasn't held an investor day in two years suddenly schedules one (quant 12d)
- **ETF flow vs. single-name flow divergence:** When ETF-level flows and single-name flows send conflicting signals — ETF outflows paired with single-name institutional buying suggests someone using the ETF sell as cover to accumulate individual names (quant 12f)

### From qualitative data

The distillation layer's role with qualitative data is limited — most qualitative signals require LLM interpretation. But a few deterministic computations apply:

**News–price divergence (qualitative 1a):**
- Cross-reference ingestion layer's directional news classification (positive, negative, neutral, mixed per ticker) with recent price action. Flag cases where news sentiment and price are diverging — negative news with flat/rising price ("priced in" signal); positive news with flat/falling price (hidden problem)

**Sentiment calibration (qualitative 2a):**
- Maintain trailing sentiment distributions per ticker and express current readings as percentiles against the name's own history, not against a universal scale. TSLA's "neutral" sentiment is louder and more volatile than most names' extremes — per-ticker calibration prevents false anomaly flags on high-sentiment-volatility names

**Prediction market deltas (qualitative 3):**
- Compute deltas between invocations per contract
- Flag moves exceeding threshold (>5 percentage points since last pull)
- Maintain trailing history per contract so the analysis pipeline sees the trajectory, not just the current level
- Normalize probabilities across platforms when multiple platforms offer contracts on the same outcome

---

## 3. Anomaly detection

The primary trigger system for the adaptive research layer. Each anomaly type has a detection threshold calibrated against recent baselines (typically 20-day trailing). Anomalies are flagged, not interpreted — interpretation is the analysis pipeline's job. Specific threshold values, the cold-start bootstrap policy, and the update procedure are specified in [threshold-calibration.md](threshold-calibration.md).

**Price and volume anomalies:**
- Volume exceeding N standard deviations from 20-day average
- Price moves exceeding expected range (ATR-relative)
- News–price divergence: news sentiment and recent price action diverging

**Flow anomalies:**
- Unusual put/call skew or large block trades
- Options volume spikes on strikes with low existing open interest (new positioning)
- Block equity prints paired with options sweeps on the same name within the same window

**Divergence anomalies:**
- Intra-sector: correlated names diverging (from 7a computation)
- Cross-sector: rotation signals (from 7b computation)
- Intermarket: lead-lag gaps, correlation breakdowns (from 7d, 7f, 7g computations)
- Options vs. equity: options market positioning defensively while equity narrative is bullish (or vice versa)

**Short selling anomalies:**
- Short interest spikes or covering activity
- Borrow cost spikes

**Fundamental anomalies:**
- Earnings estimate revision clusters (multiple analysts revising in the same direction within days)
- Expectation gap widening (from the scorecard computation)

**Macro anomalies:**
- Data release surprises exceeding historical reaction thresholds
- Funding stress composite breaching alert levels

---

## 4. Persistent state and composites

Stateful computations that persist across pipeline invocations. The distillation layer maintains these as a database, updating them at each invocation. The window sizes (volume, ATR, sentiment, gap-fill, extended-hours, lead-lag, prediction-market history) and the minimum observations required before per-ticker baselines are treated as calibrated are specified in [threshold-calibration.md § Persistence and percentile windows](threshold-calibration.md#persistence-and-percentile-windows).

**Rolling baselines (per ticker):**
- 20-day trailing averages for volume, ATR, spread, and other metrics used as anomaly detection denominators
- Trailing sentiment distributions per ticker for percentile calibration (from qualitative 2a)
- Historical gap-fill rates per ticker and gap type
- Historical extended-hours confirmation rates per ticker

**Composites:**
- Funding stress composite (from quant 6e inputs)
- Market-wide liquidity score (from quant 7c inputs)
- Per-ticker expectations scorecard (from quant 5a–5g inputs)
- Volatility regime classification (from quant 11a–11f inputs) — see below

**Prediction market state (from qualitative 3):**
- Deltas between invocations per contract
- Threshold flags (>5pp shift since last pull)
- Trailing history per contract
- Cross-platform probability normalization when multiple platforms offer contracts on the same outcome

**Volatility regime classification (quant 11f):**

The most consequential persistent state. This is a composite regime label maintained continuously and broadcast to every agent as universal context metadata. The classification:
- **Low-vol compression:** VIX low, term structure in steep contango, VVIX low, realized vol declining
- **Vol expansion:** VIX rising, term structure flattening, realized vol increasing
- **Crisis/spike:** VIX elevated, term structure in backwardation, VVIX high
- **Vol normalization:** VIX declining from elevated levels, term structure returning to contango
- **Regime transition detection:** Flag when the regime label changes, with confidence level — early transitions are more actionable but less certain than confirmed transitions

---

## Output format

The distillation layer produces structured output optimized for LLM consumption: minimal tokens, maximum signal density. Each output block is tagged with:
- **Freshness timestamp:** When the underlying data was last updated
- **Confidence/reliability tier:** Full confidence for regular-session data, discounted for extended-hours, estimated for interpolated metrics (e.g., short interest between FINRA reports)
- **Anomaly flags:** Binary flags plus magnitude for each detected anomaly, grouped for easy scanning
- **Regime context:** Current volatility regime label attached to every output block

Output is partitioned by consumer:
- **Sector analyst agents:** Receive their sector's tickers with full distillation output (indicators, anomalies, divergences)
- **Synthesizer agent (via correlation brief):** Receives the cross-asset correlation, lead-lag, and regime outputs — category 7 computations
- **All agents:** Receive the volatility regime classification label as universal context
