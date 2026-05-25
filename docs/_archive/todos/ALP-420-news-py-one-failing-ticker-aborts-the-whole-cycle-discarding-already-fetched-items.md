## Symptom

In `finnhub.news`, when one ticker's API call exhausts its retries (e.g. persistent 502, rate-limit miss, network blip), the entire cycle dies and **all items already fetched from earlier tickers in the loop are discarded** — never persisted. The cycle is marked `failed` in `collection_runs`. The next cycle re-fetches everything from `resume_since(...)`, so data isn't permanently lost, but the cycle's work is wasted and the freshness window is shifted.

## Root cause

`src/alphamind/data_sources/finnhub/news.py:124-142` — `_gather_items`:

```python
for ticker in ticker_scope:
    if rate_limiter:
        rate_limiter.acquire(_PROVIDER)
    for item in _fetch_company_news(sdk, ticker, from_date, to_date):
        items.append((item, ticker))
```

No try/except around `_fetch_company_news`. Any exception (after retries exhaust) propagates up to `collect_news`, abandoning the in-memory `items` list. Same pattern likely exists in other per-ticker collectors — worth a sweep:

* `src/alphamind/data_sources/finnhub/estimate_revisions.py` — `_ingest_ticker` loop
* `src/alphamind/data_sources/marketaux/news.py` — batched ticker loop
* `src/alphamind/data_sources/iborrowdesk/borrow_cost.py` — per-ticker loop (already aborts on first `IBorrowDeskBlockedError`)

## Design question

This may be intentional fail-fast behavior — when retries on one ticker exhaust, the vendor is probably degraded for *all* tickers, and continuing wastes rate-limit budget. Or it may be incidental and best-effort isolation would be better.

Resolve the design intent before patching:

* If fail-fast is intentional: document it in `docs/design/01-data-layer/data-and-state.md` and close this issue.
* If best-effort is preferred: implement per-ticker isolation in all per-ticker collectors uniformly.

## Scope (if best-effort path chosen)

1. Add per-ticker try/except in `_gather_items` (finnhub.news), `_ingest_ticker` loop in estimate_revisions, and equivalent points in marketaux and iborrowdesk.
2. Track skipped-ticker count and append to the run's `error_summary`.
3. If skipped fraction > threshold (e.g. 50%), still mark the run failed — partial success only counts when most tickers succeeded.
4. Unit tests per collector for the partial-failure path.

## Verification

Inject a transient error on one ticker via a stub SDK and confirm:

* Other tickers' rows persist.
* `collection_runs.status = 'success'`.
* `collection_runs.error_summary` reflects the skipped ticker.

## Priority note

Filed Low because the next cycle backfills via `resume_since`, so this is a resilience improvement rather than a data-loss bug. Bump if telemetry shows lost cycles are common.