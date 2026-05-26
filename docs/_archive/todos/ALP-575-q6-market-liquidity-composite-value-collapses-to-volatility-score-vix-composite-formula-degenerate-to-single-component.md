# q6.market_liquidity composite_value collapses to volatility_score = VIX — composite formula degenerate to single component

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the `q6.market_liquidity` module emits:

```
### q6.market_liquidity | freshness 2026-05-19T03:06:54.597572+00:00 | accumulating — reason: market_liquidity_min_observations: 12 < 60
alert_active: False
components:
  credit_spread_score: 0.76
  stress_index_score: -0.7584
  volatility_score: 17.26
composite_value: 17.26
percentile_60d: null
```

The `composite_value: 17.26` is **bit-identical** to the `volatility_score: 17.26` component, which itself is identical to the VIX level. The composite is supposed to be a multi-component blend of `credit_spread_score`, `stress_index_score`, and `volatility_score`, but the formula is degenerating to the single volatility_score component.

Two possibilities:

1. The composite formula literally is `composite_value = volatility_score` (weights set to 0/0/1 across components) — a configuration bug.
2. The formula is a sum/weighted-sum but `credit_spread_score` (0.76) and `stress_index_score` (-0.7584) numerically cancel each other (0.76 + (-0.7584) ≈ 0.0016) leaving volatility_score dominant.

Either way, downstream agents see `composite_value: 17.26` and interpret it as a real composite reading, when it's effectively just the VIX number.

## Evidence

`analysis/synthesizer/user_message.md` lines 105-112:

```
### q6.market_liquidity | freshness 2026-05-19T03:06:54.597572+00:00 | accumulating — reason: market_liquidity_min_observations: 12 < 60
alert_active: False
components:
  credit_spread_score: 0.76
  stress_index_score: -0.7584
  volatility_score: 17.26
composite_value: 17.26
percentile_60d: null
```

Note: `credit_spread_score (0.76) + stress_index_score (-0.7584) + volatility_score (17.26) = 17.2616`, NOT 17.26. If the composite were a simple sum, it would be 17.2616 (or 17.26 truncated). If it's an average of 3, it would be 5.75 (\~17.26/3). The composite_value exactly equals volatility_score suggests the formula is `composite_value = volatility_score`, not a multi-component blend.

## Root cause \[hypothesis\]

The composite formula likely has one of three issues:

1. **Hardcoded weights set to (0, 0, 1)** for (credit_spread_score, stress_index_score, volatility_score) — possibly during bootstrap-window initialization with the expectation that weights would be calibrated once observations cross threshold.
2. **Component normalization not applied**: `volatility_score: 17.26` is on the VIX scale (10-50 typical), while `credit_spread_score: 0.76` is on a z-score-like scale (-3 to +3 typical) and `stress_index_score: -0.7584` similarly. If the composite is a sum WITHOUT normalization, volatility_score dominates by magnitude.
3. **Composite computed from a different code path** that bypasses the component dict — the dict shown is for visibility only, and the composite reads volatility_score directly from the underlying VIX series.

Hypothesis 2 (missing normalization) is most consistent with the visible numbers.

## Scope

1. Audit the q6.market_liquidity composite formula. Confirm whether components are normalized before blending.
2. If normalization is missing, add z-score or scaled normalization so all three components contribute on comparable scales.
3. If the bootstrap-window behavior intentionally uses single-component fallback, document this in the module's calibration reason and emit a separate flag (e.g., `composite_method: volatility_only_bootstrap`) so downstream agents know the composite isn't a true multi-component blend.
4. Add a unit test confirming that with three non-zero, non-cancelling components, `composite_value` is distinct from any single component.

## Acceptance criteria

- [ ] q6.market_liquidity composite_value is NOT identical to any single component value (volatility_score in this case) in non-degenerate input cases
- [ ] If the bootstrap-window uses a single-component fallback, the module surfaces this explicitly in the calibration reason or a `composite_method` field
- [ ] Test case with three known component values produces a composite distinct from any single component
- [ ] Component normalization documented and applied consistently

## Verification

* Unit test the q6.market_liquidity composite calculator with synthetic values: `credit_spread_score=1.0, stress_index_score=-1.0, volatility_score=20.0`. The composite_value should NOT equal 20.0; it should reflect the multi-component blend (whatever the intended formula is).
* Read the composite formula code path. Confirm whether component normalization is applied and what the weights are.
* Inspect the module's documentation / design doc to confirm the intended formula matches the implementation.
* Manually compute the composite from the three components in the current invocation; confirm the result matches whatever formula the code implements.

## Notes

This is bootstrap-window accumulating data (12 < 60 observations), so the magnitude of impact on downstream decisions is limited this invocation. But the formula degeneracy will persist when the module crosses out of bootstrap — at which point downstream agents will be treating "VIX with extra steps" as a real composite reading.
