# Paper-evaluation harness

A thin layer that runs only in paper mode. It takes each raw Alpaca paper fill and computes an **estimated live-execution delta** — slippage + market impact + regulatory fees Alpaca's paper simulation does not model — so paper P/L can be reported two ways: raw (what Alpaca said) and live-adjusted (what we'd expect at a real exchange). The OMS's position and cash state use raw Alpaca fills exactly as Alpaca reports them; the harness attaches estimates as fill-report metadata and computes adjusted aggregates downstream.

---

## Why the harness exists

Alpaca's paper trading environment is deliberately optimistic by omission. It does not simulate: dividends (corporate actions are surfaced separately), borrow fees, market impact, price slippage, order queue position, regulatory fees, or price improvement. Orders fill at NBBO regardless of size relative to available liquidity, and partial fills arrive as a random ~10% draw rather than a function of volume participation.

For AlphaMind's paper evaluation — whose purpose is deciding *when it is safe to deploy real capital* — these omissions bias in the wrong direction. If paper P/L systematically overstates live P/L, the go-live decision will trigger too early, and live performance will underperform the paper track record.

The harness closes that gap by estimating, per fill, what the live-exchange execution would have cost relative to Alpaca's paper fill. Estimates are recorded alongside raw fills; adjusted aggregates (position-level adjusted P/L, portfolio-level adjusted daily P/L) are surfaced to evaluation consumers alongside raw aggregates.

### What the harness is *not*

The harness is not a drop-in for the deleted custom simulator. It does not intercept fill production, does not model venue capabilities, does not simulate brokers other than Alpaca, and does not gate orders on simulated liquidity. Alpaca's paper environment produces fills; the harness annotates them.

The harness is also not a pessimism knob. Per the parity-over-simulation principle in the [execution-layer README](README.md): if the harness systematically over-estimates live drag, the go-live threshold becomes unreachable without the system actually improving. The harness is tuned to estimate, not to punish.

---

## Harness outputs

For each paper fill, the harness computes a `live_execution_estimate` structure and attaches it to the fill report's metadata. The raw fill fields (price, quantity, timestamp) are untouched.

```
live_execution_estimate:
  estimated_spread_cost:        float   # $ cost of crossing the bid-ask at live execution
  estimated_impact_cost:        float   # $ adverse price movement from our own order
  estimated_regulatory_fees:    float   # $ SEC + TAF + CAT + OCC + ORF combined
  estimated_live_fill_price:    float   # paper_fill_price ± (spread/2 + impact)
  estimated_live_net_proceeds:  float   # signed effect on cash vs. raw Alpaca fill
  confidence:                   str     # "high" | "medium" | "low" — function of data availability
  components_used:              list    # which sub-models contributed (for audit)
```

The OMS persists this metadata verbatim. Position and cash ledgers remain driven by raw fills; adjusted views are computed at read time by consumers that care (the evaluation reports, the go-live gate).

### Two P/L aggregates downstream

At the portfolio-state layer, paper-mode state delivers both:

- **Raw P/L** — the direct output of Alpaca's paper fills, no adjustment. This is what the OMS's position records naturally compute, and what the PM sees when reasoning about *current* thesis status during paper trading.
- **Live-adjusted P/L** — raw P/L minus the aggregated `estimated_live_net_proceeds` deltas across all fills that produced the current position. This is the headline number for go-live evaluation.

The PM uses raw P/L for in-session decisions because the harness's estimates are still estimates — the PM should not optimize against a noisy adjusted number in the middle of a trade. The live-adjusted aggregate is a retrospective evaluation signal, not an input to per-position decisions.

---

## Sub-models

The harness's estimates come from three composable sub-models. Each is independently auditable.

### Spread + impact

Estimates the combined cost of crossing the bid-ask spread and moving the market with the order's own size. Conceptually:

```
estimated_slippage = (estimated_spread / 2) + impact_coefficient * estimated_spread * sqrt(fill_shares / ADV)
```

- **Estimated spread** comes from an observed-quote buffer (preferred) or a price/ADV/volatility model (fallback when quote data is not available). When using observed quotes from the data pipeline, a modest +10% buffer is applied to account for intraday spread widening — this is a calibrator, not a safety factor.
- **Impact coefficient** starts at the square-root-model default of 0.5 for market orders (0.25 for limits, 0.75 for stops), adjusted upward for extreme time-of-day conditions (opening/closing auctions).
- **ADV** is sourced from the data pipeline's price/volume snapshots.

For buys, the estimate is added to the paper fill price; for sells, subtracted. The result is `estimated_live_fill_price`.

**Calibration path.** Once live trading produces real fills for a given instrument, the harness periodically compares paper-fill-plus-estimate against the live fill at the same nominal price and adjusts the impact coefficient toward the observed drag. The goal is zero bias: paper-plus-estimate should match live-fill on average, with variance driven only by genuine market randomness. This is the primary knob for keeping the harness calibrated, not pessimistic.

### Regulatory fees

Uses a rate table keyed by instrument class and buy/sell side. The table is updated when Alpaca publishes fee schedule changes; the current entries are documented in [broker-adapter.md § Fee reporting](broker-adapter.md). Applied per fill:

- Equities: CAT (all), TAF (sells only), SEC (sells only)
- Options: CAT (all), SEC (sells only), ORF (all), OCC (all)
- Crypto: none

The harness's fee estimate at fill time is compared in the daily reconciliation pass against the actual EOD fee activities from Alpaca's `account/activities`; persistent drift between estimate and actual indicates the rate table needs updating.

### Fill-probability correction

Alpaca's paper environment fills limit orders whenever the NBBO touches the limit, which over-fills in a real venue where queue position and momentary volume matter. The harness does not prevent the paper fill from occurring (it does not intercept), but it tags fills where the estimated live fill probability is materially below 100%:

- Limit touches that barely reached the limit price (bar extreme == limit) with low bar volume → `confidence: low`, flagged as a likely false fill
- Thin-volume instruments where the fill size would have exceeded a realistic volume-participation share → `confidence: medium`, impact estimate inflated

Flagged fills are still reported with both raw and adjusted values; the `confidence` field lets evaluation downstream weight them or exclude them. The go-live gate does not exclude any fills but does surface the distribution of confidence levels as a signal about how noisy the paper record is.

---

## What the harness does not do

- **Does not model short-sale borrow mechanics.** AlphaMind's universe is ETB-only and Alpaca charges $0 borrow fees on ETB; there is no meaningful drag to estimate.
- **Does not model forced buy-ins.** Alpaca does not generate forced buy-ins in paper trading, and the universe composition makes them vanishingly rare in live trading. The scenario-tests layer in the risk guardrails covers the synchronized-squeeze tail case as a scripted exercise, not as a stochastic model.
- **Does not simulate options-price fills.** Options fills come from Alpaca's paper engine just like equity fills. The derivation model in [architecture.md § 4d](architecture.md) runs independently for guardrail monitoring and underlying-triggered stop submission; it is not involved in estimating the cost of the Alpaca-produced fill.
- **Does not enforce fill caps or reject large orders.** Alpaca paper will happily fill an unrealistically large order at NBBO; the harness will then attach a high impact estimate. Evaluation consumers see adjusted P/L that reflects the unrealism. The harness does not intervene because intervention would diverge from live-mode behavior — the whole point is to estimate drag without altering the execution path.

---

## Activation and deactivation

The harness is enabled only when the adapter is configured for paper mode. In live mode, fills already carry real execution costs (slippage, fees via EOD reconciliation, any short-sale costs), and the harness is bypassed entirely — no `live_execution_estimate` metadata is attached. Paper-mode-only keeps the live-mode hot path clean.

---

## Dependencies

- [broker-adapter.md](broker-adapter.md) — the fills the harness annotates come from this adapter's fill stream
- [architecture.md](architecture.md) — defines Phase 1 fill collection; the harness runs on collected fills
- [state-persistence.md](state-persistence.md) — the `live_execution_estimate` metadata is persisted alongside raw fill records
- [portfolio state — P/L](../01-data-layer/internal/portfolio-state.md) — delivers raw and live-adjusted P/L aggregates to downstream consumers
