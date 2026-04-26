# OMS commands

The write API to the engine. Five commands: four core commands for position lifecycle and one sizing command for conviction-driven adjustments. Commands are issued primarily by the portfolio manager, and in limited cases by the engine itself (protective CLOSE commands triggered by guardrail breaches between invocations — see [command origins](#command-origins) below). All commands are wrapped in command envelopes that provide full traceability regardless of origin — PM-originated envelopes per [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md), engine-originated envelopes per [engine-envelope-schema.md](engine-envelope-schema.md). The formal JSON Schema for the command vocabulary itself is in [oms-command-schema.md](oms-command-schema.md).

*Design principle — minimal effective vocabulary:* The command set must be expressive enough to handle every scenario the thesis-bracket architecture creates, but small enough that the portfolio manager (operating as an LLM in a fresh context window) can reliably select the right command and fill in the right parameters. Every parameter must be either directly available in the portfolio manager's context (portfolio state, synthesizer output, analyst recommendations) or derivable from it. If a command requires information the portfolio manager doesn't have, it will hallucinate values — that's a design failure, not an LLM failure.

*Design principle — intent-explicit commands:* Each command encodes a distinct intent — not just a mechanical action. CLOSE and ADD could theoretically be a single "resize" command with positive/negative quantities, but keeping them separate makes the activity log semantically rich: "portfolio manager closed 500 shares of NVDA because the thesis was invalidated" is a different decision than "portfolio manager added 200 shares of NVDA because conviction increased," even though both are position size changes. The thesis feedback loop benefits from this intent clarity.

*Design principle—no accountability shortcuts:* Compound commands (ROLL, HEDGE) were considered and deliberately excluded. See [../design-decisions.md](../design-decisions.md) for the full rationale. The core concern: compound commands create cognitive shortcuts that allow LLM agents to avoid honest thesis evaluation. A ROLL command makes it easy to frame a thesis failure as a "continuation." A HEDGE command makes it easy to delay closing a losing position by "managing risk." Both patterns are common human trader failure modes, and LLM agents are at least as susceptible. The five-command vocabulary forces the portfolio manager to confront every exit as a CLOSE (with an explicit invalidation reason) and every entry as an OPEN (with a full independent thesis). Hedging and position replacement are fully expressible through CLOSE + OPEN sequences within a single invocation—the sequential processing model ensures capital accounting is correct.

*Design principle — commands carry execution intent, not decision context:* OMS commands contain only the parameters the engine needs to execute. Decision context — which agent proposed the trade, what the PM's evaluation was, what parameters were modified and why — lives on the **command envelope** that wraps each command. The portfolio manager produces PM-originated envelopes; the continuous monitor produces engine-originated envelopes for guardrail breach responses (see [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the full envelope specification). The activity log stores both the envelope (for the feedback loop) and the command execution results (for position state). This separation keeps the command contract lean and execution-focused while providing full traceability through the envelope.

---

## Command overview

| Tier | Command | Purpose |
|------|---------|---------|
| Core | **OPEN** | Enter a new position with a mandatory bracket and structured thesis |
| Core | **CLOSE** | Exit an existing position (full or partial) with a resolution rationale |
| Core | **ADJUST** | Modify an existing position's bracket parameters or thesis components |
| Core | **CANCEL** | Withdraw a pending order that hasn't filled |
| Sizing | **ADD** | Increase an existing position with additional thesis justification |

---

## Core commands

### OPEN

Enter a new position. The most parameter-heavy command — creates a position, a thesis, a bracket, and submits the entry order. Every new position flows through OPEN, including hedges and position replacements — there is no special command for these patterns.

**Required parameters:**

- **Instrument:** ticker and direction (long/short) for equity; underlying, strike, expiration, type for options; full leg specification for strategies
- **Entry order:** order type (market, limit, stop-limit) and price parameters. This becomes the entry leg of the bracket
- **Position size:** quantity (shares or contracts) and equivalent dollar value — must be specified in both forms so the guardrail layer can validate against both quantity-based and dollar-based limits
- **Target:** target price or P/L level and order type for the take-profit leg. Required
- **Invalidation legs:** at least one hard invalidation (price-based stop or time-based expiration) plus any event-based invalidation conditions. Each leg specifies the condition and the order type for mechanical enforcement
- **Thesis:** complete structured thesis with summary and mandatory components covering every bracket leg (see [thesis-model.md](thesis-model.md))

**Guardrail validation:**

- Position size within per-position limits
- Sector concentration within limits after this addition
- Gross and net exposure within limits after this addition
- Sufficient capital (cash minus reserved capital for pending orders) to fund the position plus margin requirements
- Thesis completeness: every bracket leg has a corresponding thesis component
- For options and strategies: delta-adjusted exposure used for concentration and directional exposure checks, not notional. The guardrail layer computes delta (and full greeks) internally at validation time — the OPEN command does not include greeks as parameters. See "Greek computation at validation time" below

**Greek computation at validation time (options and strategies):**

The guardrail layer computes greeks internally rather than accepting LLM-provided values, via the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md). The OPEN command's instrument parameters (underlying, strike, expiration, contract type) are sufficient: the library sources underlying price from the real-time stream, IV from the data pipeline's surface, and time to expiration from the command, then applies the conservative delta buffer and per-leg aggregation per the library's contract.

The validation-time greeks serve double duty: they're used for the guardrail check, and they're persisted as the position's initial greeks (used until the first scheduled greek refresh from the continuous monitor after the entry fills).

**On success:** returns an order acknowledgment containing:
- New position ID and order ID
- Position enters "pending" state — the bracket's protective legs are held in OTO (one-triggers-other) status, activating only when the entry fills
- **Validation metadata** (for options and strategies): the computed greeks (delta, gamma, theta, vega) used in the guardrail check, the IV value used, the resulting delta-adjusted exposure, and the headroom remaining against each applicable limit. This metadata is logged in the activity log alongside the command, creating an audit trail of what the guardrail layer assumed at entry time

**On rejection:** returns a rejection payload synchronously to the portfolio manager within the same invocation, specifying which guardrail(s) blocked the command, the current limit values, the headroom available, and — for options — the computed greeks and delta-adjusted exposure that caused the rejection. The PM can immediately adjust (e.g., reduce contract count, choose a different strike with lower delta) and resubmit. See "Rejection handling" in the command processing model below. Logged in the activity log ([raw state 5b](../01-data-layer/internal/portfolio-state.md)).

---

### CLOSE

Exit an existing position, fully or partially. Used when the PM determines a thesis is no longer valid (based on analysis pipeline input, not price hitting a stop — the bracket handles that mechanically), when a target has been reached, when conviction has weakened, or when portfolio-level risk requires position reduction.

**Required parameters:**

- **Position ID:** which position to close
- **Quantity:** shares/contracts to exit, or "all" for full close. Partial closes must specify the exact amount
- **Execution method:** order type (market for urgency, limit for price protection) and price parameters if limit
- **Close rationale:** a structured explanation that feeds directly into thesis resolution. Must classify the reason and provide a specific invalidation reason:
  - *Thesis invalidated:* the analysis pipeline flagged an event-based invalidation condition as met, or the PM's own assessment is that the thesis is no longer valid. Maps to "invalidated" resolution categories. **Invalidation reason required** — the specific condition or evidence that proves the thesis wrong (e.g., "MSFT guided AI capex lower than consensus," "catalyst fired but price didn't respond as expected — causal chain was incorrect," "catalyst rescheduled, instrument cannot capture the move")
  - *Target reached:* the position has hit or approached the target and the PM is taking profits. Maps to "validated" resolution
  - *Conviction reduced:* the thesis isn't invalidated but conviction has weakened — partial close to reduce exposure while maintaining some position. Partial close only; requires updated thesis assessment
  - *Risk management:* closing for portfolio-level reasons (drawdown, concentration, regime change) independent of the individual thesis. Important to distinguish from thesis-level invalidation in the feedback loop

**Guardrail validation:**

- Position ID exists and has the specified quantity available
- For partial closes: remaining position still has a valid bracket (stops may need to be adjusted for the reduced size)

**On success:** if market order, routes immediately to the broker adapter. If limit, creates a pending close order. For full closes, the bracket's protective legs (open stops, take-profits) are cancelled automatically — they're no longer needed. For partial closes, the bracket remains active on the remaining quantity.

---

### ADJUST

Modify an existing position's bracket parameters or thesis components without changing position size. This is how the PM tightens stops on a winner, extends a time horizon, updates a target, or revises thesis components as conditions evolve.

**Required parameters:**

- **Position ID:** which position to adjust
- **Changes:** one or more of the following:
  - *Stop level:* new price for the price-based invalidation leg
  - *Target level:* new price or P/L level for the take-profit leg
  - *Time expiration:* new time-based invalidation deadline
  - *Event invalidation:* new or revised event-based invalidation condition
  - *Thesis components:* updated narratives or key assumptions for any thesis component
- **Adjustment rationale:** why the bracket is being modified — logged as a deviation in the activity log

**Guardrail validation:**

- Position ID exists
- New stop level doesn't create a loss exceeding the max per-position loss limit
- Time extension doesn't exceed any maximum position duration limit (if one exists)
- Thesis coverage remains complete after the adjustment: every bracket leg still has a corresponding thesis component

**On success:** existing protective orders at the broker are cancelled and replaced with new orders reflecting the adjusted parameters. The thesis record is updated with the new components. The activity log records the adjustment with before/after values and the PM's rationale.

**Design note—bracket modifications and the feedback loop:** Every ADJUST is logged as a deviation. The diagnostics layer tracks whether bracket modifications correlate with better or worse outcomes. A system that frequently widens stops and extends time horizons is exhibiting a common failure mode—reluctance to accept losses. This signal should be surfaced in thesis quality trends ([raw state category 6](../01-data-layer/internal/portfolio-state.md)).

---

### CANCEL

Withdraw a pending order that hasn't filled. Used when conditions have changed and an unfilled entry (or unfilled close order) is no longer desired.

**Required parameters:**

- **Order ID:** the specific pending order to cancel
- **Cancel reason:** why the order is being withdrawn — thesis no longer valid, conditions changed, entry price no longer attractive, etc.

**Guardrail validation:**

- Order ID exists and is in a cancellable state (pending, not already filled or expired)

**On success:** the order is cancelled at the broker. If the cancelled order was the entry leg of a bracket, the entire bracket is cancelled (protective legs that were in OTO status are withdrawn, the associated thesis is resolved as "cancelled — never entered"). If the cancelled order was a protective leg (which would be unusual — the PM would normally use ADJUST to change protective orders rather than cancelling them outright), the system warns that the remaining bracket is incomplete and requires immediate ADJUST to restore coverage.

**Capital release:** any capital reserved for the cancelled order is returned to available buying power.

---

## Sizing command

### ADD

Increase an existing position — add shares or contracts because conviction has increased. Distinct from OPEN because it modifies an existing position rather than creating a new one: it changes the average cost basis, it references the existing thesis (with a new component), and it may modify the bracket.

**Required parameters:**

- **Position ID:** which position to add to
- **Additional quantity:** shares/contracts to add, and equivalent dollar value
- **Entry order:** order type and price parameters for the addition
- **Thesis addition component:** a new thesis component explaining why conviction increased — what signal strengthened, what new information supports a larger position. This is appended to the existing thesis, not a replacement
- **Bracket adjustment** (optional but recommended): updated stop and/or target to reflect the new average cost basis. If the stop was set at a specific percentage below entry, adding at a different price changes the risk profile — the PM should explicitly decide whether to maintain the existing stop or adjust it

**Guardrail validation:**

- Same as OPEN: position size (now the combined total) against per-position limits, sector concentration, gross/net exposure, available capital
- For options and strategies: greek computation at validation time follows the same process as OPEN (see "Greek computation at validation time" in the OPEN section). The guardrail layer computes greeks for the proposed addition and combines them with the existing position's current greeks to assess the enlarged position's total delta-adjusted exposure. The validation response includes the computed greeks for the addition
- The enlarged position doesn't exceed any limit that the original position was within

**On success:** the addition order is routed to the broker adapter. When it fills, the position's share count, average cost basis, and market value are updated. If bracket adjustments were specified, the existing protective orders are replaced. The thesis is updated with the new addition component. For options and strategies, the position's greeks are updated to reflect the combined position (existing greeks + addition greeks) and will be refreshed at the next scheduled greek refresh.

---

## Common patterns expressed through the five commands

The following patterns were considered as dedicated compound commands and deliberately excluded (see [../design-decisions.md](../design-decisions.md)). Each is fully expressible through command sequences within a single invocation.

### Position replacement ("rolling")

When a thesis remains valid but the instrument needs to change (e.g., options approaching expiration):

1. **CLOSE** the existing position. Close rationale: *thesis invalidated — time component.* Invalidation reason: the specific timing reason (catalyst rescheduled, instrument expiring, etc.). The thesis is fully resolved — including component-level resolution — as an invalidation. The timing failure feeds the thesis duration accuracy metric.
2. **OPEN** a new position with the replacement instrument. The thesis is independent and must stand on its own — full structured thesis, full bracket, full component coverage. The thesis may reference the same underlying reasoning, but it is a new thesis with a new time expectation.

The PM issues both commands in the same invocation. The sequential processing model credits capital from the CLOSE before validating the OPEN.

### Hedging

When an existing position needs protection from external risk:

**OPEN** a new position whose thesis explains the hedging rationale. The thesis must articulate: what risk is being hedged, why the parent position's thesis is still valid, how the hedge is sized, and when the hedge should be removed. The hedge is an independent position with its own bracket — including a time-based invalidation to prevent the hedge from outliving its purpose. The PM manages the hedge lifecycle explicitly, closing it when the risk passes or the parent position is closed.

There is no automatic lifecycle binding between positions. If the PM closes the parent and forgets to close the hedge, the hedge's own bracket (specifically its time-based hard backstop) ensures it doesn't persist indefinitely.

---

## Command origins

Commands can originate from two sources, each with different authority and traceability mechanisms:

**Portfolio manager (primary).** The PM issues OPEN, CLOSE, ADJUST, ADD, and CANCEL commands during the pipeline's execute phase. All five commands are available. Each command is wrapped in a PM-originated command envelope with full evaluation context. This is the normal path for all trading decisions.

**Continuous monitor (protective only).** The continuous monitor may issue CLOSE commands between invocations when it detects a guardrail breach caused by market movement, regime changes, or margin events. Only CLOSE is permitted — the engine cannot open new positions, add to existing ones, or adjust brackets without PM involvement. Each command is wrapped in an engine-originated command envelope with a guardrail trigger record (breach rule, breach details, position selection logic). See [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the envelope specification and [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) for which breaches trigger engine-originated commands vs. which are deferred to the next invocation.

**Guardrail validation:** Engine-originated CLOSE commands bypass the normal guardrail validation path (they are *curing* a breach, not creating exposure). However, the engine must verify that the CLOSE itself doesn't create a secondary breach — e.g., closing a short position that was providing directional balance could push net long exposure over the limit. If a secondary breach would result, the engine logs the conflict and selects an alternative position or defers to the PM at the next invocation.

---

## Command processing model

### Sequencing within an invocation

The portfolio manager may issue multiple commands in a single invocation. Commands are processed in the order issued. Each command's guardrail validation accounts for the cumulative impact of all prior commands in the same invocation — if the portfolio manager issues two OPEN commands, the second one's concentration check includes the exposure from the first. If the portfolio manager issues a CLOSE followed by an OPEN, the OPEN's capital check accounts for the capital released by the CLOSE.

### Conflicting commands

The engine detects and rejects conflicting commands issued in the same invocation:

- CLOSE and ADD on the same position → rejected, PM must choose one
- CANCEL on an order referenced by another command in the same batch → rejected, flag the conflict
- Multiple ADJUSTs on the same position → only the last one takes effect, with a warning logged

### Command IDs and duplicate handling

Every command carries a unique identifier assigned deterministically by the OMS command intake layer from the envelope's structural position — the PM never generates command IDs. The full format and generation rules are specified in [oms-command-ids.md](../oms-command-ids.md).

The in-process architecture (pipeline and OMS in a single Python process, atomic Phase 2 transactions, fail-closed mid-pipeline policy) rules out the scenarios that would motivate a dedup-and-return-stored-response mechanism. A duplicate `command_id` arriving at the OMS therefore indicates a structural bug — concurrent envelope mutation, an ID-derivation flaw, or upstream corruption — and is treated as an error: the OMS raises, the invocation aborts per fail-closed, and an alert is logged. See [oms-command-ids.md §Why not dedup](../oms-command-ids.md) for the full analysis.

Broker submission failures are a separate concern handled in [broker-adapter.md](broker-adapter.md) and the submission-failure policy in [state-persistence.md](state-persistence.md) — a brief within-invocation retry window followed by abandonment, with surfacing to the originating agent at the next invocation.

### Rejection handling

When a command is rejected by the guardrail layer, the rejection is returned **synchronously to the portfolio manager within the same invocation**. The PM retains agency to adjust and retry immediately — rejections are not deferred to the next invocation.

**Rejection payload:**

- The command that was rejected
- Which specific guardrail(s) blocked it
- The current limit value and headroom available for each violated rule
- A suggested modification (e.g., "position size of $50,000 exceeds the per-position limit of $40,000; maximum allowable size is $38,500 given current exposure")
- For options and strategies: the computed greeks and delta-adjusted exposure that caused the rejection

**PM response options:**

- Reissue the command with modified parameters (reduced size, different instrument) — the reissued command gets a fresh guardrail check
- Skip the trade and proceed to the next command in the sequence
- Re-evaluate remaining commands in light of the rejection (e.g., if available capital is lower than expected, deprioritize a marginal trade)

**Logging:** Rejections are logged in the activity log ([raw state 5b](../01-data-layer/internal/portfolio-state.md)) regardless of whether the PM retries. If the PM reissues a modified command that succeeds, both the original rejection and the successful retry are logged, providing full traceability. The command envelope is updated with a `guardrail_rejection_response` modification record — see [portfolio-manager.md](../04-decision-layer/portfolio-manager.md).

**Context for this design:** The analyst and strategist pre-validate all proposals against guardrails before they reach the PM (see [analyst.md](../04-decision-layer/analyst.md)), and the PM validates its own sizing modifications before submitting commands. Execution-time rejections should be infrequent — they occur only when portfolio state shifts between upstream validation and command submission (market movement, fill resolution, regime changes). The synchronous feedback model ensures these edge cases are handled gracefully within the same invocation rather than discovered hours later.
