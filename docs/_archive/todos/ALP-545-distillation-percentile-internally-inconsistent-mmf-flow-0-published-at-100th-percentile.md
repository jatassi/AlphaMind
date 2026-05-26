# Distillation percentile internally inconsistent — mmf_flow=0 published at 100th percentile

## Symptom

The `q6.funding_stress` distillation module published `mmf_flow: 0` (raw value) alongside `mmf_flow: 100` (percentile rank) for the same component in the same invocation. A raw value of 0 at the 100th percentile of its bootstrap distribution is only possible if every other observation in the distribution was negative — implausible for a flow metric, and inconsistent with the other three components having non-zero raw values at the same 100th percentile.

Adaptive researcher `[AR-4]` independently identified this as a data-quality issue (`analysis/synthesizer/user_message.md` line 2473):

> *"\[SA-FIN-ANOM-1\] documents mmf_flow = 0 at 100th percentile — internally inconsistent; a zero inflow reading at the extreme tail of a distribution is more likely a data coding issue (null/default recorded as 0) than a genuine market condition"*

Most likely interpretations:

1. **Missing-data sentinel masquerading as real value**: the collector wrote `0` when it meant `null`, and the percentile-rank computation treated `0` as a real observation tied with everything else (collapses to top of the rank).
2. **Percentile computation bug**: when all observations have the same value (e.g., `[0, 0, 0, 0, 0, ...]`), every observation reports 100th percentile by some convention.

Either way, this is an internal-consistency violation that should be caught before publishing.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md` line 673-688:

```
### q6.funding_stress | freshness ... | bootstrap — bootstrap_reason: funding_stress_min_observations: 11 < 60
alert_active: True
component_percentiles:
  mmf_flow: 100
  repo_treasury_spread: 100
  sofr_ois_spread: 100
  term_repo_premium: 100
components:
  mmf_flow: 0                  <-- raw value 0
  repo_treasury_spread: 0.647  <-- raw values for others all non-zero
  sofr_ois_spread: 3.56
  term_repo_premium: 2.76
```

The other three components have non-zero raw values; only mmf_flow has the suspicious 0-at-100th pattern.

## Root cause hypothesis

Either:

1. The mmf_flow collector writes `0` rather than `null` when the upstream data source returns no value. The percentile-rank computation can't distinguish "true zero flow" from "no data", and ranks `0` against the available distribution (which probably contains lots of other zeros from earlier collection gaps), producing the 100th-percentile artifact.
2. The percentile-rank computation uses `>=` rather than `>` in its rank counting, so a value tied with the maximum gets rank = 100% even if it's also tied with everything else.
3. The bootstrap distribution has too few observations (11) to produce meaningful percentiles, and the percentile is calling the default code path (return 100 for "above threshold" / "no data to compare").

## Scope

Add an internal-consistency check to the distillation publishing layer:

1. If `raw_value == 0` AND `percentile == 100`, mark the component as `data_quality: suspect` and exclude it from the composite calculation.
2. If all observations in the bootstrap distribution are identical (zero variance), report the percentile as `null` (sentinel) rather than 100.
3. Audit the mmf_flow collector to confirm it emits `null` rather than `0` when the upstream is missing.

Related: [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket) (silent-default in macro collectors) and [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) (calibration vocabulary).

## Acceptance criteria

- [ ] `q6.funding_stress` no longer publishes a component with `raw_value: 0, percentile: 100` simultaneously — either the percentile is `null` or the value is excluded from the composite.
- [ ] Internal-consistency check is applied uniformly across modules that publish (raw_value, percentile) pairs.
- [ ] mmf_flow collector emits `null` when the upstream returns no value (audit step).

## Verification

* Re-run debug-e2e; confirm no module emits a (raw=0, pct=100) pair.
* Confirm the funding_stress composite recomputes without the mmf_flow component if it was excluded for data quality.

## Notes

Likely related to [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket)'s "silent default vs explicit null sentinel" theme and [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)'s "operator-facing calibration transparency" theme. The fix here is module-internal validation, but the broader convention should align with both.
