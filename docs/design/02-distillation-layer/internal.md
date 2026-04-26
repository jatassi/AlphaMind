# Portfolio state — derived metrics

Everything in this document takes raw portfolio state from [raw-state.md](../01-data-layer/internal/portfolio-state.md) and cross-references it with market data from [quantitative](../01-data-layer/external/quantitative.md) and [qualitative](../01-data-layer/external/qualitative.md) sources to produce higher-order insights. None of this is raw data — it all requires either cross-source computation or LLM judgment.

*Scope note:* Some metrics below (beta-adjusted exposure, pairwise correlation matrix) are deterministic computations that could live in the [programmatic distillation layer](external.md). They're specified here because they require portfolio-state inputs that the distillation layer doesn't currently receive. If the distillation layer's scope expands to include portfolio-aware computation, these items should migrate there. For now, they are computed as part of the pipeline's portfolio state processing and exposed as structured data to decision-layer agents via tools and context packages.

---

**7. Exposure analysis**
Derived exposure views that require cross-referencing the position inventory (raw state 1a) with live market data. The raw position inventory tells you *what the system holds*; exposure analysis tells you *what risk the system is taking*.

  *7a. Beta-adjusted exposure*
  Weights each position's market value by its beta (from quant 1f) to produce risk-adjusted exposure metrics. A $1M position in TSLA (beta ~2.0) carries different market risk than a $1M position in JPM (beta ~1.0), and raw dollar exposure doesn't capture this.
  - Beta-adjusted net exposure: sum of (position market value × beta) across longs minus shorts, as a percentage of portfolio — the true directional market risk, more meaningful than raw dollar net exposure from raw state 1b
  - Beta-adjusted gross exposure: sum of absolute (position market value × beta), as a percentage of portfolio — the true total risk footprint
  - Sector beta contribution: how much of the portfolio's total beta comes from each sector — identifies which sector is driving the portfolio's market sensitivity. Critical for the portfolio manager when macro events (FOMC, CPI) are approaching and the system needs to know where its rate/growth sensitivity is concentrated
  - Marginal beta impact: for each position, how much removing it would change the portfolio's overall beta — helps the portfolio manager prioritize which position to trim if beta needs to come down quickly

  *7b. Position correlation profile*
  How current positions relate to each other, computed by cross-referencing the position inventory with correlation data from quant category 7. A portfolio of five uncorrelated positions has different risk characteristics than five positions that all move together, even if the dollar exposure is identical.
  - Pairwise position correlation matrix: trailing correlation between all open positions, pulled from quant 7a and 7d — reveals hidden concentration where positions in different sectors are making the same bet (e.g., long NVDA and long AVGO are a correlated AI infrastructure bet despite being separate names)
  - Portfolio-level implied correlation: weighted average pairwise correlation across all positions — higher numbers mean the portfolio is more concentrated in a single theme, even if it looks diversified by sector. The portfolio manager should monitor this against the correlation limit from the risk guardrails
  - Correlation clustering: grouping positions by revealed correlation into effective "bets" — the system might hold 6 positions across tech and semis that are really 2 independent bets (an AI infrastructure cluster and a cybersecurity cluster). This clustering surfaces the true number of independent theses, not just the position count
  - Correlation change since entry: for each position, how its correlation with the rest of the portfolio has changed since entry — rising correlation means the position is becoming less diversifying, which the PM needs to know even if the position's individual thesis is still valid
  - Correlation regime flag: whether the current correlation structure across positions is typical or anomalous relative to recent history — in risk-off episodes, correlations spike across the board, and the portfolio's "diversification" evaporates. This flag alerts the PM that the portfolio's effective risk is higher than the static correlation numbers suggest

---

**8. P/L attribution**
Decomposes position and portfolio returns into components to distinguish skill from luck. Computed by cross-referencing position P/L (raw state 2a) with market returns (quant 1a), sector returns (quant 7b), and position betas (quant 1f).

*Cadence note:* Full attribution is analytically heavy and not tactically urgent — the PM making a hold/close decision mostly cares about raw P/L, distance to target, and risk/reward. Full attribution runs once per day (pre-close invocation) rather than every cycle. Intraday invocations carry forward the most recent attribution snapshot with a staleness flag.

  *8a. Position-level attribution (daily)*
  For each open and recently closed position, decomposition of P/L into:
  - Market component: SPY return × position beta — what the position would have earned from pure market exposure
  - Sector component: sector ETF return × sector beta, minus the market component — what came from sector-specific dynamics
  - Alpha (residual): total P/L minus market and sector components — the return attributable to the specific thesis, not to market or sector tailwinds. This is the only component the system can take credit for
  - Attribution ratio: alpha / total P/L — what fraction of the return came from thesis-driven selection vs. being carried by the market. A position where alpha is 80% of the return is validating the system's edge; one where alpha is 10% is just a beta proxy

  *8b. Portfolio-level attribution (daily)*
  Aggregate decomposition across all positions:
  - Total alpha generated: sum of position-level alpha — the portfolio's total value-add beyond passive exposure
  - Alpha vs. beta trend: trailing ratio of alpha-driven returns to beta-driven returns over 5-day and 20-day windows — is the system increasingly just riding the market, or is its stock selection getting sharper?
  - Attribution by sector: which sectors are generating alpha and which are just providing beta — should feed back into the analyst's sector allocation for thesis generation

---

**9. Thesis dependency mapping**
Identifies hidden concentration risk at the thesis level that position correlation (category 7b above) may not capture. Two positions can have moderate price correlation but share a single catalyst — if that catalyst fails, both theses fail simultaneously.

*Why this isn't captured by correlation data:* Price correlation measures how two assets have moved historically. Thesis dependency measures whether two positions *will* move together conditional on a specific future event. Long NVDA on "AI capex beats consensus" and long AVGO on "custom ASIC demand from hyperscalers" have different correlation profiles but both fail if the AI infrastructure spending narrative reverses. Position correlation might be 0.6; thesis dependency on the AI capex catalyst is 1.0.

  *9a. Shared catalyst identification*
  For each active thesis (from raw state 3a), extract the key assumptions and named catalysts, then identify overlaps:
  - Catalyst overlap matrix: which theses share named catalysts (e.g., "MSFT earnings AI capex commentary" as a catalyst for both an NVDA thesis and a MSFT thesis)
  - Narrative dependency clusters: groups of theses that depend on the same underlying narrative even if the specific catalysts differ — "AI capex exceeds consensus" is a narrative that multiple catalyst-specific theses might depend on
  - Directional dependency: whether overlapping theses are aligned (all benefit from the same catalyst outcome) or hedged (some benefit from catalyst success, others from catalyst failure). Aligned dependencies are concentration risk; hedged dependencies are portfolio construction

  *9b. Dependency-adjusted concentration*
  A revised concentration metric that accounts for thesis dependency, not just position correlation:
  - Effective independent thesis count: the number of truly independent bets the portfolio is making, after clustering by thesis dependency — might be 3 independent theses even if the system holds 7 positions
  - Maximum catalyst exposure: the total dollar exposure conditional on a single catalyst failing — "if AI capex disappoints, positions totaling $X and Y% of portfolio are at risk." The portfolio manager's most actionable concentration metric
  - Dependency risk flag: whether the portfolio's thesis-level concentration exceeds a threshold (to be defined in risk guardrails) — a binary alert for the PM that thesis diversification is insufficient

---

**10. Capital efficiency analysis**
Retrospective metrics measuring how effectively the system deploys capital. These are slow-moving strategic signals, not tactical inputs for the current trade decision. They feed into system tuning and help calibrate the portfolio manager's capital allocation instincts over time.

*Cadence:* Daily (pre-close invocation). These metrics change negligibly within a trading day and computing them more frequently is wasted effort.

*Data dependencies:* Utilization metrics (10a) consume the portfolio history reconstruction query pattern in [state-persistence.md](../../05-execution-layer/state-persistence.md) — specifically, portfolio value time series reconstructed from invocation snapshots and cash-affecting activity log entries. Execution efficiency (10b) consumes the execution history queries in state-persistence — fill records filtered by date range with thesis generation timestamps for time-to-deploy calculations.

  *10a. Utilization metrics*
  - Average capital utilization: trailing average of (total position value / total portfolio value) over 5-day and 20-day windows — how much of the portfolio is typically invested vs. sitting in cash. Consistently low utilization paired with strong per-trade returns suggests the analyst is generating too few theses; consistently high utilization with poor returns suggests the portfolio manager is approving too many
  - Capital turnover: total dollar value of trades executed divided by average portfolio value over a period — higher turnover is expected given the 4–72 hour thesis horizon
  - Cash drag estimate: the opportunity cost of uninvested cash — approximate return foregone based on the system's realized return on invested capital applied to the average cash balance

  *10b. Execution efficiency*
  - Time-to-deploy: average hours from thesis generation to full position execution — measures how quickly the system acts on its own ideas
  - Position sizing efficiency: comparison of intended position sizes (from thesis generation) to actual filled sizes
  - Thesis-to-execution conversion rate: percentage of PM-approved theses that result in a fully executed position

---

**11. System health diagnostics**
Metrics for monitoring the system's own process quality, separate from P/L. A system can be profitable with bad process (lucky) or unprofitable with good process (unlucky) over short periods. Only process quality is durable.

*Cadence:* Rolling performance metrics (11a) run daily. Execution quality monitoring (11b) runs weekly as a calibration diagnostic. Neither is a per-invocation input.

*Data dependencies:* Rolling performance metrics (11a) consume the portfolio history reconstruction query pattern in [state-persistence.md](../../05-execution-layer/state-persistence.md) — rolling P/L windows and drawdown history reconstructed from activity log entries. Execution quality monitoring (11b) consumes the execution history queries in state-persistence — fill records with slippage statistics aggregated by position size bucket, time of day, and liquidity conditions, plus volume participation rates from fill quantity vs. market volume.

  *11a. Rolling performance metrics (daily)*
  Standard risk-adjusted statistics over trailing windows. The portfolio manager compares current-period metrics against trailing baselines to detect drift.
  - Sharpe ratio estimate: trailing annualized risk-adjusted return at 5-day, 20-day, and inception-to-date windows. A declining Sharpe on the shorter window relative to the longer window signals recent deterioration
  - Sortino ratio: same as Sharpe but using only downside volatility — more appropriate for a system targeting asymmetric setups
  - Expectancy per trade: (win rate × average win) − (loss rate × average loss) — the expected P/L per trade, which should be positive and stable. Declining expectancy is an earlier signal than declining Sharpe because it doesn't require enough data for annualization
  - Profit factor: gross profits / gross losses — above 1.0 means profitable; the trend matters more than the level

  *11b. Execution quality monitoring (weekly)*
  Monitors execution drag and, in paper mode, the [paper-evaluation harness](../05-execution-layer/paper-evaluation-harness.md)'s estimation fidelity. This is a system calibration concern, not a trading decision input — the portfolio manager doesn't use slippage stats to evaluate a thesis. The output feeds harness calibration and surfaces any live-execution drift the go-live gate should know about.
  - Slippage statistics: average slippage per trade (submit-mid vs. actual fill) in live mode; in paper mode, raw-Alpaca-fill vs. harness-adjusted-estimate, broken down by position size and liquidity conditions — are the estimates matching live observations?
  - Fill rate: percentage of intended position size actually filled. Tracked from Alpaca's reported fills; thin-name underperformance surfaces here
  - Execution latency: measured delay from command submission to first fill event on the `trade_updates` stream

---

*Consumer map — who receives analysis outputs:*

| Consumer | Analysis categories received | Purpose |
|----------|------------------------------|---------|
| Synthesizer | 1b (exposure snapshot, via tool) | On-demand portfolio context during cross-domain synthesis — queried when market signals are relevant to existing positions |
| Strategist | All (7–11) | Thesis status classification, cross-position dynamics, portfolio-level observations. Primary consumer of derived metrics |
| Portfolio manager | 7 (exposure), 8 (attribution, daily), 9 (thesis dependency) | Risk management, concentration assessment, capital allocation decisions |
| Analyst | 9a (shared catalysts, summary only) | Avoids generating theses that create thesis-level concentration with existing positions |

*Cross-reference map — analysis inputs from other layers:*

| Analysis category | Raw state input | Market data input | Relationship |
|-------------------|----------------|-------------------|-------------|
| 7a (beta-adjusted exposure) | 1a (holdings), 1b (sector exposure) | Quant 1f (beta) | Position values × betas = risk-adjusted exposure |
| 7b (correlation profile) | 1a (holdings) | Quant 7a, 7d, 7e, 7g (correlation data) | Position pairs × trailing correlations = portfolio correlation structure |
| 8a–8b (P/L attribution) | 2a (position P/L) | Quant 1a (SPY return), 7b (sector ETF returns), 1f (beta) | P/L decomposed into market + sector + alpha |
| 9a–9b (thesis dependency) | 3a (active thesis details) | Qualitative 1–6 (catalyst data) | Thesis narratives checked for shared catalysts and narrative overlap |
| 10a–10b (capital efficiency) | 1a (holdings), 4a (cash balance), 4b (pending orders) | None | Historical deployment patterns from position, cash, and order time series |
| 11a (rolling metrics) | 2b (portfolio P/L), 2c (drawdown) | None | Standard risk-adjusted statistics over trailing windows |
| 11b (execution quality) | 1a (entry execution summary), OMS activity log, `live_execution_estimate` metadata (paper mode) | Quant 1b (volume data) | Per-position slippage and fill quality vs. actual market volume and price |
