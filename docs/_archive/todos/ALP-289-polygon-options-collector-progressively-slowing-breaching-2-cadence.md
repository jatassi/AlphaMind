## Symptom

`scripts/verify_ongoing_collection.py` fails: `polygon.options` last seen 1:13:54 ago vs 1:00:00 max (30-min cadence × 2). All 13 other collectors OK.

## Observed run durations on 2026-05-04 (prod local time)

| Started | Completed | Duration |
| -- | -- | -- |
| 07:00 | 07:08 | 8 min |
| 07:30 | 08:44 | 74 min |
| 09:00 | 10:41 | 101 min |
| 11:00 | — | 55+ min, running |
| 11:30 | — | 26+ min, running concurrently with 11:00 |

The scheduler fired the 11:30 run while 11:00 was still in-flight, so two options runs are now executing in parallel.

## Notes

* Scheduler itself is healthy: `polygon.equity` cycling normally (last "done" 11:52 prod-local).
* `collector.err.log` last touched 11:06 with an older `KeyboardInterrupt` traceback from a prior process restart — not the current slowdown.
* DB has options data; it's just stale beyond the 2× window.

## Likely causes to investigate

* Polygon options API rate-limiting or pagination drift
* Options chain growing (universe expansion, or a new front-month adding contracts)
* Concurrent runs colliding on DB writes / SMB mount

## Why this matters

Phase 1 of `scripts/RUNBOOK_end_to_end_verification.md` blocks on this: distillation + downstream agents read from `options_contract_snapshots`, so a stale options collector gates the full pipeline verification.