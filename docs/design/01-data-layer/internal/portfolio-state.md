# Portfolio state — raw state

Direct reads from the OMS database (populated from Alpaca's account / positions / orders / activities endpoints and the `trade_updates` websocket — see [broker-adapter.md](../../05-execution-layer/broker-adapter.md)) or direct aggregations of stored data. No market data cross-reference or LLM judgment. For derived metrics, see [../../02-distillation-layer/internal.md](../../02-distillation-layer/internal.md).

---

**1. Position inventory**
What the system currently owns or is short. Every open position with enough detail for any agent to assess directional exposure, concentration, and portfolio composition without further queries. The foundation — categories 2–6 reference back.

  *Design principle — positions as structured theses, not just holdings:* A position binds a directional bet to its justifying thesis, success/failure parameters, and execution history. Tight coupling makes thesis-based position management possible (see [design-decisions.md](../../design-decisions.md)) — the system can't evaluate thesis validity without the thesis attached to the position.

  *1a. Current holdings*
  Each position is delivered as the position record defined in [position-model.md](../../05-execution-layer/position-model.md): base interface (direction, entry timestamp, market value, unrealized P/L, position weight, execution history, thesis and bracket bindings) plus instrument-specific extensions for equity, options, and strategy positions. The ingestion layer applies these consumer-facing computations at each invocation:

  - Current market value: quantity × current price using the latest price from quant 1a.
  - Position weight: market value as a percentage of total portfolio value (positions + cash).
  - Position age: hours since entry timestamp — at 4–72h horizons, a 6-hour-old position is treated very differently from a 48-hour-old one.
  - Entry execution summary: VWAP achieved vs. market VWAP at entry, slippage (raw vs. live-adjusted in paper mode via the [paper-evaluation harness](../../05-execution-layer/paper-evaluation-harness.md)), fill count — computed from execution history. Feeds harness calibration and execution monitoring (see [derived metrics 11b](../../02-distillation-layer/internal.md)).

  For exposure calculations (delta-adjusted vs. notional), see [position-model.md](../../05-execution-layer/position-model.md). The ingestion layer delivers both notional and delta-adjusted exposure per position.

  *1b. Sector and directional exposure*
  Direct rollups of the position inventory. Beta-adjusted metrics requiring quant 1f live in [derived metrics 7a](../../02-distillation-layer/internal.md).
  - Sector allocation: total long and short per sector (tech, semis, financials, energy) in dollars and as portfolio percentage — primary concentration measure. Uses delta-adjusted exposure for options/strategy positions per [position-model.md](../../05-execution-layer/position-model.md).
  - Net directional exposure: long minus short as portfolio percentage — net long, short, or neutral.
  - Gross exposure: long plus short as portfolio percentage — leverage proxy. 150% gross means more directional bets than capital even at low net exposure.
  - Long/short ratio per sector: a sector can be dollar-neutral but concentrated if all longs are correlated.

---

**2. P/L and performance tracking**
AlphaMind's primary success metric is ending each day with more liquid cash than the opening balance (see [README.md](../README.md)), so P/L tracking is a core input to every decision, not a reporting function. A system up 2% on the day has different risk appetite than one down 1%, even with identical market and thesis landscapes.

  *Design principle — P/L as context, not directive:* P/L informs PM risk decisions but never directly triggers trades. Thesis drives hold/close; P/L provides risk context for sizing, entry appetite, and drawdown management. Avoids the human failure mode of letting P/L override thesis ("I'm up 3% so I'll get aggressive" or "I'm down so I'll cut everything") by keeping P/L and thesis as separate inputs the PM weighs independently.

  *Note on P/L attribution:* Decomposition into market, sector, and alpha components requires market-return and beta cross-reference; analysis-layer work running daily pre-close. See [derived metrics 8a–8b](../../02-distillation-layer/internal.md).

  *2a. Position-level P/L*
  - Unrealized P/L: current market value minus cost basis, in dollars and as percentage of cost.
  - Unrealized P/L trajectory: P/L sampled at each invocation — is the position moving toward target, stalling, or against thesis? Up 1.5% this morning, now flat reads differently than flat since entry.
  - Distance to target: current price vs. thesis target (from 3a) — how much of the expected move is captured and remains.
  - Distance to stop/invalidation: current price vs. thesis invalidation (from 3a) — room before the thesis is wrong.
  - Risk/reward at current price: remaining upside divided by remaining downside — a 3:1 ratio at entry might be 1:1 halfway to target.

  *2b. Portfolio-level P/L*
  The PM's primary scoreboard.
  - Total unrealized P/L: sum of position-level, absolute and as portfolio percentage.
  - Daily realized P/L: locked in from positions closed today — feeds the primary success metric.
  - Daily total P/L (realized + unrealized): running daily score.
  - Cumulative P/L: lifetime realized track record.
  - Rolling P/L windows: trailing 1d, 3d, 5d, 20d realized — winning/losing streak detection. A 5-day losing streak may justify reducing sizes regardless of individual thesis quality.
  - Win/loss statistics: win rate, average win/loss size, profit factor (total gains / total losses).

  *2c. Drawdown tracking*
  Drawdown limits are hard-coded constraints referenced by the PM spec (see [portfolio-manager.md](../../04-decision-layer/portfolio-manager.md)).
  - Current drawdown: percentage from the most recent equity high-water mark — monitored against the max drawdown limit.
  - Drawdown duration: time since last equity high — extended drawdowns trigger mechanical risk reduction.
  - Maximum drawdown (lifetime): deepest peak-to-trough decline — calibration input for risk guardrails.
  - Intraday drawdown: worst point today vs. opening equity — deep intraday drawdown signals over-exposure even with end-of-day recovery.
  - Drawdown by source: which positions/sectors contributed — broad-based (macro-driven) vs. concentrated (idiosyncratic) drives different responses.

---

**3. Thesis registry**
The intellectual core. Every open position exists because a thesis justified it. Explicit *why* encoding rather than position tracking by entry/exit rules.

  *Design principle — thesis as living document:* A thesis is generated by the analyst, approved (possibly modified) by the PM, attached to a position at execution, monitored every invocation, and eventually resolved (validated, invalidated, or expired). The registry tracks the full lifecycle; resolution data feeds system tuning. Every closed thesis is a training example.

  The execution engine (05-execution-layer) is the definitive thesis database with full historical record. The ingestion layer delivers a snapshot of active theses plus a summary of recent resolutions to the analysis pipeline at invocation time.

  *Note on thesis dependency:* Whether active theses share catalysts or narratives is analysis-layer work requiring LLM judgment. See [derived metrics 9a–9b](../../02-distillation-layer/internal.md).

  *3a. Active thesis details*
  Each active thesis is delivered as the record defined in [thesis-model.md](../../05-execution-layer/thesis-model.md) — summary, typed components (entry, target, invalidation rationale), key assumptions, bracket leg linkages. This section specifies the ingestion-layer delivery contract:

  - Theses delivered at the summary level by default; component-level detail available per consumer:
    - The [strategist](../../04-decision-layer/strategist.md) receives full component-level records inline because thesis-status classification is per-component (`status_rationale` cites which component weakened or held).
    - The [qualitative research agent](../../03-analysis-layer/qualitative-research.md) receives summaries inline every invocation alongside the event calendar for catalyst-proximity reasoning.
    - The [portfolio manager](../../04-decision-layer/portfolio-manager.md) receives summaries inline with on-demand component retrieval via the [thesis-component retrieval tool](../../04-decision-layer/portfolio-manager.md#retrieval-tools).
    - The [synthesizer](../../03-analysis-layer/synthesizer.md) pulls summaries on-demand via `get_active_theses_summary` when cross-referencing market signals against positions.
  - The ingestion layer delivers all status=active theses plus child components where required.
  - Each thesis carries its generation timestamp (for age) and time expectation (for staleness detection).
  - Gap between intended entry parameters and actual fill price (from execution history) is surfaced alongside the thesis for risk/reward assessment.

  *3b. Thesis status and health*
  Updated each invocation by the [strategist](../../04-decision-layer/strategist.md). The five status classifications (on-track, partially-realized, at-risk, stale, invalidated) are defined in [thesis-model.md](../../05-execution-layer/thesis-model.md). The ingestion layer also delivers:

  - Thesis age vs. expected duration: a 48h-old thesis expected to resolve in 24h is overdue.
  - Supporting signal status: for each cited signal, whether it's still present, strengthened, weakened, or reversed — assessed by the strategist during re-evaluation.

  *3c. Recent thesis resolutions*
  Rolling window of closed theses (e.g., last 10 or 5 trading days) — the process feedback loop. Resolution categories and component-level outcomes are defined in [thesis-model.md](../../05-execution-layer/thesis-model.md). The ingestion layer delivers:

  - Resolution category (validated, profitable-but-wrong, invalidated-stopped-correctly, invalidated-wrong-on-exit) and component-level outcomes (validated, wrong, inconclusive per component).
  - Resolution P/L on each closed thesis.
  - Resolution timing: how long the thesis was active vs. expected.
  - Signal post-mortem: for invalidated theses, which signal or assumption was wrong.
  - Running thesis accuracy: trailing validation rate — the core process quality metric.

---

**4. Capital and capacity**
The PM's gating input for any new position — regardless of thesis quality, the system can't enter if capital or risk limits are reached.

  *Design principle — conservative cash management:* The primary success metric (end each day with more cash than opening balance) biases toward capital preservation. Maintain a meaningful cash buffer to preserve ability to act on the next high-conviction thesis. Being fully invested when the best opportunity appears is a capital-allocation failure.

  *Note on capital efficiency:* Retrospective deployment metrics (utilization, turnover, cash drag) are analysis-layer. See [derived metrics 10a–10b](../../02-distillation-layer/internal.md).

  *4a. Cash and buying power*
  Underlying data is the cash ledger entity in [state-persistence.md](../../05-execution-layer/state-persistence.md): current cash, settled cash, reserved capital, available buying power, margin held, unsettled proceeds with settlement dates. The ingestion layer delivers these fields plus:
  - Cash as percentage of portfolio: cash / (cash + position market value). The PM maintains this above the [Minimum cash reserve guardrail](../../06-risk-guardrails/rules-and-limits.md) — 10% base of total portfolio value measured against settled cash, regime-adapted via the rule's multiplier table (8% in low-vol up to 25% in crisis), enforced T3.
  - True deployable capital: settled cash minus reserved (for pending orders) minus margin held — actually available for new positions, accounting for settlement cycles per [venue configuration](../../05-execution-layer/venue-configuration.md).
  - Reg T excess (cumulative): trailing-30d, trailing-90d, and lifetime sums of `regt_excess_over_pm` across processed fills, computed at delivery time from fill-record attribution metadata per [regt-margin-attribution.md](../../05-execution-layer/regt-margin-attribution.md). The dollar cost of running on Reg T vs. a portfolio-margin equivalent — the operator's broker-switch signal.

  *4b. Pending orders*
  Approved but unfilled orders — committed capital, an active thesis, a decision that may need revision. Records from the Orders entity in [state-persistence.md](../../05-execution-layer/state-persistence.md), filtered to status in (pending, partially-filled). The ingestion layer delivers all order fields plus:
  - Order age: hours since placement. The [strategist](../../04-decision-layer/strategist.md) flags orders older than their thesis's expected entry window.
  - Fill probability context: current price vs. limit. An order at $840 with current $842 is close to filling; at $840 with current $870, unlikely. The strategist uses this to advise maintain/modify/cancel.

  *4c. Risk budget consumption*
  Where the system stands against hard-coded risk guardrails (max position size, max sector concentration, max daily drawdown, max gross exposure) enforced by the guardrail layer and referenced by the PM.
  - Position size headroom: max minus current largest position.
  - Sector concentration headroom: per-sector max minus current allocation.
  - Daily drawdown budget remaining: when zero, no new positions and consider reducing existing — the hardest constraint.
  - Gross exposure headroom.
  - Correlation limit status: whether a new position would push implied correlation above max.
  - Constraint breach proximity: composite flag of which limits are closest, as percentage consumed.

  *4d. Active risk parameter set*
  Current values of all risk guardrails. If parameters are regime-dependent (tighter in high vol), ingested state reports which set is active.
  - Max position size, max sector concentration, max daily drawdown, max gross exposure, max portfolio correlation (current regime values).
  - Regime label from quant 11f ("low-vol compression," "normal," "elevated volatility," "crisis") — the PM needs this to understand *why* limits are where they are.
  - Parameter change flag: whether anything changed since the last invocation — overnight regime shifts can invalidate yesterday's sizing assumptions.

---

**5. Activity log**
What happened since the last invocation. Categories 1–4 describe current *state*; this describes the *trajectory*. Between invocations (every 2 hours during market hours) orders fill, stops trigger, positions open or close.

  *Design principle — changelog over snapshot diffing:* Each invocation operates in a fresh context with no memory of prior state. An explicit changelog is cleaner than forcing agents to diff snapshots.

  The event type catalog in [state-persistence.md](../../05-execution-layer/state-persistence.md) defines 25+ typed events in six groups: position lifecycle, order lifecycle, bracket, thesis, cash/margin, risk/guardrail, PM decision. Each carries a structured detail payload. This section specifies filtering and delivery to the analysis pipeline.

  *5a. Intra-invocation changelog*
  All activity log entries with the current invocation ID, ordered chronologically. Highest-attention types: `position_opened` and `position_closed` (primary state changes), `bracket_completed` (stop/invalidation triggers — thesis proven wrong between invocations), `guardrail_rejection` and `risk_limit_approached`, `margin_call` / `margin_liquidation`. State-persistence has the full set.

  *5b. PM decision log*
  All `pm_decision` entries from the most recent N invocations (configurable, typically 2–3). Each is a full **command envelope** — the PM's structured evaluation of a proposal, preserving verdict, rationale, originating agent, original proposal, modifications, adjustment category, and resulting OMS command IDs. Lets the current PM understand recent decisions without relitigating. See [portfolio-manager.md](../../04-decision-layer/portfolio-manager.md) for envelope schema.

  *5c. Position modification trail*
  For each open position, activity log entries scoped to that position ID, chronologically — a filtered view, not a separate structure. Lifetime action history: entry, scale events, bracket modifications, parameter adjustments, cumulative realized P/L from partial exits.

---

**6. Thesis quality trends**
Process health dashboard, separate from P/L. Over short periods a system can be profitable with bad process (lucky) or unprofitable with good process (unlucky); only thesis quality is durable. Provides trailing context for PM calibration and early degradation detection.

  *Design principle — meta-awareness over raw metrics:* The dangerous failure mode is gradual process degradation undetected until the drawdown is severe. Early warning to the PM (and human overseer) is the purpose.

  *Note on risk-adjusted performance and execution quality:* Sharpe, Sortino, profit factor, and execution monitoring are analysis-layer at daily/weekly cadence. See [derived metrics 11a–11b](../../02-distillation-layer/internal.md).

  *6a. Thesis accuracy trend*
  - Thesis accuracy trend: trailing validation rate (correct-for-right-reasons / total resolutions) over 5d, 20d, inception — declining accuracy is the earliest warning.
  - Thesis duration accuracy: are theses resolving faster or slower than predicted?
  - Invalidation quality: when invalidated, was the invalidation hit early (mistimed entry, weak thesis) or late (well-calibrated invalidation, wrong direction)?

  *6b. Signal reliability and conviction calibration*
  - Signal hit rate: trailing hit rate for each signal type cited in theses (unusual options activity, earnings revisions, prediction market shifts).
  - Signal-to-thesis conversion: how often each signal type leads to a PM-approved thesis.
  - Thesis generation vs. execution rate: theses per invocation vs. PM approvals.
  - Conviction calibration: validation rate by conviction level (1–5). A well-calibrated analyst shows monotonically increasing validation at higher levels; if level-4 doesn't outperform level-2, the conviction scale needs tightening. See [analyst conviction scale](../../04-decision-layer/analyst.md).
  - Conviction-sizing deviation tracking: how often the PM sizes outside the advisory band, direction (above/below), and outcome correlation — computed from command envelope modifications with `category = conviction_disagreement`.

  *6c. Performance attribution (trailing)*
  - P/L by sector: cumulative and trailing realized P/L per sector.
  - P/L by thesis type: realized P/L by nature (event-driven, mean-reversion, momentum, cross-asset divergence).
  - P/L by market regime: realized P/L by volatility regime (quant 11f) at entry.
  - Alpha vs. beta decomposition (trailing): how much total return came from directional market exposure vs. thesis-driven selection.
