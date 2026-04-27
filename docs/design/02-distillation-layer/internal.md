# Portfolio state — derived metrics

Takes raw portfolio state from [raw-state.md](../01-data-layer/internal/portfolio-state.md) and cross-references it with market data from [quantitative](../01-data-layer/external/quantitative.md) and [qualitative](../01-data-layer/external/qualitative.md) sources. Every metric requires cross-source computation or LLM judgment.

*Scope note:* Some metrics (beta-adjusted exposure, pairwise correlation matrix) are deterministic and would naturally live in [programmatic distillation](external.md), but they need portfolio-state inputs the distillation layer doesn't currently receive. Today, they're computed in the pipeline's portfolio-state processing and exposed to decision-layer agents via tools and context packages; they migrate to distillation if scope expands.

---

**7. Exposure analysis**
Derived exposure views requiring cross-reference between position inventory (raw state 1a) and live market data. Raw inventory shows *what the system holds*; exposure shows *what risk it's taking*.

  *7a. Beta-adjusted exposure*
  Position market values weighted by beta (from quant 1f). A $1M TSLA position (beta ~2.0) carries different market risk than $1M in JPM (beta ~1.0).
  - Beta-adjusted net exposure: Σ(market value × beta) longs minus shorts, as % of portfolio — true directional market risk, more meaningful than raw dollar net (raw state 1b)
  - Beta-adjusted gross exposure: Σ|market value × beta|, as % of portfolio — true total risk footprint
  - Sector beta contribution: per-sector share of portfolio beta. Critical when macro events (FOMC, CPI) approach and the PM needs to know where rate/growth sensitivity is concentrated
  - Marginal beta impact: per-position change in portfolio beta on removal — prioritizes which position to trim when beta must come down quickly

  *7b. Position correlation profile*
  Cross-references the position inventory with correlation data from quant category 7. Five uncorrelated positions carry different risk than five that move together at identical dollar exposure.
  - Pairwise position correlation matrix: trailing correlation between all open positions, from quant 7a and 7d — reveals hidden concentration where positions in different sectors make the same bet (e.g., long NVDA and long AVGO are a correlated AI-infrastructure bet)
  - Portfolio-level implied correlation: weighted average pairwise correlation — higher means more single-theme concentration even if sector-diversified. Monitored against the correlation limit in risk guardrails
  - Correlation clustering: groups positions into effective "bets" — 6 tech/semis positions might be 2 independent bets (AI-infrastructure cluster, cybersecurity cluster). Surfaces true independent-thesis count, not position count
  - Correlation change since entry: how each position's correlation with the rest of the portfolio has shifted — rising correlation means the position is becoming less diversifying, even if the individual thesis is still valid
  - Correlation regime flag: whether current correlation structure is typical or anomalous vs. recent history — in risk-off episodes correlations spike and "diversification" evaporates. Alerts the PM that effective risk exceeds static correlation numbers

---

**8. P/L attribution**
Decomposes returns into components to distinguish skill from luck. Cross-references position P/L (raw state 2a) with market returns (quant 1a), sector returns (quant 7b), and position betas (quant 1f).

*Cadence:* Analytically heavy and not tactically urgent — a PM making a hold/close decision cares about raw P/L, distance to target, and risk/reward. Runs once per day (pre-close invocation). Intraday invocations carry forward the most recent snapshot with a staleness flag.

  *8a. Position-level attribution (daily)*
  Per open and recently closed position, P/L decomposed into:
  - Market component: SPY return × position beta — pure market exposure return
  - Sector component: sector ETF return × sector beta, minus market component — sector-specific dynamics
  - Alpha (residual): total P/L minus market and sector — return attributable to the thesis, not to tailwinds. The only component the system can claim
  - Attribution ratio: alpha / total P/L — fraction from thesis-driven selection vs. market carry. 80% alpha validates edge; 10% alpha is a beta proxy

  *8b. Portfolio-level attribution (daily)*
  Aggregate across positions:
  - Total alpha generated: sum of position-level alpha — total value-add beyond passive exposure
  - Alpha vs. beta trend: trailing alpha/beta return ratio over 5-day and 20-day windows — is selection sharpening, or is the system just riding the market?
  - Attribution by sector: which sectors generate alpha and which provide beta — feeds back into analyst sector allocation

---

**9. Thesis dependency mapping**
Hidden concentration risk at the thesis level that position correlation (7b) may not capture. Two positions can have moderate price correlation but share a single catalyst — if it fails, both theses fail simultaneously.

Price correlation measures how assets have moved historically. Thesis dependency measures whether positions *will* move together conditional on a specific future event. Long NVDA on "AI capex beats consensus" and long AVGO on "custom ASIC demand from hyperscalers" have different correlation profiles but both fail if the AI-infrastructure spending narrative reverses — position correlation might be 0.6; thesis dependency on the AI-capex catalyst is 1.0.

  *9a. Shared catalyst identification*
  For each active thesis (raw state 3a), extract assumptions and named catalysts, find overlaps:
  - Catalyst overlap matrix: theses sharing named catalysts (e.g., "MSFT earnings AI capex commentary" feeding both an NVDA thesis and a MSFT thesis)
  - Narrative dependency clusters: groups depending on the same underlying narrative even if specific catalysts differ — "AI capex exceeds consensus" supports many catalyst-specific theses
  - Directional dependency: aligned (all benefit from the same catalyst outcome) vs. hedged (some benefit from catalyst success, others from failure). Aligned is concentration risk; hedged is portfolio construction

  *9b. Dependency-adjusted concentration*
  Concentration metric that accounts for thesis dependency, not just position correlation:
  - Effective independent thesis count: truly independent bets after clustering — might be 3 independent theses across 7 positions
  - Maximum catalyst exposure: total dollar exposure conditional on a single catalyst failing — "if AI capex disappoints, positions totaling $X and Y% of portfolio are at risk." The PM's most actionable concentration metric
  - Dependency risk flag: binary alert when thesis-level concentration exceeds the threshold (defined in risk guardrails)

---

**10. Capital efficiency analysis**
Retrospective metrics on how effectively the system deploys capital. Slow-moving strategic signals — feed into system tuning and PM allocation calibration, not tactical inputs for the current trade.

*Cadence:* Daily (pre-close invocation). Negligible intraday change.

*Data dependencies:* Utilization (10a) consumes the portfolio-history reconstruction query pattern in [state-persistence.md](../../05-execution-layer/state-persistence.md) — portfolio value time series reconstructed from invocation snapshots and cash-affecting activity log entries. Execution efficiency (10b) consumes execution-history queries — fill records filtered by date range with thesis generation timestamps for time-to-deploy calculations.

  *10a. Utilization metrics*
  - Average capital utilization: trailing (total position value / total portfolio value) over 5-day and 20-day windows. Low utilization with strong per-trade returns suggests the analyst generates too few theses; high utilization with poor returns suggests the PM approves too many
  - Capital turnover: total trade dollar volume / average portfolio value — higher turnover expected at the 4–72 hour thesis horizon
  - Cash drag estimate: opportunity cost of uninvested cash — realized return on invested capital applied to average cash balance

  *10b. Execution efficiency*
  - Time-to-deploy: average hours from thesis generation to full position execution
  - Position sizing efficiency: intended size (at thesis generation) vs. actual filled size
  - Thesis-to-execution conversion rate: % of PM-approved theses that result in a fully executed position

---

**11. System health diagnostics**
Process-quality monitoring, separate from P/L. Profitable-with-bad-process (lucky) and unprofitable-with-good-process (unlucky) both happen short-term; only process is durable.

*Cadence:* 11a daily; 11b weekly as a calibration diagnostic. Neither is a per-invocation input.

*Data dependencies:* 11a consumes the portfolio-history reconstruction query pattern in [state-persistence.md](../../05-execution-layer/state-persistence.md) — rolling P/L windows and drawdown history from activity log entries. 11b consumes execution-history queries — fill records with slippage stats by position-size bucket, time-of-day, and liquidity conditions, plus volume participation from fill quantity vs. market volume.

  *11a. Rolling performance metrics (daily)*
  Risk-adjusted statistics over trailing windows. The PM compares current-period vs. trailing baselines to detect drift.
  - Sharpe ratio: trailing annualized risk-adjusted return at 5-day, 20-day, and inception-to-date. Shorter window declining vs. longer signals recent deterioration
  - Sortino ratio: downside-only Sharpe — appropriate for a system targeting asymmetric setups
  - Expectancy per trade: (win rate × avg win) − (loss rate × avg loss). Earlier signal than Sharpe — no annualization data needed
  - Profit factor: gross profits / gross losses. Above 1.0 means profitable; trend matters more than level

  *11b. Execution quality monitoring (weekly)*
  Execution drag and, in paper mode, [paper-evaluation harness](../05-execution-layer/paper-evaluation-harness.md) estimation fidelity. Calibration concern, not trading input. Feeds harness calibration and surfaces live-execution drift the go-live gate should know.
  - Slippage statistics: live — submit-mid vs. actual fill per trade. Paper — raw Alpaca fill vs. harness-adjusted estimate, broken down by position size and liquidity. Are estimates matching live observations?
  - Fill rate: % of intended size actually filled. From Alpaca's reported fills; thin-name underperformance surfaces here
  - Execution latency: delay from command submission to first fill event on the `trade_updates` stream

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
