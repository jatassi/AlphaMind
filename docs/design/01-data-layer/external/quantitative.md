# Quantitative / market data — 12 categories

Data sources organized by signal type, each with a different refresh cadence and relevance profile. The first seven are core (ingested every invocation); the remaining five are supplementary.

*Core categories (every invocation):*

**1. Price and volume**
What prices are doing and how much is trading. For the 4–72h horizon, the *structure* of price and volume matters more than raw numbers. Sector researchers read context: where is price relative to meaningful levels, is volume confirming the move, how does current action compare to the recent regime. Computed from raw OHLCV feeds — the heaviest user of programmatic distillation.

  *1a. Multi-timeframe price structure*
  Price at different granularities, assessed simultaneously. The 15-minute chart might look bearish inside a bullish daily trend inside a neutral weekly structure — that layering determines whether a dip is a buying opportunity or the start of a breakdown.
  - OHLCV candles at multiple intervals: 15min, 1hr, 4hr, daily, weekly
  - Key reference levels: prior day high/low/close, weekly open, monthly open — institutional algos cluster orders here, making them self-fulfilling support/resistance
  - Swing structure: recent swing highs and swing lows at each timeframe
  - Trend state classification: per-timeframe assessment (trending up, trending down, range-bound) plus a composite multi-timeframe score

  *1b. Volume profile and distribution*
  Not just how much traded but *where* it traded. One of the most useful inputs for analyst entry/exit targeting.
  - Value area: the price range where ~70% of volume transacted over a given period (session, multi-day, weekly)
  - Point of control (POC): single most-traded price level — acts as a magnet
  - High-volume nodes: prices where the market spent the most time, likely to slow price movement
  - Low-volume nodes: prices the market moved through quickly — air pockets where price tends to accelerate
  - Developing vs. settled profile: is the current session building in the same area as prior sessions or shifting?

  *1c. Technical indicators*
  A focused set computed at multiple timeframes. The distillation layer flags *divergences* between timeframes, not just values.
  - Momentum oscillators: RSI (14-period, multi-timeframe), MACD (signal line crossovers and histogram momentum), stochastic
  - Mean-reversion bands: Bollinger bands (position within bands, band width as vol proxy), Keltner channels
  - Trend-following: moving average slopes and crossovers (20/50/200 EMA), ADX (trend strength regardless of direction)
  - Volatility: ATR (14-period, absolute and as a normalizer), Bollinger bandwidth, ATR expansion/compression regime
  - Multi-timeframe divergence flags: e.g., RSI bearish divergence on 4hr while daily RSI is still healthy

  *1d. Gap analysis*
  Overnight gaps between sessions and intraday gaps on news. Gaps represent information being priced discontinuously. Especially important for the pre-open anchored run.
  - Overnight gap: open vs. prior close, in absolute and ATR-relative terms
  - Gap classification: full gap (above/below entire prior range) vs. partial gap, with-trend vs. counter-trend
  - Gap fill probability: historical gap-fill rate for this ticker and gap type
  - Intraday gaps: sudden price dislocations on news or large orders, timestamped for correlation with flow data (category 2)

  *1e. Relative performance*
  How a name performs vs. its sector peers, sector ETF, and the broad market. A stock flat on a day when its sector is up 3% is effectively down 3% in relative terms. Relative strength divergences often precede absolute moves.
  - Relative strength vs. sector ETF: rolling ratio vs. XLK/XLF/XLE/SMH — trending, diverging, or converging
  - Relative strength vs. SPY: same against the broad market
  - Intra-sector ranking: where the name sits in its sector's daily performance distribution
  - Relative strength regime change: detection of a name shifting from sector leader to laggard (or vice versa) over a multi-day window

  *1f. Historical context and regime*
  Where current price action sits relative to recent history. Moves expressed in ATR-relative terms so analyst agents compare across tickers without adjusting for each name's volatility personality.
  - ATR-normalized move magnitude: today's range and directional move as multiples of ATR — a 2% move on TSLA is normal, a 2% move on JPM is a big deal
  - Position in 52-week range: percentile location, proximity to 52-week high/low
  - Distance from key moving averages: how far price has stretched from 20/50/200 EMA, in ATR terms — mean-reversion signal at extremes
  - Volatility regime: low-vol compression (Bollinger squeeze) vs. high-vol expansion. Regime transitions are more tradeable than steady-state

  *1g. Extended hours price action*
  Pre-market and after-hours data. Thinner markets with wider spreads but critically important for off-hours and pre-open runs. Earnings reactions, overnight news, and overseas developments get priced here first.
  - Pre-market OHLCV: aggregated candles for the pre-market session (4:00–9:30 AM ET)
  - After-hours OHLCV: aggregated candles for the after-hours session (4:00–8:00 PM ET)
  - Extended-hours volume relative to regular session: low relative volume means the price signal is less reliable
  - Gap from regular close to extended-hours last trade: the "live" overnight gap before the regular open
  - Confidence discount flag: the distillation layer tags all extended-hours metrics with a reliability weight — moves in thin liquidity often overstate the regular open

**2. Order flow and microstructure**
What's happening beneath the price — who's buying, who's selling, how liquidity is structured. Reveals institutional footprints before they show up in price. For this system's time horizon, raw tick-level microstructure isn't useful in its native form (that's HFT territory). What matters is *aggregated* flow analysis that reveals institutional intent and liquidity regime over hours, not milliseconds.

  *2a. Trade tape analysis*
  Executed trades aggregated into meaningful windows. Distills the raw tape into "who's in control over the last N hours." Critical for distinguishing "price moved because of real directional flow" vs. "price drifted on thin volume."
  - Net dollar flow: buy-side vs. sell-side pressure over rolling windows (1hr, 4hr, session)
  - Trade size distribution: shifting mix of small/medium/large prints signals changing participant composition
  - Aggressor-side balance: ratio of trades hitting bid vs. lifting ask
  - Volume-weighted flow direction: net flow normalized by total volume to capture intensity
  - *Retail flow triangulation:* The question "is this retail-driven?" is answered by combining trade size distribution (small prints trending up), options flow patterns (3b — small-lot call buying), and social sentiment from the qualitative layer — not through dedicated retail segmentation infrastructure, which relies on dated heuristics (odd-lot analysis) or expensive vendor data. The adaptive research layer can assemble this triangulation on-demand when investigating whether a move has staying power.

  *2b. Liquidity and depth*
  Standing order book structure as aggregated health indicators, not real-time L2 snapshots. Tells you whether the market can absorb a move and flags liquidity vacuums. The portfolio manager especially needs this — a thinning book on a name where the system holds a position is a risk signal independent of direction.
  - Bid-ask spread trends: average spread over rolling windows, spread widening as a stress signal
  - Depth at multiple levels: how much size is resting at best bid/ask and at 5/10/20 bps away
  - Order book imbalance ratio: bid depth vs. ask depth at various levels
  - Liquidity score: composite measure of spread + depth + fill probability for the system's typical position sizes

  *2c. Block and institutional flow*
  Large prints and off-exchange activity. This is the primary institutional footprint signal. *(Note: dark pool data has been consolidated here from category 12 — alternative/exotic — since it is core flow data, not alternative.)*
  - Block trades: prints above a threshold size (e.g., >10k shares or >$500k notional), timestamped and side-tagged
  - Dark pool volume: percentage of total volume traded off-exchange, and directional inference from dark pool prints relative to NBBO
  - FINRA short volume reports: daily short sale volume per ticker (more timely than short interest, which is bi-monthly)
  - Sustained dark pool buying/selling: rolling detection of one-sided dark pool flow above/below VWAP — signals accumulation or distribution
  - Venue-level dark pool attribution: different dark pools have different participant profiles — Sigma X (Goldman) skews institutional, IEX is designed for informed flow protection, BATS dark carries more retail-originated flow. A large dark pool print on a Goldman venue reads differently than the same volume on a retail-heavy venue. The distillation layer should tag dark pool prints with venue and, where possible, classify the likely participant type. This isn't always available (some venues report to FINRA TRF without venue-level granularity), but when it is, it meaningfully improves the institutional footprint detection that 2c is built around.

  *2d. Auction mechanics*
  Opening and closing auction data — massive liquidity events that can represent 10%+ of daily volume. Specifically relevant for the two anchored runs (pre-open at 9:00 AM ET and pre-close at 3:30 PM ET).
  - MOC/MOO imbalance: buy vs. sell imbalance in market-on-close and market-on-open orders, as published by NYSE/NASDAQ
  - Imbalance evolution: how the imbalance changes in the minutes before the auction (directional drift signals late-arriving institutional flow)
  - Auction price vs. continuous price: gap between the auction print and the last continuous trade — large gaps signal pent-up demand/supply
  - Closing auction volume concentration: what fraction of a name's daily volume is in the close, and is it unusual?

  *2e. Intraday flow patterns*
  The temporal structure of trading activity. Unusual concentration of volume at atypical times often signals programmatic execution or rebalancing with a predictable wind-down.
  - VWAP trajectory vs. price: is institutional execution running above or below VWAP? Persistent deviation signals directional intent
  - Volume distribution: intraday volume curve compared to the ticker's typical pattern — front-loaded, back-loaded, or unusual midday concentration
  - End-of-day positioning flows: directional flow in the last 30–60 minutes, distinct from the closing auction
  - Time-of-day volume anomalies: bursts at unusual hours on no news, suggesting programmatic or rebalancing activity

  *2f. Extended-hours order flow*
  The flow-side complement to 1g's extended-hours price data. Category 1g tells you *what* happened in pre-market and after-hours; this tells you *who* is driving it. For a system whose first daily run fires at 9:00 AM ET, distinguishing a pre-market gap driven by institutional block flow from one driven by thin retail volume is the difference between a high-conviction opening thesis and a trap. Extended-hours flow is inherently noisier than regular-session flow — wider spreads, thinner books, fewer participants — so all metrics here carry the same confidence discount applied to 1g price data.
  - Extended-hours trade tape: net dollar flow and aggressor-side balance (same metrics as 2a) computed separately for the pre-market and after-hours sessions
  - Extended-hours trade size distribution: participant mix during thin sessions — a pre-market move dominated by large prints reads very differently than one built from small retail-sized trades
  - Extended-hours block detection: block trades (same thresholds as 2c) occurring outside regular hours — institutional prints in pre-market are high-signal because the institution chose to act before the full liquidity pool opens, suggesting urgency or information asymmetry
  - Extended-hours dark pool activity: off-exchange volume during extended sessions — limited but occasionally meaningful; large dark pool prints in pre-market on earnings mornings signal institutional positioning before the open
  - Regular-session confirmation flag: the distillation layer should track whether extended-hours flow direction is confirmed or reversed in the first 30 minutes of regular trading — historical confirmation rate per ticker helps calibrate how much weight to give pre-market signals

  *Cross-reference note:* Block/institutional flow (2c) and unusual options activity (3b) should be read together by the synthesizer. A block equity print paired with options sweeps on the same name within the same window is a much stronger signal than either alone.

**3. Derivatives and options**
The market's forward-looking expectations. Options markets are often smarter than spot because positioning requires conviction — someone spending real capital on short-dated options is making a time-bound directional bet, which maps directly to the system's 4–72 hour thesis horizon.

  *3a. Implied volatility structure*
  The market's expectation of move magnitude and its shape across strikes and time. Feeds sector researchers for contextualizing whether a price move is "expected" or anomalous, and feeds the analyst for assessing whether a setup is cheap or expensive relative to expected movement.
  - Term structure: near-term vs. longer-dated IV, contango/backwardation shape
  - Skew: put/call IV differential, how lopsided the market's fear is between upside and downside
  - IV vs. realized vol: is the market over- or under-pricing movement relative to what's actually happening?
  - IV rank / IV percentile: current IV relative to its own trailing range

  *3b. Unusual options activity*
  The institutional footprint detector. Probably the single highest-signal subcategory for the system — large, deliberate positioning on time-bound directional bets.
  - Large block trades: single prints above threshold notional value
  - Sweeps: orders hitting the ask across multiple exchanges simultaneously (urgency signal)
  - Volume vs. open interest: volume spikes on strikes with low existing OI suggest new positioning
  - New positioning vs. closing/rolling: critical distinction — opening trades signal conviction, closing trades signal unwinding

  *3c. Open interest and positioning landscape*
  The aggregate battlefield map — where positions are concentrated and how they're shifting. Covers both single-name options OI and equity index futures positioning.
  - Strike-level OI concentration: walls of puts, call magnets, max pain levels
  - OI day-over-day changes: where is new interest building or draining?
  - Expiration clustering: how much OI is concentrated in near-term vs. longer-dated expirations
  - Put/call OI ratio by strike and expiration
  - Equity index futures positioning (COT): asset manager and leveraged fund positioning in S&P 500, NASDAQ 100, and sector futures — weekly data, distinct from single-name options positioning. Extreme speculative positioning is a contrarian signal. *(Absorbed from former category 9 — sentiment and positioning)*

  *3d. Dealer / market maker exposure*
  The market structure layer that determines the *character* of the next move rather than its direction. Especially important for the portfolio manager's risk assessment.
  - Gamma exposure (GEX): net dealer gamma positioning — long gamma dampens moves, short gamma amplifies them
  - GEX flip levels: price levels where dealer hedging behavior changes character
  - Delta exposure (DEX): net directional dealer hedging pressure
  - Vanna and charm flows: how dealer hedging shifts with vol and time decay

  *3e. Earnings-implied moves*
  Earnings are the single biggest catalyst type for individual names on the 4–72 hour horizon. Deserves dedicated treatment.
  - Implied earnings move: straddle pricing for the nearest expiration spanning the earnings date
  - Historical earnings move comparison: implied vs. what the stock has actually done over the last N earnings
  - Straddle richness: is the earnings vol premium cheap or expensive relative to history?
  - Pre-earnings IV ramp: how quickly is IV building into the event, and is the ramp unusual?

  *3f. Put/call dynamics*
  Ratios and flow character at multiple levels. Raw ratios are less useful than the *nature* of the flow — the same ratio reads very differently depending on whether puts are being bought to open (bearish conviction) or sold to open (income/bullish).
  - Put/call volume ratio: per-ticker, per-sector, and index-level
  - Flow directionality: buy-to-open vs. sell-to-open breakdown for puts and calls
  - Protective vs. speculative: puts on names with large existing long interest read as hedging, not bearish bets
  - Index vs. single-stock skew: when index put buying diverges from single-stock put buying, it signals macro hedging vs. idiosyncratic concern

  *3g. Cross-ticker options signals*
  Simultaneous or correlated positioning across multiple names that reveals relative value theses or pair trades. Lower frequency but high signal-to-noise when it appears.
  - Pair trade signatures: simultaneous bullish flow on one name + bearish flow on a correlated peer (e.g., NVDA calls + AMD puts)
  - Sector-wide sweeps: the same directional bet appearing across multiple names in a sector within a short window
  - Cross-sector hedging: options flow that pairs directional bets in one sector with hedges in another (e.g., long energy calls + long financials puts as an inflation trade)
  - ETF vs. single-stock divergence: when options flow on sector ETFs diverges from flow on the underlying names

  *3h. Sector ETF options flow*
  Institutions frequently express sector-level views through ETF options rather than single-name positions — a large put buy on XLF is a different and more direct signal than aggregating individual bank name puts. Category 3g flags ETF-vs-single-stock divergence as a cross-ticker signal, but the sector researcher agents need their sector ETF's options flow as a first-class input alongside the single-name data they already receive.
  - Sector ETF unusual activity: large block trades, sweeps, and volume-vs-OI spikes on XLK, XLF, XLE, SMH, and QQQ — same detection framework as 3b applied to ETFs
  - ETF put/call flow character: buy-to-open vs. sell-to-open breakdown for sector ETF options — heavy ETF put buying-to-open is a sector-level bearish signal distinct from individual name hedging
  - ETF IV vs. single-name IV composite: when IV on XLF spikes but IV on the underlying bank names hasn't caught up (or vice versa), the divergence signals that the sector view hasn't propagated to individual names yet — a timing edge for the sector researcher
  - ETF options flow as sector sentiment: net directional flow on sector ETFs as a real-time sector sentiment barometer — feeds the sector researcher briefs alongside the single-name data from 3a–3f
  - Index hedging vs. sector conviction: large SPY or QQQ put flow reads as macro hedging, while large XLK put flow reads as tech-specific concern — the distillation layer should distinguish these when both appear simultaneously

**4. Short selling and lending**
The bearish side of the ledger. Who's betting against what, how expensive is it to borrow, where is covering happening. Asymmetric signal — short sellers tend to be more informed than the average long because they're paying to hold a negative position (borrow costs, dividend liability, margin requirements), so they tend to have higher conviction.

  *Data timeliness concern:* This category has the worst latency profile of any core category. Short interest is bi-monthly (FINRA settlement cycle). Borrow cost data is daily at best. Only daily short volume (FINRA) provides same-day signal. The programmatic distillation layer must triangulate across the core subcategories (4a–4c) to construct a near-real-time picture — using daily short volume (fast) to estimate how short interest (slow) is changing between official reports, and using borrow cost spikes (fast) to infer utilization shifts before they're confirmed. The deeper lending structure data (4d) and derived squeeze composite (4e) are available as on-demand research infrastructure for the adaptive research layer. Any metric below should be annotated with its native refresh rate so the system knows what's fresh vs. estimated.

  *4a. Short interest and utilization*
  The headline numbers — how much of the float is sold short and how much of the lendable supply is in use. The rate of change matters more than the absolute level: a stock going from 15% to 25% SI over two cycles is more actionable than one sitting at 40% for months.
  - Short interest (% of float): FINRA bi-monthly settlement data — the official count, but stale by the time it publishes
  - Short interest ratio (days to cover): SI divided by average daily volume — how long it would take shorts to unwind, a squeeze pressure gauge
  - Utilization rate: percentage of lendable shares currently lent out, available daily from securities lending platforms — utilization approaching 100% is a squeeze setup signal
  - SI trend and rate of change: cycle-over-cycle change in short interest, flagging acceleration or deceleration

  *4b. Borrow cost and fee dynamics*
  The price of being short. When borrow fees spike, demand to short is outstripping lendable supply. A leading indicator of both squeeze potential and informed bearish conviction (someone is willing to pay up).
  - Current borrow fee: annualized cost to borrow, available daily from lending platforms
  - Fee trend and spikes: rate of change in borrow cost — sudden jumps are more actionable than elevated steady-state
  - Fee term structure: front-loaded cost (short-term event bet) vs. flat across tenors (structural bear thesis)
  - Hard-to-borrow list status: whether the name appears on prime broker HTB lists — the extreme end of the supply/demand imbalance

  *4c. Short volume and flow*
  Daily short sale activity, distinct from accumulated short interest. FINRA publishes this daily per ticker, making it the most timely signal in this category.
  - Daily short volume: total short sale volume as reported by FINRA
  - Short volume ratio: short volume as a percentage of total daily volume — spikes on otherwise quiet days are a strong signal
  - Short volume trend: multi-day rolling average to smooth noise and detect sustained directional shorting
  - Contextualization flag: short volume must be read against order flow data (category 2) — not all short volume is bearish; market makers short as part of hedging and liquidity provision. The distillation layer should flag when short volume appears directional vs. mechanical.

  *4d. Securities lending market structure — ON-DEMAND RESEARCH INFRASTRUCTURE*
  The supply side — who's lending, how much is available, and how the lendable pool is changing. Detects "the squeeze is coming" before the price move: supply contracting while demand holds steady forces covering regardless of the short's thesis. Data is daily from securities lending platforms (IHS Markit, S3 Partners, etc.) — too slow and too expensive to justify routine ingestion, but exactly what the adaptive research layer needs when investigating a squeeze thesis on a specific name. Build the API connection; don't schedule it.
  - Lendable supply: total shares available for lending
  - Supply trend: is the lendable pool growing or shrinking? Large holders recalling shares pulls supply and forces covering
  - Lender concentration: how many lenders are providing the supply — concentrated lending is fragile (one recall triggers a cascade)
  - New loan creation vs. returns: net lending flow direction — are new borrows being initiated or are existing borrows being returned?
  - Reg SHO threshold list status: names with persistent fails-to-deliver that trigger mandatory close-out requirements — the extreme squeeze catalyst signal. Checkable on-demand; the underlying FTD data (SEC-published, ~2 week delay) is too stale for routine ingestion on a 4–72 hour horizon, but threshold list membership is a useful binary flag during squeeze investigations.

  *4e. Short squeeze composite — ON-DEMAND DERIVED METRIC*
  Not a raw data source but a derived state assessment synthesized from 4a–4d. Given current short interest, utilization, borrow cost, and price/flow data from categories 1 and 2, is this name in squeeze territory? Feeds directly into the analyst — squeeze setups are high-conviction, time-bound asymmetric trades, exactly the system's target profile. This is a computation the adaptive research layer runs when pursuing a squeeze thesis, not a metric maintained on a schedule. Its reliability depends on the freshness of the underlying data — 4a–4c provide real-time-ish inputs, 4d provides the deeper lending structure context when pulled on-demand.
  - Squeeze score: composite metric on a continuum from "normal" through "elevated short pressure" to "active squeeze conditions"
  - Squeeze trigger proximity: how close the name is to conditions that historically precipitate short covering cascades
  - Covering velocity estimate: if covering begins, how fast can it unwind given current depth (from 2b) and days-to-cover ratio
  - Catalyst sensitivity: how much of a price move would be needed to push the name from "elevated" to "active squeeze" — ties into the analyst's thesis sizing

**5. Fundamental and earnings**
Company-level numbers, but *not* for deep fundamental analysis or long-term valuation. On a 4–72 hour horizon, the system doesn't care whether a company is fundamentally undervalued. It cares about how *expectations* are shifting and where the market's consensus is wrong or about to be surprised. The signal isn't in the numbers themselves — it's in the delta between what the market expects and what's happening.

  *Design principle — expectations vs. reality scorecard:* The distillation layer should maintain a running per-ticker scorecard that tracks the gap between market expectations (consensus estimates, guidance, analyst targets) and incoming reality signals (estimate revisions, reported actuals, insider behavior). This scorecard updates in near-real-time as new estimates and ratings come in, but re-anchors quarterly when actuals land. The scorecard's job is to answer one question per ticker at any given moment: "Is the expectation gap widening, narrowing, or stable?" A widening gap in either direction is the primary thesis input for the analyst. The timeliness profile here is bimodal — estimate revisions and rating changes are near-real-time, while reported financials and ownership filings are quarterly/delayed. The scorecard bridges these cadences into a single continuously-updated signal.

  *5a. Earnings estimate dynamics*
  The single most actionable subcategory. Sell-side consensus estimates are a proxy for what the market has priced in. When estimates move, prices follow — but not always immediately and not always proportionally. That lag is the opportunity.
  - Consensus EPS and revenue estimates: current quarter, next quarter, full year — the baseline expectation
  - Revision direction and magnitude: which way estimates are moving and by how much, for each time horizon
  - Revision velocity and breadth: one analyst bumping numbers is noise; five analysts revising in the same direction within days is signal. Track count of revisions, percentage of covering analysts revising, and the time clustering of revisions
  - Estimate dispersion: spread between highest and lowest estimates — wide dispersion means high uncertainty and potential for sharp repricing
  - Revision momentum: are revisions accelerating (more/bigger revisions this week than last) or decelerating?
  - Intraday revision timestamps: sell-side analysts often publish estimate changes in clusters — before market open, around midday, after the close. A revision that drops at 2 PM may not be reflected in price until the next morning. For a system running every 2 hours during market hours, knowing *when* during the day a revision hit is the difference between catching the move and chasing it. The ingestion layer should timestamp revisions to the hour, not just the day, and the distillation layer should flag revisions that have landed since the last run but haven't yet been absorbed by price action (revision-price lag detection).

  *5b. Earnings calendar and event proximity*
  Where each ticker sits relative to its next earnings date. Partly catalyst scheduling, partly risk management — as earnings approach, IV ramps (see 3e), volume patterns shift, and the thesis framework changes.
  - Days to next earnings: simple countdown, with flags at key thresholds (e.g., within 7 days, within 2 days)
  - Event window mapping: pre-announcement quiet period start, expected report date, conference call time, typical post-earnings drift window
  - Historical post-earnings drift: does this name tend to continue moving in the earnings-reaction direction over the following 1–5 days, or does it mean-revert?
  - Earnings clustering: when multiple names in the same sector report in the same window, the first reporter's reaction shapes expectations for the rest — flag these sector earnings clusters

  *5c. Revenue and growth trajectory*
  Trailing reported numbers as a baseline for where consensus should be anchoring. The system needs context: is this a company accelerating, decelerating, or inflecting? A stock with three quarters of accelerating revenue growth gets treated very differently on a miss than one that's been decelerating.
  - Revenue QoQ and YoY growth rates: trailing 4–8 quarters, to establish the trend
  - Growth acceleration/deceleration: is the growth rate itself increasing or decreasing? Second derivative matters more than first for market reaction
  - Beat/miss history: trailing pattern of results vs. consensus — serial beaters get punished harder on the first miss, serial missers get rewarded harder on the first beat
  - Revenue mix shifts: for diversified names, which segments are growing and which are shrinking — relevant for tech names where cloud/AI vs. legacy segments drive very different multiples

  *5d. Margin and profitability shifts*
  The market's "quality of earnings" read. Revenue beats get a very different reaction depending on whether margins expanded or compressed alongside them.
  - Gross margin trajectory: trailing 4–8 quarters, trend direction and rate of change
  - Operating leverage: spread between revenue growth and operating expense growth — positive spread means the business is scaling efficiently
  - EPS growth vs. revenue growth: when EPS grows faster than revenue, margins are expanding; the reverse signals margin pressure
  - Profitability inflection detection: for growth names especially, the transition from unprofitable to profitable (or vice versa) is a regime change that reprices the stock

  *5e. Guidance and forward commentary*
  What management signaled about the future. There's a quantifiable dimension that sits alongside the qualitative analysis (which the qualitative pipeline handles separately).
  - Guidance vs. consensus: did next-quarter and full-year guidance come in above, in-line, or below consensus estimates? The "guidance surprise" is often more predictive of post-earnings drift than the actual beat/miss
  - Guidance range width: narrow range signals management confidence, wide range signals uncertainty
  - Guidance revision: did management raise, lower, or maintain the outlook relative to prior guidance? A raise-and-beat is the strongest bull signal; a beat-and-lower-guidance is often a sell-the-news setup
  - Forward commentary tone score: a simplified quantification of management's forward-looking language — not full NLP, but flagging whether the qualitative pipeline should dig deeper

  *5f. Analyst rating and target landscape*
  Aggregate sell-side positioning. Not because analysts are right (they're often late), but because rating changes cause mechanical flow — upgrades trigger buying programs at funds that screen on consensus ratings.
  - Consensus rating: current aggregate (buy/hold/sell distribution) and recent trend
  - Rating changes: upgrades, downgrades, and initiations within the last N days — timestamped and attributed
  - Price target distribution: mean, median, high, and low targets across covering analysts
  - Price vs. consensus target: is the stock trading above or below the mean target? Stocks trading above all targets have a "who's left to buy" problem; stocks trading below the lowest target may be pricing in something analysts haven't caught
  - Target revision clustering: multiple target raises/cuts in a short window amplify the signal

  *5g. Insider and institutional ownership shifts*
  How the people closest to the company are positioning. Insider buying is a stronger signal than insider selling (selling can be for liquidity, buying is almost always conviction). Institutional data is lagged but directionally useful.
  - Insider transactions: Form 4 filings — buys, sells, and option exercises. Clustered insider buying (multiple insiders within a short window) is a strong medium-term signal
  - Insider buy/sell ratio: net insider sentiment over trailing 30/90 days
  - 13F institutional ownership changes: quarterly snapshots of who's accumulating, reducing, or exiting — flag new positions from prominent funds and large percentage changes
  - Institutional ownership concentration: percentage held by top 10 holders, and whether that concentration is increasing (fewer hands, more conviction) or decreasing (distribution)

**6. Macro and rates**
The gravitational field everything trades in. The system doesn't trade macro instruments directly, but every sector in the universe is sensitive to macro through different channels: financials are directly rate-linked, tech is sensitive through the discount rate on future earnings, energy is tied to growth expectations and dollar strength. Think of this category as the weather system — individual stocks are boats on the ocean, macro and rates are the tides and currents.

  *Design principle — surprise over level:* Most macro data points have both a level component (where is the 10Y right now) and a surprise component (did CPI beat expectations). For the system's 4–72 hour horizon, the surprise component is almost always more actionable than the level. The distillation layer should express macro data primarily in terms of deviation from expectations rather than absolute values.

  *6a. Yield curve and treasury rates*
  The most information-dense single data source in markets. The curve's shape, level, and rate of change encode market expectations for growth, inflation, and monetary policy simultaneously.
  - Key rate levels: 2Y (monetary policy proxy), 5Y and 10Y (equity discount rate), 30Y (long-term growth/inflation expectations), fed funds effective rate
  - Curve spreads: 2s10s, 2s30s, 3m10s — steepening vs. flattening signals changing recession/expansion expectations
  - Rate of change: daily and multi-day moves at each tenor, in both absolute and z-score terms — a 15bp move in the 10Y in a single session is a volatility event for every sector
  - Real yields: TIPS-implied real rates at 5Y and 10Y — rising real yields are the most direct headwind for growth/tech multiples
  - Curve regime classification: normal (upward sloping), flat, inverted, steepening, flattening — regime transitions matter more than steady-state shape
  - Treasury auction results: a poorly received 10Y or 30Y auction (weak bid-to-cover ratio, large tail, high primary dealer allocation) can move rates 5–10bp in an hour and cascade into equities immediately. Auctions happen on a known schedule (Treasury publishes the calendar quarterly), results are available within minutes, and the system can pre-position awareness of upcoming auctions via the event calendar (6g). Key metrics per auction: bid-to-cover ratio vs. recent average, tail (spread between auction yield and when-issued yield at cutoff — positive tail means weak demand), dealer vs. indirect vs. direct allocation (high dealer allocation means end-user demand was weak), and auction size relative to recent issuance. For the financials sector, which holds treasuries directly and whose earnings are rate-sensitive, auction results are a first-order signal. For tech, the transmission is through the discount rate — a weak 10Y auction that pushes yields up 8bp reprices growth multiples within hours.

  *6b. Federal Reserve and monetary policy*
  The market's probabilistic expectations for rate decisions. One of the rare cases where the system has access to a clean, real-time probability distribution over a binary outcome.
  - Fed funds futures implied probabilities: probability of hike, cut, or hold at each upcoming FOMC meeting
  - Rate path expectations: implied terminal rate and expected timing — how many cuts/hikes does the market expect over the next 12 months
  - Probability shift velocity: a 10% change in December cut probability over two days is more actionable than the same shift over two weeks
  - Dot plot vs. market pricing: the gap between the Fed's own projections and what markets are pricing — divergence here often resolves violently at FOMC meetings
  - Fed speaker impact: scheduled appearances and their market impact — some speakers (Chair, Vice Chair) move markets more than others, and the system should weight accordingly

  *6c. Inflation expectations*
  Both market-derived (continuous) and data-driven (event-based). The most actionable signal is the surprise component of data releases.
  - Breakeven inflation rates: 5Y and 10Y TIPS spreads — real-time market expectations, continuous signal
  - Breakeven trend and rate of change: drifting breakevens between data releases signal shifting inflation narrative before the next hard data point confirms
  - CPI/PPI/PCE surprise: actual release vs. consensus estimate — magnitude and direction drive immediate repricing across all four sectors
  - Core vs. headline decomposition: which components drove the surprise — shelter, services, goods, energy — affects which sectors get repriced
  - Inflation regime classification: hot, cooling, stable, deflation risk — regime transitions change the playbook for rate expectations and sector rotation

  *6d. Economic growth indicators*
  High-frequency data that tells you whether the economy is expanding or contracting. Not for macro forecasting — for detecting when the market's growth narrative is about to shift.
  - PMI/ISM manufacturing and services: monthly headline and subcomponents (new orders is the leading subcomponent), plus intra-month regional Fed surveys (Empire, Philly, Dallas, Richmond) that preview the national number
  - Employment data: weekly initial claims (the most timely), monthly NFP (headline, revisions, wage growth, unemployment rate), ADP as a noisy NFP preview
  - Consumer data: retail sales (monthly), consumer confidence/sentiment surveys (Michigan, Conference Board)
  - Housing: existing/new home sales, building permits, housing starts — slower-moving but relevant for financials exposure
  - Growth surprise index: aggregate measure of whether data is broadly beating or missing expectations (similar to Citi Economic Surprise Index) — a running score of "is the consensus macro narrative too optimistic or too pessimistic"

  *6e. Credit conditions and spreads*
  The risk appetite barometer. Credit markets are often earlier than equities at detecting stress — IG spread widening typically leads equity weakness by days. For financials specifically, credit conditions directly affect earnings.

  *Public credit:*
  - Investment grade (IG) spreads: option-adjusted spread vs. treasuries — the baseline risk appetite measure
  - High yield (HY) spreads: same as IG but for below-investment-grade — more sensitive to recession risk, widens faster in stress
  - IG and HY spread rate of change: widening velocity matters more than level — a 20bp widening in a week is a different signal than 20bp over two months
  - TED spread and SOFR-treasury spread: interbank stress and funding condition indicators
  - Commercial paper rates: short-term corporate funding costs, stress here signals liquidity problems
  - CDS indices: CDX IG and CDX HY as real-time tradeable credit risk proxies

  *Private credit:*
  Data is inherently more opaque than public credit, but enough is available to detect stress building before it surfaces in public markets. BDC prices dropping while IG spreads are flat is an early warning signal.
  - BDC stock prices and NAV discounts: publicly traded BDCs (ARCC, MAIN, BXSL, OBDC, etc.) as real-time proxies for private credit health — price-to-NAV discount widening signals market distrust of book values
  - BDC price divergence from IG/HY: when BDC prices weaken while public credit spreads are stable, private credit stress is building that hasn't surfaced yet
  - CLO tranche pricing: senior vs. mezzanine vs. equity tranche spreads from daily pricing services — stress cascades bottom-up, so equity tranche widening leads senior tranche widening
  - Direct lending indices: weekly/monthly data from providers like Cliffwater or KBRA — default rates, recovery rates, and yield trends in private lending
  - Bank credit quality commentary: quarterly earnings commentary on credit reserves, charge-off trends, and private credit book exposure — fed into the qualitative pipeline for interpretation but flagged quantitatively when reserve builds accelerate

  *Funding and repo markets:*
  The plumbing layer underneath credit — overnight and short-term funding conditions. Repo stress was the canary in the coal mine for September 2019 and March 2020. Funding market dislocations lead credit spread widening, which in turn leads equity weakness, making this the earliest link in the stress transmission chain. The same lead-lag logic the system uses for credit → equity (7f) applies here, but funding markets are even earlier.
  - SOFR rate and spread to fed funds: the baseline overnight funding rate — SOFR spiking above the fed funds target range signals collateral scarcity or reserve shortages in the banking system
  - Repo-treasury spread: the gap between overnight repo rates and comparable treasury yields — widening signals funding stress, collateral hoarding, or balance sheet constraints at dealers. The September 2019 repo spike (SOFR briefly hit 5.25% vs. a 2% target) preceded broader equity volatility by days
  - Term repo rates: 1-week, 2-week, 1-month repo rates — term funding stress (as opposed to overnight) signals that market participants expect the dislocation to persist, not resolve overnight
  - Fed reverse repo facility usage: the RRP acts as a liquidity floor — high usage means excess reserves are parked at the Fed rather than deployed in markets. Rapid changes in RRP usage signal shifts in the plumbing that affect bank reserves and lending capacity
  - Money market fund flows: weekly data on government and prime money market fund assets — flight-to-quality moves show up here as inflows to government MMFs and outflows from prime MMFs. Large, sudden shifts signal institutional risk aversion at the most conservative end of the capital structure
  - Funding stress composite: the distillation layer should maintain a composite of SOFR spread + repo-treasury spread + term repo premium + MMF flow direction — a single "funding market health" score that the portfolio manager and synthesizer can read as a systemic risk early warning. Elevated readings should trigger the adaptive research layer to investigate the specific source of stress

  *6f. Dollar and foreign exchange*
  The system doesn't trade FX, but dollar moves affect the entire universe through three distinct transmission channels. The system needs to know not just *that* the dollar moved but *why*, because the same headline "dollar is stronger" has completely different implications depending on the driver.

  *Earnings translation channel (primarily tech):*
  Dollar strength means lower reported earnings for companies with international revenue. AAPL (~60% international), MSFT (~50%), GOOG, META, NVDA all take meaningful translation hits from sustained dollar strength. A sharp DXY move is effectively an earnings revision for half the tech universe that won't show up in analyst estimates for days — the system can front-run that.
  - DXY (US Dollar Index): headline level, daily and multi-day change, z-score relative to trailing range
  - EUR/USD: the largest DXY component — European recession fears strengthen the dollar through this cross

  *Commodity pricing channel (primarily energy):*
  Oil, gas, and metals are priced in dollars. Dollar strength mechanically pressures commodity prices. But there's a second-order effect: dollar strength from risk-off sentiment hits energy twice (dollar headwind + growth slowdown); dollar strength from rate differentials only hits once (mechanical). The system should flag *why* the dollar is moving.
  - DXY-commodity correlation regime: is the current dollar move driving commodities or are they diverging?
  - USD/commodity-currency crosses: USD/CAD, USD/NOK as energy-linked FX proxies

  *Global financial conditions channel (all sectors):*
  The carry trade and liquidity channel. Rapid moves here force leveraged global portfolios to de-risk across all asset classes regardless of domestic fundamentals.
  - USD/JPY: the carry trade proxy — sharp yen strengthening (carry unwind) is a de-risking signal for all US equities. The August 2024 yen unwind took US equities down hard despite nothing changing domestically
  - USD/CNY: China growth and trade friction signal — CNY weakness ripples into semis (supply chain exposure) and energy (demand expectations)
  - Dollar move attribution: the distillation layer should attempt to classify each significant dollar move as rate-differential-driven, risk-sentiment-driven, or trade-flow-driven, since the equity implications differ for each

  *Not tracked:* EM currencies and exotic crosses. Signal-to-noise ratio drops fast outside the majors for a US large-cap equity portfolio.

  *6g. Macro event calendar and surprise index*
  The scheduling and scoring layer that ties everything else together. The system needs to know not just what data is coming but when, and how recent releases have compared to expectations.
  - Event calendar: FOMC meetings (decision + minutes), CPI/PPI/PCE dates, NFP, ISM/PMI, GDP, consumer confidence, Fed speaker appearances — all with timestamps and consensus estimates where applicable
  - Macro surprise index: running aggregate of whether data has been broadly beating or missing expectations — tells you if the consensus narrative is too optimistic or pessimistic
  - Event proximity flags: alerts when a market-moving data release is within N hours — the sector researchers and portfolio manager need to know when event risk is imminent
  - Historical event impact by sector: how each sector in the universe has historically reacted to surprises in each data type — hot CPI historically hits tech harder than energy, weak NFP hits financials harder than semis. This lookup table helps the analyst size sector-specific theses around events

**7. Cross-asset and correlation**
A fundamentally different kind of category from the other six. Everything else measures *things* — prices, volumes, spreads, positions. This category measures *relationships between things*. It doesn't have its own raw data sources — it's computed entirely from categories 1–6 and 8 by the programmatic distillation layer. But the signals it produces are arguably the most valuable for the synthesizer agent: divergences between correlated assets are one of the clearest setups for the 4–72 hour horizon, because when things that should move together stop moving together, something is about to resolve.

  *Architectural note:* This category's output should be delivered to the synthesizer agent as a dedicated **correlation and regime brief**, separate from the sector researcher briefs. The sector researchers see their own sector's internals; cross-asset correlation is inherently cross-domain and is the synthesizer's native territory. Embedding correlation data within each sector brief would fragment the picture.

  *7a. Intra-sector correlation and divergence*
  The tightest correlation lens. Names within the same sector normally move together — when they diverge, either one is mispriced or the market is repricing a company-specific factor that hasn't propagated. Feeds directly into the sector researcher agents.
  - Rolling pairwise correlation matrix: within each sector, trailing 20-day and 60-day correlation between all pairs
  - Divergence detection: automated flagging of names breaking from sector group behavior — e.g., NVDA rallying while AMD and AVGO are flat
  - Divergence magnitude and duration: how far and for how long a name has decoupled — fresh divergences are more actionable than ones that have persisted for weeks
  - Historical divergence resolution: when similar divergences have occurred in this pair historically, how did they resolve — did the leader pull back, the laggard catch up, or did the divergence persist (signaling a fundamental regime shift)?

  *7b. Cross-sector rotation*
  Money flowing between sectors on a relative basis. When tech sells off while financials rally, the market is repricing growth vs. value, rates, or risk appetite.
  - Sector ETF relative performance: rolling ratio of XLK/XLF/XLE/SMH vs. SPY and vs. each other
  - Sector ETF flow data: net inflows/outflows for major sector ETFs — tracks where new capital is being allocated
  - Broad equity fund flows: aggregate net inflows/outflows into equity mutual funds and ETFs (weekly from ICI, daily for ETFs) — the demand/supply picture for equities as an asset class. Sustained outflows mean the tide is going out regardless of sector-level signals. *(Absorbed from former category 9 — sentiment and positioning)*
  - Rotation velocity: slow rotation over weeks is a regime shift; sharp rotation intraday is usually event-driven and potentially mean-reverting — the system should classify each rotation signal by speed
  - Rotation narrative classification: the distillation layer should tag rotation patterns with their likely driver — rate-driven (financials vs. tech), growth-driven (cyclicals vs. defensives), risk-appetite-driven (high-beta vs. low-beta)

  *7c. Index vs. constituent behavior*
  The relationship between broad indices and their components. Market-cap-weighted indices can mask what's happening underneath — a handful of mega-caps can paper over broad weakness or suppress a broad rally. Also includes market-wide microstructure health metrics that capture systemic trading conditions. *(Market-wide microstructure absorbed from former category 10 — market structure and internals)*
  - Breadth indicators: percentage of universe names above 20/50/200-day EMA — rising SPY with declining breadth is a fragile rally
  - Advance/decline within each sector: how many names are up vs. down on the day, and the magnitude spread
  - Equal-weight vs. cap-weight performance: when the equal-weight version of a sector underperforms the cap-weight version, the rally is narrow and concentrated in the largest names
  - New highs vs. new lows: 52-week high/low counts within the universe — divergence from index direction is a classic breadth warning
  - Market-wide aggregate spread: average bid-ask spread across the universe — widening signals systemic liquidity deterioration
  - Aggregate volume vs. trailing average: total market volume relative to 20-day average — low volume means less conviction behind moves, high volume confirms them
  - Market-wide liquidity score: composite of spread + depth + volume across the universe — tells the portfolio manager whether the current environment is hospitable to the system's typical position sizes

  *7d. Intermarket regime signals*
  Classic cross-asset relationships that define market regimes. These relationships aren't static — they have regimes, and regime transitions invalidate strategies built on the old relationship. Also includes market-wide leverage and sentiment context that informs regime assessment. *(Margin debt and sentiment surveys absorbed from former category 9 — sentiment and positioning)*
  - Stocks vs. bonds: SPY vs. TLT correlation — positive correlation (inflation regime, both sell off together) vs. negative correlation (growth regime, bonds hedge equities). When this flips, portfolio construction changes
  - Gold vs. real yields: gold typically moves inversely to real yields — divergence signals a safe-haven bid or structural demand shift
  - Oil vs. energy stocks: the beta of energy names to crude — when energy stocks stop tracking oil, the market is repricing the sector's fundamentals independently of the commodity
  - VIX vs. SPY: the normal inverse relationship — divergence (VIX rising on a flat/rising market) signals hedging demand building beneath the surface
  - Correlation regime stability: is each of these relationships holding steady or in flux? A relationship that has been stable for months carries more predictive weight than one that's been unstable
  - Margin debt and leverage: FINRA margin debt data (monthly) — rising margin debt in a rally means the rally is fueled by borrowed money and is fragile. Margin debt near historical extremes is a contrarian signal. Primarily informs the portfolio manager's risk assessment
  - Sentiment surveys: AAII bull/bear ratio, Investors Intelligence, CNN Fear & Greed Index — contrarian indicators at extremes. These measure *opinion* rather than *behavior* (all other positioning data measures what people are doing with money). Useful as context rather than direct trade triggers: extreme bullishness suggests the market is crowded, extreme bearishness suggests capitulation. Weekly frequency

  *7e. Implied vs. realized correlation*
  A derivatives-market signal that tells the system whether the current environment rewards thesis-driven individual-name trading or whether macro is overwhelming everything.
  - CBOE implied correlation index: what the options market expects cross-stock correlation to be
  - Realized correlation: what correlation has actually been over the trailing window
  - Implied-realized gap: when implied is much higher than realized, the market is overpaying for correlation hedges (dispersion opportunity). When implied is lower than realized, the market is underpricing systemic risk
  - Correlation regime for strategy selection: high-correlation environments favor macro/sector bets over single-name theses; low-correlation environments favor the system's individual-name thesis generation

  *7f. Lead-lag relationships*
  Which assets tend to move first when a repricing happens. When a confirmed move in a leading asset occurs and the expected lag response is missing, that's a high-conviction short-term thesis.
  - Funding → credit → equity lead-lag: funding market stress (repo/SOFR from 6e) leads credit spread widening, which in turn leads equity weakness — the full transmission chain. Flag when funding stress appears without a credit or equity response, and separately when credit spreads widen (from 6e) without equity response
  - Semis → tech lead-lag: semiconductor names often lead broader tech by hours to days on AI/infrastructure narrative shifts
  - Financials → market lead-lag: financial stocks often lead the broad market on rate-driven moves
  - Commodity futures → energy stocks lead-lag: crude/gas moves often lead the energy sector names by hours
  - Rolling lead-lag estimates: the distillation layer should maintain trailing estimates of lead-lag timing between key pairs and flag when a lead asset has moved but the expected lag response is overdue
  - Lead-lag regime shifts: the leader/follower structure isn't fixed — when the normal lead-lag relationship inverts, it signals a regime change in what's driving the market

  *7g. Correlation regime change detection*
  The meta-layer. Not what correlations are, but whether they're changing. Correlation structure shifts are one of the strongest signals that a new macro regime is emerging — often before the narrative catches up.
  - Rolling correlation stability: are pairwise correlations across the universe holding steady or in flux? Measured as the standard deviation of trailing correlation estimates
  - Correlation breakdown detection: flag when any major pairwise or sector-level correlation exceeds historical norms for rate of change
  - Dispersion shifts: sudden increase in cross-stock dispersion (names moving independently that normally move together) signals a regime transition
  - Narrative lag indicator: when correlation structure has shifted but financial media narrative hasn't caught up, the market is in an early-stage regime transition — this is where the synthesizer agent adds the most value by naming the new regime before it's consensus

*Supplementary categories (high value, lower frequency):*

**8. Commodities**
Sits at the intersection of two roles: for the energy sector, commodities are the *direct driver* (crude moves and energy stock moves are mechanically linked). For the other three sectors, commodities are an *environmental signal* — they tell you about inflation expectations, growth trajectory, and supply chain conditions that ripple through tech, semis, and financials on a lag. Agricultural commodities are excluded — the ags → food CPI → rates → equities chain has too many hops with noise and lag at each step for the 4–72 hour horizon, and ag supply shocks will surface in inflation breakevens (6c) and the macro surprise index (6g) first. If the commodity data vendor covers ags at trivial marginal cost, keep a dormant connector for the adaptive research layer.

  *8a. Crude oil*
  The single most important commodity for the system given energy is a quarter of the universe. Not one signal but several — price, curve shape, regional spreads, and inventory data each tell a different story.
  - WTI and Brent spot price: level, daily change, and multi-day trend
  - Futures curve shape: backwardation (near > far, implies supply tightness) vs. contango (far > near, implies surplus) — the curve shape is a forward-looking supply/demand signal
  - WTI-Brent spread: US-specific vs. global dynamics — widening spread signals US oversupply or export constraints
  - EIA weekly petroleum report: crude inventory draw/build vs. consensus expectations — effectively an "earnings report" for the entire energy sector every Wednesday at 10:30 AM ET. The surprise component (actual vs. expected) drives immediate repricing
  - Implied volatility on crude futures: how much movement the oil market is pricing — spikes signal event risk (OPEC meetings, geopolitical escalation)

  *8b. Natural gas*
  Distinct market from crude with its own supply/demand dynamics, seasonality, and catalyst structure. More seasonal and weather-sensitive, which makes it more predictable in some ways.
  - Henry Hub spot and front-month futures: level and daily change
  - Natural gas futures curve: seasonal shape and deviations from typical seasonal pattern
  - EIA natural gas storage report: weekly injection/withdrawal vs. expectations — Thursday catalyst, same pattern as crude EIA report
  - Weather forecast as demand driver: heating degree days (winter) and cooling degree days (summer) forecasts — cold snaps and heat waves create directional setups the system can anticipate
  - LNG export volumes and pricing: relevant for LNG-exposed names (LNG, ET) — global LNG spot prices (JKM, TTF) signal export demand

  *8c. Industrial metals*
  Economic health indicators and manufacturing cost inputs. Copper especially is a real-time growth barometer that can diverge from equity pricing.
  - Copper: spot price, futures curve, and LME warehouse inventory levels — copper rallying when equities are soft suggests the physical economy is stronger than the market is pricing; copper weakening while equities hold suggests an unpriced growth slowdown
  - Aluminum: spot price and production data — feeds into manufacturing cost structures for semis and tech hardware
  - Copper-to-gold ratio: a classic growth-vs-safety barometer — rising ratio signals growth confidence, falling ratio signals defensive positioning
  - Industrial metals vs. equity divergence: the distillation layer should flag when industrial metals and equities are sending conflicting growth signals

  *8d. Precious metals*
  Primarily macro signals rather than direct equity drivers. Gold's relationship with real yields is one of the most stable intermarket correlations — breaks from that relationship signal something the rest of the market hasn't priced.
  - Gold: spot price, daily change, and position relative to real yields (from 6a) — gold surging while real yields are stable means safe-haven demand or central bank buying that isn't visible in equities yet
  - Silver: spot price and gold/silver ratio — ratio extremes are a risk-appetite signal (silver outperforms gold in risk-on, underperforms in risk-off)
  - Gold ETF flows (GLD, IAU): institutional and retail appetite for safe-haven exposure — sustained inflows during calm equity markets signal quiet accumulation of hedges
  - Central bank gold buying signals: monthly/quarterly data from WGC — structural demand that can decouple gold from its normal real-yield relationship

  *8e. Energy derivatives and crack spreads*
  The refined product layer that connects crude to actual profitability for integrated and refining names in the energy universe.
  - Gasoline crack spread: the price difference between crude and gasoline — widening spreads signal refiner profitability improvement
  - Heating oil / diesel crack spread: same as gasoline but for distillates — seasonal patterns and demand signals
  - 3-2-1 crack spread: the composite refining margin benchmark (3 barrels crude → 2 barrels gasoline + 1 barrel distillate)
  - Gasoline demand patterns: seasonal driving demand (driving season vs. off-season), weekly EIA demand data
  - Crack spread vs. energy stock performance: the distillation layer should flag when crack spreads diverge from energy stock prices — spreads widening while stocks lag is a setup

  *8f. Commodity positioning and flow*
  How market participants are positioned in commodity markets. Extreme positioning is a contrarian signal.
  - Commitments of Traders (COT) reports: commercial hedger vs. speculative positioning in crude, gas, gold, copper futures — weekly data, published Friday for the prior Tuesday
  - Speculative positioning extremes: when speculators are max-long (or max-short) crude, the next move is more likely to reverse — flag historical percentile of net speculative positioning
  - Commodity ETF flows: USO, GLD, SLV, UNG net inflows/outflows — real-time retail and institutional appetite for commodity exposure
  - Managed money positioning trend: are commodity hedge funds adding or reducing exposure? Direction and velocity of the trend matters more than the level

**~~9. Sentiment and positioning~~ — DISSOLVED**
All components absorbed into existing categories: fund flows → 7b (cross-sector rotation), margin debt and leverage → 7d (intermarket regime signals), sentiment surveys → 7d, equity index futures positioning → 3c (open interest and positioning landscape). Put/call ratios were already in 3f, retail/institutional segmentation triangulated from existing signals (see 2a), commodity positioning in 8f, institutional ownership in 5g.

**~~10. Market structure and internals~~ — DISSOLVED**
All components already covered: breadth indicators → 7c (index vs. constituent behavior), advance/decline → 7c, new highs/lows → 7c, sector relative strength → 7b (cross-sector rotation), correlation regime detection → 7g. Market-wide microstructure health metrics (aggregate spreads, volume, liquidity score) added to 7c during category 9 dissolution.

**11. Volatility regime**
The market-wide volatility environment as a systemic signal. Everything else in the data model measures volatility at the ticker level (3a, 1f) or the correlation level (7e). This category captures the VIX-derived regime that affects every sector equally and changes the entire playbook: position sizing, thesis duration, entry/exit tactics, and which kinds of setups the system should be looking for.

  *Architectural note — regime label broadcast:* The volatility regime classification (11f) is not just a synthesizer input. It should be delivered to *every* agent as universal context metadata alongside their primary inputs. When the regime shifts from compression to expansion, every agent needs to know immediately because it changes what "normal" looks like for every metric they evaluate. The sector researchers recalibrate what counts as an anomalous move, the analyst adjusts thesis generation and position sizing, and the PM adjusts risk tolerance.

  *11a. VIX level and dynamics*
  The headline — but the level alone is almost useless without context. VIX at 20 after sitting at 12 for months is a completely different signal from VIX at 20 coming down from 35. The velocity and direction of VIX movement is the primary signal, not the number itself.
  - VIX spot: current level
  - VIX percentile rank: where current VIX sits relative to its own trailing 1-year and 5-year range
  - VIX rate of change: daily and multi-day moves — a spike from 14 to 22 in two days means the market is repricing risk right now; VIX sitting at 22 for a month means elevated risk is already priced
  - VIX distance from trailing mean: how far VIX has deviated from its own recent average, in standard deviation terms — extreme deviations tend to mean-revert

  *11b. VIX term structure*
  The shape of the VIX futures curve — where the market's temporal expectations about volatility live.
  - Term structure shape: VIX spot vs. VX1 (front month) vs. VX2 vs. VX3 — plotted as a curve
  - Contango/backwardation state: contango (near < far) is normal, backwardation (near > far) is the stress signal indicating an imminent event or crisis
  - Steepness: steep contango means deep short-term complacency (contrarian signal); deep backwardation means acute near-term stress
  - Regime classification: steep contango, flat contango, flat, mild backwardation, deep backwardation — transitions between these states are more actionable than steady-state readings
  - Roll yield signal: the cost of rolling VIX futures positions — steep contango makes long-vol expensive (volatility sellers rewarded), backwardation makes short-vol expensive (volatility buyers rewarded)

  *11c. VVIX — volatility of volatility*
  How uncertain the market is about volatility itself. A regime *uncertainty* signal distinct from regime *level*.
  - VVIX level: current reading and percentile rank
  - VIX/VVIX matrix: the combination of VIX level and VVIX level defines four distinct states:
    - Low VIX + low VVIX: calm and confident — normal operations
    - High VIX + low VVIX: stressed but stable — the market thinks it knows how bad it is
    - High VIX + high VVIX: stressed and uncertain — the market doesn't know how much worse it could get (highest risk state)
    - Low VIX + high VVIX: calm surface, deep anxiety — the market isn't confident the calm will hold (the sneaky dangerous state, historically precedes sharp moves)

  *11d. Realized vs. implied volatility gap*
  The market-wide risk premium — is the market's fear justified by actual movement?
  - VIX vs. SPX realized vol: trailing 10-day and 20-day realized volatility of SPX vs. current VIX level
  - Risk premium (implied minus realized): positive = market is paying for protection not justified by actual movement; negative = actual risk is materializing beyond what was priced
  - Gap direction of travel: closing gap (realized catching up to implied) is an acceleration signal — risk is materializing. Widening gap (implied pulling away from realized) means fear is outrunning reality — potential for vol mean-reversion
  - Historical gap percentile: where the current gap sits relative to its own trailing range — extreme premium or discount is actionable

  *11e. SKEW index*
  The tail risk barometer. Measures implied vol of deep OTM puts relative to ATM options. One of the most underappreciated signals in the volatility complex.
  - SKEW level: current reading and percentile rank
  - VIX/SKEW combination: the critical pairing:
    - Low VIX + high SKEW: "surface calm, deep anxiety" — institutional money is quietly buying disaster insurance while the market looks fine. Historically precedes some of the sharpest selloffs because the market was structurally fragile despite appearing calm
    - High VIX + high SKEW: broad fear with tail hedging — the market is scared and protecting against worst case
    - Low VIX + low SKEW: genuine complacency — no one is hedging anything
    - High VIX + low SKEW: fear is concentrated in at-the-money, not tails — the market expects more of the same, not a regime change
  - SKEW trend: rising SKEW over days/weeks signals building institutional anxiety even if VIX is flat

  *11f. Volatility regime classification*
  The composite state assessment — a derived regime label maintained continuously by the distillation layer. This is the output that gets broadcast to all agents as universal context.
  - Low-vol compression: VIX low, term structure in steep contango, VVIX low, realized vol declining. Mean-reversion strategies work well, breakout risk is building. Bollinger squeezes on individual names (from 1f) are more likely to resolve into trending moves
  - Vol expansion: VIX rising, term structure flattening, realized vol increasing. Momentum and breakout strategies work, mean-reversion becomes dangerous. The system should favor thesis-driven directional trades over range-bound setups
  - Crisis/spike: VIX elevated, term structure in backwardation, VVIX high. Reduce position sizes, widen stops, favor defensive theses, bias toward closing positions rather than opening new ones. The pre-close anchored run should be especially aggressive about risk reduction in this regime
  - Vol normalization: VIX declining from elevated levels, term structure returning to contango. Opportunities in names that overreacted during the spike — oversold bounces on thesis-intact names. The system should increase position sizes cautiously as the regime stabilizes
  - Regime transition detection: flag when the regime label changes, with confidence level — early transitions are more actionable but less certain than confirmed transitions

**12. Corporate actions and structural flows** *(renamed from "alternative / exotic")*
Event-driven, lower-frequency signals that create mechanical or predictable flows. What unites this category: when an index adds a stock, passive funds *must* buy; when a buyback blackout lifts, the corporate bid *will* return; when a 13D gets filed, activist pressure *is* coming. The predictability of the flow is what makes these valuable — they're closer to physics than psychology. *(Note: dark pool activity previously listed here has been moved to category 2c. Insider transactions and 13F data have been moved to category 5g.)*

  *12a. Index rebalance and reconstitution*
  The most mechanically predictable flow in markets. Every dollar of passive money tracking major indices must rebalance when composition or weights change. Flows are quantifiable (estimated from AUM), timing is known (dates published), direction is forced.
  - Index add/remove announcements: S&P 500, NASDAQ 100, Russell — additions are multi-day bullish catalysts, removals are multi-day bearish catalysts for affected names
  - Estimated passive flow magnitude: AUM tracking the index × weight change = approximate dollar flow the name will absorb — larger passive flow means stronger and more sustained price impact
  - Quarterly reconstitution weight changes: even without adds/removes, weight adjustments from market cap changes create rebalance flow across every constituent
  - Float and share count changes: events that affect index weight calculations outside normal reconstitution — stock splits, secondary offerings, buyback-driven share reduction — can trigger off-cycle rebalancing
  - Rebalance date proximity: flag when a confirmed rebalance event is within N days for any universe name — the flow typically starts building before the official date as front-runners position

  *12b. Corporate buyback execution*
  The company as a buyer of its own stock. Active buyback programs create a steady bid, but it's gated by regulatory and corporate calendar constraints.
  - Active buyback program status: which universe names have authorized buyback programs, remaining authorization size, and recent execution pace
  - Buyback blackout windows: typically ~2 weeks before earnings through ~2 days after — the corporate bid is absent during this period. A stock selling off during blackout is more vulnerable than the same selloff when the company can step in
  - Buyback window open/close dates: estimated per ticker based on earnings calendar — the moment the window reopens after earnings, fresh corporate demand enters
  - Buyback as percentage of daily volume: for names with large programs (AAPL, GOOG, META), buyback execution can be a meaningful fraction of daily volume — its presence or absence shifts the supply/demand balance
  - 10b5-1 plan indicators: pre-scheduled buyback plans that execute automatically — these provide a more predictable bid than discretionary buybacks

  *12c. Regulatory filings and corporate events*
  SEC filings beyond the insider transactions in 5g. Sporadic but high-signal when they fire, often creating asymmetric setups because the market takes time to digest them.
  - Schedule 13D filings: activist investors crossing 5% ownership with intent to influence — the "someone is coming for this company" signal. Triggers repricing over days as the market assesses the activist's thesis and likely actions
  - 8-K filings: material events catch-all — executive departures, M&A announcements, credit facility changes, material impairments, strategic pivots. The system should flag 8-K filings for universe names and classify by event type
  - HSR Act filings: Hart-Scott-Rodino pre-merger notifications as M&A signals when they surface through reporting
  - Shelf registration statements (S-3): a company registering shares for future sale — quiet warning that dilutive supply is coming. The gap between registration and actual offering is the window of uncertainty
  - Proxy fight filings (DFAN14A): signals contested board elections or activist campaigns — creates event-driven volatility with a known resolution date

  *12d. Analyst and investor event calendar*
  Scheduled events where companies present to institutional investors outside the normal earnings cycle. Don't get the same attention as earnings but can move stocks 5–10% on strategy updates or guidance changes.
  - Investor day / analyst day / capital markets day: dates and agenda topics for universe names — management updating strategy, raising/lowering long-term targets, or announcing new initiatives
  - Industry conference presentations: scheduled speaking slots at major conferences (Goldman Sachs, JP Morgan, Morgan Stanley conferences, CES, etc.)
  - Event novelty signal: a company that hasn't held an investor day in two years and suddenly schedules one is signaling something — the distillation layer should flag unusual scheduling patterns
  - Post-event drift tracking: historical pattern of how each name's stock reacts in the 1–5 days following investor events — some companies consistently use these events to deliver positive surprises, others use them to reset expectations downward

  *12e. Lock-up and secondary offering calendar*
  Known supply events that create predictable selling pressure.
  - IPO lock-up expiration dates: for any universe names within ~18 months of IPO, the date when insiders and early investors gain the ability to sell — known catalyst for downward pressure
  - Lock-up overhang: estimated insider ownership × stock performance since IPO = approximate selling pressure. Strong post-IPO performance means insiders are sitting on large gains and more likely to sell at lock-up expiration
  - Secondary and follow-on offering announcements: dilutive offerings create direct selling pressure — the announcement itself often moves the stock more than the actual pricing
  - Shelf takedown risk: for names with active shelf registrations (from 12c), the probability of an imminent offering based on stock price performance, capital needs, and market conditions

  *12f. ETF creation/redemption and rebalance*
  Mechanical flows from ETF structure that can distort single-name signals. When a sector ETF sees large inflows, authorized participants must buy the underlying stocks in proportion — creating correlated buying pressure independent of any fundamental view.
  - Sector ETF flow magnitude: daily creation/redemption activity for XLK, XLF, XLE, SMH, and other ETFs holding universe names
  - ETF-driven volume estimate: estimated percentage of a ticker's daily volume attributable to ETF creation/redemption — when this is elevated, single-name flow signals in category 2 may be misleading (what looks like institutional buying might be ETF-driven)
  - Thematic ETF rebalance: AI, cybersecurity, clean energy, and other thematic ETFs that hold universe names — rebalance events create flows that aren't captured by sector ETF tracking
  - ETF flow vs. single-name flow divergence: the distillation layer should flag when ETF-level flows and single-name flows are sending conflicting signals — ETF outflows paired with single-name institutional buying suggests someone is using the ETF sell as cover to accumulate individual names

