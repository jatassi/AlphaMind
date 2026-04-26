# Orders & brackets

The engine supports a complete order vocabulary spanning equities, options, and complex strategies, organized in three tiers. Every position requires a mandatory thesis-linked bracket that defines exit conditions at the point of entry.

---

## Order type hierarchy

### Tier 1 — Atomic orders

Single instruction, single fill or cancel. These are the primitives — every complex order decomposes into these. They apply equally to long entries, short entries, and exits across all instrument types.

- **Market order:** execute immediately at the best available price
- **Limit order:** execute at the specified price or better — rests in the book until filled, cancelled, or expired
- **Stop order:** triggers a market order when the price reaches the stop level — used for invalidation-based exits
- **Stop-limit order:** triggers a limit order when the price reaches the stop level — provides price protection on triggered exits at the cost of potential non-fill

### Tier 2 — Contingent orders

Lifecycle-linked groups of atomic orders that manage each other's state. These map directly to the thesis-bracket structure (see mandatory brackets below).

- **Bracket order:** the primary order structure — entry + take-profit limit + stop-loss, submitted as a single unit linked to a thesis ID. When the entry leg fills, the protective legs activate. If the entry leg is cancelled or expires, the protective legs are cancelled automatically. This is how the mandatory thesis-bracket binding is implemented at the order level.
- **OCO (one-cancels-other):** a pair of orders where filling or triggering one automatically cancels the other. The take-profit and stop-loss legs of a bracket are an OCO pair — when the target fills, the stop cancels, and vice versa.
- **OTO (one-triggers-other):** an order that activates contingent orders only upon its own fill. The entry leg of a bracket is an OTO that triggers the protective OCO pair. Used when the entry is a limit order that hasn't filled yet — protective orders only become active once the position exists.

### Tier 3 — Instrument-specific orders

Orders with parameters specific to the instrument class.

**Short selling:**
- Short market/limit orders (sell shares not currently owned)
- Buy-to-cover orders (close a short position)
- Locate/borrow tracking: the engine tracks borrow availability and cost for each short position
- Forced buy-in handling: models the risk that borrowed shares are recalled

**Options:**
- Options orders carry additional parameters: underlying ticker, strike price, expiration date, contract type (call/put), contract multiplier
- Buy-to-open / sell-to-open (establish new positions)
- Buy-to-close / sell-to-close (close existing positions)
- Exercise and assignment handling: models early exercise risk for American-style options

**Multi-leg strategies:**
- Strategy orders group multiple options legs into a single submission with a net debit or credit
- Supported structures: vertical spreads, horizontal (calendar) spreads, straddles, strangles, iron condors, and custom multi-leg combinations
- Multi-leg strategies submit as Alpaca `order_class: mleg` with up to 4 legs. Alpaca executes mleg orders as combined fills when liquidity permits; per-leg fills arrive on the `trade_updates` stream tagged with the parent strategy ID (see [broker-adapter.md](broker-adapter.md))
- Strategy-level bracket: the protective orders operate on the strategy's net P/L or the underlying's price, not on individual legs

---

## Mandatory thesis-linked brackets

Every position in AlphaMind must have a complete bracket order — entry parameters, target exit condition, and at least one hard invalidation condition. The engine structurally rejects naked entries (entries without defined exit parameters). This enforces thesis-driven position management at the engine level, not by convention.

*Design rationale:* On AlphaMind's 4–72 hour time horizon, there is no legitimate circumstance that justifies entering a position without defined exit parameters. The cases were evaluated during design:

- **Emergency hedging** can be expressed as a bracket with time-based invalidation ("close hedge when primary risk is resolved or in 24 hours, whichever comes first"). This also prevents the classic mistake of hedges that outlive their purpose.
- **Momentum / breakout trades** without a defined invalidation are exactly the undisciplined behavior the thesis architecture is designed to prevent. If the portfolio manager can't articulate where it's wrong, it shouldn't be entering.
- **Crisis situations** with undefined risk are the *most* dangerous scenario for undefined exits. A wide invalidation ("accept up to 3% loss while the situation develops") is better than no invalidation.
- **Complex options positions** where price targets don't map cleanly can define exits in P/L terms ("close at 50% profit or 30% loss on premium") or time terms ("close 1 day before expiration").

### Three invalidation types

The bracket supports three types of invalidation conditions, reflecting the different ways a thesis can be proven wrong:

**Price-based invalidation.** A traditional stop loss — the engine enforces automatically by submitting a stop or stop-limit order as part of the bracket. "Close if NVDA drops below $820." This is the hard mechanical backstop. For equities, Alpaca's native bracket handles the lifecycle. For options, Alpaca does not support brackets; the continuous monitor evaluates the trigger against the underlying equity stream and submits the closing order when it fires (see [architecture.md § 4e](architecture.md)).

**Time-based invalidation.** Thesis expiration — the engine enforces automatically by monitoring elapsed time against the thesis's expected duration. "Close if not resolved within 48 hours." Prevents positions from lingering indefinitely on stale theses.

**Event-based invalidation.** A qualitative condition that the analysis pipeline evaluates — the engine stores the condition and flags the position when the pipeline identifies the condition as met. "Thesis invalid if MSFT guides AI capex lower than consensus." The engine cannot enforce this mechanically; the portfolio manager agent must act on the flag. This is the "soft" leg of the bracket.

**Hard backstop requirement:** Every bracket must have at least one hard leg (price-based or time-based) that the engine can enforce without PM intervention. This guarantees that even if the portfolio manager agent fails to act on a soft invalidation flag, or if the pipeline encounters an error, no position can exist indefinitely without a mechanical exit condition.

### Options price-based stops: trigger on the underlying

For options and strategy positions, price-based invalidation legs trigger on the **underlying instrument's price**, not the option's price. The trigger condition and the execution action are separated:

1. **Trigger condition:** "NVDA underlying ≤ $865" — evaluated against the real-time underlying equity stream, the same clean, high-fidelity data feed used for equity stops. No derived pricing is involved in the trigger decision.

2. **Execution action:** when the trigger fires, the continuous monitor submits a closing market order (or configurable sell-limit) on the option position via the broker adapter. Alpaca's options fill applies — spread, liquidity, and fill timing are Alpaca's responsibility. The actual exit price depends on the option's market conditions at the moment the underlying trigger fires. In paper mode, the [paper-evaluation harness](paper-evaluation-harness.md) estimates the expected live-execution delta on top of Alpaca's paper fill.

*Design rationale — thesis invalidation, not capital protection:* The price-based invalidation leg is a thesis statement — "a move below $865 indicates a decisively negative earnings reaction." The thesis is about the underlying's behavior, so the trigger should test the underlying's price directly. The underlying equity stream provides clean, real-time data with no derivation error, making trigger detection reliable.

Capital protection — preventing a position from silently bleeding value through IV crush, theta decay, or other Greek effects without a large underlying move — is handled by the guardrail layer's position-level max loss rule (see [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)). This separation keeps brackets focused on thesis invalidation and guardrails focused on mechanical risk control. It also ensures capital protection applies uniformly to all options positions, not just those where the trader remembered to include an option-price floor.

*Consequence for execution:* The PM accepts that the option-side fill price is approximate when an underlying-triggered stop fires. The option's value at the trigger moment depends on delta, IV, time decay, and market conditions — all of which shift between when the stop level was set and when it's hit. Position-sizing discipline (limiting total premium at risk per position) ensures this approximation is bounded — the difference between the estimated and actual option exit price is a fraction of an already-tolerable total loss.

*Strategy positions:* For multi-leg strategies, the price-based stop triggers on the underlying and closes the entire strategy (all legs) via market orders. The strategy's bracket operates on the underlying's price, not on individual leg prices or the strategy's net premium.

### P/L-based bracket legs: anchor to actual fill price

Bracket legs can be defined in P/L terms rather than absolute price terms — "close at 80% profit on premium" or "close at 30% loss on position cost." When the OPEN command specifies a P/L-based target or stop, it also includes the equivalent absolute price computed from the planned entry price (e.g., 80% profit on $18.50 premium → $33.30 limit). This absolute price is what the engine initially submits to the gateway as a resting order.

When the entry leg fills, the engine recalculates P/L-based protective leg prices using the **actual fill price** as the anchor. If the entry was a limit at $18.50 but filled at $17.80, an 80% profit target recalculates from $33.30 to $32.04. The engine cancels the originally submitted protective order and replaces it with the recalculated price.

This recalculation happens as part of the bracket activation sequence — the same Phase 1 step that transitions the bracket from pending-entry to active and submits protective legs to the gateway ([state-persistence.md](state-persistence.md), Phase 1 write path, step 4). No additional processing phase is needed.

*Design rationale:* P/L targets express the thesis's risk/reward structure — "I expect 80% upside and I'm wrong at 30% downside." The risk/reward ratio is the thesis, not the specific dollar value. Anchoring to planned entry would silently distort the intended risk/reward when fills deviate from plan. A fill at $17.80 instead of $18.50 is a 3.8% better entry — the thesis's 80% upside target should benefit from that improvement, not remain pinned to a stale reference.

*Absolute-price legs are unaffected:* Price-based invalidation legs that trigger on the underlying (e.g., "stop if NVDA ≤ $865") are thesis statements about the underlying's behavior, not about position P/L. These are submitted as specified in the OPEN command and are not recalculated on fill. Only legs explicitly flagged as P/L-based (`target_type: pl_percentage` or `stop_type: pl_percentage`) are subject to fill-anchor recalculation.

*Activity log:* The recalculation is logged as a `bracket_modified` event with source = "fill-anchor-recalculation" (distinct from PM-initiated modifications), recording the original and recalculated prices for the audit trail.

### Bracket modification

The portfolio manager may modify a bracket after entry — widening a stop, extending a time horizon, adjusting a target. This is a first-class operation, not an exception. However, every bracket modification is logged as a deviation in the activity log ([raw state category 5b](../01-data-layer/internal/portfolio-state.md)) with the portfolio manager's rationale. The feedback loop tracks whether bracket modifications correlate with better or worse outcomes, providing a diagnostic signal for system tuning.

### Corporate action handling

When a corporate action fires on a position with an active bracket — stock split, reverse split, stock dividend, cash dividend, merger, acquisition, or spin-off — the engine cancels the bracket and flags the position with `corporate_action_adjustment_needed`. The strategist sees the flag on the position record at its next invocation (delivered as part of [portfolio state](../01-data-layer/internal/portfolio-state.md)) and produces either a fresh `adjust-bracket` with re-scaled parameters or a `close` through the normal command path (see [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions) for the per-action-type decision logic).

*Design rationale — uniform cancel-and-review:* mechanical bracket re-scaling would preserve thesis economics for arithmetic events (splits, stock dividends) but would silently adjust brackets under structural events (mergers, spin-offs) where the thesis genuinely needs re-evaluation. A uniform cancel-and-review policy puts reasoning into the loop on every corporate action. Splits get a quick "yes, same thesis, re-bracket at the new scale" pass; non-invariant events get the thesis re-assessment they warrant. One policy across all action types is simpler to specify and simpler to test than per-type routing; the modest ceremony on routine splits is accepted as the cost of the simpler model.

*Timing:* scheduled corporate actions are applied at the first scheduled invocation on or after ex-date. The anchored pre-open invocation at 9:00 AM ET is the normal case — position-level adjustments fire first, brackets are cancelled, then the strategist and PM run with the flagged positions in context and produce fresh brackets before the 9:30 AM market open. This minimizes the in-market-hours window during which a flagged position lacks a bracket. Non-scheduled corporate-action-adjacent events (trading halts, bankruptcies, delistings) are outside the scope of this policy and handled by their own mechanisms.

*Between-invocation safety:* during the brief window between the corporate action applying and the strategist producing a fresh bracket, the position-level max loss guardrail ([rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)) remains active. It reads current P/L against the post-action cost basis (which the position-level mechanics already adjusted) and fires a protective CLOSE if the position loses more than its per-position max regardless of whether a bracket stop is in place. The guardrail provides the backstop; the bracket provides the thesis-level exit.

*Activity log:* the engine records `corporate_action_applied` (carrying action type and ratio/amount), `bracket_cancelled_corporate_action` on the bracket cancellation, and a normal `bracket_modified` entry with source `corporate_action_adjustment` when the strategist/PM submit the fresh bracket.

*Prerequisite scope:* this section defines only the bracket lifecycle under corporate actions. The underlying position-level mechanics — quantity scaling, cost basis adjustment, cash crediting/debiting, short-position dividend obligation flow, merger conversion, spin-off splitting, the activity log catalog, and Phase 1 integration timing — are specified in [corporate-actions.md](corporate-actions.md).
