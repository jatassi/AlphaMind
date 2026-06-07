# Paper-evaluation harness

A thin layer running only in paper mode. Takes each raw Alpaca paper fill and computes an **estimated live-execution delta** — slippage + market impact + regulatory fees Alpaca's paper simulation does not model — so paper P/L reports two ways: raw (what Alpaca said) and live-adjusted (what we'd expect at a real exchange). The OMS's position and cash state use raw Alpaca fills as reported; the harness attaches estimates as fill-report metadata and computes adjusted aggregates downstream.

---

## Why the harness exists

Alpaca's paper trading environment is deliberately optimistic by omission. It does not simulate: dividends (corporate actions are surfaced separately), borrow fees, market impact, price slippage, order queue position, regulatory fees, or price improvement. Orders fill at NBBO regardless of size relative to available liquidity, and partial fills arrive as a random ~10% draw rather than a function of volume participation.

For AlphaMind's paper evaluation — whose purpose is deciding *when it is safe to deploy real capital* — these omissions bias in the wrong direction. If paper P/L systematically overstates live P/L, the go-live decision triggers too early and live underperforms the paper track record.

The harness closes that gap by estimating, per fill, what live-exchange execution would have cost relative to Alpaca's paper fill. Estimates record alongside raw fills; adjusted aggregates (position-level adjusted P/L, portfolio-level adjusted daily P/L) surface to evaluation consumers alongside raw aggregates.

### What the harness is *not*

The harness is not a drop-in for the deleted custom simulator. It does not intercept fill production, model venue capabilities, simulate non-Alpaca brokers, or gate orders on simulated liquidity. Alpaca's paper environment produces fills; the harness annotates them.

The harness is also not a pessimism knob. Per the parity-over-simulation principle in the [execution-layer README](README.md): if the harness systematically over-estimates live drag, the go-live threshold becomes unreachable without the system actually improving. The harness estimates, it does not punish.

---

## Harness outputs

For each paper fill, the harness computes a `live_execution_estimate` structure and attaches it to the fill report's metadata. Raw fill fields (price, quantity, timestamp) are untouched.

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

- **Raw P/L** — direct output of Alpaca's paper fills, no adjustment. What the OMS's position records compute, and what the PM sees when reasoning about *current* thesis status during paper trading.
- **Live-adjusted P/L** — raw P/L minus the aggregated `estimated_live_net_proceeds` deltas across all fills producing the current position. The headline number for go-live evaluation.

The PM uses raw P/L for in-session decisions — estimates are still estimates, and the PM should not optimize against a noisy adjusted number mid-trade. The live-adjusted aggregate is a retrospective evaluation signal, not an input to per-position decisions.

---

## Sub-models

Estimates come from three composable, independently-auditable sub-models.

### Spread + impact

Estimates the combined cost of crossing the bid-ask spread and moving the market with the order's own size:

```
estimated_slippage = (estimated_spread / 2) + impact_coefficient * estimated_spread * sqrt(fill_shares / ADV)
```

- **Estimated spread** — from an observed-quote buffer (preferred) or a price/ADV/volatility model (fallback). Observed quotes from the data pipeline get a +10% buffer for intraday spread widening — a calibrator, not a safety factor.
- **Impact coefficient** — square-root-model default 0.5 for market orders (0.25 for limits, 0.75 for stops), adjusted upward for extreme time-of-day conditions (opening/closing auctions).
- **ADV** — from the data pipeline's price/volume snapshots.

For buys, the estimate adds to the paper fill price; for sells, subtracts. Result: `estimated_live_fill_price`.

**Calibration path.** Once live trading produces real fills for an instrument, the harness periodically compares paper-fill-plus-estimate against the live fill at the same nominal price and adjusts the impact coefficient toward observed drag. Goal: zero bias. Paper-plus-estimate matches live-fill on average, with variance driven only by genuine market randomness. The primary knob for keeping the harness calibrated, not pessimistic.

### Regulatory fees

Rate table keyed by instrument class and buy/sell side. Updated when Alpaca publishes fee schedule changes; current entries in [broker-adapter.md § Fee reporting](broker-adapter.md). Applied per fill:

- Equities: CAT (all), TAF (sells only), SEC (sells only)
- Options: CAT (all), SEC (sells only), ORF (all), OCC (all)
- Crypto: none

The fee estimate at fill time is compared in the daily reconciliation pass against actual EOD fee activities from Alpaca's `account/activities`; persistent drift indicates the rate table needs updating.

### Fill-probability correction

Alpaca's paper environment fills limit orders whenever the NBBO touches the limit, which over-fills in a real venue where queue position and momentary volume matter. The harness does not intercept paper fills, but tags fills where estimated live fill probability is materially below 100%:

- Limit touches that barely reached the limit price (bar extreme == limit) with low bar volume → `confidence: low`, flagged as a likely false fill
- Thin-volume instruments where fill size would have exceeded a realistic volume-participation share → `confidence: medium`, impact estimate inflated

Flagged fills still report with both raw and adjusted values; the `confidence` field lets evaluation downstream weight or exclude them. The go-live gate does not exclude any fills but surfaces the distribution of confidence levels as a noise signal.

---

## What the harness does not do

- **Short-sale borrow mechanics.** AlphaMind's universe is ETB-only; Alpaca charges $0 borrow fees on ETB. No meaningful drag to estimate.
- **Forced buy-ins.** Alpaca does not generate them in paper, and the universe composition makes them vanishingly rare live. The scenario-tests layer in risk guardrails covers the synchronized-squeeze tail case as a scripted exercise.
- **Options-price fills.** Options fills come from Alpaca's paper engine like equity fills. The derivation model in [architecture.md § 4d](architecture.md) runs independently for guardrail monitoring and underlying-triggered stop submission; it does not estimate the cost of Alpaca-produced fills.
- **Fill caps or large-order rejection.** Alpaca paper happily fills an unrealistically large order at NBBO; the harness attaches a high impact estimate. Evaluation consumers see adjusted P/L reflecting the unrealism. Intervention would diverge from live-mode behavior — the point is to estimate drag without altering the execution path.

---

## Activation and deactivation

Enabled only when the adapter is configured for paper mode. In live mode, fills already carry real execution costs (slippage, fees via EOD reconciliation, short-sale costs), and the harness is bypassed entirely — no `live_execution_estimate` metadata is attached. Keeps the live-mode hot path clean.

---

## Dependencies

- [broker-adapter.md](broker-adapter.md) — the fills the harness annotates come from this adapter's fill stream
- [architecture.md](architecture.md) — defines fill collection; the harness runs on collected fills
- [state-persistence.md](state-persistence.md) — the `live_execution_estimate` metadata is persisted alongside raw fill records
- [portfolio state — P/L](../01-data-layer/internal/portfolio-state.md) — delivers raw and live-adjusted P/L aggregates to downstream consumers
