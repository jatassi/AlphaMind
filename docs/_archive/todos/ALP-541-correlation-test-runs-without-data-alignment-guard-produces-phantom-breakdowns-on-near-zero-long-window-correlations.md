## Symptom

The correlation-breakdown σ-test in `q7.correlation_breakdown.*` computes `(short_corr - long_corr) / sigma` and publishes pairs with deviation ≥ 3σ as `investigate_now` flags. The test has no precondition guard for:

1. **Insufficient overlapping observations** between the two tickers' return histories.
2. **Long-window correlation magnitude below the noise floor** — when `|long_corr|` is \~0, any short-window deviation looks like a "breakdown" against zero, but the long-window baseline is itself meaningless.

The result is phantom breakdowns where there is no real divergence to flag — just sparse or non-overlapping data.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md`:

* Line 261: `[CR-61] CTRA_TSLA: short 0.7833 vs. long 0 (deviation 3.597 sigma)` — long-window correlation is **exactly zero**, indicating CTRA and TSLA's return histories don't overlap in the long window (or one is brand-new in the universe). Flagged at 3.597σ.
* Line 273: `[CR-64] C_GOOGL: short 8.881e-05 vs. long 0.3955 (deviation 3.099 sigma)` — short-window correlation is `8.881e-05` (essentially zero from a numerical-precision-floor calculation, likely 1 / N²). Long-window correlation is plausible at 0.3955.
* Line 263: `[CR-61] CTRA_TSLA` is also exemplary because TSLA is a tech name and CTRA is energy — they shouldn't correlate strongly in either direction; the short-window 0.7833 reading is itself an artifact of small-N coincidence.

Pattern is widespread: at least a dozen pairs in CR-15..CR-160 show either `long ≈ 0` or `short ≈ 0` with σ ≥ 3.

The adaptive researcher independently noticed but didn't flag the alignment issue at scale — it just routed META-locus and CSCO investigations.

## Root cause hypothesis

The σ-test denominator `sigma` is estimated under an assumption that both windows have approximately the configured number of observations (20-day short, 60-day long). When either window has fewer overlapping return observations than expected (new ticker, sparse history, holiday alignment), the variance estimate is wrong, and the σ math produces inflated deviations.

Compounding: when `|long_corr|` is below the test's noise floor, the deviation `(short - long)` is dominated by the short-window noise rather than a genuine shift in joint dynamics — there's no joint dynamics to shift away from.

## Scope

Add two preconditions before publishing a correlation breakdown flag for a pair:

1. **Observation count guard**: require `min(overlapping_returns_short, overlapping_returns_long) >= configured_minimum` (e.g., 90% of window length). Pairs failing this are emitted as `calibration: insufficient_data` (or whichever sentinel [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) establishes) rather than as a σ-flag.
2. **Magnitude noise-floor guard**: require `|long_corr| >= correlation_noise_floor` (e.g., 0.05) before computing the deviation σ. Pairs below the floor are emitted as `calibration: insufficient_baseline`.

Both guards should be configurable in `alphamind.config` rather than hard-coded.

## Acceptance criteria

- [ ] CTRA_TSLA (`long 0`), C_GOOGL (`short 8.881e-05`), and similar pairs no longer appear in the `correlation_breakdown` flag stream as `investigate_now`; they are either filtered or surfaced under an `insufficient_data` / `insufficient_baseline` label.
- [ ] The total `correlation_breakdown_flag` count drops materially (expected: 30-50% reduction even before MATH-2 multiple-comparison correction).
- [ ] A pair's flag includes the overlapping observation count and the long-window magnitude so downstream agents can audit the calculation.

## Verification

* Re-run debug-e2e; spot-check that no published correlation breakdown has `long_correlation ≈ 0` or `short_correlation < 1e-3`.
* Confirm the total `INVESTIGATE NOW` correlation_breakdown count is meaningfully lower than the 148 in inv-20260518T111140Z.

## Notes

This is the first of three coordinated fixes in the correlation breakdown pipeline: data-alignment guard (this issue), multiple-comparison correction (MATH-2), and locus aggregation (MATH-3). All three can ship independently; combined they should reduce 146 flags to a handful of high-conviction, locus-aggregated, multiplicity-corrected signals.