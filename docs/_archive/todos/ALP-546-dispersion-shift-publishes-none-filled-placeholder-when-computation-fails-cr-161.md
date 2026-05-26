# dispersion_shift publishes None-filled placeholder when computation fails (CR-161)

## Symptom

The `dispersion_shift` sub-module within `q7.correlation_breakdown` publishes a placeholder entry with all fields set to `None` rather than being omitted from the output when its computation cannot run:

```
[CR-161] dispersion_shift: short None vs. long None (deviation None sigma)
  Short correlation: None
  Long correlation: None
  Deviation sigma: None
```

The synthesizer then has to ignore this entry, and downstream agents see a meaningless line in the correlation brief.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md` line 661-664:

```
[CR-161] dispersion_shift: short None vs. long None (deviation None sigma)
  Short correlation: None
  Long correlation: None
  Deviation sigma: None
```

Appears between the last correlation breakdown pair (CR-160) and the narrative_lag block (CR-162).

## Root cause hypothesis

The `dispersion_shift` computation requires inputs that weren't available this invocation (likely tied to the same intermarket / FRED data gaps in [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket)). Instead of returning an empty result and being omitted from the brief, the module returns a tuple of `Nones` that gets formatted by the standard correlation-pair formatter.

## Scope

Either:

1. Have the `dispersion_shift` module return an empty result on missing inputs; the assembler skips empty entries.
2. Use the `calibration: unavailable` sentinel (per [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)) and emit a structured "not computed this cycle" note instead of `None` fields.

Option 2 is more consistent with the broader missing-data sentinel convention being established by [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) / [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket) / [ALP-538](https://linear.app/alphamind-jatassi/issue/ALP-538/sentiment-pipeline-frozen-vol0change0-universal-benchmarks-share) / [ALP-539](https://linear.app/alphamind-jatassi/issue/ALP-539/ctra-ticker-deep-pull-returns-11-day-stale-data-while-other-tickers).

## Acceptance criteria

- [ ] When `dispersion_shift` cannot compute (missing inputs, insufficient observations), it either omits its entry from the correlation brief or emits a clearly-labeled `unavailable` entry with a reason — not a `None` placeholder.

## Verification

* Re-run debug-e2e; confirm CR-161 is either absent from the brief or labeled with an explicit unavailable reason.

## Notes

Small cleanup. Same pattern as MATH-5 (internal-consistency validation) and [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) (calibration vocabulary). Low priority because the synthesizer correctly ignored it in this invocation; impact is brief-readability rather than incorrect downstream behavior.
