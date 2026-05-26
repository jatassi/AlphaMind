# q7.breadth_internals: pct_above_50d_ema and pct_above_200d_ema are bit-identical (0.6515) — different windows producing same value

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the `q7.breadth_internals` module emits:

```
pct_above_200d_ema: 0.6515
pct_above_20d_ema: 0.6212
pct_above_50d_ema: 0.6515
```

Two of the three breadth measures (`pct_above_50d_ema` and `pct_above_200d_ema`) are bit-identical at 0.6515. The 20d measure is distinct at 0.6212.

The 50d and 200d EMAs are different smoothing windows over the same price series; they will rarely yield identical "% of universe above EMA" counts. With an active universe of 66 single-name tickers, 0.6515 ≈ 43/66 = 0.6515151... — so it appears 43 of 66 tickers are above BOTH the 50d and 200d EMA. That's plausible but coincidental.

The synthesizer brief publishes all three values in its UNIVERSAL CONTEXT block, where downstream agents read them as distinct breadth measures.

## Evidence

`analysis/synthesizer/user_message.md` lines 132-134:

```
pct_above_200d_ema: 0.6515
pct_above_20d_ema: 0.6212
pct_above_50d_ema: 0.6515
```

The `accumulating` calibration tag for `q7.breadth_internals` (line 114, `breadth_long_ema_observations: 128 < 200`) explains why the 200d window is undertrained, but it doesn't explain why the 50d window (which presumably has 128 observations available — more than 50d worth) yields identical output to the 200d.

For comparison: with 66 tickers, possible bit-identical-fraction collisions are at N/66 values like 43/66 = 0.6515. A genuine match would require BOTH the 50d-EMA-above count and the 200d-EMA-above count to land at exactly 43 — possible but suspicious.

## Root cause \[hypothesis\]

Three candidate root causes:

1. **Both 50d and 200d measures computed from the 200d EMA series**: If the code that reads "above 50d EMA" actually reads "above 200d EMA" due to a variable misnaming or column-confusion bug, both measures degenerate to the same value.
2. **Bootstrap-window initialization**: If the 50d EMA is initialized to the 200d EMA's value when 50d observations are below threshold, they remain identical until enough 50d observations accumulate. The calibration state lists `breadth_long_ema_observations: 128 < 200`, which is the 200d threshold — but a 50d window should have crossed its 50-observation threshold by now.
3. **Genuine coincidence (43 of 66 tickers above both EMAs)**: Possible if the active universe is in a trending regime where the 50d and 200d EMAs are close enough that the same set of tickers crosses both.

Hypothesis 1 (variable confusion) and hypothesis 2 (bootstrap-window 50d-falls-back-to-200d) are most consistent with the exact-match pattern.

## Scope

1. Audit the q7.breadth_internals code that computes `pct_above_50d_ema` and `pct_above_200d_ema`. Confirm they read from distinct EMA columns/series.
2. If a bootstrap-window fallback exists where the 50d falls back to the 200d, document and surface it explicitly in the module's calibration reason.
3. Add a unit test with a synthetic universe where 50d and 200d EMAs are known to diverge — confirm the output values are distinct.
4. Verify whether the 50d EMA's observation count is independently calibrated; if `breadth_long_ema_observations: 128 < 200` is shared across both 50d and 200d, split into separate fields.

## Acceptance criteria

- [ ] `pct_above_50d_ema` and `pct_above_200d_ema` are computed from distinct EMA series and produce distinct output values when the underlying EMAs differ
- [ ] If a bootstrap-window fallback degenerates one to the other, the calibration reason surfaces this explicitly
- [ ] Unit test with synthetic divergent EMAs produces distinct breadth values

## Verification

* Unit test the breadth calculator with a synthetic universe of N tickers where 50d EMAs and 200d EMAs are constructed to differ (e.g., trending up: 50d-EMA > 200d-EMA for all tickers). Confirm `pct_above_50d_ema` and `pct_above_200d_ema` differ in the output.
* Read the code path; verify `pct_above_50d_ema` reads from the 50d-EMA column/series, not the 200d-EMA one.
* Query the underlying EMA table for the 66 active universe tickers. Compute `pct_above_50d_ema` manually and compare against the module's output. Same for 200d.
* Inspect the calibration-state assembler to confirm 50d and 200d observation counts are tracked separately.

## Notes

This is bootstrap-window data (`breadth_long_ema_observations: 128 < 200`), so the bit-identical pattern may resolve naturally once observations cross threshold. Worth filing now because (a) the 50d measure should have crossed its own threshold, and (b) the underlying computation should produce distinct values regardless of accumulation state.
