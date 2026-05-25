## Symptom

The `bootstrap` calibration label in distillation publishing currently means two semantically opposite things:

1. **Accumulating** — collector is healthy and writing; we just don't have enough history yet to reach calibrated status. Time will fix it.
2. **Unavailable** — collector isn't running, isn't authed, vendor is returning errors, or required data series simply isn't being captured. Time will NOT fix it; operator action required.

Both states currently emit `calibration: bootstrap` with a free-text `bootstrap_reason` that the operator must read and parse to figure out which kind they're in. The e2e verification harness doesn't elevate either state — it completes successfully and the operator has to manually grep the synthesizer's `user_message.md` to discover collector failures.

The conflation is dangerous because:

* The system **looks** like it's working — green run, no errors.
* "Bootstrap" implies "give it time" — operator assumes the system will self-heal.
* Actual collector failures (DTWEXBGS, FRED DGS, intermarket beta series) have been silently masquerading as bootstrap for an unknown duration.
* Future e2e runs will inherit the same blind spot — exactly the bootstrap-context concern the operator raised after this review.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

Same `bootstrap` label, very different underlying states (`analysis/synthesizer/user_message.md`):

```
### q6.dollar_attribution | freshness 2026-05-18T11:11:40.552373+00:00 | bootstrap — bootstrap_reason: dollar_attribution: DTWEXBGS history unavailable
### q6.funding_stress | freshness 2026-05-18T11:11:40.552373+00:00 | bootstrap — bootstrap_reason: funding_stress_min_observations: 11 < 60
### q7.intermarket_regime.gld_real_yields | freshness 2026-05-18T11:11:40.552373+00:00 | bootstrap — bootstrap_reason: gld_real_yields_observations: 0 < 60
```

Manual triage required to determine:

* `dollar_attribution` is **unavailable** (FRED collector down or unauthed — needs operator action).
* `funding_stress` is **accumulating** (11/60 obs, healthy, will reach calibrated as data lands).
* `gld_real_yields` is **unavailable** (0 obs, intermarket collector not running — needs operator action).

Plus the operator-surface gap:

* `progress.jsonl` shows phases completing successfully with no data-health summary.
* `data_calibration_state.json` exists at the invocation root but is `{}` empty — scaffolding present, never populated.
* No "data health" line in the harness stdout or exit summary.
* The only way to discover the 5 unavailable series is to read the 192KB synthesizer user_message manually.

The bootstrap-context concern from the operator after the 2026-05-18 review:

> *"the system should be more transparent about this. The difference between 'we haven't collected enough data yet' and 'this isn't working' should be extremely obvious to the operator and surface as part of e2e verification."*

## Scope

**Layer 1 — Calibration vocabulary (distillation publishing):**

Split the current `calibration: bootstrap` label into three explicit states:

| Label | Meaning | Operator action |
| -- | -- | -- |
| `calibrated` | Sufficient history; signal is reliable | None |
| `accumulating` | Collector healthy, observations < required minimum | Wait (system will self-heal as data lands) |
| `unavailable` | Zero observations, collector failure, or vendor error | Investigate collector / credentials / vendor |

Decision rule (proposed):

* `observations == 0` AND (`bootstrap_reason` contains "unavailable" OR collector never wrote a row) → `unavailable`
* `0 < observations < required_min` → `accumulating`
* `observations >= required_min` → `calibrated`

Per-series `unavailable_reason` retained (FRED key missing, vendor 502, etc.) — same free-text field, now scoped to the explicit failure case.

**Layer 2 — E2E verification operator surface:**

(a) Populate `data_calibration_state.json` at the invocation root with a structured summary:

```json
{
  "summary": {
    "calibrated": 12,
    "accumulating": 4,
    "unavailable": 5
  },
  "unavailable": [
    {"module": "q6.dollar_attribution", "reason": "DTWEXBGS history unavailable", "since": "..."},
    {"module": "q7.intermarket_regime.gld_real_yields", "reason": "0 observations", "since": "..."},
    ...
  ],
  "accumulating": [
    {"module": "q6.funding_stress", "observations": 11, "required": 60, "eta_calibrated_at": "..."},
    ...
  ]
}
```

(b) E2E harness prints a DATA HEALTH block at the end of the run that elevates the `unavailable` list to operator attention with red severity, lists `accumulating` series with their progress (X/N obs) as a green/yellow note, and counts the `calibrated` series.

(c) Optional: harness exits non-zero or emits a distinct warning if any module is `unavailable` for more than a configurable number of consecutive invocations (e.g., 3) — collector down for 3 runs in a row is no longer "transient."

## Acceptance criteria

- [ ] Distillation publishing emits exactly one of `calibrated` / `accumulating` / `unavailable` per series. No more `bootstrap` label.
- [ ] `data_calibration_state.json` is populated with the structured summary at every invocation (not `{}`).
- [ ] E2E harness output includes a `=== DATA HEALTH ===` block at the end listing unavailable series with their reasons.
- [ ] The 5 series currently failing under `bootstrap` in inv-20260518T111140Z (DTWEXBGS, FRED DGS, gld_real_yields, oil_xle_beta, vix_spy) are correctly labeled `unavailable` on the next run.
- [ ] The 4 series genuinely bootstrapping (funding_stress, market_liquidity, breadth_internals, spy_tlt) are correctly labeled `accumulating` with their progress visible.

## Verification

* Re-run debug-e2e harness; confirm the operator can see at-a-glance which series are unavailable vs accumulating without opening any artifact file.
* Confirm `data_calibration_state.json` is non-empty and contains the structured summary.
* Grep the synthesizer's `user_message.md` for the string `bootstrap` — should return zero hits (replaced by the new three-state vocabulary).

## Notes

This issue is the operator-experience layer over the same missing-data territory as [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket) (collector audits + null sentinel in distillation), [ALP-538](https://linear.app/alphamind-jatassi/issue/ALP-538/sentiment-pipeline-frozen-vol0change0-universal-benchmarks-share) (sentiment defaults), [ALP-539](https://linear.app/alphamind-jatassi/issue/ALP-539/ctra-ticker-deep-pull-returns-11-day-stale-data-while-other-tickers) (ticker_deep_pull quality flag). The convention established here (three-state calibration + structured summary) should be mirrored by those issues' implementations.

Suggested sequencing: land this issue's Layer 1 (vocabulary split) first to establish the convention, then [ALP-537](https://linear.app/alphamind-jatassi/issue/ALP-537/macrofred-collectors-silently-absent-dtwexbgs-dgs-series-intermarket)/538/539 mirror it in their respective modules. Layer 2 (e2e operator surface) is independent and can ship in parallel.