# term_structure_basis = exactly 0.0 — VX1-VX2 futures collector likely silent, default-fallback masquerades as flat term structure

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the volatility regime block in the qualitative researcher's input shows `term_structure_basis: 0.0` — an exact zero reading.

A VIX term-structure basis of exactly 0.0 implies VX1 (front-month VIX future) and VX2 (next-month VIX future) trade at identical prices. In practice the term structure is almost always non-zero — contango (positive) is the normal state and backwardation (negative) is the stress signal. Exactly 0.0 is statistically implausible from live data and is the signature of a default-fallback path emitting `0.0` when the VX1/VX2 collector can't compute.

The synthesizer brief publishes this in the REGIME section as part of the regime characterization:

```
Underlying: VIX 17.26, term-structure basis 0, VVIX percentile 50, realized vol 0.1245
```

Downstream agents see `term-structure basis 0` and read it as "flat term structure" — a real volatility-market read — rather than as "we don't have VX1/VX2 data".

This matters because the term-structure basis is a load-bearing regime indicator: contango → vol-selling is profitable / normal market; backwardation → vol-buying is profitable / stress signal. A 0.0 default masquerades as "neither", but the underlying truth might be either.

## Evidence

`analysis/qualitative_researcher/user_message.md` line 14:

```
term_structure_basis: 0.0
```

`analysis/synthesizer/user_message.md` line 16:

```
[CR-1] vol_expansion (stable, indicator agreement 4/4)
  Prior label: vol_expansion
  Invocations held: 13
  Underlying: VIX 17.26, term-structure basis 0, VVIX percentile 50, realized vol 0.1245
```

Note the synthesizer formats it as integer `0` (truncating from `0.0`) which loses sign information even if the underlying field carried a small positive/negative real value.

## Root cause \[hypothesis\]

The term-structure basis requires VIX futures data — specifically the front-month and next-month VIX futures prices. Likely candidates:

1. **VIX futures collector silent** — VX1/VX2 are not in `data_calibration_state.json`'s `unavailable` list, so the calibration layer doesn't surface this as a known-missing module.
2. **Field initialization to 0.0 when computation skipped** — the term_structure_basis field may be initialized to 0.0 and only updated when the calculation completes; if the calculation is skipped (missing dependencies, bootstrap window), the initialized 0.0 leaks through.
3. **Reference data store contains zero** — if the underlying VIX futures table has VX1 = VX2 for all rows (an ingestion issue), the calculation correctly returns 0.0 from buggy upstream data.

## Scope

1. Confirm whether VIX futures (VX1, VX2) are being collected. Check for a futures or VIX-term-structure table in the schema, and `collector.log` for relevant ingestion events.
2. If VIX futures are unavailable, add them to `data_calibration_state.json` `unavailable` list with the \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\] vocabulary, matching the FRED DGS / dollar attribution pattern.
3. Replace the `term_structure_basis: 0.0` default with `null` when the calculation cannot run.
4. Audit the synthesizer brief's REGIME section formatting — when the field is `null`, render explicitly as `null` rather than `0`. Also preserve sign-precision on non-null real values (don't truncate `-0.42` to `0`).

## Acceptance criteria

- [ ] VIX futures collection status documented (running / unavailable / accumulating)
- [ ] `term_structure_basis` field returns `null` when calculation cannot run; never returns `0.0` as a default
- [ ] Calibration state includes the VIX-term-structure module with explicit reason when unavailable
- [ ] Synthesizer's REGIME section preserves null vs zero distinction
- [ ] Unit test for the basis calculator confirms `null` output on missing-data input

## Verification

* Query the VIX futures table (or whatever stores VX1/VX2) directly. Confirm whether rows exist for the recent window.
* Grep `collector.log` for VIX-futures collector events — confirm collector status.
* Unit test the basis calculator: pass three synthetic inputs (VX1/VX2 both present / one missing / both missing) — confirm output is `<real-basis>` / `null` / `null` respectively, never `0.0`.
* Inspect the synthesizer brief's REGIME section formatting code; confirm a `null` value renders as `null (unavailable: <reason>)` not `0`, and that a non-null `-0.42` does not truncate to `0`.

## Notes

This is a third instance of the missing-data sentinel pattern (after \[\[[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)\]\] sentiment defaults and \[\[[ALP-571](https://linear.app/alphamind-jatassi/issue/ALP-571/vvix-percentile-exactly-500-default-fallback-masquerading-as-live)\]\] VVIX percentile). All three follow the same anti-pattern: a default numeric value (0.0 / 0.7843... / 50.0) that downstream cannot distinguish from real data. The shared remedy is the \[\[[ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification)\]\] calibration vocabulary applied at the data layer + `null` propagation through the brief.
