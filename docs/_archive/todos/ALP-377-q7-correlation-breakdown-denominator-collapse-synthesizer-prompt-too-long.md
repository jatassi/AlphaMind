## Summary

The 2026-05-09 end-to-end verification run failed at Phase 5 (synthesizer) with `SDKFailure: Prompt is too long`. Root cause: `_correlation_breakdown_blocks` in `src/alphamind/distillation/q7/correlation_regime_change.py` emitted 2,681 of 3,160 candidate ticker pairs (85%) as "correlation regime breakdowns", inflating the synthesizer's correlation/regime brief from a typical \~80 KB (May 3-4 baseline) to 928 KB. The pathology is a degenerate sigma denominator computed over highly-autocorrelated overlapping rolling windows; the threshold of 1.5σ then admits nearly every pair when the universe is populated. Today's run was the first end-to-end against a fully-populated 80-ticker universe — the bootstrap fix in commit `5ab842d` rehydrated `sector_classification.domain_researcher` between May 4 and May 9, which unmasked the bug.

## Diagnosis

**The bad denominator.** `q7/correlation_regime_change.py:118-195` computes the breakdown magnitude as:

```python
rolling = _rolling_pair_correlations(
    a_returns=history_a,
    b_returns=history_b,
    window=short_window_days,  # 20
)
sigma = statistics.pstdev(rolling)
magnitude = abs(short_corr - long_corr) / sigma
if magnitude < correlation_shift_sigma:  # 1.5
    continue
```

With `correlation_long_days=60` and `correlation_short_days=20` (`config/distillation.yaml:66-67`), the trailing history is 40 days of returns. Rolling 20-day correlations across that history give 21 values, where adjacent values share 19/20 = 95% of their data. The pstdev of that highly-autocorrelated series collapses to \~0.0004 in steady-state pairs, so a 0.04 absolute correlation difference becomes 100σ.

**The threshold.** `narrative_lag_correlation_shift_sigma: 1.5` in `config/distillation.yaml:59`. Combined with the collapsed denominator, this admits 85% of all universe pairs.

**Distribution evidence from today's run** (2,681 emitted breakdowns):

| Statistic | Value |
| -- | -- |
| Median deviation | 10.86σ |
| p90 | 75.71σ |
| p99 | 671σ |
| Max | 8,964σ |
| Pairs above 100σ | 198 |
| Pairs above 1,000σ | 16 |

A median of 10σ is mathematically impossible for a properly-calibrated denominator — the bulk of "breakdowns" are denominator-collapse artifacts, not real regime shifts.

**Specific pathological example.** AAPL_APA: short=−0.44, long=−0.483, deviation reported as 99.79σ. The actual correlation difference is 0.043 (statistically meaningless). It is divided by σ ≈ 0.000431 derived from rolling correlations that were essentially constant, inflating a noise-level deviation into a "very extreme" reading.

**Why today and not earlier.** Commit `5ab842d` (May 3, 2026) fixed `bootstrap.py` to populate `sector_classification.domain_researcher` (was hardcoded to empty string). Between May 4 and May 9, the prod bootstrap re-ran idempotently and rehydrated the universe to 80 eligible tickers. Today's run is the first end-to-end against a populated universe → 3,160 candidate pairs → 2,681 trip the 1.5σ bar → 928 KB correlation/regime brief → 810 KB synthesizer user_message → SDK rejects.

**Independent corroboration from the adaptive researcher.** The adaptive researcher's brief (Phase 4b) deferred 2,682 anomalies as "magnitudes reaching 21,025 are numerically impossible for any well-defined statistical measure (genuine correlation divergence metrics cap at small multiples); assessed as systematic q7 calculation engine failure requiring pipeline audit". The agent diagnosed the same pathology end-of-pipeline that the brief size symptom surfaces upstream.

**Comparison to a neighboring implementation.** `intra_sector_correlation.py:56` uses a different denominator: the cross-sectional pstdev of all sector-pair long-window correlations, computed once per sector. That construction is unaffected by today's pathology and is a relevant reference for fix Option C below.

## Files and design references

**Code.**

* `src/alphamind/distillation/q7/correlation_regime_change.py` — locus of the bug. `_correlation_breakdown_blocks` at lines 118-195; magnitude formula at line 157.
* `src/alphamind/distillation/correlation_brief.py` — brief renderer that emits one CR-N reference per breakdown block (`_decompose_correlation_breakdown_block`, lines 339-367). Has no top-N or magnitude filter at the brief layer; entirely passes through what q7 emits.
* `src/alphamind/distillation/q7/intra_sector_correlation.py` — neighboring module with a cross-sectional baseline (line 56). Reference for Option C.
* `config/distillation.yaml:58-60` — `narrative_lag_correlation_shift_sigma: 1.5` and `narrative_lag_media_silence_hours: 12`.
* `config/distillation.yaml:62-77` — persistence windows including `correlation_short_days: 20` and `correlation_long_days: 60`.

**Design docs.**

* `docs/design/02-distillation-layer/external.md` § "Correlation regime change detection (quant 7g)" at lines 156-160. The original spec for the breakdown / dispersion-shift / narrative-lag triple. The design doc says "pairwise or sector-level correlation exceeding historical norms for rate of change" — i.e., the spec leaves the choice of denominator open, and the current implementation chose a time-series rolling baseline that is statistically biased.

**Today's run artifacts.**

* `.archive/verify-pipeline-20260509/invocations/20260509T173208Z-verify-pipeline/` — full diagnostic archive including the 928 KB `stage_artifacts/correlation_regime_brief.json`, the 810 KB synthesizer `user_message.md`, and the zero-byte synthesizer `response.md` from the prompt rejection.

## Fix options

**(A) Effect-size floor plus top-N cap.**

Scope: edit `_correlation_breakdown_blocks` to require `abs(short_corr - long_corr) >= 0.15` (or similar effect-size floor) before evaluating the sigma test, then sort the surviving blocks by magnitude and cap to the top N (e.g., 30) before returning. Add the floor and the cap as `CorrelationRegimeChangeConfig` fields so they are configurable.

How it changes behavior: an absolute correlation difference below the floor fails the gate regardless of the rolling-stdev denominator, so denominator-collapse cases are filtered at their source. The top-N cap bounds the brief size deterministically.

What it does not address: the magnitudes printed in the surviving blocks remain inflated — the calculation pathology itself is unchanged, so surviving pairs still show their original deviation in σ.

Estimated effort: a few hours, including fixture regen under `tests/fixtures/distillation/` and updating the `config/distillation.yaml` schema.

**(B) Fisher z-transform with Bartlett-corrected variance.**

Scope: replace the rolling-stdev denominator with the Fisher-z null variance. Define `z_short = atanh(short_corr)` and `z_long = atanh(long_corr)`, then compute `magnitude = abs(z_short - z_long) * sqrt((N - 3) / 2)` where N is the short-window day count. Standard construction for testing whether two correlation estimates from independent samples differ at a given confidence level.

How it changes behavior: the Fisher z-transform stabilizes the variance of correlation estimates so that a fixed-effect-size test is well-calibrated regardless of the underlying correlation magnitude. The denominator no longer depends on the autocorrelated history of rolling correlations.

What it changes: every pair's magnitude is recomputed on a new scale; the existing 1.5σ threshold is meaningless on the new scale and would need recalibration (likely \~2.0-3.0σ depending on desired detection rate against historical fixtures).

Estimated effort: one to two days, including threshold recalibration against historical fixtures and a regression test that confirms a known ground-truth regime change still fires under the new test.

**(C) Cross-sectional baseline, mirroring** `intra_sector_correlation`.

Scope: replace the per-pair time-series rolling baseline with a single cross-sectional baseline — the pstdev of all pair long-window correlations across the universe — used as the denominator for every pair's magnitude. Same construction as `q7/intra_sector_correlation.py:56`, just applied to the universe rather than per-sector. Drops `_rolling_pair_correlations` entirely.

How it changes behavior: a stable denominator shared across all pairs prevents per-pair denominator collapse. "Extreme" comes to mean "much larger than the typical universe-pair correlation difference" — a different semantic from per-pair historical surprise.

Tradeoff: loses sensitivity to pair-specific historical stability. A pair with a historically tight correlation that suddenly diverged would look the same as a pair with a historically loose correlation that diverged by the same amount. Whether that matches the desired semantic is a design call.

Estimated effort: half a day, including fixture regen.

**(D) Bump the threshold from 1.5 to 5.0.**

Scope: change `narrative_lag_correlation_shift_sigma` from 1.5 to 5.0 in `config/distillation.yaml:59`. One-line config edit, no code changes.

How it changes behavior: ships in minutes with zero engineering risk; the threshold-pass count drops in proportion to the denominator distribution.

What it does not address: the underlying calculation is unchanged. Today's median deviation was 10.86σ, so a 5σ threshold still admits roughly the upper half of today's emitted pairs (\~1,300+ pairs); the brief stays at \~500 KB and the synthesizer prompt remains at risk. The magnitudes printed in the brief remain inflated regardless of the threshold.