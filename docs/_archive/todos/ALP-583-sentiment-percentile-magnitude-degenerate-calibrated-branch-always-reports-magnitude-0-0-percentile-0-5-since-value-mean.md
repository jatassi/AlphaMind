# Sentiment percentile/magnitude degenerate — calibrated branch always reports `magnitude=0.0, percentile=0.5` since `value == mean`

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, every CALIBRATED ticker in `SENTIMENT AGGREGATES` shows `magnitude=0.0, percentile=0.5` regardless of underlying sentiment. Examples (from `analysis/qualitative_researcher/user_message.md`):

```
AAPL: directional=0.2858037746478873, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
AMD:  directional=0.36915613636363637, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=True
CSCO: directional=0.2724443548387097, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
GOOG: directional=0.3717430975609756, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
JPM:  directional=0.18565454545454543, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
META: directional=0.24958281249999995, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
NVDA: directional=0.20150518691588784, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=False
TSLA: directional=0.19359092592592592, magnitude=0.0, change=0.0, vol=0, percentile=0.5, divergence=True
```

`directional_score` varies per ticker (it's the raw `row.mean`), but `magnitude` and `percentile_vs_self` are bit-identical placeholders across every CALIBRATED ticker — the same "shared placeholder masquerading as signal" failure mode that [ALP-538](https://linear.app/alphamind-jatassi/issue/ALP-538/sentiment-pipeline-frozen-vol0change0-universal-benchmarks-share) / [ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf) were filed to eliminate, just in a different bucket.

## Root cause

`src/alphamind/analysis/qualitative_research/loaders.py:553-562`. In the CALIBRATED branch:

```python
mean = float(row.mean)
stdev = float(row.stdev)

percentile = _percentile_from_normal(value=float(row.mean), mean=mean, stdev=stdev)
magnitude = min(1.0, abs(float(row.mean) - mean) / stdev) if stdev > 0 else 0.0
```

`value == mean` by construction, so:

* `z = (row.mean - row.mean) / row.stdev = 0` → `percentile = Phi(0) = 0.5`.
* `abs(row.mean - row.mean) / stdev = 0` → `magnitude = 0.0`.

Pre-[ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf), the ACCUMULATING branch substituted `(pool_mean, pool_stdev)` for `(mean, stdev)`, producing non-degenerate (but bit-identical) pool-fallback values for sub-threshold tickers. With [ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf) the ACCUMULATING branch now emits null sentinels, but the CALIBRATED-branch degeneracy is still present — surfaced more cleanly now that the pool path no longer masks it.

## Scope

Decide what `percentile_vs_self` and `magnitude` should actually measure for a CALIBRATED ticker, then wire the math to a non-degenerate comparator. Candidates for "the value being percentile-d":

1. **Latest period's mean** — compute the mean of articles published in the inter-baseline window (separate from the rolling baseline mean). Percentile that against the trailing distribution `(row.mean, row.stdev)`.
2. **Prior-row mean** — `value = recent[1].mean, distribution = (row.mean, row.stdev)`. Asks "how unusual was the prior period given the current distribution".
3. **Cross-sectional percentile** — percentile `row.mean` against the universe-pooled `(mean, stdev)` at this point in time. Loses "vs self" semantics but gives a meaningful number.

The choice depends on what the LLM should read in the rendered bundle. The current docstring says "percentile of the value against (mean, stdev)" but doesn't define "value" — design needed.

## Acceptance criteria

- [ ] CALIBRATED tickers emit `magnitude` and `percentile_vs_self` values that vary per ticker (not bit-identical across the universe)
- [ ] The docstring on `SentimentAggregate.percentile_vs_self` / `magnitude` states what is being percentile-d / measured
- [ ] Unit test: two CALIBRATED tickers with different `row.mean` values must produce different `magnitude` and `percentile_vs_self`

## Verification

* Query the rendered `SENTIMENT AGGREGATES` block from a live invocation and confirm calibrated tickers no longer cluster at `magnitude=0.0, percentile=0.5`.
* Unit test the loader with two distinct CALIBRATED rows — assert their `magnitude` / `percentile_vs_self` values differ.

## Notes

Discovered during [ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf) PR review (PR #117). Explicitly out of scope for [ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf), which targets the ACCUMULATING/UNAVAILABLE null-sentinel path. This bug is pre-existing; [ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf) makes it the steady-state behavior for every calibrated ticker by removing the pool-fallback path that previously masked the degeneracy for sub-threshold rows.
