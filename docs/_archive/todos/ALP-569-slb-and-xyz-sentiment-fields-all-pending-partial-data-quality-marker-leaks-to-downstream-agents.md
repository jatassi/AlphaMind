# SLB and XYZ sentiment fields all "pending" — partial-data quality marker leaks to downstream agents

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, two tickers (SLB, XYZ) have all sentiment fields set to the string `pending` in the qualitative researcher's SENTIMENT AGGREGATES block:

```
SLB: directional=pending, magnitude=pending, change=pending, vol=pending, percentile=pending, divergence=pending
XYZ: directional=pending, magnitude=pending, change=pending, vol=pending, percentile=pending, divergence=pending
```

CTRA has `divergence=pending` (single field) while other fields carry real numeric values:

```
CTRA: directional=0.07841320000000002, magnitude=0.5644332856662724, change=0.0, vol=0, percentile=0.2862296456519743, divergence=pending
```

The "pending" string is being passed through to the downstream agent input without conversion to a missing-data sentinel or annotation explaining what "pending" means. Three concrete problems:

1. **Type contract violation**: every other ticker emits numeric values for these fields. A string "pending" mixed in is a typed-data violation that downstream agents may stringly parse or silently treat as "no signal".
2. **Untyped "pending" meaning**: is "pending" = "we just added this ticker and have no history yet" / "vendor returned us pending" / "the sentiment calculation hasn't run yet for this name"? Each implies a different reason and a different next-cycle expectation.
3. **Partial vs full pending mix**: CTRA's `divergence=pending` with other fields numeric suggests the divergence calculation has its own pending-state independent of the rest. The downstream consumer needs to know whether to use the numeric fields when only one is pending.

Downstream agents in this invocation didn't trip on these (the QR response didn't surface SLB/XYZ in its threads), but the pattern is fragile.

## Evidence

`analysis/qualitative_researcher/user_message.md` lines 57, 99, 117:

```
CTRA: directional=0.07841320000000002, magnitude=0.5644332856662724, change=0.0, vol=0, percentile=0.2862296456519743, divergence=pending
SLB: directional=pending, magnitude=pending, change=pending, vol=pending, percentile=pending, divergence=pending
XYZ: directional=pending, magnitude=pending, change=pending, vol=pending, percentile=pending, divergence=pending
```

## Root cause \[hypothesis\]

Likely two sub-issues sharing a `pending` placeholder:

1. **Per-ticker bootstrap state for SLB/XYZ**: these tickers may have been recently added to the active universe (XYZ as a 2024 Square→Block rename, SLB potentially having a vendor identifier hiccup) and the sentiment ingestion pipeline hasn't accumulated a baseline yet. "pending" likely means "still bootstrapping per-ticker baseline".
2. **Per-field divergence pending for CTRA**: divergence detection requires a stable history; if CTRA's history is too thin (its ticker_deep_pull was 13 days stale per \[\[[ALP-570](https://linear.app/alphamind-jatassi/issue/ALP-570/ctra-ticker-deep-pull-data-13-days-stale-on-2026-05-19-invocation)\]\]), the divergence calculation may bail out with "pending" while other fields can still emit.

The "pending" string is a partial-data marker that should be either (a) converted to `null` / explicit sentinel matching the calibration vocabulary established by \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\], or (b) annotated with a reason ("pending: bootstrap baseline" vs "pending: insufficient history for divergence calc").

## Scope

1. Audit where in the sentiment aggregation pipeline the string `"pending"` is being emitted. Replace with the calibration-state vocabulary (`accumulating`/`unavailable` from \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\]) so partial-data conditions surface consistently.
2. For per-field pending (CTRA's divergence) vs per-ticker pending (SLB, XYZ), distinguish in the output schema. Per-field pending should still allow other fields to emit numeric values.
3. Investigate the root cause for SLB and XYZ specifically — are these recently-added tickers? Is the sentiment ingestion missing them by ticker symbol? If XYZ → Block rename is the issue, that's a vendor identifier handling bug.
4. Downstream agents (QR, synthesizer) should treat `null` / sentinel as "no signal" rather than "weak signal", consistent with the \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\] benchmark-default fix.

## Acceptance criteria

- [ ] Sentiment aggregates schema replaces string `"pending"` with explicit `null` or calibration vocabulary marker
- [ ] Per-field pending and per-ticker pending have distinguishable representations in the schema
- [ ] SLB and XYZ have explicit root-cause investigation: confirm bootstrap state vs vendor symbol issue
- [ ] Downstream agents handle missing-data sentiment consistently across SLB/XYZ pending, ETF defaults \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\], and any other "we have no signal" cases

## Verification

* Query the sentiment aggregation store directly for SLB and XYZ. Confirm whether rows exist (and have valid numeric values that the aggregator coerces to "pending"), or rows are absent (per-ticker bootstrap genuinely empty).
* Query for CTRA divergence specifically: the underlying divergence-history table should reveal whether the calculation has insufficient history vs the field-emission code defaulting to "pending".
* Inspect ticker normalization tables for XYZ — confirm whether the Square→Block rename is correctly mapped in vendor identifier lookups.
* Unit test the sentiment aggregator with three synthetic inputs: (a) ticker with full history, (b) ticker mid-bootstrap, (c) ticker with one field unreliable. Confirm each produces a distinguishable schema-level marker, not the literal string "pending".

## Notes

This is related to \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\] (default-fallback values for ETF tickers) and \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\] (calibration vocabulary). The missing-data sentinel convention should cover all three patterns:

* Bit-identical defaults for tickers without per-ticker data (\[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\])
* String "pending" placeholders (this issue)
* Calibration accumulating/unavailable for time-series modules (\[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\], landed)
