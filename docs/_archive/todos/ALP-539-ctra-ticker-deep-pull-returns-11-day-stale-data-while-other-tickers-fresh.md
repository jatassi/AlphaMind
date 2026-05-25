## Symptom

The adaptive researcher's `ticker_deep_pull(CTRA)` call returned data with `freshness 2026-05-07` on a 2026-05-18 invocation — 11 days stale. Other tickers pulled in the same invocation returned data dated 2026-05-15 or 2026-05-16, fitting the expected 1-3 trading-day lag for end-of-day fields. CTRA is the only ticker observed with this magnitude of staleness in this invocation.

This is particularly consequential because CTRA was the subject of an active anomaly investigation (\[SA-ENERGY-ANOM-1\] flagged a -3.07 / 2.45x ATR counter-trend gap on the 2026-05-18 session), and the stale May 7 data couldn't actually verify the May 18 anomaly.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`analysis/synthesizer/user_message.md` adaptive researcher findings:

* \[AR-5\] line 2486: `ticker_deep_pull CTRA (freshness 2026-05-07, 11 days stale): pct_change_1d -8.62%, pct_change_5d -8.74%, volume_vs_avg_ratio 7.18x`
* \[AR-5\] line 2487: *"Data staleness caveat: the deep pull reflects a May 7 session; \[SA-ENERGY-ANOM-1\] references a May 18 session anomaly — these are likely separate but directionally consistent events"*

Comparison — same invocation, other tickers:

* \[AR-1\] line 2420: `ticker_deep_pull META (freshness 2026-05-15)`
* \[AR-1\] line 2421: `ticker_deep_pull GOOG (freshness 2026-05-16)`
* \[AR-2\] line 2439: `ticker_deep_pull CSCO (freshness 2026-05-16)`
* \[AR-3\] line 2455: `ticker_deep_pull ASML (freshness 2026-05-16)`
* \[AR-3\] line 2456: `ticker_deep_pull NVDA (freshness 2026-05-16)`
* \[AR-6\] line 2503: `ticker_deep_pull NVDA (freshness 2026-05-16)`
* \[AR-7\] line 2520: `ticker_deep_pull WFC (freshness 2026-05-16)`

So 6 of 7 deep-pull responses are May 15-16 (1-3 days lag), only CTRA at May 7 (11 days lag).

## Root cause hypotheses

Three candidates, each calling for a different fix:

1. **Ticker-specific collector failure:** the underlying market-data collector for CTRA stopped writing on 2026-05-07 but the deep-pull tool returns the last-known-good row without surfacing a freshness threshold violation. Other tickers in the energy sector (COP, EOG, DVN, OXY, etc.) would also be stale if so — needs cross-check.
2. **Deep-pull tool silently returns stale data when fresh data is missing:** the tool's behavior on cache-miss should be to return an error or null, not the last-known-good. If a freshness threshold (e.g., "fail if data older than 5 days") is configured but not enforced for some code path, this matches the symptom.
3. **Vendor-specific dropout:** the upstream vendor (Finnhub? Alpaca?) dropped CTRA from a recent batch and silently returned the previous day's row.

## Scope

(a) Verify whether other energy-sector tickers (COP, EOG, DVN, OXY, SLB, BKR, LNG, APA) are also stale beyond expected 1-3 day lag — if yes, this is a sector-wide collector failure; if no, it's CTRA-specific.

(b) Add a freshness threshold to `ticker_deep_pull` — if returned data is older than N trading days, raise an explicit `quality: stale` flag in the response (the adaptive researcher noticed the staleness manually; the tool should surface it programmatically).

(c) Fix the underlying CTRA collector path once root cause is identified.

## Acceptance criteria

- [ ] CTRA ticker_deep_pull returns data ≤3 trading days stale on the next invocation.
- [ ] When a ticker has no fresh data, `ticker_deep_pull` returns `{quality: stale, freshness: ..., reason: ...}` rather than silently returning the last-known-good row.

## Verification

* Re-run debug-e2e; cross-check freshness across all energy-sector ticker deep-pulls. Confirm CTRA is no longer the outlier.
* Confirm `quality` field is present in deep-pull responses and surfaces "stale" appropriately.

## Notes

Likely independent of bootstrap state — other tickers (including ones with arguably less coverage like WFC, ASML) returned fresh data, so the collector infrastructure is mostly working. CTRA-specific failure mode warrants direct investigation.