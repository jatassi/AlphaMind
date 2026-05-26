# q1.gap module unavailable with 0 observations — gap-detection collector may not be writing rows

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the calibration state surfaces `q1.gap` as `unavailable` with three entries all citing the same reason:

```
{ module: q1.gap, reason: "gap_fill_min_events: 0 < 30 (0 observations)" }
{ module: q1.gap, reason: "gap_fill_min_events: 0 < 30 (0 observations)" }
{ module: q1.gap, reason: "gap_fill_min_events: 0 < 30 (0 observations)" }
```

The "0 observations" count is what makes this suspicious. Gap events should accumulate naturally over time as tickers experience open-to-close gaps; with an active universe of \~66 single-name tickers across 4 sectors and 30 days of bootstrap-window observations, the expected gap-event count should be > 0. Domain researchers in this invocation explicitly cite per-ticker gap data (e.g., tech_semis: "MU gapped down $43.44 (ATR ratio -0.955, full gap)", "AMD -$16.36 (ATR ratio -0.707, full gap)" etc.), so the gap-detection logic IS detecting gaps at the per-session ticker analysis level — but no aggregated `q1.gap_fill_min_events` observations have been accumulated.

The triple-listing of the same module/reason in the calibration state's `unavailable` list also suggests a defensive write loop, possibly one entry per sector that has the same root cause — but that's a cosmetic concern relative to the primary "0 observations" question.

Adaptive researcher's signal_quality_reason for tech_semis correctly identified this:
"gap fill probabilities are unavailable (0 of 30 required events)"

## Evidence

`data_calibration_state.json` lines 104-116:

```
"unavailable": [
  {
    "module": "q1.gap",
    "reason": "gap_fill_min_events: 0 < 30 (0 observations)"
  },
  {
    "module": "q1.gap",
    "reason": "gap_fill_min_events: 0 < 30 (0 observations)"
  },
  {
    "module": "q1.gap",
    "reason": "gap_fill_min_events: 0 < 30 (0 observations)"
  },
  ...
]
```

For contrast, gap data IS being detected at the per-ticker domain-researcher level:

`analysis/tech_semis_researcher/response_initial.md` (SA-TECH-3):

> "Gap data shows MU gapped down $43.44 (ATR ratio -0.955, full gap), AMD -$16.36 (ATR ratio -0.707, full), TER -$16.98 (ATR ratio -0.774, full), AMAT -$12.14 (ATR ratio -0.689, full), LRCX -$9.71 (ATR ratio -0.726, full), MRVL -$8.68 (ATR ratio -0.872, full), and INTC -$6.145 (ATR ratio -0.795, full); all classified full gaps down."

So per-ticker gap detection works in the current-session analysis pipeline, but the `q1.gap` aggregate module that tracks gap-fill statistics over time has 0 observations.

## Root cause \[hypothesis\]

Three candidate root causes:

1. **q1.gap module not wired to the gap-detection pipeline**: The per-session gap detection that produces "MU gapped down $43.44" feeds the domain researcher input but may not be writing rows to whatever table backs the `q1.gap` aggregate. The aggregate's `gap_fill_min_events` count stays at 0 because nothing is incrementing it.
2. **Gap-fill events are a separate concept**: `gap_fill_min_events` may specifically track "gap that has subsequently been filled" events (not raw gap detections). If gap-fill detection requires N+M sessions after the gap session to determine fill status, and the rolling window doesn't have those follow-up sessions, fill events would stay at 0 in a bootstrap window.
3. **Triple-listed entries**: Three identical entries in the `unavailable` list (same module, same reason) is either a defensive duplicate write per sector or per-window-size, OR a deduplication bug in calibration state assembly.

## Scope

1. Confirm whether `q1.gap` / `gap_fill_min_events` is the same concept as the per-ticker gap detection used by domain researchers. If they're different concepts, document the distinction explicitly so "0 observations" with bootstrap reason is clearly expected vs unexpected.
2. If `q1.gap` is supposed to feed off the same gap-detection pipeline, audit why aggregate observations aren't accumulating despite per-ticker gaps being detected and surfaced.
3. Deduplicate the `unavailable` list in the calibration state assembly — three identical entries should be one entry (or three with distinct reasons that explain why they're separate).

## Acceptance criteria

- [ ] `q1.gap` module's relationship to the per-ticker gap-detection pipeline is documented
- [ ] If `q1.gap` is supposed to accumulate, observations are accumulating (count > 0 after a few invocations)
- [ ] If `q1.gap` is genuinely expected to stay at 0 during a bootstrap window of N sessions, the reason text clarifies this
- [ ] `unavailable` list in calibration state has no duplicate entries with bit-identical module+reason

## Verification

* Query the underlying gap-events table (or the table backing `gap_fill_min_events`) directly. Compare row count to the per-session gap counts visible in the domain researcher artifacts.
* Read the gap-detection module's code path; trace whether per-ticker gap detection writes rows to the same table that `q1.gap` reads from.
* Unit test the calibration-state assembler with three modules sharing the same reason; confirm the output `unavailable` list has one entry (deduped), not three.
* Manually inspect the `data_calibration_state.json` from a future invocation to confirm de-duplication and any change in `q1.gap` observation count.

## Notes

This is Hard rule 1 borderline — the `0 observations` count could be either "bootstrap-accumulating below threshold" (expected) or "collector silent" (urgent). The duplicate-entry pattern and the contrast with per-session gap detection that IS working tip it toward investigate-worthy rather than purely-bootstrap. If the operator confirms this is genuinely bootstrap-expected (i.e., `gap_fill_min_events` tracks gap-fill resolutions which take N sessions to accumulate), this issue can be downgraded to a documentation/clarification fix.
