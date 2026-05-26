# Sentiment pipeline frozen — change/vol universally zero, benchmark/ETF tickers share identical default-fallback values

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the SENTIMENT AGGREGATES block in the qualitative researcher input shows two clear default-fallback patterns:

1. **Universal change/vol freeze**: every ticker in the universe (76 of 76) has `change=0.0, vol=0`. Sentiment momentum tracking and volume tracking are completely flat across the entire universe.
2. **Benchmark/ETF default-fallback values**: 14 benchmark and ETF tickers (GLD, HYG, IEF, IWM, QQQ, RSP, SMH, SOXX, SPY, TLT, USO, XLE, XLF, XLK) all share **bit-identical** sentiment values: `directional=0.0`, `magnitude=0.7843835113206449`, `change=0.0`, `vol=0`, `percentile=0.21640755526283445`, `divergence=False`.

Bit-identical magnitude and percentile values across 14 distinct tickers is not statistically plausible from real data — this is a default-fallback path being hit for tickers without per-ticker sentiment ingestion. The default `magnitude=0.7843...` corresponds to a 21.6th percentile reading, which is what downstream agents will interpret as "modestly weak sentiment" for these ETFs rather than "we have no data".

The qualitative researcher noted the issue obliquely in its response: "zero change and volume readings across all names reflect a quiet overnight Asian session or stale sentiment feed — the directional scores and percentiles are informative but carry a staleness caveat." But the agent's reading is one of two possibilities; the bit-identical defaults across 14 ETF tickers point to "stale sentiment feed / default-fallback path" being the actual cause.

## Evidence

`analysis/qualitative_researcher/user_message.md` lines 39-118 — all 76 sentiment rows. Selected lines showing the bit-identical pattern for ETF/benchmark tickers:

```
GLD: directional=0.0, magnitude=0.7843835113206449, change=0.0, vol=0, percentile=0.21640755526283445, divergence=False
HYG: directional=0.0, magnitude=0.7843835113206449, change=0.0, vol=0, percentile=0.21640755526283445, divergence=False
IEF: directional=0.0, magnitude=0.7843835113206449, change=0.0, vol=0, percentile=0.21640755526283445, divergence=False
... (14 ETF/benchmark tickers all bit-identical)
```

Universal change/vol freeze: every ticker (single-name and ETF alike) shows `change=0.0, vol=0`.

## Root cause \[hypothesis\]

Two distinct sub-bugs:

1. **Benchmark/ETF default-fallback** — single-name tickers in the active universe (AAPL, NVDA, etc.) get per-ticker sentiment from the news ingestion pipeline. ETFs and benchmarks don't have per-ticker news the same way, so they fall back to a default. The default emits a non-null magnitude (`0.7843...`) and percentile (`0.21640...`) instead of `null` / `pending` / missing-data sentinel.
2. **Change/vol freeze** — sentiment delta tracking (vs prior invocation) is failing globally. This may share the root cause with \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] (news pipeline failure at 03:06 UTC) — if no new headlines were ingested in the 24h window, then sentiment didn't change vs prior. But the magnitude of the freeze (every single ticker shows exactly 0.0 change, 0 vol) is stronger than "no headlines this window" should produce — there should be at least some between-invocation drift.

## Scope

1. Replace the benchmark/ETF default-fallback (`magnitude=0.7843835113206449`, `percentile=0.21640755526283445`) with explicit `null` / missing-data sentinel that downstream agents recognize as "no per-ticker sentiment" rather than "modestly weak sentiment".
2. Investigate the universal change/vol freeze — likely related to \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] but verify independently. If change/vol is computed from the news pipeline that's empty, the dependency should be made explicit and the field should be `null` rather than `0.0` when upstream is empty.
3. Document the per-ticker sentiment-source contract: which tickers do/don't have per-ticker sentiment ingestion, and what the missing-data behavior is.
4. Audit `directional=0.0` for benchmark/ETFs — is 0.0 the correct "no signal" value, or is it ambiguous with "weakly bearish"?

## Acceptance criteria

- [ ] Benchmark/ETF tickers without per-ticker sentiment ingestion emit `null` (or explicit sentinel) rather than the `0.7843835113206449` magnitude default
- [ ] Sentiment change/vol fields explicitly carry `null` when the upstream pipeline has no new data, distinguishable from genuine `0.0` change
- [ ] Downstream agents (QR, synthesizer, analyst, strategist) handle `null` sentiment correctly (treat as "no signal" not "weak signal")
- [ ] Unit test confirms ETF rows distinct from single-name rows in the sentiment aggregator output

## Verification

* Query the sentiment aggregation table/store directly for the 14 ETF tickers (GLD, HYG, IEF, IWM, QQQ, RSP, SMH, SOXX, SPY, TLT, USO, XLE, XLF, XLK). Confirm rows are either absent or carry an explicit null/sentinel — not bit-identical numeric defaults.
* Unit test the sentiment aggregator with two inputs: (a) ticker with per-ticker news data, (b) ticker without per-ticker news data. Confirm (b) returns null/sentinel, not `magnitude=0.7843...`.
* Inspect the input bundle assembler that produces SENTIMENT AGGREGATES. Verify it preserves null values from the aggregator without coercing to 0.0.

## Notes

This connects to \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] (news pipeline) and the broader missing-data sentinel convention (\[\[[ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket)\]\]/538/539 family from the seed run). The composite signal-quality story across these three issues is: when upstream data is missing, every downstream computation should emit `null` / explicit sentinel, not a default numeric value that masquerades as real signal.
