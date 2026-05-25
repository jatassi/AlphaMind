## Symptom

The qualitative researcher (QR) receives an empty cross-sector news digest (`Headlines: 0 collected, 0 shown below` across all 4 sub-sections: Macro, Tech/Semis, Financials, Energy, plus Earnings Calls and High-Priority Flags), even though each domain researcher's input bundle contains 30 properly-attributed headlines from the same lookback window. The QR correctly detected the failure and tagged `signal_quality: degraded`, but downstream synthesis lost the cross-sector narrative layer for the invocation.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/qualitative_researcher/user_message.md` lines 19-35:

```
## NEWS DIGEST

=== NEWS DIGEST (invocation inv-20260518T111140Z-7b54d0d6, covering 2026-05-18T11:11:40Z → 2026-05-18T11:11:40Z) ===
Headlines: 0 collected, 0 shown below

--- MACRO / CROSS-SECTOR (top 5) ---
--- TECH / SEMIS (top 5) ---
--- FINANCIALS (top 5) ---
--- ENERGY (top 5) ---
--- EARNINGS CALLS SINCE LAST INVOCATION (if any) ---
--- HIGH-PRIORITY FLAGS (0-3, regardless of sector) ---
```

Same invocation, `analysis/tech_semis_researcher/user_message.md` lines 1655-1685 contains 30 well-formed headlines with tier, source, ticker, timestamp:

```
[1] 2026-05-18T01:21:33Z [tier_3] Yahoo | MSFT | Kenya Setback Tests Microsoft Azure Growth Plans...
[4] 2026-05-18T00:38:15Z [tier_3] Yahoo | NVDA | Kioxia Shares Awash in Buy Orders After AI-Driven Profit Surge
[10] 2026-05-17T23:21:24Z [tier_3] seekingalpha.com | CSCO | Cisco Systems, Inc. 2026 Q3 - Results - Earnings Call Presentation
```

Also: the lookback window in the empty digest is degenerate — `2026-05-18T11:11:40Z → 2026-05-18T11:11:40Z` (start == end). The sector bundles use `Lookback window: last 24 hours, Data freshness: 2026-05-18T02:31:14Z`.

QR self-diagnosed (`response_initial.md` line 14): *"News API: hard unavailable (0 articles, tool confirms* `quality: unavailable`*)"*.

## Root cause hypothesis

The cross-sector news digest assembly path computes a zero-width window (start == end) and finds no rows. Possible mechanisms:

1. The digest's window-derivation logic incorrectly uses `(snapshot_ts, snapshot_ts)` rather than `(snapshot_ts - lookback, snapshot_ts)`.
2. The digest queries on a different column (e.g., publication time) than the sector bundles (which use `last 24h` lookback).
3. The "top N" composite-score ranker filter is filtering everything out due to a scoring bug.

The sector-bundle path works correctly, so the headline rows exist in storage — only the cross-sector digest assembly is broken.

## Scope

Fix the cross-sector news digest assembly so QR receives the same headline corpus that domain researchers do, with appropriate top-N selection (`news_digest_top_n_high_priority: 3`, `news_digest_top_n_per_sector: 5` from `resolved_config.json`).

## Acceptance criteria

- [ ] On a fresh e2e invocation with headlines present in storage, QR's `## NEWS DIGEST` section is populated with `top_n_per_sector` per sector slot and `top_n_high_priority` in the high-priority flags slot.
- [ ] Lookback window header reads `(snapshot_ts - 24h, snapshot_ts)` (or whatever the configured lookback is), not `(snapshot_ts, snapshot_ts)`.
- [ ] QR's `signal_quality` no longer flags news as unavailable when the underlying corpus is non-empty.

## Verification

* Re-run the debug-e2e harness; confirm QR `user_message.md` shows populated headlines.
* Spot-check that QR's narrative threads cite headline IDs (e.g., `[QR-1] ... evidence: [1] Kioxia...`) rather than relying solely on event_calendar and thesis_summary.
* Diff QR's digest against tech_semis_researcher's HEADLINES section — top-N selection should be a deterministic subset of the same underlying corpus.

## Notes

Cross-sector digest failure is independent of the bootstrap data-volume issue — headlines existed in storage at invocation time (proven by the populated sector bundles). This is a code path bug, not a data-gap.