# Portfolio state — raw state

Direct reads from the OMS database (populated from Alpaca's account / positions / orders / activities endpoints and the `trade_updates` websocket — see [broker-adapter.md](../../05-execution-layer/broker-adapter.md)) or direct aggregations of stored data. No market data cross-referencing or LLM judgment required. For derived metrics that require market data inputs, see [derived-metrics.md](derived-metrics.md).

---

**1. Position inventory**
The complete picture of what the system currently owns or is short. Every open position, with enough detail that any agent receiving this data can immediately assess directional exposure, concentration, and portfolio composition without needing to query additional sources. This is the foundation — categories 2–6 below all reference back to the position inventory.

  *Design principle — positions as structured theses, not just holdings:* A position in AlphaMind is never just "long 500 shares of NVDA." It's a structured object that binds a directional bet to the thesis that justifies it, the parameters that define success and failure, and the execution history that got the system there. This tight coupling between position and thesis is what makes thesis-based position management (see [design-decisions.md](../../design-decisions.md)) possible — the system can't evaluate whether a thesis is still valid if the thesis isn't attached to the position it created.

  *1a. Current holdings*
  The raw inventory — what the system holds right now. Each position is delivered as the position record defined in [position-model.md](../../05-execution-layer/position-model.md), which specifies the base interface (direction, entry timestamp, market value, unrealized P/L, position weight, execution history, thesis and bracket bindings) and instrument-specific extensions for equity, options, and strategy positions. The position model is the single authoritative definition of the position type hierarchy — this section specifies only the consumer-facing computations that the ingestion layer applies on top of the stored record at each pipeline invocation:

  - Current market value: quantity × current price, updated at pipeline invocation using the latest price from quant 1a. The position model defines this field; the ingestion layer computes the value using the most recent market data.
  - Position weight: market value as a percentage of total portfolio value (positions + cash) — the concentration measure
  - Position age: current time minus entry timestamp (from the position record), expressed in hours — on the 4–72 hour time horizon, a position that's 6 hours old is treated very differently from one that's 48 hours old
  - Entry execution summary: VWAP achieved vs. market VWAP at time of entry, slippage estimate (raw vs. live-adjusted in paper mode via the [paper-evaluation harness](../../05-execution-layer/paper-evaluation-harness.md)), number of fills — computed from the position's execution history. The quality-of-execution record that feeds back into harness calibration and analysis-layer execution monitoring (see [derived metrics 11b](../../02-distillation-layer/internal.md))

  For exposure calculations (delta-adjusted vs. notional), see the exposure calculation section in [position-model.md](../../05-execution-layer/position-model.md). The ingestion layer delivers both notional and delta-adjusted exposure for every position.

  *1b. Sector and directional exposure*
  Aggregated views of the position inventory that reveal portfolio-level characteristics not visible from individual positions. These are direct rollups — no market data cross-referencing required. Beta-adjusted exposure metrics that require quant 1f inputs live in [derived metrics 7a](derived-metrics.md).
  - Sector allocation: total long and short exposure per sector (tech, semis, financials, energy), expressed as both dollar value and percentage of portfolio — the primary concentration measure. Uses delta-adjusted exposure for options and strategy positions per [position-model.md](../../05-execution-layer/position-model.md)
  - Net directional exposure: total long value minus total short value, expressed as a percentage of portfolio — tells the strategist and PM whether the system is net long, net short, or roughly neutral
  - Gross exposure: total long value plus total short value, as a percentage of portfolio — a leverage proxy. Gross exposure at 150% means the system has more directional bets on than it has capital, even if net exposure is low
  - Long/short ratio per sector: within each sector, the balance between long and short exposure — a sector can be dollar-neutral but still have concentration risk if all the longs are correlated names

---

**2. P/L and performance tracking**
How the portfolio and each position are performing. AlphaMind's primary success metric is ending each trading day with more liquid cash than the opening balance (see [README.md](../README.md)), which means P/L tracking isn't just a reporting function — it's a core input to every pipeline decision. A system that's up 2% on the day has different risk appetite than one that's down 1%, even if the market data and thesis landscape are identical.

  *Design principle — P/L as context, not directive:* P/L data should inform the portfolio manager's risk decisions but should never directly trigger trades. The portfolio manager shouldn't close a winning position just because it's up enough, or hold a losing position hoping it recovers. The thesis drives the hold/close decision; P/L provides the risk context for position sizing, new entry appetite, and drawdown management. A common failure mode in human trading is letting P/L override thesis — "I'm up 3% today so I'll get aggressive" or "I'm down so I'll cut everything" — and the system should be architected to avoid this by keeping P/L and thesis data as separate inputs that the portfolio manager weighs independently.

  *Note on P/L attribution:* The decomposition of P/L into market, sector, and alpha components requires cross-referencing with market returns and beta data, making it analysis-layer work. It runs daily in the pre-close invocation. See [derived metrics 8a–8b](../../02-distillation-layer/internal.md).

  *2a. Position-level P/L*
  How each individual position is performing against its entry.
  - Unrealized P/L: current market value minus cost basis, both in absolute dollars and as a percentage of position cost — the running score for each open position
  - Unrealized P/L trajectory: P/L over time since entry, sampled at each pipeline invocation — is the position moving toward its target, stalling, or moving against the thesis? A position that was up 1.5% this morning and is now flat is a different risk signal than one that's been flat since entry
  - Distance to target: current price relative to the thesis target price (from category 3a) — how much of the expected move has been captured and how much remains
  - Distance to stop/invalidation: current price relative to the thesis invalidation level (from category 3a) — how much room the position has before the thesis is declared wrong
  - Risk/reward ratio at current price: remaining upside to target divided by remaining downside to invalidation — a ratio that was 3:1 at entry might be 1:1 now if the position has moved halfway to target, changing the risk calculus

  *2b. Portfolio-level P/L*
  The aggregate performance view — the portfolio manager's primary scoreboard.
  - Total unrealized P/L: sum of all position-level unrealized P/L, both absolute and as a percentage of portfolio value
  - Daily realized P/L: profits and losses locked in from positions closed during the current trading day — contributes directly to the primary success metric
  - Daily total P/L (realized + unrealized): the running daily score — above zero means the system is winning today
  - Cumulative P/L: total realized P/L since the system started paper trading — the lifetime track record
  - Rolling P/L windows: trailing 1-day, 3-day, 5-day, and 20-day realized P/L — reveals whether the system is in a winning or losing streak and the magnitude. The portfolio manager should use this to calibrate risk appetite: a 5-day losing streak might justify reducing position sizes regardless of individual thesis quality
  - Win/loss statistics: win rate (percentage of closed positions with positive P/L), average win size, average loss size, and profit factor (total gains / total losses) — overall system health metrics that the PM should reference when deciding how aggressively to size new positions

  *2c. Drawdown tracking*
  How far the portfolio has fallen from recent highs. Drawdown management is one of the portfolio manager's most critical responsibilities. Drawdown limits are one of the hard-coded programmatic constraints referenced in the portfolio manager spec (see [portfolio-manager.md](../../04-decision-layer/portfolio-manager.md)).
  - Current drawdown: percentage decline from the most recent portfolio equity high-water mark — the number the portfolio manager monitors against the max drawdown limit
  - Drawdown duration: how long the system has been in drawdown (days/hours since the last equity high) — extended drawdowns should create mechanical risk reduction
  - Maximum drawdown (lifetime): the deepest peak-to-trough decline the system has experienced — a calibration input for the risk guardrails
  - Intraday drawdown: the worst point of the current day relative to the day's opening equity — even if the system recovers by close, a deep intraday drawdown signals that the portfolio was exposed to more risk than intended
  - Drawdown by source: decomposition into which positions or sectors contributed most to the current drawdown — helps the PM identify whether drawdown is broad-based (macro-driven, reduce everything) or concentrated (idiosyncratic, address the specific position)

---

**3. Thesis registry**
The intellectual core of portfolio state. Every open position exists because a thesis justified it. The thesis registry maintains the structured record of every active thesis and its current status. This is what makes AlphaMind fundamentally different from a quantitative trading system that tracks positions by entry/exit rules — the system explicitly encodes *why* it holds each position and continuously evaluates whether that reasoning is still valid.

  *Design principle — thesis as living document:* A thesis isn't a static statement written at entry time. It has a lifecycle: it's generated by the analyst, approved (possibly modified) by the portfolio manager, attached to a position at execution, monitored at every subsequent pipeline invocation, and eventually resolved (validated, invalidated, or expired). The thesis registry tracks this full lifecycle, and the resolution data feeds back into system tuning. Every closed thesis is a training example — did the system's reasoning process work or not, and why?

  *Relationship to thesis tracking in the execution engine:* The execution engine (05-execution-layer) maintains the definitive thesis database. The portfolio state ingestion layer reads a snapshot of active theses at pipeline invocation time and delivers them to the analysis pipeline alongside the position inventory. The key distinction: the execution engine stores the complete historical record (including all resolved theses); the ingestion layer delivers only the currently active theses plus a summary of recent resolutions.

  *Note on thesis dependency:* Whether multiple active theses share underlying catalysts or narratives is an analysis-layer concern — it requires LLM judgment to assess narrative overlap. See [derived metrics 9a–9b](../../02-distillation-layer/internal.md) for thesis dependency mapping.

  *3a. Active thesis details*
  Each active thesis is delivered as the structured thesis record defined in [thesis-model.md](../../05-execution-layer/thesis-model.md) — summary, typed components (entry rationale, target rationale, invalidation rationale), key assumptions, and bracket leg linkages. The thesis model is the authoritative definition of thesis structure, component types, mandatory coverage rules, and the three consumption modes (programmatic, targeted LLM evaluation, full thesis evaluation). This section specifies only the ingestion-layer delivery contract:

  - Theses are delivered at the summary level by default. Component-level detail is available to specific consumers via the mechanisms below
  - The [strategist](../../04-decision-layer/strategist.md) receives full component-level records inline in its input bundle, because thesis-status classification is per-component (the strategist's `status_rationale` cites which component weakened or held)
  - The [qualitative research agent](../../03-analysis-layer/qualitative-research.md) receives thesis summaries inline every invocation — needed alongside the event calendar for catalyst-proximity reasoning, where a tool pull would be redundant
  - The [portfolio manager](../../04-decision-layer/portfolio-manager.md) receives thesis summaries inline, with on-demand component retrieval via the [thesis-component retrieval tool](../../04-decision-layer/portfolio-manager.md#retrieval-tools) when verifying a strategist citation against the underlying thesis record
  - The [synthesizer](../../03-analysis-layer/synthesizer.md) receives no thesis data by default; it pulls summaries on-demand via the `get_active_theses_summary` tool when cross-referencing market signals against active positions
  - The ingestion layer delivers all theses with status = active, plus their child components when the consumer requires them
  - Each thesis carries its generation timestamp (for age computation) and time expectation (for staleness detection)
  - The gap between intended entry parameters and actual fill price (from the position's execution history) is surfaced alongside the thesis to inform risk/reward assessment

  *3b. Thesis status and health*
  The current evaluation of each active thesis — is it on track, at risk, or stale? Updated at each pipeline invocation by the [strategist](../../04-decision-layer/strategist.md) as part of its per-position assessment. The five status classifications (on-track, partially-realized, at-risk, stale, invalidated) are defined in [thesis-model.md](../../05-execution-layer/thesis-model.md). In addition to the status classification, the ingestion layer delivers:

  - Thesis age vs. expected duration: hours elapsed since thesis generation compared to the expected resolution timeframe — a 48-hour-old thesis that was expected to resolve in 24 hours is overdue
  - Supporting signal status: for each signal cited in the thesis narrative, whether that signal is still present, strengthened, weakened, or reversed — assessed by the [strategist](../../04-decision-layer/strategist.md) during its thesis re-evaluation pass

  *3c. Recent thesis resolutions*
  A rolling summary of recently closed theses — the feedback loop that tells the system whether its reasoning process is working. The full resolution history lives in the execution engine; the ingestion layer delivers a sliding window (e.g., last 10 resolved theses, or last 5 trading days) to provide recency context. Resolution uses the thesis-level resolution categories and component-level resolution outcomes defined in [thesis-model.md](../../05-execution-layer/thesis-model.md). For each recently resolved thesis, the ingestion layer delivers:

  - Resolution category (validated, profitable-but-wrong, invalidated-stopped-correctly, invalidated-wrong-on-exit) and component-level outcomes (validated, wrong, inconclusive per component)
  - Resolution P/L: the realized profit or loss on each closed thesis
  - Resolution timing: how long the thesis was active before resolution — did it resolve faster or slower than expected?
  - Signal post-mortem: for invalidated theses, which signal or assumption was wrong — was the qualitative read incorrect, did a quantitative signal give a false positive, or did an unexpected external event overwhelm the thesis?
  - Running thesis accuracy: trailing percentage of theses that were validated (correct for the right reasons) vs. total resolutions — the core process quality metric

---

**4. Capital and capacity**
How much dry powder the system has and how much more risk it can take on. The portfolio manager's gating input for any new position — regardless of how compelling a thesis is, the system can't enter it if capital constraints or risk limits have been reached.

  *Design principle — conservative cash management:* The primary success metric (end each day with more liquid cash than the opening balance) creates a natural bias toward capital preservation. The system should always maintain a meaningful cash buffer — not because cash earns returns, but because the ability to act on the next high-conviction thesis is itself valuable. A system that's fully invested when the best opportunity of the week appears has failed at capital allocation even if its existing positions are good.

  *Note on capital efficiency:* Retrospective metrics measuring how effectively capital has been deployed over time (utilization rates, turnover, cash drag) are analysis-layer work. See [derived metrics 10a–10b](../../02-distillation-layer/internal.md).

  *4a. Cash and buying power*
  The liquidity picture — what the system can deploy right now. The underlying data is read from the cash ledger entity in [state-persistence.md](../../05-execution-layer/state-persistence.md), which tracks: current cash balance, settled cash balance, reserved capital, available buying power, margin held, and unsettled proceeds (with per-transaction settlement dates). The ingestion layer delivers these fields plus the following consumer-specific computations:
  - Cash as percentage of portfolio: current cash balance / (cash + position market value) — the liquidity ratio. The portfolio manager should maintain this above a minimum floor (a future risk guardrail to be specified) to preserve optionality
  - True deployable capital: settled cash minus reserved capital (for pending orders) minus margin held — the amount actually available for new positions, accounting for settlement cycles per [venue configuration](../../05-execution-layer/venue-configuration.md)

  *4b. Pending orders*
  Outstanding orders that have been approved but not yet filled. A limit-order entry from the prior invocation that hasn't triggered is a first-class portfolio state object — it represents committed capital, an active thesis, and a decision that may need to be revised. The underlying order records are read from the Orders entity in [state-persistence.md](../../05-execution-layer/state-persistence.md), filtered to status in (pending, partially-filled). The ingestion layer delivers each pending order's fields as defined in the state-persistence Orders entity, plus the following consumer-specific computations:
  - Order age: hours since the order was placed — a limit buy at $840 from 12 hours ago might be stale if conditions have shifted. The [strategist](../../04-decision-layer/strategist.md) should flag orders older than their associated thesis's expected entry window
  - Fill probability assessment context: the current price relative to each order's limit price — an order to buy NVDA at $840 when the current price is $842 is close to filling; one at $840 when the current price is $870 is unlikely without a significant move. The strategist uses this to advise the PM on whether to maintain, modify, or cancel pending orders

  *4c. Risk budget consumption*
  How close the system is to its hard-coded risk limits. The risk guardrails (max position size, max sector concentration, max daily drawdown, max gross exposure, etc.) are programmatic constraints enforced by the guardrail layer and referenced by the portfolio manager. This sub-category reports where the system currently stands against each limit.
  - Position size headroom: maximum allowed position size minus the current largest position
  - Sector concentration headroom: per-sector maximum allocation minus current sector allocation — which sectors have room for new positions and which are at capacity
  - Daily drawdown budget remaining: max daily drawdown limit minus current daily drawdown — when this reaches zero, the system should not open new positions and should consider reducing existing ones. This is the hardest constraint
  - Gross exposure headroom: max gross exposure minus current gross exposure
  - Correlation limit status: whether adding a new position in any given name would push the portfolio's implied correlation above the maximum allowed level
  - Constraint breach proximity: a composite flag indicating which limits are closest to being hit, expressed as a percentage of the limit consumed

  *4d. Active risk parameter set*
  The current values of all risk guardrails. If risk parameters are regime-dependent (tighter in high-volatility environments, slightly looser in compression), the ingested state must report which parameter set is active so the portfolio manager knows what limits it's operating under.
  - Max position size, max sector concentration, max daily drawdown, max gross exposure, max portfolio correlation (all current regime values)
  - Regime label: which volatility regime (from quant 11f) triggered the current parameter set — "low-vol compression," "normal," "elevated volatility," "crisis." The portfolio manager needs this label to understand *why* limits are where they are and to anticipate whether limits are about to shift
  - Parameter change flag: whether any risk parameter changed since the last invocation — if the volatility regime shifted overnight and limits tightened, the portfolio manager needs to know immediately that yesterday's sizing assumptions may no longer be valid

---

**5. Activity log**
The record of what happened since the last pipeline invocation. Categories 1–4 describe the current *state*; this category describes the recent *trajectory* that produced that state. The system runs every 2 hours during market hours, and between invocations things happen — orders fill, stops trigger, positions open or close. Without an explicit changelog, downstream agents would need to diff two snapshots to figure out what's new, which is both wasteful and error-prone.

  *Design principle — changelog over snapshot diffing:* Each pipeline invocation operates in a fresh context window. It has no memory of the prior invocation's state. Making the changelog an explicit ingestion output is cleaner than forcing agents to infer changes.

  The authoritative enumeration of event types and their structured payloads is the activity log event type catalog in [state-persistence.md](../../05-execution-layer/state-persistence.md), which defines 25+ typed events organized into six groups: position lifecycle, order lifecycle, bracket, thesis, cash/margin, risk/guardrail, and PM decision events. Each event carries a structured detail payload with full semantic context — not just what changed but why and how. This section specifies how those events are filtered and delivered to the analysis pipeline.

  *5a. Intra-invocation changelog*
  All activity log entries with the current invocation ID — events that occurred between the previous pipeline invocation and this one, ordered chronologically. The most important event types for the analysis pipeline's attention are: `position_opened` and `position_closed` (the primary state changes), `bracket_completed` (stop/invalidation triggers — highest urgency, indicating a thesis was proven wrong between invocations), `guardrail_rejection` and `risk_limit_approached` (constraint events the PM needs to be aware of), and `margin_call` / `margin_liquidation` (forced actions). The full event catalog in state-persistence provides the complete set.

  *5b. PM decision log*
  All entries with event type `pm_decision` from the most recent N invocations (configurable sliding window, typically 2–3 invocations). Each entry is a full **command envelope** — the portfolio manager's structured evaluation of a single analyst or strategist proposal. The envelope preserves not just the PM's verdict and rationale but also: which agent originated the proposal, what was originally proposed, what modifications the PM made (if any), the adjustment category for each modification, and the resulting OMS command IDs. This allows the current invocation's PM — operating in a fresh context — to understand both the existing portfolio composition and the reasoning behind recent decisions without relitigating them. See [portfolio-manager.md](../../04-decision-layer/portfolio-manager.md) for the full envelope schema.

  *5c. Position modification trail*
  For each open position, all activity log entries scoped to that position ID, ordered chronologically — a filtered view of the full activity log, not a separate data structure. This provides the lifetime action history: entry, scale events (additions and reductions), bracket modifications, parameter adjustments, and cumulative realized P/L from partial exits.

---

**6. Thesis quality trends**
The process health dashboard — separate from P/L, because a system can be profitable with bad process (lucky) or unprofitable with good process (unlucky) over short periods. Only thesis quality is durable. This category provides the trailing context that helps the portfolio manager calibrate decisions against the system's own track record and detect degradation before it compounds.

  *Design principle — meta-awareness over raw metrics:* The most dangerous failure mode for a trading system isn't a single bad trade — it's a gradual degradation in process quality that isn't detected until the drawdown is severe. This category's purpose is to give the portfolio manager (and eventually, the human overseeing the system) early warning of process deterioration.

  *Note on risk-adjusted performance metrics and execution quality:* Sharpe, Sortino, profit factor, and execution monitoring are analysis-layer concerns computed at daily or weekly cadence. See [derived metrics 11a–11b](derived-metrics.md).

  *6a. Thesis accuracy trend*
  - Thesis accuracy trend: trailing validation rate (correct-for-right-reasons / total resolutions) over 5-day, 20-day, and inception windows — declining accuracy is the earliest warning of process degradation
  - Thesis duration accuracy: how well the system's time expectations match reality — are theses consistently resolving faster or slower than predicted?
  - Invalidation quality: when theses are invalidated, is the invalidation condition being hit early (suggesting the entry was mistimed or the thesis was weak) or late (suggesting the invalidation was well-calibrated but the thesis was wrong about direction)

  *6b. Signal reliability and conviction calibration*
  - Signal hit rate: for each signal type cited in theses (unusual options activity, earnings estimate revision, prediction market shift, etc.), the trailing hit rate
  - Signal-to-thesis conversion: how often each signal type leads to a thesis that the PM approves
  - Thesis generation rate vs. execution rate: how many theses is the analyst generating per invocation, and how many does the portfolio manager approve?
  - Conviction calibration: thesis validation rate grouped by conviction level (1–5). A well-calibrated analyst should show monotonically increasing validation rates at higher conviction levels. If level-4 trades don't meaningfully outperform level-2 trades, the conviction scale is poorly calibrated and the signal criteria definitions need tightening. See [analyst conviction scale](../../04-decision-layer/analyst.md) for the level definitions
  - Conviction-sizing deviation tracking: how often the PM sizes outside the advisory band for a given conviction level, the direction of deviation (above or below band), and whether deviations correlate with better or worse outcomes — computed from command envelope modifications with `category = conviction_disagreement`

  *6c. Performance attribution (trailing)*
  Where the returns are coming from — which sectors, which thesis types, which market conditions produce the best and worst outcomes.
  - P/L by sector: cumulative and trailing realized P/L attributed to each sector
  - P/L by thesis type: realized P/L grouped by the nature of the thesis — event-driven, mean-reversion, momentum, cross-asset divergence, etc.
  - P/L by market regime: realized P/L grouped by the volatility regime (from quant 11f) at the time of entry
  - Alpha vs. beta decomposition (trailing): of the system's total return over trailing windows, how much came from directional market exposure (beta) and how much from thesis-driven selection (alpha)
