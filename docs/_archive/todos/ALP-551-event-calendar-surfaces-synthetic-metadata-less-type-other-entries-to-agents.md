# Event calendar surfaces synthetic / metadata-less "type=other" entries to agents

## Symptom

The 72-hour event calendar fed to the qualitative researcher and sector researchers includes entries that have no ticker association, no sector, no consensus payload, and a `type=other` classification. The entry names look synthetic (not real public companies). They add noise to the calendar without providing actionable signal.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/qualitative_researcher/user_message.md` lines 248-254 — EVENT CALENDAR (next 72h):

```
- 2026-05-19T00:00:00Z | Aperture AC | type=other | sectors=(none) | tickers=(none) | consensus=(none)
- 2026-05-20T00:00:00Z |  | type=earnings | sectors=(none) | tickers=NVDA | consensus=EPS $1.79, revenue $80.0B
- 2026-05-20T00:00:00Z |  | type=earnings | sectors=(none) | tickers=CSCO | consensus=EPS $1.05, revenue $15.8B
- 2026-05-20T00:00:00Z | Lincoln International, Inc. | type=other | sectors=(none) | tickers=(none) | consensus=(none)
- 2026-05-21T00:00:00Z |  | type=earnings | sectors=(none) | tickers=MRVL | consensus=EPS $0.80, revenue $2.4B
- 2026-05-21T00:00:00Z |  | type=earnings | sectors=(none) | tickers=INTU | consensus=EPS $12.80, revenue $8.7B
- 2026-05-21T00:00:00Z | Conexeu Sciences Inc. | type=other | sectors=(none) | tickers=(none) | consensus=(none)
```

The three `type=other` entries ("Aperture AC", "Lincoln International, Inc.", "Conexeu Sciences Inc.") do not appear to be real public companies in the active universe. They have no ticker, no sector classification, no consensus payload — they're pure metadata-less placeholders cluttering an otherwise actionable list.

Agents correctly ignored the entries — the qualitative researcher's `QR-3` thread cites NVDA/CSCO/MRVL/INTU and skips the type=other entries. Impact is brief-readability and an unnecessary token cost in researcher prompts.

## Root cause hypothesis

Two possibilities:

1. **Synthetic-seed leakage.** The event_calendar collector or fixture-loader is including synthetic test-seed entries (likely company names from `Aperture Science`, `Lincoln International`, etc. — names that look like financial-industry stock-photo placeholders or movie references). These should be filtered when not in `type ∈ {earnings, dividend, fed_meeting, ipo, regulatory}` (the actionable types).
2. **Real-but-broken metadata extraction.** The events are real calendar events (analyst days, conferences, company-specific catalysts) but the ingestion didn't populate sectors/tickers fields. Without ticker association, they can't be routed to the right researcher.

Either way, the fix is the same: filter or enrich.

## Scope

Either:

(a) Filter `type=other` events with no ticker association before they reach the per-invocation event calendar.

(b) If the events are legitimate (analyst days, conferences for portfolio names), enrich them with ticker/sector metadata at the ingestion layer so they route properly.

## Acceptance criteria

- [ ] Event calendar contains no entries with `type=other` AND `tickers=(none)` AND `consensus=(none)` — either filtered or enriched with ticker association.
- [ ] Per-invocation event calendar size is bounded — no synthetic/placeholder entries propagate to researcher inputs.

## Verification

* Re-run debug-e2e; spot-check the EVENT CALENDAR section of any researcher input bundle. Confirm no `type=other | tickers=(none)` entries remain.

## Notes

Low priority — agents correctly ignored the entries. Cleanup pays off in token costs across the operational fleet (\~6 wasted lines per researcher per invocation) and brief readability.
