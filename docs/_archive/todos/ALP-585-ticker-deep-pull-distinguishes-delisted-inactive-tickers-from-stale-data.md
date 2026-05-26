# ticker_deep_pull distinguishes delisted/inactive tickers from stale data

## Symptom

When an analysis-layer agent (adaptive researcher, analyst, etc.) calls `ticker_deep_pull` on a delisted/inactive ticker, the tool returns `quality=ToolQuality.STALE` with a generic freshness message (e.g., `"price_volume: latest bar 2026-05-06 is 13 days stale (threshold 5 days)"`). The agent has no way to distinguish "ingestion failure on a live ticker" from "ticker is delisted, this data is final and correct." The two situations call for very different downstream behavior — chase the data gap vs. treat the price action as a resolved corporate event.

Concrete example from `inv-20260519T030654Z-60f10023`: the adaptive researcher's AR-2 thread investigating a CTRA anomaly sat at "inconclusive" because the only signal was 13-day staleness. CTRA had actually been acquired by Devon Energy on 2026-05-07; the "-8.62% 1d" was the final pre-merger session, fully explained by the deal close. With a "delisted: acquired by DVN on 2026-05-07" reason, the researcher would have resolved the thread immediately instead of asking for follow-up data that doesn't exist.

## Evidence

`analysis/adaptive_researcher/response_initial.md` AR-2 finding from `inv-20260519T030654Z-60f10023`:

```
[AR-2] CTRA investigation
  ticker_deep_pull CTRA (data stale — 2026-05-06, 13 days old): last close $32.56, 1d -8.62%, 5d -8.74%; the near-identity of 1d and 5d loss confirms the entire multi-day decline was concentrated in a single session
  ...
  Missing: Two data gaps prevent resolution: (1) the ticker_deep_pull data is 13 days stale, so whether the price has recovered or deteriorated further since May 6 is unknown
```

Current `_load_price_volume` at `src/alphamind/analysis/tools/ticker_deep_pull.py:282-287`:

```python
if age_days > _OHLCV_STALE_AGE_DAYS:
    reason = (
        f"price_volume: latest bar {latest_period_start.date().isoformat()} "
        f"is {age_days} days stale (threshold {_OHLCV_STALE_AGE_DAYS} days)"
    )
    return payload, latest_period_start, ToolQuality.STALE, reason
```

The current `_ticker_in_universe` helper at `src/alphamind/analysis/tools/ticker_deep_pull.py:199-205` only checks existence — it does not consult `is_active`, `removed_date`, or `removal_reason`. The "delisted" signal exists in `asset_universe` (once [ALP-584](https://linear.app/alphamind-jatassi/issue/ALP-584/deactivate-mergedacquired-tickers-in-asset-universe-on-completion) lands) but is invisible to the tool today.

## Scope

1. Extend the asset-universe check to consult `is_active`, `removed_date`, and `removal_reason`. When the ticker exists but `is_active=0`:
   * Skip the per-category loaders entirely.
   * Return a dedicated envelope with `quality=ToolQuality.UNAVAILABLE` (or a new `DELISTED` quality if the team prefers — argue against; UNAVAILABLE already captures "no useful data" semantics) and a reason string like `"{ticker} delisted on {removed_date} ({removal_reason})"`.
2. Ensure the reason is structured enough that the agent can act on it programmatically — exact format is a design call, but it must include `removed_date` and `removal_reason` verbatim.
3. Update tests: existing `test_ticker_deep_pull.py` covers the stale-bar case; add coverage for the inactive-ticker case.
4. No changes to the freshness contract itself — that's working as designed.

## Acceptance criteria

- [ ] When `asset_universe.{ticker}.is_active=0`, `ticker_deep_pull` returns a response with `quality=UNAVAILABLE` and a reason naming `removed_date` and `removal_reason`, without invoking per-category loaders.
- [ ] When the ticker is active and bars are stale, behavior is unchanged (still `STALE` with the existing freshness message).
- [ ] When the ticker is active and data is fresh, behavior is unchanged.
- [ ] Unit test: synthetic `asset_universe` row with `is_active=0`, `removed_date='2026-05-07'`, `removal_reason='acquired by DVN'` → tool returns expected reason; per-category payloads are `None`.
- [ ] Unit test: ticker absent from `asset_universe` continues to return the existing UNAVAILABLE envelope (don't regress that path).

## Verification

* Run `uv run pytest tests/analysis/tools/test_ticker_deep_pull.py --testmon -n auto` and confirm green.
* Manually invoke the tool against a CTRA-shaped fixture after [ALP-584](https://linear.app/alphamind-jatassi/issue/ALP-584/deactivate-mergedacquired-tickers-in-asset-universe-on-completion) lands; confirm the reason string surfaces "delisted ... acquired by DVN" instead of "13 days stale".
* No real prod behavior change until [ALP-584](https://linear.app/alphamind-jatassi/issue/ALP-584/deactivate-mergedacquired-tickers-in-asset-universe-on-completion) backfills `asset_universe.CTRA.is_active=0` — but the tool change can ship independently and is safe (no-op while all tickers remain `is_active=1`).

## Notes

Related to [ALP-584](https://linear.app/alphamind-jatassi/issue/ALP-584/deactivate-mergedacquired-tickers-in-asset-universe-on-completion) (asset_universe deactivation on merger). This issue depends on `asset_universe` correctly carrying the inactive state, but the tool change itself can ship independently — it's a no-op until that state is populated, and adds correct semantics the moment it is.
