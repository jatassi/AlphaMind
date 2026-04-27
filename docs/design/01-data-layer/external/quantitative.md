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
What's happening beneath the price — who's buying, who's selling, how liquidity is structured. Reveals institutional footprints before they show up in price. *Aggregated* flow analysis over hours, not milliseconds — raw tick-level microstructure is HFT territory.

  *2a. Trade tape analysis*
  Executed trades aggregated into meaningful windows. Distinguishes "price moved because of real directional flow" from "price drifted on thin volume."
  - Net dollar flow: buy-side vs. sell-side pressure over rolling windows (1hr, 4hr, session)
  - Trade size distribution: shifting mix of small/medium/large prints signals changing participant composition
  - Aggressor-side balance: ratio of trades hitting bid vs. lifting ask
  - Volume-weighted flow direction: net flow normalized by total volume to capture intensity
  - *Retail flow triangulation:* "Is this retail-driven?" is answered by combining trade size distribution (small prints trending up), options flow patterns (3b — small-lot call buying), and social sentiment — not dedicated retail segmentation infrastructure (dated heuristics or expensive vendor data). The adaptive research layer assembles this on-demand when investigating whether a move has staying power.

  *2b. Liquidity and depth*
  Standing order book structure as aggregated health indicators. Tells whether the market can absorb a move and flags liquidity vacuums. The PM especially needs this — a thinning book on a name where the system holds a position is a risk signal independent of direction.
  - Bid-ask spread trends: average spread over rolling windows; widening as a stress signal
  - Depth at multiple levels: size resting at best bid/ask and at 5/10/20 bps away
  - Order book imbalance ratio: bid depth vs. ask depth at various levels
  - Liquidity score: composite of spread + depth + fill probability for the system's typical position sizes

  *2c. Block and institutional flow*
  Large prints and off-exchange activity — the primary institutional footprint signal.
  - Block trades: prints above a threshold size (e.g., >10k shares or >$500k notional), timestamped and side-tagged
  - Dark pool volume: percentage of total volume traded off-exchange; directional inference from dark pool prints relative to NBBO
  - FINRA short volume reports: daily short sale volume per ticker (more timely than bi-monthly short interest)
  - Sustained dark pool buying/selling: rolling detection of one-sided dark pool flow above/below VWAP — accumulation or distribution
  - Venue-level dark pool attribution: dark pools have different participant profiles — Sigma X (Goldman) institutional, IEX informed-flow protection, BATS dark more retail-originated. A large print on a Goldman venue reads differently than the same volume on a retail-heavy venue. Tag prints with venue and (where reported) participant type — meaningfully improves institutional footprint detection. Some venues report to FINRA TRF without venue-level granularity.

  *2d. Auction mechanics*
  Opening and closing auction data — massive liquidity events that can represent 10%+ of daily volume. Specifically relevant for the pre-open (9:00 AM ET) and pre-close (3:30 PM ET) anchored runs.
  - MOC/MOO imbalance: buy vs. sell imbalance in market-on-close and market-on-open orders, published by NYSE/NASDAQ
  - Imbalance evolution: how imbalance changes in the minutes before the auction (directional drift signals late-arriving institutional flow)
  - Auction price vs. continuous price: gap between auction print and last continuous trade — large gaps signal pent-up demand/supply
  - Closing auction volume concentration: what fraction of daily volume is in the close, and is it unusual?

  *2e. Intraday flow patterns*
  Temporal structure of trading activity. Unusual concentration at atypical times often signals programmatic execution or rebalancing.
  - VWAP trajectory vs. price: is institutional execution running above or below VWAP? Persistent deviation signals directional intent
  - Volume distribution: intraday curve vs. the ticker's typical pattern — front-loaded, back-loaded, or unusual midday concentration
  - End-of-day positioning flows: directional flow in the last 30–60 minutes, distinct from the closing auction
  - Time-of-day volume anomalies: bursts at unusual hours on no news, suggesting programmatic or rebalancing activity

  *2f. Extended-hours order flow*
  Flow-side complement to 1g's extended-hours price data — *who* is driving the pre-market/after-hours action. For a system whose first daily run fires at 9:00 AM ET, distinguishing a pre-market gap driven by institutional block flow from one driven by thin retail volume is the difference between a high-conviction opening thesis and a trap. Carries the same confidence discount as 1g.
  - Extended-hours trade tape: net dollar flow and aggressor-side balance (same metrics as 2a) computed separately for pre-market and after-hours
  - Extended-hours trade size distribution: a pre-market move dominated by large prints reads very differently than one built from small retail-sized trades
  - Extended-hours block detection: block trades (same thresholds as 2c) outside regular hours — institutional pre-market prints are high-signal because the institution chose to act before the full liquidity pool opens
  - Extended-hours dark pool activity: limited but occasionally meaningful; large prints in pre-market on earnings mornings signal institutional positioning before the open
  - Regular-session confirmation flag: track whether extended-hours flow direction is confirmed or reversed in the first 30 minutes of regular trading — historical confirmation rate per ticker calibrates pre-market signal weight

  *Cross-reference note:* Block/institutional flow (2c) and unusual options activity (3b) should be read together by the synthesizer. A block equity print paired with options sweeps on the same name within the same window is a much stronger signal than either alone.

**3. Derivatives and options**
The market's forward-looking expectations. Options markets are often smarter than spot because positioning requires conviction — capital on short-dated options is a time-bound directional bet, mapping directly to the 4–72h thesis horizon.

  *3a. Implied volatility structure*
  The market's expectation of move magnitude and shape across strikes and time. Sector researchers contextualize whether a price move is "expected" or anomalous; the analyst assesses whether a setup is cheap or expensive relative to expected movement.
  - Term structure: near-term vs. longer-dated IV, contango/backwardation shape
  - Skew: put/call IV differential, how lopsided the market's fear is between upside and downside
  - IV vs. realized vol: is the market over- or under-pricing movement relative to what's actually happening?
  - IV rank / IV percentile: current IV relative to its own trailing range

  *3b. Unusual options activity*
  The institutional footprint detector — likely the highest-signal subcategory. Large, deliberate positioning on time-bound directional bets.
  - Large block trades: single prints above threshold notional value
  - Sweeps: orders hitting the ask across multiple exchanges simultaneously (urgency signal)
  - Volume vs. open interest: volume spikes on strikes with low existing OI suggest new positioning
  - New positioning vs. closing/rolling: critical distinction — opening trades signal conviction, closing trades signal unwinding

  *3c. Open interest and positioning landscape*
  Where positions are concentrated and how they're shifting. Covers single-name options OI and equity index futures positioning.
  - Strike-level OI concentration: walls of puts, call magnets, max pain levels
  - OI day-over-day changes: where is new interest building or draining?
  - Expiration clustering: how much OI is concentrated in near-term vs. longer-dated expirations
  - Put/call OI ratio by strike and expiration
  - Equity index futures positioning (COT): asset manager and leveraged fund positioning in S&P 500, NASDAQ 100, sector futures — weekly data. Extreme speculative positioning is a contrarian signal.

  *3d. Dealer / market maker exposure*
  Determines the *character* of the next move rather than its direction. Important for PM risk assessment.
  - Gamma exposure (GEX): net dealer gamma positioning — long gamma dampens moves, short gamma amplifies them
  - GEX flip levels: price levels where dealer hedging behavior changes character
  - Delta exposure (DEX): net directional dealer hedging pressure
  - Vanna and charm flows: how dealer hedging shifts with vol and time decay

  *3e. Earnings-implied moves*
  Earnings are the single biggest catalyst type on the 4–72h horizon.
  - Implied earnings move: straddle pricing for the nearest expiration spanning the earnings date
  - Historical earnings move comparison: implied vs. actual stock moves over the last N earnings
  - Straddle richness: is the earnings vol premium cheap or expensive relative to history?
  - Pre-earnings IV ramp: how quickly IV builds into the event, and is the ramp unusual?

  *3f. Put/call dynamics*
  The *nature* of the flow matters more than raw ratios — same ratio reads very differently depending on whether puts are bought to open (bearish conviction) or sold to open (income/bullish).
  - Put/call volume ratio: per-ticker, per-sector, and index-level
  - Flow directionality: buy-to-open vs. sell-to-open breakdown for puts and calls
  - Protective vs. speculative: puts on names with large existing long interest read as hedging, not bearish bets
  - Index vs. single-stock skew: when index put buying diverges from single-stock put buying, signals macro hedging vs. idiosyncratic concern

  *3g. Cross-ticker options signals*
  Correlated positioning across multiple names that reveals relative value theses or pair trades. Lower frequency, high signal-to-noise.
  - Pair trade signatures: simultaneous bullish flow on one name + bearish flow on a correlated peer (e.g., NVDA calls + AMD puts)
  - Sector-wide sweeps: the same directional bet across multiple names in a sector within a short window
  - Cross-sector hedging: directional bets in one sector paired with hedges in another (e.g., long energy calls + long financials puts as an inflation trade)
  - ETF vs. single-stock divergence: when sector ETF options flow diverges from underlying names

  *3h. Sector ETF options flow*
  Institutions frequently express sector views through ETF options — a large XLF put buy is more direct than aggregating individual bank name puts. Sector researcher agents need ETF options flow as a first-class input alongside single-name data.
  - Sector ETF unusual activity: large blocks, sweeps, and volume-vs-OI spikes on XLK, XLF, XLE, SMH, QQQ — same detection framework as 3b applied to ETFs
  - ETF put/call flow character: buy-to-open vs. sell-to-open breakdown — heavy ETF put-to-open is a sector-level bearish signal distinct from individual name hedging
  - ETF IV vs. single-name IV composite: XLF IV spiking while underlying bank IVs haven't (or vice versa) signals the sector view hasn't propagated yet — a timing edge for the sector researcher
  - ETF options flow as sector sentiment: net directional flow on sector ETFs as a real-time sector barometer — feeds sector researcher briefs alongside 3a–3f
  - Index hedging vs. sector conviction: large SPY/QQQ put flow reads as macro hedging; large XLK put flow reads as tech-specific concern — distillation distinguishes when both appear

**4. Short selling and lending**
The bearish side of the ledger. Asymmetric signal — short sellers tend to be more informed than the average long because they pay to hold a negative position (borrow costs, dividend liability, margin requirements), so conviction is higher.

  *Data timeliness concern:* This category has the worst latency profile of any core category. Short interest is bi-monthly (FINRA settlement cycle). Borrow cost data is daily at best. Only daily short volume (FINRA) provides same-day signal. The distillation layer triangulates across 4a–4c — using daily short volume (fast) to estimate how short interest (slow) is changing between official reports, and using borrow cost spikes (fast) to infer utilization shifts before they're confirmed. Deeper lending structure (4d) and the squeeze composite (4e) are on-demand for the adaptive research layer. Annotate metrics with native refresh rate so the system knows what's fresh vs. estimated.

  *4a. Short interest and utilization*
  How much of the float is sold short and how much lendable supply is in use. Rate of change matters more than absolute level: 15% to 25% SI over two cycles is more actionable than one sitting at 40% for months.
  - Short interest (% of float): FINRA bi-monthly settlement data — the official count, stale by the time it publishes
  - Short interest ratio (days to cover): SI divided by average daily volume — how long shorts would take to unwind
  - Utilization rate: percentage of lendable shares currently lent out, available daily from lending platforms — approaching 100% is a squeeze setup
  - SI trend and rate of change: cycle-over-cycle change, flagging acceleration or deceleration

  *4b. Borrow cost and fee dynamics*
  The price of being short. Fee spikes mean demand to short is outstripping lendable supply — a leading indicator of squeeze potential and informed bearish conviction.
  - Current borrow fee: annualized cost to borrow, available daily from lending platforms
  - Fee trend and spikes: rate of change — sudden jumps are more actionable than elevated steady-state
  - Fee term structure: front-loaded (short-term event bet) vs. flat across tenors (structural bear thesis)
  - Hard-to-borrow list status: whether the name appears on prime broker HTB lists — extreme end of supply/demand imbalance

  *4c. Short volume and flow*
  Daily short sale activity, distinct from accumulated short interest. FINRA publishes this daily per ticker — the most timely signal in this category.
  - Daily short volume: total short sale volume as reported by FINRA
  - Short volume ratio: short volume as a percentage of total daily volume — spikes on otherwise quiet days are a strong signal
  - Short volume trend: multi-day rolling average to smooth noise and detect sustained directional shorting
  - Contextualization flag: read against order flow (category 2) — not all short volume is bearish; market makers short for hedging and liquidity provision. Flag directional vs. mechanical short volume.

  *4d. Securities lending market structure — ON-DEMAND RESEARCH INFRASTRUCTURE*
  The supply side. Detects "squeeze is coming" before the price move: supply contracting while demand holds forces covering regardless of the short's thesis. Daily data from securities lending platforms (IHS Markit, S3 Partners) — too slow and expensive for routine ingestion, but what the adaptive research layer needs when investigating a squeeze thesis. Build the API connection; don't schedule it.
  - Lendable supply: total shares available for lending
  - Supply trend: is the lendable pool growing or shrinking? Large holders recalling shares pulls supply and forces covering
  - Lender concentration: how many lenders provide the supply — concentrated lending is fragile (one recall triggers a cascade)
  - New loan creation vs. returns: net lending flow — new borrows or returned existing borrows?
  - Reg SHO threshold list status: names with persistent fails-to-deliver triggering mandatory close-out — extreme squeeze catalyst. Threshold list membership is a useful binary flag during squeeze investigations; underlying FTD data (SEC-published, ~2 week delay) is too stale for routine ingestion.

  *4e. Short squeeze composite — ON-DEMAND DERIVED METRIC*
  Derived state assessment synthesized from 4a–4d plus price/flow from categories 1 and 2: is this name in squeeze territory? Squeeze setups are high-conviction, time-bound asymmetric trades — the system's target profile. The adaptive research layer computes this when pursuing a squeeze thesis. Reliability depends on freshness of underlying data — 4a–4c real-time-ish, 4d on-demand.
  - Squeeze score: composite metric from "normal" through "elevated short pressure" to "active squeeze conditions"
  - Squeeze trigger proximity: how close the name is to conditions that historically precipitate covering cascades
  - Covering velocity estimate: if covering begins, how fast can it unwind given current depth (2b) and days-to-cover
  - Catalyst sensitivity: price move needed to push from "elevated" to "active squeeze" — ties into thesis sizing

**5. Fundamental and earnings**
Company-level numbers, but *not* for deep fundamental analysis or long-term valuation. On a 4–72h horizon, the system cares about how *expectations* are shifting and where consensus is wrong or about to be surprised. The signal is in the delta between what the market expects and what's happening.

  *Design principle — expectations vs. reality scorecard:* The distillation layer maintains a per-ticker scorecard tracking the gap between market expectations (consensus estimates, guidance, analyst targets) and reality signals (revisions, actuals, insider behavior). Updates near-real-time as new estimates land; re-anchors quarterly on actuals. Answers one question per ticker: "Is the expectation gap widening, narrowing, or stable?" Widening (either direction) is the primary thesis input for the analyst. Bimodal timeliness — revisions and rating changes are near-real-time, financials and ownership filings are quarterly/delayed — bridged into a single continuously-updated signal.

  *5a. Earnings estimate dynamics*
  The most actionable subcategory. Sell-side consensus estimates proxy for what the market has priced in; when estimates move, prices follow — but not always immediately or proportionally. That lag is the opportunity.
  - Consensus EPS and revenue estimates: current quarter, next quarter, full year — the baseline expectation
  - Revision direction and magnitude: which way estimates are moving and by how much, per time horizon
  - Revision velocity and breadth: one analyst bumping numbers is noise; five revising in the same direction within days is signal. Track revision count, percentage of covering analysts revising, and time clustering
  - Estimate dispersion: spread between highest and lowest estimates — wide dispersion means high uncertainty and potential for sharp repricing
  - Revision momentum: are revisions accelerating or decelerating?
  - Intraday revision timestamps: sell-side analysts publish in clusters — before open, midday, after close. A 2 PM revision may not hit price until next morning. For a system running every 2 hours, knowing *when* during the day a revision hit is the difference between catching and chasing the move. Timestamp revisions to the hour; flag revisions landed since last run but not yet absorbed by price (revision-price lag detection).

  *5b. Earnings calendar and event proximity*
  Where each ticker sits relative to its next earnings date. As earnings approach, IV ramps (see 3e), volume patterns shift, and the thesis framework changes.
  - Days to next earnings: countdown, with flags at key thresholds (within 7 days, within 2 days)
  - Event window mapping: pre-announcement quiet period start, expected report date, conference call time, typical post-earnings drift window
  - Historical post-earnings drift: does this name continue moving in the reaction direction over the following 1–5 days, or mean-revert?
  - Earnings clustering: when multiple names in the same sector report in the same window, the first reporter's reaction shapes expectations for the rest

  *5c. Revenue and growth trajectory*
  Baseline for where consensus should be anchoring. A stock with three quarters of accelerating revenue growth gets treated very differently on a miss than one that's been decelerating.
  - Revenue QoQ and YoY growth rates: trailing 4–8 quarters
  - Growth acceleration/deceleration: second derivative matters more than first for market reaction
  - Beat/miss history: trailing pattern vs. consensus — serial beaters get punished harder on first miss, serial missers get rewarded harder on first beat
  - Revenue mix shifts: for diversified names, which segments are growing and which are shrinking — relevant for tech where cloud/AI vs. legacy segments drive very different multiples

  *5d. Margin and profitability shifts*
  Quality-of-earnings read. Revenue beats get very different reactions depending on whether margins expanded or compressed.
  - Gross margin trajectory: trailing 4–8 quarters, trend direction and rate of change
  - Operating leverage: spread between revenue growth and operating expense growth — positive spread means scaling efficiently
  - EPS growth vs. revenue growth: EPS growing faster means margins expanding; reverse signals margin pressure
  - Profitability inflection detection: transition from unprofitable to profitable (or vice versa) is a regime change that reprices growth names

  *5e. Guidance and forward commentary*
  What management signaled about the future — quantifiable dimension alongside the qualitative pipeline's separate analysis.
  - Guidance vs. consensus: above, in-line, or below for next-quarter and full-year? "Guidance surprise" is often more predictive of post-earnings drift than the beat/miss
  - Guidance range width: narrow range signals confidence; wide signals uncertainty
  - Guidance revision: raised, lowered, or maintained vs. prior? Raise-and-beat is the strongest bull signal; beat-and-lower is often a sell-the-news setup
  - Forward commentary tone score: simplified quantification of management's forward-looking language — flags whether the qualitative pipeline should dig deeper

  *5f. Analyst rating and target landscape*
  Aggregate sell-side positioning. Not because analysts are right (they're often late), but because rating changes cause mechanical flow — upgrades trigger buying programs at funds screening on consensus ratings.
  - Consensus rating: current aggregate (buy/hold/sell distribution) and recent trend
  - Rating changes: upgrades, downgrades, and initiations within last N days — timestamped and attributed
  - Price target distribution: mean, median, high, low across covering analysts
  - Price vs. consensus target: stocks above all targets have a "who's left to buy" problem; stocks below the lowest target may be pricing something analysts haven't caught
  - Target revision clustering: multiple target raises/cuts in a short window amplify the signal

  *5g. Insider and institutional ownership shifts*
  How the people closest to the company are positioning. Insider buying is a stronger signal than insider selling (selling can be for liquidity, buying is almost always conviction).
  - Insider transactions: Form 4 filings — buys, sells, option exercises. Clustered insider buying is a strong medium-term signal
  - Insider buy/sell ratio: net insider sentiment over trailing 30/90 days
  - 13F institutional ownership changes: quarterly — flag new positions from prominent funds and large percentage changes
  - Institutional ownership concentration: percentage held by top 10 holders, and whether concentration is increasing (more conviction) or decreasing (distribution)

**6. Macro and rates**
The gravitational field everything trades in. Every sector is sensitive through different channels: financials directly rate-linked, tech through the discount rate on future earnings, energy through growth expectations and dollar strength. Macro is the tides; individual stocks are the boats.

  *Design principle — surprise over level:* Most macro data has a level component (where is the 10Y) and a surprise component (did CPI beat). For the 4–72h horizon, the surprise component is almost always more actionable. Distillation expresses macro data primarily as deviation from expectations rather than absolute values.

  *6a. Yield curve and treasury rates*
  The most information-dense single data source in markets. Curve shape, level, and rate of change encode growth, inflation, and monetary policy expectations simultaneously.
  - Key rate levels: 2Y (monetary policy proxy), 5Y and 10Y (equity discount rate), 30Y (long-term growth/inflation), fed funds effective rate
  - Curve spreads: 2s10s, 2s30s, 3m10s — steepening vs. flattening signals changing recession/expansion expectations
  - Rate of change: daily and multi-day moves at each tenor, absolute and z-score — a 15bp move in the 10Y in a single session is a vol event for every sector
  - Real yields: TIPS-implied real rates at 5Y and 10Y — rising real yields are the most direct headwind for growth/tech multiples
  - Curve regime classification: normal, flat, inverted, steepening, flattening — regime transitions matter more than steady-state shape
  - Treasury auction results: a poorly received 10Y or 30Y auction (weak bid-to-cover, large tail, high primary dealer allocation) can move rates 5–10bp in an hour and cascade into equities. Auctions are on a known schedule (Treasury publishes quarterly); results within minutes; pre-position awareness via the event calendar (6g). Key metrics per auction: bid-to-cover vs. recent average, tail (auction yield minus when-issued at cutoff — positive tail means weak demand), dealer vs. indirect vs. direct allocation, auction size relative to recent issuance. First-order signal for financials (rate-sensitive earnings); for tech, transmission is through the discount rate — a weak 10Y auction pushing yields up 8bp reprices growth multiples within hours.

  *6b. Federal Reserve and monetary policy*
  The market's probabilistic expectations for rate decisions — a clean, real-time probability distribution over a binary outcome.
  - Fed funds futures implied probabilities: hike, cut, or hold at each upcoming FOMC meeting
  - Rate path expectations: implied terminal rate and timing — how many cuts/hikes the market expects over 12 months
  - Probability shift velocity: a 10% change in December cut probability over two days is more actionable than the same shift over two weeks
  - Dot plot vs. market pricing: gap between the Fed's projections and what markets are pricing — divergence often resolves violently at FOMC meetings
  - Fed speaker impact: scheduled appearances; weight Chair and Vice Chair more heavily

  *6c. Inflation expectations*
  Market-derived (continuous) and data-driven (event-based). The actionable signal is the surprise component of releases.
  - Breakeven inflation rates: 5Y and 10Y TIPS spreads — real-time market expectations, continuous signal
  - Breakeven trend and rate of change: drift between releases signals shifting narrative before the next hard data point
  - CPI/PPI/PCE surprise: actual vs. consensus — magnitude and direction drive immediate repricing across all four sectors
  - Core vs. headline decomposition: which components drove the surprise (shelter, services, goods, energy) — affects which sectors get repriced
  - Inflation regime classification: hot, cooling, stable, deflation risk — regime transitions change the playbook for rate expectations and sector rotation

  *6d. Economic growth indicators*
  High-frequency data on whether the economy is expanding or contracting. Not for forecasting — for detecting when the growth narrative is about to shift.
  - PMI/ISM manufacturing and services: monthly headline and subcomponents (new orders leading), plus intra-month regional Fed surveys (Empire, Philly, Dallas, Richmond) that preview the national number
  - Employment data: weekly initial claims (most timely), monthly NFP (headline, revisions, wage growth, unemployment rate), ADP as a noisy NFP preview
  - Consumer data: retail sales (monthly), consumer confidence/sentiment surveys (Michigan, Conference Board)
  - Housing: existing/new home sales, building permits, housing starts — slower-moving but relevant for financials exposure
  - Growth surprise index: aggregate measure of whether data is broadly beating or missing expectations (similar to Citi Economic Surprise Index) — running score of consensus optimism/pessimism

  *6e. Credit conditions and spreads*
  Risk appetite barometer. Credit markets often lead equities at detecting stress — IG spread widening typically precedes equity weakness by days. Directly affects financials earnings.

  *Public credit:*
  - Investment grade (IG) spreads: option-adjusted spread vs. treasuries — baseline risk appetite measure
  - High yield (HY) spreads: below-investment-grade — more sensitive to recession risk, widens faster in stress
  - IG and HY spread rate of change: widening velocity matters more than level — 20bp widening in a week is a different signal than 20bp over two months
  - TED spread and SOFR-treasury spread: interbank stress and funding condition indicators
  - Commercial paper rates: short-term corporate funding costs; stress signals liquidity problems
  - CDS indices: CDX IG and CDX HY as real-time tradeable credit risk proxies

  *Private credit:*
  More opaque than public credit, but enough is available to detect stress before it surfaces publicly. BDC prices dropping while IG spreads are flat is an early warning signal.
  - BDC stock prices and NAV discounts: publicly traded BDCs (ARCC, MAIN, BXSL, OBDC) as real-time proxies — price-to-NAV discount widening signals market distrust of book values
  - BDC price divergence from IG/HY: BDC weakness with stable public spreads = private credit stress building that hasn't surfaced
  - CLO tranche pricing: senior vs. mezzanine vs. equity tranche spreads — stress cascades bottom-up, so equity tranche widening leads senior tranche widening
  - Direct lending indices: weekly/monthly data from Cliffwater or KBRA — default rates, recovery rates, yield trends
  - Bank credit quality commentary: quarterly earnings commentary on credit reserves, charge-offs, private credit book exposure — fed into the qualitative pipeline; flagged quantitatively when reserve builds accelerate

  *Funding and repo markets:*
  The plumbing under credit. Repo stress was the canary in September 2019 and March 2020. Funding dislocations lead credit spread widening which leads equity weakness — earliest link in the stress transmission chain (extends 7f credit → equity logic earlier).
  - SOFR rate and spread to fed funds: baseline overnight funding rate — SOFR spiking above the fed funds target range signals collateral scarcity or reserve shortages
  - Repo-treasury spread: gap between overnight repo and comparable treasury yields — widening signals funding stress, collateral hoarding, or balance sheet constraints at dealers. The September 2019 repo spike (SOFR briefly hit 5.25% vs. a 2% target) preceded broader equity volatility by days
  - Term repo rates: 1-week, 2-week, 1-month — term stress signals participants expect dislocation to persist, not resolve overnight
  - Fed reverse repo facility usage: the RRP acts as a liquidity floor — high usage means excess reserves parked at the Fed rather than deployed. Rapid changes signal shifts affecting bank reserves and lending capacity
  - Money market fund flows: weekly data on government and prime MMFs — flight-to-quality shows up as inflows to government MMFs and outflows from prime. Sudden shifts signal institutional risk aversion at the conservative end of the capital structure
  - Funding stress composite: distillation maintains SOFR spread + repo-treasury spread + term repo premium + MMF flow direction as a single "funding market health" score for the PM and synthesizer as systemic-risk early warning. Elevated readings trigger adaptive research

  *6f. Dollar and foreign exchange*
  Dollar moves affect the universe through three transmission channels. The system needs to know not just *that* the dollar moved but *why*.

  *Earnings translation channel (primarily tech):*
  Dollar strength reduces reported earnings for international-revenue companies. AAPL (~60% international), MSFT (~50%), GOOG, META, NVDA take meaningful translation hits from sustained strength. A sharp DXY move is effectively an earnings revision for half the tech universe that won't show up in estimates for days — front-runnable.
  - DXY (US Dollar Index): level, daily and multi-day change, z-score relative to trailing range
  - EUR/USD: the largest DXY component — European recession fears strengthen the dollar through this cross

  *Commodity pricing channel (primarily energy):*
  Oil, gas, and metals are priced in dollars; strength mechanically pressures commodity prices. Second-order effect: dollar strength from risk-off hits energy twice (dollar headwind + growth slowdown); dollar strength from rate differentials hits once (mechanical). Flag *why* the dollar is moving.
  - DXY-commodity correlation regime: is the current dollar move driving commodities or are they diverging?
  - USD/commodity-currency crosses: USD/CAD, USD/NOK as energy-linked FX proxies

  *Global financial conditions channel (all sectors):*
  Carry trade and liquidity channel. Rapid moves force leveraged global portfolios to de-risk across asset classes regardless of domestic fundamentals.
  - USD/JPY: the carry trade proxy — sharp yen strengthening (carry unwind) is a de-risking signal for US equities. The August 2024 yen unwind took US equities down hard despite nothing changing domestically
  - USD/CNY: China growth and trade friction signal — CNY weakness ripples into semis (supply chain exposure) and energy (demand expectations)
  - Dollar move attribution: classify each significant move as rate-differential-driven, risk-sentiment-driven, or trade-flow-driven — equity implications differ

  *Not tracked:* EM currencies and exotic crosses. Signal-to-noise drops fast outside the majors for a US large-cap portfolio.

  *6g. Macro event calendar and surprise index*
  The scheduling and scoring layer for macro releases — FOMC meetings (decision + minutes), CPI/PPI/PCE dates, NFP, ISM/PMI, GDP, consumer confidence, Fed speaker appearances — feeds the unified [`EventCalendar`](../schema/events.py) (Events:CAL) alongside regulatory / policy / geopolitical events. The macro surprise index, event proximity flags, and historical event-impact-by-sector lookup live on that entity.

**7. Cross-asset and correlation**
Measures *relationships between things*, not the things themselves. No raw data sources — computed from categories 1–6 and 8 by the distillation layer. Divergences between correlated assets are one of the clearest setups for the 4–72h horizon: when things that should move together stop moving together, something is about to resolve.

  *Architectural note:* This category's output flows to the synthesizer as a dedicated **correlation and regime brief**, separate from sector researcher briefs. Cross-asset correlation is inherently cross-domain and the synthesizer's native territory; embedding it in sector briefs would fragment the picture.

  *7a. Intra-sector correlation and divergence*
  The tightest correlation lens. Names within a sector normally move together — divergence means one is mispriced or the market is repricing a company-specific factor. Feeds directly into sector researchers.
  - Rolling pairwise correlation matrix: within each sector, trailing 20-day and 60-day correlation between all pairs
  - Divergence detection: automated flagging of names breaking from sector behavior — e.g., NVDA rallying while AMD and AVGO are flat
  - Divergence magnitude and duration: fresh divergences are more actionable than ones persisting for weeks
  - Historical divergence resolution: when similar divergences occurred in this pair historically, how did they resolve — leader pulled back, laggard caught up, or divergence persisted (signaling fundamental regime shift)?

  *7b. Cross-sector rotation*
  Money flowing between sectors on a relative basis. Tech selling off while financials rally is the market repricing growth vs. value, rates, or risk appetite.
  - Sector ETF relative performance: rolling ratio of XLK/XLF/XLE/SMH vs. SPY and vs. each other
  - Sector ETF flow data: net inflows/outflows for major sector ETFs
  - Broad equity fund flows: aggregate net flows into equity mutual funds and ETFs (weekly ICI, daily ETFs) — demand/supply for equities as an asset class. Sustained outflows mean the tide is going out regardless of sector-level signals
  - Rotation velocity: slow rotation over weeks is a regime shift; sharp intraday rotation is usually event-driven and potentially mean-reverting — classify each rotation by speed
  - Rotation narrative classification: tag rotation patterns with likely driver — rate-driven (financials vs. tech), growth-driven (cyclicals vs. defensives), risk-appetite-driven (high-beta vs. low-beta)

  *7c. Index vs. constituent behavior*
  Relationship between broad indices and their components. Market-cap-weighted indices can mask underlying dynamics — mega-caps can paper over broad weakness or suppress a broad rally. Includes market-wide microstructure health metrics.
  - Breadth indicators: percentage of universe names above 20/50/200-day EMA — rising SPY with declining breadth is a fragile rally
  - Advance/decline within each sector: how many names are up vs. down, and the magnitude spread
  - Equal-weight vs. cap-weight performance: equal-weight underperforming cap-weight means the rally is narrow and concentrated in the largest names
  - New highs vs. new lows: 52-week high/low counts within the universe — divergence from index direction is a classic breadth warning
  - Market-wide aggregate spread: average bid-ask spread across the universe — widening signals systemic liquidity deterioration
  - Aggregate volume vs. trailing average: total market volume relative to 20-day average — low volume means less conviction behind moves
  - Market-wide liquidity score: composite of spread + depth + volume across the universe — tells the PM whether the environment is hospitable to typical position sizes

  *7d. Intermarket regime signals*
  Classic cross-asset relationships defining market regimes. Relationships have regimes, and transitions invalidate strategies built on the old relationship. Includes market-wide leverage and sentiment context.
  - Stocks vs. bonds: SPY vs. TLT correlation — positive (inflation regime, both sell off together) vs. negative (growth regime, bonds hedge equities). When this flips, portfolio construction changes
  - Gold vs. real yields: gold typically moves inversely to real yields — divergence signals safe-haven bid or structural demand shift
  - Oil vs. energy stocks: the beta of energy names to crude — when energy stocks stop tracking oil, the market is repricing sector fundamentals independently of the commodity
  - VIX vs. SPY: normal inverse relationship — divergence (VIX rising on flat/rising market) signals hedging demand building beneath the surface
  - Correlation regime stability: is each relationship holding steady or in flux? A relationship stable for months carries more predictive weight
  - Margin debt and leverage: FINRA margin debt (monthly) — rising margin debt in a rally means borrowed-money fragility. Margin debt near historical extremes is contrarian. Primarily informs PM risk assessment
  - Sentiment surveys: AAII bull/bear ratio, Investors Intelligence, CNN Fear & Greed Index — contrarian at extremes. Measures *opinion* rather than *behavior* (other positioning data measures money). Context, not direct trade triggers: extreme bullishness suggests crowding, extreme bearishness suggests capitulation. Weekly frequency

  *7e. Implied vs. realized correlation*
  Tells the system whether the current environment rewards thesis-driven single-name trading or whether macro is overwhelming everything.
  - CBOE implied correlation index: what the options market expects cross-stock correlation to be
  - Realized correlation: actual correlation over the trailing window
  - Implied-realized gap: implied much higher than realized = market overpaying for correlation hedges (dispersion opportunity). Implied lower than realized = market underpricing systemic risk
  - Correlation regime for strategy selection: high-correlation environments favor macro/sector bets; low-correlation environments favor single-name thesis generation

  *7f. Lead-lag relationships*
  Which assets move first when repricing happens. Confirmed move in a leading asset with missing lag response is a high-conviction short-term thesis.
  - Funding → credit → equity lead-lag: funding market stress (repo/SOFR from 6e) leads credit spread widening leads equity weakness. Flag when funding stress appears without credit or equity response, and when credit widens without equity response
  - Semis → tech lead-lag: semiconductor names often lead broader tech by hours to days on AI/infrastructure narrative shifts
  - Financials → market lead-lag: financial stocks often lead the broad market on rate-driven moves
  - Commodity futures → energy stocks lead-lag: crude/gas moves often lead energy sector names by hours
  - Rolling lead-lag estimates: maintain trailing estimates of lead-lag timing between key pairs; flag when a lead asset has moved but expected lag response is overdue
  - Lead-lag regime shifts: when normal lead-lag inverts, regime change in market drivers

  *7g. Correlation regime change detection*
  Whether correlations are changing, not what they are. Correlation structure shifts are one of the strongest regime-emergence signals — often before the narrative catches up.
  - Rolling correlation stability: are pairwise correlations across the universe steady or in flux? Standard deviation of trailing correlation estimates
  - Correlation breakdown detection: flag when any major pairwise or sector-level correlation exceeds historical rate-of-change norms
  - Dispersion shifts: sudden increase in cross-stock dispersion signals regime transition
  - Narrative lag indicator: correlation structure shifted but financial media narrative hasn't caught up = early-stage regime transition. The synthesizer adds maximum value here by naming the new regime before it's consensus

*Supplementary categories (high value, lower frequency):*

**8. Commodities**
For energy, commodities are the direct driver (crude/energy-stock moves are mechanically linked). For other sectors, commodities are an environmental signal — inflation expectations, growth trajectory, and supply chain conditions that ripple through tech, semis, and financials on a lag. Agricultural commodities are excluded; ag supply shocks surface in inflation breakevens (6c) and the macro surprise index (6g). Keep a dormant ag connector for adaptive research if the vendor covers it at trivial marginal cost.

  *8a. Crude oil*
  The single most important commodity given energy is a quarter of the universe. Multiple signals — price, curve shape, regional spreads, inventory data each tell a different story.
  - WTI and Brent spot price: level, daily change, multi-day trend
  - Futures curve shape: backwardation (near > far, supply tightness) vs. contango (far > near, surplus) — forward-looking supply/demand signal
  - WTI-Brent spread: US-specific vs. global dynamics — widening signals US oversupply or export constraints
  - EIA weekly petroleum report: crude inventory draw/build vs. consensus — effectively an "earnings report" for the energy sector every Wednesday at 10:30 AM ET. Surprise component drives immediate repricing
  - Implied volatility on crude futures: how much movement the oil market is pricing — spikes signal event risk (OPEC meetings, geopolitical escalation)

  *8b. Natural gas*
  Distinct market from crude with its own dynamics. More seasonal and weather-sensitive, more predictable in some ways.
  - Henry Hub spot and front-month futures: level and daily change
  - Natural gas futures curve: seasonal shape and deviations from typical seasonal pattern
  - EIA natural gas storage report: weekly injection/withdrawal vs. expectations — Thursday catalyst, same pattern as crude EIA
  - Weather forecast as demand driver: heating degree days (winter) and cooling degree days (summer) — cold snaps and heat waves create anticipatable directional setups
  - LNG export volumes and pricing: relevant for LNG, ET — global LNG spot prices (JKM, TTF) signal export demand

  *8c. Industrial metals*
  Economic health indicators and manufacturing cost inputs. Copper especially is a real-time growth barometer that can diverge from equity pricing.
  - Copper: spot, futures curve, LME warehouse inventory — copper rallying with soft equities means the physical economy is stronger than priced; copper weakening with held equities suggests an unpriced growth slowdown
  - Aluminum: spot and production data — feeds manufacturing cost structures for semis and tech hardware
  - Copper-to-gold ratio: classic growth-vs-safety barometer — rising = growth confidence, falling = defensive positioning
  - Industrial metals vs. equity divergence: flag when metals and equities send conflicting growth signals

  *8d. Precious metals*
  Macro signals rather than direct equity drivers. Gold-real-yields is one of the most stable intermarket correlations — breaks signal something the rest of the market hasn't priced.
  - Gold: spot, daily change, position relative to real yields (6a) — gold surging with stable real yields means safe-haven demand or central bank buying not yet visible in equities
  - Silver: spot and gold/silver ratio — extremes are a risk-appetite signal (silver outperforms gold in risk-on, underperforms in risk-off)
  - Gold ETF flows (GLD, IAU): institutional and retail safe-haven appetite — sustained inflows during calm equity markets signal quiet hedge accumulation
  - Central bank gold buying signals: monthly/quarterly WGC data — structural demand that can decouple gold from its normal real-yield relationship

  *8e. Energy derivatives and crack spreads*
  Refined-product layer connecting crude to profitability for integrated and refining names.
  - Gasoline crack spread: crude vs. gasoline — widening signals refiner profitability improvement
  - Heating oil / diesel crack spread: distillate version — seasonal patterns and demand signals
  - 3-2-1 crack spread: composite refining margin (3 barrels crude → 2 gasoline + 1 distillate)
  - Gasoline demand patterns: driving-season vs. off-season, weekly EIA demand data
  - Crack spread vs. energy stock performance: flag when crack spreads diverge from energy stock prices — widening spreads with lagging stocks is a setup

  *8f. Commodity positioning and flow*
  Extreme positioning is a contrarian signal.
  - Commitments of Traders (COT): commercial hedger vs. speculative positioning in crude, gas, gold, copper futures — weekly, published Friday for prior Tuesday
  - Speculative positioning extremes: when speculators are max-long (or max-short) crude, the next move is more likely to reverse — flag historical percentile of net speculative positioning
  - Commodity ETF flows: USO, GLD, SLV, UNG net flows — real-time retail and institutional commodity appetite
  - Managed money positioning trend: direction and velocity matter more than level

**11. Volatility regime**
Market-wide volatility environment as a systemic signal. Other categories measure volatility at the ticker level (3a, 1f) or correlation level (7e); this captures the VIX-derived regime that affects every sector and changes the entire playbook: position sizing, thesis duration, entry/exit tactics, setup types.

  *Architectural note — regime label broadcast:* The volatility regime classification (11f) is delivered to *every* agent as universal context metadata. Regime shifts (compression → expansion) change what "normal" looks like for every metric. Sector researchers recalibrate what counts as an anomalous move; the analyst adjusts thesis generation and position sizing; the PM adjusts risk tolerance.

  *11a. VIX level and dynamics*
  Level alone is almost useless. VIX at 20 after sitting at 12 for months is a different signal from VIX at 20 coming down from 35. Velocity and direction are the primary signals.
  - VIX spot: current level
  - VIX percentile rank: relative to trailing 1-year and 5-year range
  - VIX rate of change: daily and multi-day moves — a spike from 14 to 22 in two days means risk is being repriced now; VIX at 22 for a month means risk is already priced
  - VIX distance from trailing mean: how far VIX has deviated from recent average, in standard deviation terms — extreme deviations tend to mean-revert

  *11b. VIX term structure*
  The shape of the VIX futures curve.
  - Term structure shape: VIX spot vs. VX1 (front month) vs. VX2 vs. VX3 — plotted as a curve
  - Contango/backwardation state: contango (near < far) is normal; backwardation (near > far) signals imminent event or crisis
  - Steepness: steep contango = deep short-term complacency (contrarian); deep backwardation = acute near-term stress
  - Regime classification: steep contango, flat contango, flat, mild backwardation, deep backwardation — transitions are more actionable than steady-state
  - Roll yield signal: cost of rolling VIX futures — steep contango makes long-vol expensive (vol sellers rewarded); backwardation makes short-vol expensive (vol buyers rewarded)

  *11c. VVIX — volatility of volatility*
  How uncertain the market is about volatility itself. Regime *uncertainty* distinct from regime *level*.
  - VVIX level: current reading and percentile rank
  - VIX/VVIX matrix: combination defines four states:
    - Low VIX + low VVIX: calm and confident — normal operations
    - High VIX + low VVIX: stressed but stable — the market thinks it knows how bad it is
    - High VIX + high VVIX: stressed and uncertain — the market doesn't know how much worse it could get (highest risk state)
    - Low VIX + high VVIX: calm surface, deep anxiety — historically precedes sharp moves

  *11d. Realized vs. implied volatility gap*
  Market-wide risk premium — is fear justified by actual movement?
  - VIX vs. SPX realized vol: trailing 10-day and 20-day realized vs. current VIX
  - Risk premium (implied minus realized): positive = paying for unjustified protection; negative = actual risk materializing beyond what was priced
  - Gap direction of travel: closing gap (realized catching up to implied) = risk materializing. Widening gap (implied pulling away from realized) = fear outrunning reality, potential vol mean-reversion
  - Historical gap percentile: extreme premium or discount is actionable

  *11e. SKEW index*
  Tail risk barometer — implied vol of deep OTM puts relative to ATM. Underappreciated signal.
  - SKEW level: current reading and percentile rank
  - VIX/SKEW combination:
    - Low VIX + high SKEW: surface calm, deep anxiety — institutional disaster insurance buying. Historically precedes some of the sharpest selloffs
    - High VIX + high SKEW: broad fear with tail hedging — scared and protecting worst case
    - Low VIX + low SKEW: genuine complacency — nobody hedging
    - High VIX + low SKEW: fear concentrated at-the-money, not tails — more of the same expected, not regime change
  - SKEW trend: rising SKEW over days/weeks = building institutional anxiety even if VIX is flat

  *11f. Volatility regime classification*
  Composite state assessment maintained by distillation; broadcast to all agents.
  - Low-vol compression: VIX low, term structure steep contango, VVIX low, realized vol declining. Mean-reversion strategies work; breakout risk building. Bollinger squeezes (1f) more likely to resolve into trending moves
  - Vol expansion: VIX rising, term structure flattening, realized vol increasing. Momentum/breakout strategies work; mean-reversion dangerous. Favor thesis-driven directional over range-bound
  - Crisis/spike: VIX elevated, term structure backwardation, VVIX high. Reduce sizes, widen stops, favor defensive theses, bias toward closing. Pre-close run should be especially aggressive about risk reduction
  - Vol normalization: VIX declining from elevated, term structure returning to contango. Opportunities in names that overreacted during the spike — oversold bounces on thesis-intact names. Increase sizes cautiously as regime stabilizes
  - Regime transition detection: flag when the label changes with confidence level — early transitions are more actionable but less certain than confirmed

**12. Corporate actions and structural flows**
Event-driven, lower-frequency signals creating mechanical or predictable flows. When an index adds a stock, passive funds *must* buy; when a buyback blackout lifts, the corporate bid *will* return; when a 13D files, activist pressure *is* coming. Predictability of flow makes these valuable — closer to physics than psychology.

  *12a. Index rebalance and reconstitution*
  The most mechanically predictable flow. Every dollar of passive money tracking major indices must rebalance when composition or weights change. Flows are quantifiable (AUM-estimated), timing is known (dates published), direction is forced.
  - Index add/remove announcements: S&P 500, NASDAQ 100, Russell — additions are multi-day bullish, removals multi-day bearish
  - Estimated passive flow magnitude: AUM tracking the index × weight change = approximate dollar flow — larger flow means stronger and more sustained price impact
  - Quarterly reconstitution weight changes: weight adjustments from market cap changes create rebalance flow across every constituent
  - Float and share count changes: stock splits, secondary offerings, buyback-driven share reduction can trigger off-cycle rebalancing
  - Rebalance date proximity: flag when a confirmed rebalance is within N days — flow typically starts building before the official date as front-runners position

  *12b. Corporate buyback execution*
  The company as a buyer of its own stock. Active programs create a steady bid, gated by regulatory and corporate calendar constraints.
  - Active buyback program status: which universe names have authorized programs, remaining authorization size, recent execution pace
  - Buyback blackout windows: typically ~2 weeks before earnings through ~2 days after — corporate bid absent. Selloffs during blackout are more vulnerable than the same selloff when the company can step in
  - Buyback window open/close dates: estimated per ticker from earnings calendar — fresh corporate demand enters when the window reopens
  - Buyback as percentage of daily volume: for AAPL, GOOG, META, buyback can be a meaningful fraction of daily volume — presence or absence shifts supply/demand balance
  - 10b5-1 plan indicators: pre-scheduled plans executing automatically — more predictable bid than discretionary

  *12c. Regulatory filings and corporate events*
  SEC filings beyond 5g insider transactions. Sporadic but high-signal — markets take time to digest, creating asymmetric setups.
  - Schedule 13D filings: activist investors crossing 5% with intent to influence — "someone is coming for this company." Triggers repricing over days as the market assesses thesis and likely actions
  - 8-K filings: material events catch-all — executive departures, M&A announcements, credit facility changes, material impairments, strategic pivots. Flag for universe names and classify by event type
  - HSR Act filings: Hart-Scott-Rodino pre-merger notifications as M&A signals when reported
  - Shelf registration statements (S-3): registering shares for future sale — quiet warning of dilutive supply. Gap between registration and offering is the uncertainty window
  - Proxy fight filings (DFAN14A): contested board elections or activist campaigns — event-driven volatility with known resolution date

  *12d. Analyst and investor event calendar*
  Companies presenting to institutional investors outside the earnings cycle. Less attention than earnings but can move stocks 5–10% on strategy updates or guidance.
  - Investor day / analyst day / capital markets day: dates and agenda topics — management updating strategy, raising/lowering long-term targets, announcing initiatives
  - Industry conference presentations: scheduled slots at Goldman Sachs, JP Morgan, Morgan Stanley conferences, CES, etc.
  - Event novelty signal: a company that hasn't held an investor day in two years suddenly scheduling one is signaling something — flag unusual scheduling patterns
  - Post-event drift tracking: historical pattern of stock reaction 1–5 days post-event — some companies consistently use these to deliver positive surprises, others to reset expectations downward

  *12e. Lock-up and secondary offering calendar*
  Known supply events creating predictable selling pressure.
  - IPO lock-up expiration dates: for universe names within ~18 months of IPO, when insiders and early investors gain ability to sell
  - Lock-up overhang: insider ownership × post-IPO performance ≈ selling pressure. Strong post-IPO performance means large gains, more likely to sell at expiration
  - Secondary and follow-on offering announcements: dilutive offerings create direct selling pressure — the announcement often moves more than the actual pricing
  - Shelf takedown risk: for names with active shelf registrations (12c), probability of imminent offering based on price, capital needs, market conditions

  *12f. ETF creation/redemption and rebalance*
  Mechanical flows from ETF structure that can distort single-name signals. Large sector ETF inflows force authorized participants to buy underlying stocks proportionally — correlated buying pressure independent of fundamental view.
  - Sector ETF flow magnitude: daily creation/redemption activity for XLK, XLF, XLE, SMH, and other ETFs holding universe names
  - ETF-driven volume estimate: percentage of a ticker's daily volume attributable to ETF creation/redemption — when elevated, single-name flow signals (category 2) may be misleading (what looks like institutional buying might be ETF-driven)
  - Thematic ETF rebalance: AI, cybersecurity, clean energy ETFs holding universe names — rebalance flows not captured by sector ETF tracking
  - ETF flow vs. single-name flow divergence: ETF outflows paired with single-name institutional buying suggests someone using the ETF sell as cover to accumulate individual names

