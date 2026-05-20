# Position model

The engine supports equities (long and short), options (single-leg), and multi-leg options strategies. An instrument hierarchy with a shared base interface: downstream consumers (analysis pipeline, portfolio manager, guardrail layer) reason about any position generically through the base, or inspect instrument-specific details when needed.

---

## Base position (all instruments)

Every position has:

- **Position ID:** unique identifier
- **Thesis ID:** binding to the thesis record justifying this position (see [thesis-model.md](thesis-model.md))
- **Bracket parameters:** target exit condition and invalidation conditions with type classification (see [orders-and-brackets.md](orders-and-brackets.md))
- **Direction:** long or short. This field is meaningful only for equity and single-leg options positions; strategy positions carry it as an inert placeholder (see § Strategy position).
- **Entry timestamp:** time of initial fill
- **Current market value:** updated at each pipeline invocation using latest price data
- **Unrealized P/L:** current market value minus cost basis, absolute and percentage
- **Position weight:** market value as a percentage of total portfolio value (positions + cash)
- **Execution history:** all fills for this position — entries, partial exits, additions

The analysis pipeline works through the base generically. Instrument-specific details are available when needed (e.g., guardrail layer checking options greeks).

---

## Equity position

Extends the base with: ticker, share count, average cost basis per share.

Cost basis is the blended entry price across all fills — the P/L reference point. Position weight = share count × current price / total portfolio value.

**Short equity positions** add:

- **Borrow rate:** annualized cost of borrowing shares — deducted from P/L on a daily accrual basis
- **Locate status:** whether the borrow is currently located and stable, or at risk of recall
- **Margin held:** collateral required to maintain the short position (see margin model below)

Short positions have theoretically unlimited loss (stock can rise indefinitely), which the guardrail layer accounts for in exposure calculations and sizing limits.

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

Tracks the **greeks** — delta, gamma, theta, vega — critical because they change the meaning of exposure and position weight:

- **Delta** determines effective directional exposure. An option with 0.3 delta on 10 contracts (1000 notional shares) has effective exposure equivalent to 300 shares, not 1000. The guardrail layer uses delta-adjusted exposure (see below).
- **Theta** represents daily time decay — options lose value every day even if the underlying doesn't move. A first-class risk for P/L tracking.
- **Gamma** measures how delta changes as the underlying moves — high gamma positions can rapidly change their exposure profile, relevant for intraday risk between invocations.
- **Vega** measures sensitivity to implied volatility changes — relevant for thesis evaluation when the thesis depends on a volatility view.

**Options bracket extensions:** Options brackets use two exit mechanisms reflecting the separation between thesis invalidation and capital protection:

1. **Underlying-triggered stops** (thesis invalidation): price-based invalidation legs trigger on the underlying equity's price, not the option's. The thesis is about the underlying's behavior ("a move below $865 indicates the earnings reaction was negative"), so the trigger tests the underlying directly. On trigger, the engine submits a market sell on the option position. See [orders-and-brackets.md](orders-and-brackets.md).

2. **P/L-based exits** (target and profit management): "close at 80% profit on premium" or "close at 50% loss on premium." Use the option's derived price to estimate P/L. Necessary because options can gain or lose significant value through delta, IV, and time decay effects that don't map linearly to underlying price levels.

Capital protection against Greek-driven erosion (IV crush, theta decay destroying option value without a large underlying move) is handled by the guardrail layer's position-level max loss rule (see [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)), not by the bracket. Ensures uniform protection across all options positions.

---

## Strategy position

A multi-leg options strategy modeled as a **single position with component legs**, not independent options positions.

*Design rationale:* Strategy legs hedge each other. Viewing them individually would cause the guardrail layer to see risk that doesn't exist at the strategy level (e.g., an iron condor has defined max loss at the strategy level even though individual short legs have high theoretical risk), and would make P/L attribution meaningless. The strategy is one trade idea expressed through multiple instruments.

**Position-level direction is a category error for strategies.** Long/short direction is not economically meaningful for a multi-leg strategy — an iron condor is neither long nor short. A strategy position carries `direction = LONG` as an inert placeholder required by the shared `PositionRecord` schema. Every consumer of a strategy position must derive directional sign from per-leg directions, net delta, or net premium — not from the position-level `direction` field. Branching on position-level `direction` for a strategy is a bug. The clean refactor — making `direction` instrument-specific or optional — is tracked as ALP-591.

A strategy position holds:

- **Strategy type label:** vertical spread, iron condor, straddle, calendar spread, custom, etc.
- **Component legs:** each an options position with full detail
- **Net premium:** total debit paid or credit received across legs. **Sign convention: positive for a net-debit strategy (premium paid), negative for a net-credit strategy (premium received).**
- **Max profit:** best-case P/L — computed from the leg structure (positive value, or positive-infinity for unbounded upside)
- **Max loss:** worst-case P/L — the risk the guardrail layer uses for margin and concentration (negative value, or negative-infinity for unbounded downside). The strategy's **capital at risk** is the magnitude of `max_loss_usd`.
- **Breakeven levels:** the underlying price(s) at which the strategy breaks even
- **Strategy-level greeks:** net-signed aggregate across component legs — a net-short-delta strategy has a negative delta

The bracket operates at the strategy level: target and invalidation reference the strategy's net P/L or the underlying's price, not individual legs.

---

## Exposure calculations: delta-adjusted

Portfolio-level exposure calculations ([raw state category 1b](../01-data-layer/internal/portfolio-state.md)) must be instrument-aware. "Exposure" means different things for different instruments.

The position model reports **both** notional exposure and delta-adjusted exposure for every position. The guardrail layer uses delta-adjusted exposure for concentration and risk limits — ensuring a deep out-of-the-money protective put doesn't consume sector concentration budget like an equivalent notional equity position would.

For equity, notional and delta-adjusted are identical (delta = 1 for longs, -1 for shorts). For options, delta-adjusted exposure = contract count × multiplier × delta × underlying price. For strategies, uses the strategy-level net delta.

---

## Margin model: realistic simulation

The engine simulates realistic margin requirements for all position types — applies in paper mode to faithfully preview live capital requirements, and carries forward into live.

**Short equity positions:** Reg T margin rules — initial margin at entry (typically 150% of position value), maintenance margin monitored continuously (typically 130%), margin calls when equity falls below maintenance.

**Short options positions:** margin calculated from underlying price, strike distance, and volatility — reflecting the actual margin a broker would require.

**Defined-risk strategies:** strategies with capped loss (vertical spreads, iron condors) require margin equal to the strategy's max loss, not individual legs. The correct treatment — prevents penalizing well-structured trades.

**Margin calls:** when a position's margin requirement exceeds available margin, the engine flags a margin call. The PM must resolve it (closing or reducing positions) within a configurable window. If unresolved, the engine force-liquidates the position with the highest margin deficit — mirroring real broker behavior. Margin call events log in the activity log ([raw state category 5a](../01-data-layer/internal/portfolio-state.md)) as high-urgency events.

---

## Corporate-action-pending positions

When a corporate action fires on a position's underlying, the engine cancels the bracket and sets `corporate_action_adjustment_needed` on the position record. The position is delivered to the strategist as a flagged item at the next invocation; the strategist produces either a fresh `adjust-bracket` or a `close` per its per-action defaults (see [orders-and-brackets.md § Corporate action handling](orders-and-brackets.md#corporate-action-handling) and [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions)). The flag clears when the strategist's recommendation is approved and the fresh bracket is in place.

**Spin-off children** are the one structural exception to the otherwise-mandatory thesis and bracket bindings. A `SPIN` activity creates a new local position with `thesis_id: null`, `bracket_id: null`, `corporate_action_adjustment_needed: true`, and `origin: spin_off_from_<parent_position_id>`. The orphan state is transient — the strategist resolves it in the same invocation that integrated the spin-off. Per-action mechanics (quantity, cost basis, ticker, cash-ledger movements) are in [corporate-actions.md](corporate-actions.md).
