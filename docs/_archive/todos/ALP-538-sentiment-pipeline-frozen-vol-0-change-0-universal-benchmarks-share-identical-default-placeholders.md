## Symptom

The per-ticker sentiment aggregates fed to QR show two distinct freeze patterns:

1. **Universal time-series collapse:** every single ticker (80+) reports `vol=0` and `change=0`, regardless of the underlying directional value. A genuine sentiment feed should show dispersion in volatility and non-zero change deltas across at least some names.
2. **Benchmark/stub placeholders:** the broad-market ETFs (SPY, QQQ, IWM, RSP), sector ETFs (XLE, XLF, XLK, SMH, SOXX), intermarket benchmarks (GLD, HYG, IEF, TLT, USO), and **XYZ** (an in-universe tech-sector ticker) all share *exactly identical* placeholder values: `directional=0.0, magnitude=0.784233967478751, change=0.0, vol=0, percentile=0.2164514186941695, divergence=False`. These values look like a single fallback default being broadcast to anything the pipeline doesn't have real data for.

QR independently diagnosed both patterns (`analysis/qualitative_researcher/response_initial.md` line 14):

> *"all 80+ sentiment tickers show vol=0 and change=0.0 universally, with ETFs carrying identical artifact values (directional=0.0, magnitude=0.784..., percentile=0.216...), indicating a pipeline freeze rather than genuine readings"*

XYZ (Block, formerly Square) is in the active universe (`sectors.tech` in `resolved_config.json`) but received the benchmark default value. That suggests the fallback path catches anything where the lookup returns no rows, regardless of whether it's actually a benchmark.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/qualitative_researcher/user_message.md`:

* Lines 39-118: all 80+ tickers, every single one `vol=0, change=0.0`.
* Tickers with identical benchmark-default values (lines 62, 66, 68, 71, 95, 96, 99, 100, 102, 103, 106, 111, 114, 115, 116, 117): GLD, HYG, IEF, IWM, QQQ, RSP, SLB(!), SMH, SOXX, SPY, TLT, USO, XLE, XLF, XLK, XYZ — note SLB is in the energy sector (not a benchmark) but still got defaulted.

Reproducibly:

```
GLD: directional=0.0, magnitude=0.784233967478751, change=0.0, vol=0, percentile=0.2164514186941695, divergence=False
XYZ: directional=0.0, magnitude=0.784233967478751, change=0.0, vol=0, percentile=0.2164514186941695, divergence=False
```

— bit-for-bit identical, including the trailing-decimal magnitude (`0.784233967478751`) and percentile (`0.2164514186941695`).

## Root cause hypothesis

1. **vol/change collapse:** The sentiment aggregation likely requires a time-series of prior snapshots to compute vol and change deltas; if the prior-snapshot lookup is mis-keyed (wrong invocation ID, wrong window) then every name reports `0` rather than `null`.
2. **Benchmark fallback:** A `get_sentiment_or_default(ticker)` style helper is returning a hard-coded default tuple whenever the lookup misses, and the lookup is missing on benchmark tickers (no headline coverage), SLB (sparse coverage), and XYZ (recently added or sparse coverage). The default values look like the average baseline from earlier training data, not a sentinel.

## Scope

(a) Restore the time-series for vol/change computation; if there is insufficient history, emit `null` rather than `0`.

(b) Replace the hardcoded benchmark-default tuple with an explicit missing-data sentinel (`null` fields, `divergence: pending` or `divergence: null`), so downstream agents can distinguish "no sentiment data" from "neutral sentiment data."

## Acceptance criteria

- [ ] On a fresh invocation with at least 2 prior snapshots in storage, at least some tickers show non-zero `vol` and non-zero `change`.
- [ ] No two non-related tickers (e.g., GLD and XYZ) share bit-identical placeholder values.
- [ ] Tickers with no sentiment data emit explicit null/missing fields, not numeric defaults.

## Verification

* Diff sentiment aggregates across two consecutive invocations — at least the `change` field should differ for any ticker with new headlines.
* Confirm SLB and XYZ either have real sentiment data or are marked explicitly missing.

## Notes

Some `vol=0` reads may have a bootstrap explanation if the system genuinely has fewer than N prior snapshots. The identical-placeholder pattern across benchmarks + XYZ is NOT bootstrap-explained (the values are too specific to be coincidence) and must be fixed regardless. Recommend tackling the placeholder issue first.