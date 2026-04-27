# Qualitative / event data — 6 categories

The system's information edge. [Quantitative.md](quantitative.md) measures what has happened; this covers the unstructured, interpretive, forward-looking information that explains *why* and *what might happen next* — raw material where LLMs add value.

Qualitative data requires LLM interpretation from the start — programmatic "distillation" of a news article or earnings call transcript loses the nuance that makes it valuable. The ingestion layer collects and structures raw payloads; the analysis pipeline's qualitative research layer (see [qualitative-research.md](../../03-analysis-layer/qualitative-research.md)) interprets.

*Relationship to quantitative data:* Several quant categories have qualitative counterparts. Analyst ratings appear as structured data in quant 5f; the research note reasoning lives here. The macro event calendar is quant 6g; Fed speaker tone lives here. Earnings numbers are quant 5a–5g; transcript analysis lives here. Quant captures the *what*; this captures the *why* and *what if*.

*Ingestion architecture:* Qualitative data is event-driven and irregular. The ingestion layer handles scheduled pulls (prediction market prices, sentiment aggregates, calendar events) and event-triggered collection (breaking news, regulatory filings, earnings transcripts). Baseline research runs a fixed sweep every invocation; adaptive research makes targeted pulls based on quant anomaly flags.

---

**1. News and financial media**
The primary unstructured source and the most direct input to the information edge. Signal lives in the pattern across headlines, narrative shifts over hours and days, and the gap between news and price. A stock flat while three negative articles hit in two hours suggests the bad news is priced or disbelieved; the same articles with a 3% drop suggests agreement.

  *Design principle — narrative over headlines:* Individual headlines are noisy. The qualitative research layer extracts the *narrative arc* — is coverage of a name or sector getting more bullish or bearish over 6–24 hours? Is the framing shifting (yesterday's "temporary headwind" becoming today's "structural concern")? Narrative shifts often precede price moves by hours; detecting the shift before repositioning completes is the edge.

  *1a. Breaking news and real-time headlines*
  Wire service and financial news headlines collected continuously, batch-delivered each invocation. The ingestion layer timestamps and tags by ticker, sector, and topic — no interpretation.
  - Wire service headlines: Reuters, Bloomberg, Dow Jones/WSJ, AP — what algorithmic news-trading systems key off. The system competes on *depth of interpretation*, not speed: "NVDA CFO sells $20M in stock" triggers algo selling; the system reads the filing, checks 10b5-1 vs. discretionary, cross-references quant 5g insider data, and determines whether it's bearish or routine.
  - Financial news outlets: CNBC, Bloomberg, FT, WSJ, Reuters, MarketWatch, Barron's. CNBC drives retail sentiment, FT/WSJ drive institutional narrative, Bloomberg is the fastest.
  - Per-ticker news volume: count of articles mentioning a universe ticker over rolling windows (2h, 6h, 24h). Volume spikes *preceding* price moves signal persistence; spikes *following* moves suggest exhaustion.
  - Cross-ticker news clustering: multiple tickers in the same sector in the same story or short window forms a sector-level narrative — "three semis companies in China export restriction articles within 4 hours" is a sector signal even if only one name has moved.
  - News-price divergence flag: ingestion tags news with directional classification (positive/negative/neutral/mixed per ticker); distillation flags divergence — negative news with flat/rising price is "priced in," positive news with flat/falling price suggests a hidden problem.

  *Source credibility metadata:* News in 1a and 1b is tagged with a credibility tier at ingestion — Tier 1 (wire services, Bloomberg/Reuters/WSJ exclusives), Tier 2 (major financial outlets, established reporters), Tier 3 (blogs, aggregators, social-media-first). The qualitative research layer weights accordingly: Tier 1 exclusives can anchor a thesis; Tier 3 rumors only corroborate. The ingestion layer also tags which outlet broke the story since the first-mover sets the market's framing.

  *Sector-specific source lists:* Each sector has specialized channels general financial media doesn't cover well; maintained as configuration. **Tech / Semiconductors:** The Information, Semafor, SemiAnalysis, DigiTimes, SEMI.org, AI-focused outlets. **Energy:** Argus Media, Platts, Energy Intelligence, RigZone. **Financials:** American Banker, Risk.net, and Fed-watching journalists (Timiraos at WSJ, Nick at FT). Headline sweep (1a) and long-form scan (1b) include these alongside general financial media.

  *1b. Long-form analysis and opinion*
  Deeper than headlines but slower. Research notes, op-eds, investigative pieces that shape institutional thinking over days. The system captures publicly visible signals — published summaries, analyst quotes in media, narrative framing reaching the broader market.
  - Sell-side research note summaries: major-bank calls (upgrade/downgrade, thesis change, sector view shift) reach financial media within hours. Ingestion captures source, ticker(s), and directional call. Structured rating/target data goes to quant 5f; reasoning lives here.
  - Opinion and editorial tone: is the opinion ecosystem around a name or sector shifting? A cluster of bearish op-eds after a rally signals a building contrarian case. Adaptive research reads actual text when investigating.
  - Investigative and feature articles: longer-cycle signals. An FT deep dive on "cracks in the AI infrastructure spending thesis" may not move stocks on publication day but shapes institutional positioning over the week. Flag long-form pieces mentioning universe tickers or sectors, prioritized by outlet prestige and author prominence.
  - Insider transaction narrative: media coverage of sector-wide patterns ("semiconductor insider selling wave accelerates") is distinct from individual filings in quant 5g — collectively-routine sales can signal something the structured data doesn't capture.
  - Short reports and activist short narratives: when an activist short seller (Hindenburg, Muddy Waters, Citron) publishes against a universe name, the report creates a 4–72h trading opportunity as the market digests claims and the company responds. Tag as high-priority adaptive research trigger.
  - M&A rumors and deal speculation: Bloomberg "considering options" or "exploring sale" headlines on universe names are among the highest-signal qualitative events at this horizon — a credible rumor can move a name 10%+ intraday. Flag any M&A-related headline (acquisition, merger, takeover, strategic review, activist involvement). 6c covers M&A from the advisory-pipeline angle; this covers M&A as a direct catalyst for universe names as targets or acquirers.

**2. Social and alternative sentiment**
Aggregated crowd signals supplementing institutional media. The system ingests *aggregated* sentiment indicators, not raw feeds — parsing individual tweets or Reddit posts is a noise problem; aggregate direction and intensity is a useful contrarian and confirmation signal. Most valuable at extremes: euphoria signals crowding, capitulation signals potential bottoms.

  *Design principle — contrarian at extremes, confirming in the middle:* Sentiment is directionally useful in the middle range (crowd agrees with the move) but contrarian at extremes — when every retail trader is bullish, who's left to buy? "Extreme" must be calibrated per ticker since TSLA has a structurally wider sentiment range than JPM.

  *2a. Aggregated social sentiment scores*
  Pre-computed per-ticker sentiment from vendors processing social media firehoses (Twitter/X, Reddit, StockTwits) — the system doesn't build its own NLP pipeline.
  - Per-ticker sentiment score: directional with magnitude and confidence, refreshed at least hourly during market hours.
  - Sentiment rate of change: velocity matters more than level — neutral to strongly bullish in 4 hours is more actionable than week-long bullishness.
  - Sentiment volume: total social mentions per ticker over rolling windows. Volume spikes independent of direction indicate the name is "in play" — context for adaptive research investigating retail participation.
  - Sentiment-price divergence: bullish sentiment with declining price (or vice versa) — crowd disagrees with market, resolves in one of two ways, both tradeable.
  - Per-ticker calibration: TSLA's "neutral" is louder than most names' extremes. Distillation maintains trailing sentiment distributions per ticker and expresses current readings as per-name percentiles, not a universal scale.

  *Platform note:* Aggregated scores draw from Twitter/X, Reddit (r/wallstreetbets, r/stocks, r/investing), and StockTwits. Platform breakdown is a vendor implementation detail; different platforms skew toward different participant types (Reddit/StockTwits = retail, FinTwit = mixed retail/institutional) and the vendor's weighting accounts for this.

  *2b. Alternative sentiment proxies*
  Non-social signals reflecting crowd behavior.
  - Google Trends: search interest for tickers and company names. Spikes for "NVDA stock" or "is NVDA overvalued" map to retail attention regime shifts. Most useful as a contrarian flag at extremes.
  - Retail brokerage flow: some brokers publish aggregate user activity (most-bought, most-sold). Direct measure of retail positioning — retail buying heavily while institutional flow (quant 2c) sells creates a flagged divergence.

  *2c. Options flow narrative*
  Qualitative interpretation of unusual options activity. Quant 3 captures flow, positioning, and IV as numbers; this captures the *narrative* — large blocks, sweeps, and commentary about whether "smart money is hedging" or "someone knows something."
  - Unusual options activity reports: media and options analytics services flag large puts/calls, unusual volume vs. OI, institutional blocks. Tag for adaptive research when mentioning universe names.
  - Options-equity narrative divergence: defensive options positioning (rising put/call ratios, skew steepening) against a bullish equity narrative is a signal. Options markets are typically more informed at short horizons.
  - Earnings options positioning commentary: ahead of earnings, options-market commentary on expected move size, unusual pre-earnings positioning, and whether risk is priced above or below historical norms — qualitative complement to quant 3e implied move data.

**3. Prediction markets**
Forward-looking probability estimates on discrete outcomes. Prediction markets express collective expectations as tradeable prices, creating continuous probability estimates that update in real time. Unlike sentiment (which measures opinion), prediction markets measure *conviction weighted by money at risk* — capital behind a 70% probability of a rate cut is a stronger signal than a survey respondent checking a box.

  *Design principle — delta over level:* Rate of change in prediction market odds is more actionable than the absolute level. A 15-point overnight swing (65% to 80% on a rate hold) tells the system new information has arrived and the market hasn't fully absorbed the implications. The adaptive research layer investigates what drove the shift and whether equity prices have caught up.

  *Ingestion cadence:* Prediction market prices are pulled every invocation (lightweight API calls). The distillation layer computes and stores deltas between invocations, flags moves exceeding a threshold (e.g., >5 percentage points since last pull), and maintains a trailing history per contract so the analysis pipeline sees trajectory, not just current level.

  *3a. Monetary policy outcome markets*
  The cleanest and most liquid category. FOMC decisions are binary (hike/cut/hold), the calendar is known, and equity implications are well-mapped across all four sectors.
  - Fed funds rate decision probabilities: probability of hike, cut, or hold at each upcoming FOMC meeting — mirrored in fed funds futures (quant 6b), but prediction markets can diverge and the divergence itself is a signal
  - Basis point magnitude expectations: within a "cut" or "hike" outcome, the expected size — 25bp vs. 50bp carries very different equity implications
  - Terminal rate expectations: where the market expects the rate cycle to end, as a probability distribution — feeds tech and financials thesis generation
  - Inter-meeting action probability: rare but consequential — emergency cuts or hikes between scheduled meetings. When this rises above trivial levels, it signals extreme market stress or policy urgency
  - Prediction market vs. futures divergence: when prediction market odds materially diverge from fed funds futures implied probabilities (quant 6b), one market is wrong or they're processing different information. The qualitative research layer investigates

  *3b. Regulatory and political outcome markets*
  Government actions that create step-function changes in the operating environment for specific sectors or names.
  - Antitrust and competition outcomes: probability of enforcement actions against mega-cap tech (DOJ vs. GOOG, FTC vs. META/AMZN), merger approval/block probabilities for pending deals. Antitrust outcomes create binary repricing events — unexpected enforcement actions can move a name 5–15% in a session
  - Trade policy and tariff outcomes: probability of new tariff actions, trade agreement developments, or export control changes. Semiconductors are the most exposed — export controls on chip sales to China directly affect NVDA, AMD, AVGO, ASML, LRCX, KLAC, AMAT. Tariff actions also ripple through energy (pipeline approval, LNG export policy) and tech (supply chain)
  - Financial regulation: probability of changes to capital requirements, banking regulation, crypto regulation (affects COIN, SQ), CFPB enforcement actions
  - Tax policy: corporate tax rate changes or proposals — affects all sectors, magnitude name-specific based on effective tax rate and geographic revenue mix
  - Election and political transition markets: during election cycles, probability of outcomes that would change the regulatory and fiscal environment. Not routine, but during election years becomes a primary driver for all four sectors

  *3c. Geopolitical outcome markets*
  Events with broad market impact that aren't sector-specific policy decisions.
  - Conflict escalation/de-escalation: probabilities for geopolitical conflicts that affect energy supply (Middle East, Russia-Ukraine), semiconductor supply chains (Taiwan Strait), or broad risk appetite. Energy is most directly exposed, but a major event reprices risk across all sectors
  - Sanctions and trade disruption: probability of new sanctions affecting specific supply chains — Russia/energy, China/semiconductors
  - OPEC decision outcomes: probability of production cuts, increases, or holds at upcoming OPEC+ meetings. Discrete, binary events with known dates and direct energy sector impact. Cross-reference with oil futures curve shape (quant 8a) — divergence between prediction markets and futures is tradeable

  *Data source note:* Polymarket, Kalshi, PredictIt (where still operating), and Metaculus are the primary platforms. Liquidity varies dramatically by contract — the ingestion layer tracks contract liquidity alongside probability and discounts low-liquidity contracts. The distillation layer normalizes probabilities across platforms when multiple platforms offer contracts on the same outcome.

**4. Earnings and management commentary**
The qualitative dimension of corporate events. Quant 5a–5g capture the numbers — estimates, actuals, revisions, guidance ranges. This captures the *words*: what management said, how they said it, what analysts asked, and what the answers revealed or concealed. For the 4–72h horizon, management commentary during earnings calls contains forward-looking signals that take hours to days to fully propagate into analyst estimates and price.

  *Design principle — tone and emphasis over transcript completeness:* Routine ingestion extracts key qualitative signals — management tone shifts, new language or emphasis patterns, specific forward-looking statements, analyst question themes, and non-answers (what management avoided saying is often more informative than what they said). Full transcript access is adaptive-research infrastructure for deep dives.

  *Ingestion timing:* Earnings calls for universe tickers are the highest-priority qualitative event. The ingestion layer triggers collection within minutes of the call ending (or during, if real-time transcript APIs are used). For the ~65 universe names, earnings concentrate in 4–6 week windows per quarter — 2–4 calls per trading day during peak season, zero most other days. The pipeline invocation immediately following an earnings call flags it as a priority event for the qualitative research layer.

  *4a. Management tone and language analysis*
  How management talks about the business, not just what they report.
  - Tone shift detection: comparing current call language to prior quarters — is management more cautious, more bullish, more evasive? Shifts in qualifier usage ("we expect" → "we hope," "strong" → "resilient," "growth" → "stability") are leading indicators of future guidance revisions
  - Forward-looking statement extraction: specific claims about future performance, pipeline, demand environment, competitive dynamics. Raw material for thesis generation — "unprecedented demand" vs. "demand environment remains healthy" conveys meaningfully different information
  - Emphasis and time allocation: what management spent the most time discussing signals what they think matters. A CFO spending five minutes on cost efficiency who previously spent five minutes on revenue growth is telling the market something
  - Hedging and qualification language: increased use of hedging words ("approximately," "we believe," "subject to"), caveats, and risk disclaimers relative to prior calls — leading indicator of guidance cuts
  - New topic introduction: when management introduces a topic they've never discussed before (e.g., a tech company suddenly discussing "efficiency" and "discipline" after years of "investment" and "growth"), a narrative regime change is underway

  *4b. Analyst Q&A dynamics*
  Questions reveal what the institutional community is worried about. The quality and directness of answers reveal how justified those worries are.
  - Question theme clustering: if 6 of 8 analysts ask about the same topic (AI monetization, credit quality, refining margins), that topic is the market's primary concern and will drive the post-earnings narrative
  - Non-answer detection: when management deflects, redirects, or gives a notably vague answer to a direct question, flag it. Evasion on a topic the market cares about is a negative signal
  - Analyst tone: are questions hostile, probing, or softball? More aggressive questioning from sell-side analysts (who generally maintain cordial relationships) signals institutional pressure for harder answers
  - Follow-up persistence: when an analyst pushes back and asks a follow-up, the topic is important enough that institutional clients demanded clarity. The follow-up answer (or continued deflection) is high-signal

  *4c. Cross-company earnings intelligence*
  During earnings season, each company's results inform expectations for peers.
  - Sector bellwether reads: when the first major name in a sector reports, its commentary on demand environment, pricing, competition, and macro conditions sets expectations for every subsequent reporter. TSM informs NVDA. JPM informs the banking sector. XOM informs energy
  - Customer/supplier commentary chains: companies in the same supply chain reference each other. Hyperscalers (AMZN, GOOG, MSFT) discussing "optimizing AI infrastructure spending" is a direct signal for NVDA and AVGO. Major banks discussing "tightening lending standards" is a signal for the broader economy
  - Guidance consensus formation: as multiple names report, a sector-level guidance consensus emerges. If the first three semis companies guide above consensus, the market front-runs upward revisions for the rest — magnitude depends on business similarity
  - Earnings season momentum: early-season beat rates and guidance direction shape expectations for later reporters. A strong early season creates a "bar raised" dynamic where later reporters need larger beats to move stock

  *4d. Conference call context and prepared remarks*
  The prepared section contains management's curated narrative — what they want the market to focus on.
  - Strategic pivot signals: management reframing the company's story — "growth at all costs" to "profitable growth," "hardware" to "software and services," "domestic" to "international expansion." These typically take 2–3 quarters to fully reprice
  - Capital allocation commentary: buyback intentions, dividend policy, M&A appetite, capex guidance. For tech especially, a shift in AI-infrastructure capex guidance is a primary thesis input for the entire AI trade
  - Competitive dynamics commentary: management calling out previously-ignored competitive threats, or dismissing previously-acknowledged competitors, signals their assessment of market structure shifts
  - Macro commentary: how management sees the macro environment — consumer strength/weakness, enterprise spending, regulatory outlook. Aggregated across universe names, this is a real-time survey of economic conditions from the people running the businesses

  *4e. Conferences, investor days, and non-earnings corporate events*
  Management provides strategic updates between earnings through analyst days, investor days, industry conferences (CES, GTC, JPM Healthcare, Goldman Sachs, Morgan Stanley TMT), and fireside chats. These often contain more candid commentary than quarterly calls because management is speaking to a self-selected audience.
  - Industry conference presentations: for tech/semis especially, GTC (NVDA), AWS re:Invent (AMZN), Google I/O (GOOG), and Build (MSFT) are primary catalysts that can reprice names over 4–72 hours. The ingestion layer tracks the conference calendar and flags presentations from universe company executives
  - Investor and analyst day signals: companies hold investor days to update long-term targets, announce strategic shifts, or provide segment deep dives. Infrequent (typically annual) but high-signal — long-term margin or growth framework revisions can reprice the stock over days
  - Real-time social and media reaction: the crowd's real-time interpretation of conference and investor-day commentary can front-run price reaction by minutes to hours. Treat real-time coverage as a high-priority adaptive research trigger, similar to earnings calls
  - Cross-company conference intelligence: when multiple companies in the same sector present at the same conference over consecutive days, the narrative arc across presentations is informative — are all semis companies telling the same demand story, or is one outlier?

**5. Regulatory, policy, and geopolitical events**
Discrete events from government and international actors that create step-function changes in the market environment. Unlike news (category 1), which describes and interprets events, this category tracks the events themselves — the actions, decisions, and announcements from entities with the power to change the rules.

  *Design principle — monitor the calendar, investigate the surprises:* Most regulatory and policy events are scheduled (FOMC meetings, CPI releases, congressional hearings) or at least anticipated (an expected regulatory ruling, a known antitrust investigation timeline). The baseline research layer covers the calendar. When an *unscheduled* event occurs — an emergency Fed statement, an unexpected trade action, a surprise regulatory ruling — the adaptive research layer should immediately prioritize it regardless of what the quant anomaly flags are showing.

  *5a. Federal Reserve communications*
  Beyond the rate decision itself (which is covered in quant 6b as a number and in prediction markets category 3a as a probability), what the Fed *says* often matters more than what it does. Markets have largely solved the problem of pricing the next rate decision — the real uncertainty is in the path beyond the next meeting, which is communicated through language.
  - FOMC statement text analysis: word-by-word comparison to the prior statement — what changed, what was added, what was removed. The bond market reacts to statement changes within seconds; the equity market takes longer to process the implications, creating a window for the system
  - Press conference tone and language: the Chair's Q&A reveals the committee's thinking more than the prepared statement. Specific phrases get market attention ("data dependent," "in no hurry," "all options on the table") — shifts in these phrases signal policy direction changes
  - FOMC minutes: released three weeks after the meeting. Often stale by the time they publish, but occasionally reveal debate dynamics or dissent that weren't visible in the statement or presser. The system should flag minutes that contain surprising dissent counts or new policy discussion topics
  - Fed Beige Book: anecdotal economic conditions from each Fed district, published 8 times per year. Not market-moving on its own, but useful context for interpreting subsequent data releases — if the Beige Book describes "softening labor demand" and the next NFP report misses, the combination is more meaningful than either alone
  - Individual Fed speaker commentary: scheduled speeches and Q&A appearances from Fed governors and regional presidents. Some speakers are more influential than others (Chair > Vice Chairs > voting members > non-voting members). The ingestion layer should tag each appearance with the speaker's influence tier and voting status. Hawkish or dovish shifts from key speakers between meetings can move rate expectations (quant 6b) and prediction market odds (category 3a) within hours
  - Non-Fed central bank decisions (light touch): ECB and BOJ rate decisions and major policy shifts affect US equities through the rate differential and currency channel. A surprise BOJ policy shift moves USD/JPY, which moves the entire risk-on/risk-off trade. ECB rate decisions affect the transatlantic rate spread, which matters for financials. The system does not need the same depth of analysis as for the Fed — a headline-level monitor of major ECB and BOJ decisions, with adaptive research triggers on surprises, is sufficient to catch the tail events that ripple into US equity positioning

  *5b. Regulatory actions and rulings*
  Government agencies making decisions that directly affect universe names or sectors.
  - **SEC enforcement and rulemaking:** new rules, enforcement actions, and guidance changes affecting trading, disclosure, crypto (COIN), or corporate governance for universe names. SEC actions tend to be slower-burn signals — the initial announcement creates a move, the full impact plays out over weeks to months as compliance costs and business model implications become clear
  - **FTC and DOJ antitrust:** investigation milestones, lawsuit filings, settlement negotiations, and rulings. For mega-cap tech names (GOOG, META, AMZN, AAPL, MSFT), antitrust is a primary tail risk. Track investigation stages (preliminary inquiry → formal investigation → complaint filed → trial → ruling) and probability-weight from prediction markets (category 3b)
  - **CFPB and banking regulators:** for financials universe names. CFPB enforcement actions, OCC guidance, FDIC policy changes, stress test results and methodologies. Capital requirement changes are particularly impactful — they directly affect buyback capacity and dividend policy for JPM, BAC, GS, MS, C, WFC
  - **EPA and energy regulators:** for energy universe names. Emissions standards, drilling permits, pipeline approvals, renewable energy mandates. Regulatory uncertainty acts as a discount on energy company valuations — clarity in either direction (more restrictive or more permissive) reduces the uncertainty discount
  - **International regulators:** EU Digital Markets Act enforcement (affects AAPL, GOOG, META, MSFT, AMZN), China's tech regulatory actions (affects any company with China exposure), international semiconductor export controls (affects the entire semis universe)

  *5c. Executive action*
  Executive orders and administrative actions that can materially reprice sectors within hours. Executive orders on trade, technology policy (AI regulation, semiconductor policy), energy (drilling on federal land, LNG export approvals), and financial regulation are the fastest-moving policy catalysts and the most relevant for the 4–72 hour time horizon. Legislative activity (bills, committee markups, floor votes) moves too slowly and fails too often to be a routine ingestion priority — significant legislative milestones should be captured through the news sweep (category 1) and the regulatory event calendar (5e) rather than tracked as a dedicated sub-category

  *5d. Geopolitical events and international developments*
  Events outside the US that affect the system's universe through supply chains, commodity markets, risk appetite, or cross-border trade.
  - **Taiwan Strait and semiconductor supply chain:** the single highest-impact geopolitical risk for the system's universe. Any escalation in China-Taiwan tensions directly affects TSM (and by extension the entire semis universe that depends on TSMC fabrication) and triggers broad risk-off sentiment. The system should maintain a baseline awareness of cross-strait conditions and flag any change in rhetoric, military activity, or diplomatic developments
  - **Middle East and energy supply:** conflict, sanctions, or diplomatic developments affecting oil transit routes (Strait of Hormuz, Suez Canal) or production (Iran sanctions, Saudi-Iran dynamics, Iraq/Libya production disruptions). Direct impact on crude pricing (quant 8a) and the entire energy universe
  - **Russia-Ukraine and European energy:** ongoing sanctions enforcement, conflict escalation/de-escalation, and European energy security dynamics. Affects natural gas pricing (quant 8b), LNG-exposed names (LNG, ET), and broad risk appetite
  - **China economic policy:** stimulus announcements, property sector developments, trade data, and monetary policy actions from PBOC. China demand signals ripple through energy (demand expectations), semiconductors (supply chain and market access), and tech (revenue exposure). The ingestion layer should monitor major China economic data releases and policy announcements with the same attention given to US macro data
  - **OPEC+ decisions and dynamics:** production quota decisions, compliance monitoring, and intra-OPEC politics. OPEC decisions are scheduled events (meeting calendar is public), but the real signal is in the pre-meeting negotiations, compliance levels between meetings, and unilateral actions by member states. Cross-reference with prediction market odds on production outcomes (category 3c) and crude futures positioning (quant 8a, 8f)
  - **Trade agreements and disputes:** tariff announcements, trade deal progress, WTO rulings, and bilateral trade tensions. The semis and tech sectors are most exposed to US-China trade dynamics, energy to OPEC and sanctions policy, financials to cross-border capital flow regulations

  *5e. Regulatory and policy event calendar*
  The scheduling dimension — what's coming and when.
  - Known event dates: FOMC meetings, CPI/PPI/PCE releases, congressional hearings involving universe names or sectors, regulatory comment deadlines, court hearing dates for antitrust cases, OPEC+ meetings, Treasury auction schedule
  - Event clustering risk: when multiple significant events fall in the same window (e.g., CPI release + FOMC meeting + OPEC decision in the same week), the combined uncertainty compounds. The portfolio manager needs to know when event clustering creates elevated risk so it can adjust position sizing and thesis duration accordingly
  - Unscheduled event monitoring: some of the most impactful events aren't scheduled — emergency Fed actions, surprise executive orders, geopolitical escalations. The ingestion layer can't predict these, but the baseline research component should maintain awareness of conditions that increase the probability of unscheduled events (elevated geopolitical tension, unusual Fed speaker language suggesting urgency, political dynamics suggesting imminent executive action)

  *Cross-reference note:* This category should be read alongside the macro event calendar in quant 6g. Quant 6g provides the data release schedule with consensus estimates (the "what and when"). This category provides the interpretive context (the "why it matters and what the range of outcomes looks like"). The qualitative research layer needs both.

**6. Sector-specific qualitative catalysts**
Qualitative signals that are unique to specific sectors in the universe. These don't fit neatly into the cross-sector categories above because they require domain-specific knowledge to interpret and their relevance is confined to a single sector's researcher agent.

  *Design principle — sector researcher native inputs:* Categories 1–5 produce cross-sector qualitative signals that flow through the synthesizer. This category produces signals that are native to a specific sector researcher agent. The tech/semis researcher receives 6a, the energy researcher receives 6b, the financials researcher receives 6c. The sector researcher is better positioned to interpret these domain-specific signals than the synthesizer because interpretation requires sector context — the same OPEC headline reads differently to an energy specialist than to a generalist.

  *6a. Technology and semiconductor catalysts*
  The qualitative signals that shape tech and semiconductor valuations beyond what price, flow, and earnings data capture.
  - **AI narrative and spending signals:** the single most important qualitative catalyst for the current tech/semis universe. Hyperscaler capex commentary (MSFT, GOOG, AMZN, META discussing AI infrastructure spending plans), AI startup funding announcements, enterprise AI adoption surveys and anecdotes, and commentary from chip designers (NVDA, AMD) and equipment makers (LRCX, KLAC, AMAT, ASML) about demand visibility. The narrative around "is AI spending sustainable or is this a bubble" is currently the primary driver of the most valuable names in the universe. Track the narrative arc, not individual data points
  - **Supply chain intelligence:** semiconductor lead times, fab utilization rates, inventory levels across the supply chain (wafer fab → packaging → OEM → distributor → enterprise). Sources include DigiTimes, SEMI industry data, company commentary in earnings calls (category 4), and trade publication reports. Supply chain tightness signals pricing power; supply chain loosening signals potential margin pressure
  - **Product cycle dynamics:** major product launches (AAPL iPhone cycles, NVDA GPU architecture transitions, AMD competitive positioning), platform shifts (cloud → AI, on-premise → SaaS), and technology inflection points. Product cycles create predictable demand patterns — the system should track where each major name is in its product cycle (pre-launch anticipation, launch, ramp, maturity, decline)
  - **Competitive dynamics and market share:** when competitive dynamics shift — a new entrant gains traction (e.g., AMD taking share from INTC, or a new AI chip challenger), a dominant player makes a strategic pivot, or a partnership restructures the competitive landscape — the repricing can take days to weeks as the market reassesses
  - **Export controls and trade restrictions:** US semiconductor export controls to China are a first-order catalyst for NVDA, AMD, AVGO, LRCX, KLAC, AMAT, ASML. Track not just the rules themselves (category 5b) but the industry's qualitative response — workaround strategies, alternative market development, compliance costs, and revenue impact estimates from company commentary and trade press

  *6b. Energy sector catalysts*
  The qualitative overlay on commodity markets and energy company operations.
  - **OPEC rhetoric and compliance:** between formal meetings, OPEC members and the Secretary General make public statements about production targets, compliance levels, and market outlook. The tone and consistency of these statements between meetings is a leading indicator for the next decision. Track whether members are signaling willingness to cut further, dissatisfaction with quotas, or unilateral action
  - **Inventory and supply narrative:** beyond the quantitative EIA/API data (quant 8a, 8b), the *narrative* around inventories matters. Are draws being attributed to strong demand or reduced imports? Are builds being attributed to weak demand or refinery maintenance? The narrative framing in energy trade press shapes how the next data point will be interpreted
  - **Weather and seasonal patterns:** extreme weather events (hurricanes threatening Gulf production, extreme cold/heat driving demand) are qualitative catalysts with quantifiable impact. Hurricane season (June–November) creates recurring risk for Gulf Coast production and refining — the system should heighten energy catalyst monitoring during this period
  - **Pipeline and infrastructure developments:** approval/denial of pipeline projects, LNG export terminal construction progress, and refinery capacity changes. These are slow-moving but create step-function repricing when decisions are announced

  *6c. Financials sector catalysts*
  The qualitative signals specific to banking, payments, and fintech.
  - **Credit conditions narrative:** beyond the quantitative credit spread data (quant 6e), the qualitative assessment of credit conditions from bank management, credit rating agencies, and financial regulators. Are banks tightening or loosening lending standards? Is consumer credit quality holding up or deteriorating? The Senior Loan Officer Survey (quarterly from the Fed) is the single most authoritative source — it directly reveals bank lending behavior
  - **Rate environment commentary:** how financial sector participants describe the rate environment — are banks positioned for higher-for-longer, are they hedged for rapid cuts, is net interest margin expansion expected to continue or plateau? This commentary, primarily from earnings calls (category 4) and investor conferences, is the qualitative complement to the yield curve data in quant 6a
  - **M&A and deal pipeline:** financial sector M&A activity (bank consolidation, fintech acquisitions, payment network partnerships) and the broader M&A advisory pipeline as described by investment bank management. A surge in M&A commentary from GS, MS, and JPM signals improving deal activity — a direct revenue catalyst for investment banks and an indirect bullish signal for the economy
  - **Regulatory posture shifts:** the qualitative dimension of financial regulation — tone of regulatory commentary, confirmation of new regulatory appointees with known policy preferences, shifts in enforcement priorities. A change in the CFPB director or OCC Comptroller can reprice the entire banking sector's regulatory risk over days
  - **Consumer and payment trends:** for payments and fintech names (V, MA, PYPL, SQ), qualitative signals about consumer spending patterns, e-commerce growth trends, cross-border payment volumes, and digital payment adoption. These names are high-frequency economic indicators — their transaction data reflects real-time consumer behavior
  - **Crypto and digital assets narrative:** for COIN and SQ specifically, the crypto regulatory and adoption narrative. SEC enforcement actions, institutional adoption milestones, Bitcoin ETF flows, and major exchange developments create volatility on these names that is largely independent of broader financial sector dynamics

---

*Ingestion frequency summary:*

| Category | Baseline cadence | Event-triggered |
|----------|-----------------|----------------|
| 1. News and financial media | Every invocation (headline sweep) | Breaking news, M&A rumors, short reports on universe names |
| 2. Social, alternative sentiment, and options narrative | Every invocation (aggregate scores) | Sentiment spike flags, unusual options activity reports |
| 3. Prediction markets | Every invocation (all tracked contracts) | >5pp shift on any contract |
| 4. Earnings, management commentary, and corporate events | During earnings season + conference calendar | Immediate post-call for universe names, major conference presentations |
| 5. Regulatory, policy, and geopolitical | Every invocation (calendar + headline sweep) | Unscheduled government/geopolitical events, non-Fed central bank surprises |
| 6. Sector-specific qualitative catalysts | Every invocation (sector-specific source sweep) | Major catalyst events per sector |

*Cross-reference map — where qualitative meets quantitative:*

| Qualitative category | Quantitative counterpart | Relationship |
|---------------------|------------------------|-------------|
| 1a/1b (news, M&A rumors, short reports) | 5f (analyst ratings), 5g (insider transactions) | News describes reasoning and narrative; quant tracks structured outputs |
| 2c (options flow narrative) | 3a–3e (options data) | Qualitative interpretation of unusual activity; quant tracks flow and positioning |
| 3a (monetary policy markets) | 6b (Fed funds futures) | Both measure rate expectations; divergence is signal |
| 3c (OPEC outcome markets) | 8a (crude oil) | Prediction markets price the decision; quant tracks commodity impact |
| 4a–4e (earnings and corporate event commentary) | 5a–5g (fundamental data) | Commentary gives the *why*; quant gives the *what* |
| 5a (Fed + non-Fed central bank communications) | 6a, 6b (rates and policy) | Central bank words move rates; quant measures the rate moves |
| 5d (geopolitical events) | 8a–8e (commodities) | Events affect supply; quant measures price impact |
| 6a (tech/semi catalysts) | Supply chain data across 1, 5 | Qualitative narrative contextualizes quantitative signals |
| 6c (financials catalysts) | 6e (credit conditions) | Qualitative credit narrative contextualizes spread data |
