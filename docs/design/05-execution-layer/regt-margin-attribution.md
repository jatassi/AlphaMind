# Reg T margin attribution

The OMS computes, per fill, the marginal Reg T margin consumed and the marginal portfolio-margin-equivalent that would have been consumed under a published broker portfolio-margin model. The difference — the **Reg T excess** — accumulates into a portfolio-state field and surfaces in the [command center](../command-center.md). The signal anchors any future broker-switch decision in dollars.

---

## Why this exists

[`venue-configuration.md § Reg T ceiling`](venue-configuration.md#regulatory-and-account-constraints) establishes that Alpaca runs Reg T (per-leg margining), not portfolio margin (risk-net margining). For a hedged book — long NVDA against short AMD, long call against an underlying short — Reg T charges margin on the gross legs while portfolio margin would charge on the net risk. The doc notes Reg T is non-binding at current and near-term deployment scale.

This module monitors what running on Reg T actually costs, in dollars, vs. the portfolio-margin equivalent. If the cumulative excess grows large enough to justify the cost of switching brokers (account migration, adapter rewrite, parity re-verification), the operator has a defensible number to anchor the decision on.

The number is calibrated to be defensible, not precise: a version-pinned reference model with documented assumptions is more auditable than a sophisticated model whose outputs are hard to trace.

---

## Outputs

For each processed fill, the OMS computes a `regt_margin_attribution` structure and persists it as fill-record metadata.

```
regt_margin_attribution:
  regt_margin_before:           float    # Reg T initial-margin requirement immediately pre-fill (portfolio aggregate)
  regt_margin_after:            float    # Reg T initial-margin requirement immediately post-fill (portfolio aggregate)
  regt_marginal_consumption:    float    # after - before; signed (negative = fill releases margin)
  pm_equivalent_before:         float    # Portfolio-margin-equivalent initial-margin requirement pre-fill
  pm_equivalent_after:          float    # Portfolio-margin-equivalent initial-margin requirement post-fill
  pm_marginal_consumption:      float    # after - before; signed
  regt_excess_over_pm:          float    # regt_marginal_consumption - pm_marginal_consumption
  pm_model_version:             str      # e.g. "occ_tims_v1_2026Q2"
```

`regt_excess_over_pm` is the headline value — the dollar cost of running on Reg T attributable to this fill. The cumulative aggregate across all attributed fills is the operator's broker-switch signal.

Initial-margin equivalent only. Both `regt_margin_*` and `pm_equivalent_*` snapshot the requirement at fill time; ongoing maintenance margin between fills is not modeled here.

---

## Computation flow

Runs in **Phase 1 fill processing** ([`state-persistence.md § Phase 1`](state-persistence.md)). Each unprocessed fill is integrated into position state; the attribution computation runs alongside.

For a batch of unprocessed fills, fills are processed in `fill_timestamp` order so each fill's `*_before` reflects all prior fills in the batch. Single-pass per fill:

1. **`*_before` snapshot.** Compute the portfolio-aggregate Reg T requirement and the portfolio-margin-equivalent requirement against the pre-fill position set.
2. **Integrate the fill.** Standard Phase 1 update of position state, cost basis, cash ledger.
3. **`*_after` snapshot.** Recompute both requirements against the post-fill position set.
4. **Marginal deltas.** Subtract; populate the structure; persist alongside the fill record.

The Reg T side reuses the per-leg formulas in [`venue-configuration.md § Margin tiers`](venue-configuration.md#regulatory-and-account-constraints) summed across positions. The PM side runs the OCC TIMS / FINRA 4210 baseline reference model below.

The underlying price, IV surface, and Black-Scholes implementation are the surfaces [`guardrail-evaluation.md`](../06-risk-guardrails/guardrail-evaluation.md) already uses for T3 validation; the same data and code path serves both.

---

## Portfolio-margin reference model: OCC TIMS / FINRA 4210 baseline v1

Per-class-group scenario stress against OCC TIMS's published shock parameters, summed across class groups.

**Class group.** One underlying plus all derivatives written on it. An equity-only position is its own class group. An options strategy on NVDA contributes to the NVDA class group along with any long or short NVDA stock.

**Per-class-group stress.** For each class group:

1. Apply OCC TIMS's published shock for the underlying's asset class — e.g., ±15% for high-cap S&P 500 equity, ±20% for small-cap, ETF-specific values per OCC TIMS's table. Discretize across the standard 10-point grid (equidistant shock points from worst-down to worst-up).
2. At each shock point, revalue all instruments in the class group. Equity revalues at the shocked underlying price. Options revalue via Black-Scholes with the shocked underlying and an IV adjustment paired to the price-shock direction; reuses the pricing infrastructure in [`guardrail-evaluation.md § Delta-adjusted exposure`](../06-risk-guardrails/guardrail-evaluation.md#delta-adjusted-exposure).
3. The class group's margin requirement = the absolute value of the worst (most-negative) P/L across the shock grid.

**Aggregation.** Sum the per-class-group margins. Inter-class offsets — where correlated class groups would net against each other under OCC TIMS's product-group methodology — are v2 scope. Without offsets, the PM-equivalent number is biased *upward* relative to OCC TIMS's actual output, which biases the Reg T excess (`regt - pm`) *downward*. Conservative direction: understates the broker-switch case rather than overstating it.

**Version-pinning.** The shock parameter table is a versioned snapshot of the OCC TIMS RBH/CPM User Guide (the methodology FINRA Rule 4210(g) points to; e.g., `pm_model_version: "occ_tims_v1_2026Q2"`). The snapshot lives in implementation config; refresh is operator-driven. Aggregates over time are filterable by version when methodology changes.

---

## Aggregation and delivery

Cumulative aggregates are computed at portfolio-state delivery time by summing `regt_excess_over_pm` across fill-record metadata for the requested window. No separate aggregate table.

Delivered fields (added to [`portfolio-state.md § 4a Cash and buying power`](../01-data-layer/internal/portfolio-state.md)):

- `regt_excess_trailing_30d`
- `regt_excess_trailing_90d`
- `regt_excess_lifetime`

Surfaced in the [command center](../command-center.md)'s Cash and capital pane.

Aggregates start from fills bearing this metadata. Fills predating the module contribute zero — the lifetime aggregate is therefore "lifetime-since-this-module-landed," not "since-account-inception."

---

## Activation

Always-on; runs in both paper and live mode. The signal is a policy comparison, not a parity adjustment, and is meaningful in both:

- **Paper:** what the system would pay in Reg T excess if live.
- **Live:** what the system is actually paying vs. the PM equivalent.

Distinct from the [paper-evaluation harness](paper-evaluation-harness.md), which is paper-only because it reconstructs live drag Alpaca's paper engine omits.

---

## v2 candidates

- **Inter-class correlation offsets.** Mirror OCC TIMS's product-group methodology where correlated class groups (index ETFs vs. constituents, sector pairs) net against each other before aggregation. Reduces the v1 conservative bias.
- **Maintenance-margin equivalent.** A continuous-evaluation companion that tracks ongoing margin drag across position lifetimes, not just at fill events.
- **Threshold alert.** When `regt_excess_trailing_30d` exceeds a configured floor, surface to the operator via the [command center](../command-center.md) alerts pane as a broker-revisit prompt.

---

## Dependencies

- [Venue configuration](venue-configuration.md) — Reg T per-leg formulas; the policy this module compares against.
- [State persistence — fill records](state-persistence.md) — where `regt_margin_attribution` metadata is persisted.
- [Portfolio state — cash and buying power](../01-data-layer/internal/portfolio-state.md) — where the cumulative aggregates surface.
- [Command center — Portfolio dashboard](../command-center.md) — Cash and capital pane displays the running aggregate.
- [Guardrail evaluation](../06-risk-guardrails/guardrail-evaluation.md) — Black-Scholes + IV-surface infrastructure reused for the OCC TIMS options revaluation step.
- [Position model](position-model.md) — class-group composition follows position-model semantics for equity, options, and strategy positions.
