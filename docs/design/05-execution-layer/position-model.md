# Position model

The engine supports equities (long and short), options (single-leg), and multi-leg options strategies. The position model uses an instrument hierarchy with a shared base interface: downstream consumers (the analysis pipeline, the portfolio manager, the guardrail layer) can reason about any position generically through the base interface, or inspect instrument-specific details when needed.

---

## Base position (all instruments)

Every position, regardless of instrument type, has:

- **Position ID:** unique identifier
- **Thesis ID:** binding to the thesis record that justifies this position (see [thesis-model.md](thesis-model.md))
- **Bracket parameters:** target exit condition and invalidation conditions with type classification (see [orders-and-brackets.md](orders-and-brackets.md))
- **Direction:** long or short
- **Entry timestamp:** time of initial fill
- **Current market value:** updated at each pipeline invocation using latest price data
- **Unrealized P/L:** current market value minus cost basis, both absolute and percentage
- **Position weight:** market value as a percentage of total portfolio value (positions + cash)
- **Execution history:** all fills associated with this position — entries, partial exits, additions

This base interface is what the analysis pipeline works with generically. Instrument-specific details are available when a consumer needs them (e.g., the guardrail layer checking options greeks for exposure calculations).

---

## Equity position

Extends the base with: ticker, share count, average cost basis per share.

Straightforward for long positions. The cost basis is the blended entry price across all fills — the P/L reference point. Position weight is share count × current price / total portfolio value.

**Short equity positions** add:

- **Borrow rate:** the annualized cost of borrowing shares for the short sale — deducted from P/L on a daily accrual basis
- **Locate status:** whether the borrow is currently located and stable, or at risk of recall
- **Margin held:** the collateral required to maintain the short position (see margin model below)

Short positions have theoretically unlimited loss (the stock can go up indefinitely), which the guardrail layer must account for in exposure calculations and position sizing limits.

---

## Options position

Extends the base with:

- **Underlying ticker:** the stock the option derives from
- **Strike price:** the exercise price
- **Expiration date:** when the contract expires
- **Contract type:** call or put
- **Contract count:** number of contracts held
- **Contract multiplier:** shares per contract (typically 100)
- **Premium paid per contract:** the entry cost—the cost basis for options P/L

Additionally tracks the **greeks** — delta, gamma, theta, vega — which are critical because they change the meaning of exposure and position weight:

- **Delta** determines the effective directional exposure. An option with 0.3 delta on 10 contracts (1000 notional shares) has effective exposure equivalent to 300 shares, not 1000. This is why the guardrail layer uses delta-adjusted exposure (see below).
- **Theta** represents daily time decay — options lose value every day even if the underlying doesn't move. This is a first-class risk that the P/L tracking system must account for.
- **Gamma** measures how delta changes as the underlying moves — high gamma positions can rapidly change their exposure profile, which matters for intraday risk between invocations.
- **Vega** measures sensitivity to implied volatility changes — relevant for thesis evaluation when the thesis depends on a volatility view rather than a directional view.

**Options bracket extensions:** Options brackets use two distinct exit mechanisms that reflect the separation between thesis invalidation and capital protection:

1. **Underlying-triggered stops** (thesis invalidation): price-based invalidation legs trigger on the underlying equity's price, not the option's price. The thesis is about the underlying's behavior ("a move below $865 indicates the earnings reaction was negative"), so the trigger tests the underlying directly. When triggered, the engine submits a market sell on the option position. See [orders-and-brackets.md](orders-and-brackets.md) for the full spec.

2. **P/L-based exits** (target and profit management): "close at 80% profit on premium" or "close at 50% loss on premium." These use the option's derived price to estimate P/L. This is necessary because options can gain or lose significant value through delta, IV, and time decay effects that don't map linearly to underlying price levels.

Capital protection against Greek-driven erosion (IV crush, theta decay destroying option value without a large underlying move) is handled by the guardrail layer's position-level max loss rule (see [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)), not by the bracket. This ensures uniform protection across all options positions.

---

## Strategy position

A multi-leg options strategy modeled as a **single position with component legs**, not as independent options positions.

*Design rationale:* The legs of a strategy hedge each other. Viewing them individually would cause the guardrail layer to see risk that doesn't exist at the strategy level (e.g., an iron condor has defined max loss at the strategy level even though individual short legs have high theoretical risk), and would make P/L attribution meaningless. The strategy is one trade idea expressed through multiple instruments.

A strategy position holds:

- **Strategy type label:** vertical spread, iron condor, straddle, calendar spread, custom, etc.
- **Component legs:** each an options position with full options position detail
- **Net premium:** total debit paid or credit received across all legs
- **Max profit:** the best-case P/L for the strategy — computed from the leg structure
- **Max loss:** the worst-case P/L — the risk the guardrail layer uses for margin and concentration calculations
- **Breakeven levels:** the underlying price(s) at which the strategy breaks even
- **Strategy-level greeks:** summed from component legs — the aggregate exposure profile

The bracket operates at the strategy level: target and invalidation conditions reference the strategy's net P/L or the underlying's price, not individual legs.

---

## Exposure calculations: delta-adjusted

Portfolio-level exposure calculations ([raw state category 1b](../01-data-layer/internal/portfolio-state.md)) must be instrument-aware. "Exposure" means different things for different instruments.

The position model reports **both** notional exposure and delta-adjusted exposure for every position. The guardrail layer uses delta-adjusted exposure for concentration and risk limits. This ensures that a deep out-of-the-money protective put doesn't consume sector concentration budget the way an equivalent notional equity position would — the correct economic treatment.

For equity positions, notional and delta-adjusted exposure are identical (delta = 1 for longs, delta = -1 for shorts). For options positions, delta-adjusted exposure = contract count × multiplier × delta × underlying price. For strategy positions, delta-adjusted exposure uses the strategy-level net delta.

---

## Margin model: realistic simulation

The engine simulates realistic margin requirements for all position types. This applies in paper trading mode to faithfully preview the capital requirements of live trading, and carries forward directly into live mode.

**Short equity positions:** modeled under Reg T margin rules — initial margin requirement at entry (typically 150% of position value), maintenance margin monitored continuously (typically 130%), margin calls triggered when equity falls below maintenance levels.

**Short options positions:** margin calculated based on the underlying's price, strike distance, and volatility — reflecting the actual margin a broker would require.

**Defined-risk strategies:** strategies with capped loss (vertical spreads, iron condors) require margin equal to the max loss of the strategy, not the margin of individual legs. This is the correct treatment and prevents the margin model from penalizing well-structured trades.

**Margin calls:** when a position's margin requirement exceeds available margin, the engine flags a margin call. The portfolio manager must resolve it (by closing or reducing positions) within a configurable time window. If unresolved, the engine force-liquidates the position with the highest margin deficit—mirroring real broker behavior. Margin call events are logged in the activity log ([raw state category 5a](../01-data-layer/internal/portfolio-state.md)) as high-urgency events.

---

## Corporate-action-pending positions

When a corporate action fires on a position's underlying, the engine cancels the bracket and sets a `corporate_action_adjustment_needed` flag on the position record. The position is delivered to the strategist as a flagged item at the next invocation; the strategist produces either a fresh `adjust-bracket` or a `close` per its per-action defaults (see [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling) and [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)). The flag is cleared when the strategist's recommendation is approved and the resulting fresh bracket is in place.

**Spin-off children** are the one structural exception to the otherwise-mandatory thesis and bracket bindings. A `SPIN` activity creates a new local position representing the spun-off shares with `thesis_id: null`, `bracket_id: null`, `corporate_action_adjustment_needed: true`, and an `origin: spin_off_from_<parent_position_id>` provenance reference. The orphan state is transient by design — the strategist resolves it within the same invocation that integrated the spin-off. The full per-action mechanics (quantity, cost basis, ticker, and cash-ledger movements) are specified in [corporate-actions.md](corporate-actions.md).
