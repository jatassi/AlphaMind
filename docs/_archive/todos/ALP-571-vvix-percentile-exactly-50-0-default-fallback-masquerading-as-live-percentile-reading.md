# VVIX percentile = exactly 50.0 — default fallback masquerading as live percentile reading

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the volatility regime block in the qualitative researcher's input shows `vvix_percentile: 50.0` — an exact 50.0 reading. Live percentile readings against a multi-year VVIX history are continuous-valued; landing on exactly 50.0 (to the precision shown) is statistically implausible.

This is the signature of a default-fallback path: when the VVIX percentile calculator can't compute (missing data, insufficient history, vendor failure), it emits `50.0` as a "neutral" default rather than `null` / explicit sentinel. Downstream agents see "VVIX 50th percentile" and treat it as a real "median-vol" reading; the synthesizer's REGIME section in the brief also publishes `VVIX percentile 50` as if it were live data.

This is a missing-data sentinel issue parallel to \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\] (benchmark/ETF sentiment defaults) but for the regime block.

## Evidence

`analysis/qualitative_researcher/user_message.md` line 17:

```
vvix_percentile: 50.0
```

`analysis/synthesizer/user_message.md` line 16 (in the REGIME section of the brief):

```
[CR-1] vol_expansion (stable, indicator agreement 4/4)
  Prior label: vol_expansion
  Invocations held: 13
  Underlying: VIX 17.26, term-structure basis 0, VVIX percentile 50, realized vol 0.1245
```

For comparison, the other VIX-related values are non-round and clearly live:

* `vix_level: 17.26` (real)
* `realized_vol_5d: 0.12449539190076665` (real)
* `realized_vol_20d: 0.10725599854445596` (real)

VVIX at exactly 50.0 stands out.

## Root cause \[hypothesis\]

The VVIX percentile calculation requires VVIX time-series history (likely from CBOE or vendor), sufficient observations to compute a percentile rank, and a current VVIX reading. If any of these is missing, the calculation falls back to 50.0 ("centered / no signal"). The fallback should instead emit `null` per the missing-data sentinel convention.

Likely candidates:

* VVIX collector silent (similar to dollar attribution and FRED DGS in `data_calibration_state.json` — listed as `unavailable` there). VVIX should be in the calibration state as `unavailable` if so.
* VVIX collector running but insufficient observations to percentile-rank. Should be `accumulating` per the \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\] vocabulary.
* VVIX history exists but the percentile computation has a bug returning a constant 50.0.

## Scope

1. Confirm whether VVIX is being collected. Check the schema for a VVIX time-series table, and `collector.log` for VVIX ingestion events.
2. If VVIX is unavailable, surface it explicitly: add VVIX to the calibration state's `unavailable` block matching the FRED DGS / dollar attribution pattern from \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\].
3. Replace the `vvix_percentile: 50.0` default with `null` when the calculation can't run, and propagate `null` through the synthesizer brief's REGIME section so downstream agents see explicit missing-data signal.
4. Apply the same fix-pattern to other regime-block fields that may have default-fallback paths.

## Acceptance criteria

- [ ] VVIX collection status documented (running / unavailable / accumulating)
- [ ] `vvix_percentile` field returns `null` when calculation cannot run; never returns `50.0` as a default
- [ ] Calibration state includes VVIX module with explicit `accumulating` / `unavailable` reason when applicable
- [ ] Synthesizer's REGIME section in the brief shows `VVIX percentile null (unavailable: <reason>)` rather than a number when the field is missing
- [ ] Unit test for the percentile calculator confirms `null` output on insufficient-history input

## Verification

* Query the VVIX time-series table directly. Confirm whether rows exist and how many. If zero or fewer than the percentile threshold, the bootstrap-window expectation should match.
* Grep `collector.log` for VVIX ingestion events — confirm collector status.
* Unit test the percentile calculator: pass three synthetic histories (full / accumulating / unavailable) — confirm output is `<real-percentile>` / `null` / `null` respectively, never `50.0`.
* Inspect the regime-block assembler in code; confirm it propagates `null` from the calculator into the assembled YAML rather than defaulting to 50.0.

## Notes

This is a specific instance of the broader missing-data sentinel pattern that the \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\] calibration vocabulary establishes. Related to \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\] (sentiment defaults) and \[\[[ALP-572](https://linear.app/alphamind-jatassi/issue/ALP-572/term-structure-basis-exactly-00-vx1-vx2-futures-collector-likely)\]\] (term_structure_basis = 0.0 default fallback). All three follow the same anti-pattern: a default numeric value that downstream cannot distinguish from real data.
