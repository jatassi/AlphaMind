## Symptom

Several macro data series that feed `universal_context` arrive at the invocation with **zero observations** (not low — zero), causing their distillation modules to fall through to a bootstrap fallback that publishes defaults (`correlation: 0`, `beta: 0`) as if they were real signal. Downstream agents have no way to distinguish "no data" from "the real value is approximately zero."

The 0-observation series:

| Module | Bootstrap reason | Default published |
| -- | -- | -- |
| `q6.dollar_attribution` | DTWEXBGS history unavailable | (omitted entirely) |
| `q6.yield_curve_regime` | required FRED DGS series unavailable | (omitted entirely) |
| `q7.intermarket_regime.gld_real_yields` | `gld_real_yields_observations: 0 < 60` | `correlation: 0` |
| `q7.intermarket_regime.oil_xle_beta` | `oil_xle_beta_observations: 0 < 60` | `long_beta: 0, short_beta: 0, beta_drift: 0` |
| `q7.intermarket_regime.vix_spy` | `vix_spy_observations: 0 < 60` | `correlation: 0` |

Distinguish these from the low-but-nonzero observation modules (`funding_stress: 11/60`, `market_liquidity: 11/60`, `breadth_internals: 128/200`, `spy_tlt: 39/60`) — those are genuinely bootstrap-explained and accumulating; the 0-observation series have not landed any data points at all.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md`:

* Line 672: `### q6.dollar_attribution | bootstrap — bootstrap_reason: dollar_attribution: DTWEXBGS history unavailable`
* Line 703: `### q6.yield_curve_regime | bootstrap — bootstrap_reason: yield_curve: required FRED DGS series unavailable`
* Lines 725-739: gld_real_yields, oil_xle_beta, vix_spy all at `observations: 0 < 60` with `correlation: 0` / `beta: 0` published.

The adaptive researcher's \[AR-4\] thread (`analysis/synthesizer/user_message.md` line 2469-2478) flagged the impact directly:

> *"macro_data queries for SOFR_OIS_spread, repo_rate, HY_credit_spread, and us_10y_yield all returned unavailable — cannot independently verify any funding market reading; the primary verification path is completely blocked this cycle."*

And the energy researcher \[SA-ENERGY-TC-1\] (line 2359) notes:

> *"Conviction is capped at moderate because crude price tape is absent in this invocation (oil_xle_beta has zero observations, no commodity price confirmation available)"*

## Root cause hypothesis

Two separate problems:

1. **Collector not running / not authed:** DTWEXBGS and FRED DGS series likely require a FRED API key or a separate scheduled collector that isn't running on the production server. The `gld_real_yields`, `oil_xle_beta`, `vix_spy` modules likely depend on the same underlying price-history collectors not being scheduled or not having backfilled history.
2. **Silent default vs explicit missing sentinel:** Distillation modules with `observations: 0` publish `correlation: 0` / `beta: 0` instead of `None` / `null` / missing-data sentinel. This is more dangerous than the explicit "X<60" bootstrap label because downstream agents see a real numeric value.

## Scope

(a) Audit which collectors should be producing these series and confirm they are scheduled and authenticated on the production server.

(b) Distillation publishing: when `observations: 0`, emit a missing-data sentinel (`null` / omitted from per_contract) rather than a numeric default. Bootstrap_reason text remains as-is.

## Acceptance criteria

- [ ] All five 0-observation series above are confirmed to have an active collector and credentials (or are explicitly out-of-scope and removed from `universal_context` entirely).
- [ ] On the next e2e invocation, at least one observation has landed for each series the system claims to collect.
- [ ] When `observations: 0`, the distillation module emits `correlation: null` / `beta: null` (or omits the numeric fields) rather than `0`. The bootstrap label remains the source of truth for calibration state.

## Verification

* `sqlite3 /Volumes/Users/jacks/AlphaMind/data/alphamind.db "SELECT name, COUNT(*) FROM <macro_observations_table> WHERE series IN ('DTWEXBGS', 'DGS10', 'GLD_REAL_YIELD', ...) GROUP BY series;"` should return non-zero counts after the fix lands.
* Re-run debug-e2e harness; spot-check that no `universal_context` entry has both `observations: 0` and a numeric (non-null) default value.

## Notes

Some `min_observations: 11 < 60` modules in the same invocation are genuinely bootstrap-explained — those are NOT in scope for this issue. Scope here is restricted to the **0-observation** series and the silent-default behavior.