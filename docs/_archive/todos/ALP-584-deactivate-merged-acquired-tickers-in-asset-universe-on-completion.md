# Deactivate merged/acquired tickers in asset_universe on completion

## Symptom

`asset_universe` rows for tickers that have been acquired/merged and no longer trade remain `is_active=1` indefinitely, causing the polygon.equity collector to query Polygon for them every 15 min forever and the active-ticker scope to include phantom tickers.

Concrete example: CTRA (Coterra Energy) was acquired by Devon Energy and ceased trading after the close on **2026-05-06**. As of 2026-05-19 (12 days post-merger), `asset_universe.CTRA` still shows `is_active=1`, `removed_date=NULL`. The polygon.equity collector keeps requesting bars and gets empty responses every cycle.

This propagates downstream: the adaptive researcher's anomaly investigation on CTRA's "13-day stale" data was structurally unresolvable in `inv-20260519T030654Z-60f10023` because no upstream signal indicated CTRA was delisted rather than merely missing data (see related ticker_deep_pull issue).

## Evidence

Query against production DB at 2026-05-19:

```sql
SELECT ticker, is_active, removed_date, removal_reason, last_updated
FROM asset_universe WHERE ticker='CTRA';
-- CTRA | 1 | NULL | NULL | 2026-05-02T10:00:03+00:00

SELECT timeframe, MAX(period_start) FROM ohlcv_bars WHERE ticker='CTRA' GROUP BY timeframe;
-- 15min | 2026-05-06T23:45:00+00:00
-- 1d    | 2026-05-06T04:00:00+00:00
-- 1h    | 2026-05-06T23:00:00+00:00
-- 1w    | 2026-05-03T04:00:00+00:00
-- 4h    | 2026-05-06T20:00:00+00:00
```

All timeframes halt at the close of the 2026-05-06 session — clean delisting signature.

`corporate_actions` table contains only `cash_dividend` rows for CTRA — no `merger` / `acquisition` / `delisting` event recorded, even though news in `news_articles` clearly captured the completion:

* `2026-05-07 06:06:16Z` finnhub — "Coterra Energy Inc.(NYSE: CTRA) dropped from FTSE All-World Index"
* `2026-05-07 08:31:39Z` finnhub — "Devon Energy and Coterra Energy Complete Merger"
* `2026-05-12 06:51:14Z` finnhub — "Fitch Upgrades Coterra and Maintains Rating Watch Positive Following Devon Merger Completion"

## Root cause

Two gaps:

1. **Corporate-actions collector does not ingest mergers/acquisitions.** Review `polygon.corporate_actions` (and any sibling source) — only dividends + splits appear in `corporate_actions` for CTRA. Either the source doesn't expose mergers, or the collector filters them out, or the upsert silently drops them.
2. **No reconciler flips** `asset_universe.is_active=0` **on merger/delisting.** Even if a merger row existed in `corporate_actions`, nothing today consumes that to deactivate the merged ticker, stamp `removed_date`, and write `removal_reason`.

The polygon.equity collector at `src/alphamind/data_sources/polygon/equity.py:160-172` also silently swallows empty bar responses (`200 OK + []`) with no log line, masking the symptom — but that's a separate observability issue, not the root cause.

## Scope

1. Extend the corporate-actions ingestion pipeline (likely `src/alphamind/data_sources/polygon/corporate_actions.py` plus any auxiliary source) to capture merger/acquisition/delisting events. The `corporate_actions` table already has `acquirer_ticker`, `spin_off_ticker`, and `new_ticker` columns for this shape — verify they're populated when applicable.
2. Add a reconciler step (cron, post-collector hook, or merged into the collector itself) that consumes any merger/acquisition/delisting row in `corporate_actions` and updates the target ticker's `asset_universe` row: `is_active=0`, `removed_date=<ex_date>`, `removal_reason='acquired by <acquirer_ticker>'` (or appropriate phrasing for other corp-event types).
3. CTRA-specific cleanup row: insert the merger event into `corporate_actions` and apply the deactivation manually (or via the new reconciler) so the next adaptive-researcher invocation sees CTRA inactive.

## Acceptance criteria

- [ ] Corporate-actions collector ingests merger/acquisition/delisting events when they appear in the vendor feed (or a documented gap is filed if the chosen vendor does not expose them, with a fallback plan).
- [ ] A merger/acquisition row in `corporate_actions` for ticker T results in `asset_universe.T.is_active=0`, `removed_date=<ex_date>`, `removal_reason` populated within one collection cycle.
- [ ] CTRA-specific cleanup: `corporate_actions` has a merger row with `acquirer_ticker='DVN'`, `ex_date='2026-05-07'`; `asset_universe.CTRA.is_active=0`, `removed_date='2026-05-07'`, `removal_reason` mentions DVN acquisition.
- [ ] Integration test: synthetic merger row → reconciler runs → asset_universe row flipped correctly.

## Verification

* `SELECT * FROM asset_universe WHERE ticker='CTRA';` shows `is_active=0` and a non-null `removed_date` / `removal_reason`.
* Subsequent polygon.equity collector runs no longer attempt CTRA (it's filtered out by `active_universe_tickers`).
* Spot-check at least one additional historical merger ticker if one exists in the asset universe to confirm the reconciler isn't CTRA-specific.

## Notes

Related to `ticker_deep_pull` delisted-ticker semantics — that issue is the downstream surface that benefits most from this state being correct, but it can ship independently since the tool reads `asset_universe.is_active` directly.
