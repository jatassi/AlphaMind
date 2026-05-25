## Symptom

`finnhub.news` fails with `FinnhubAPIException(status_code: 502)` at exactly the same two cycle boundaries every single day:

* 01:30 ET cycle → errors at \~01:31:02
* 06:30 ET cycle → errors at \~06:31:02

Verified across 2026-05-07, 05-08, 05-09, 05-10 — same minute, same vendor 502 nginx response. The 48 other cycles per day succeed normally; only those two consistently fail. Pattern is too regular to be random vendor flakiness — likely Finnhub's nightly maintenance or rate-limit-reset windows.

## Root cause

Two contributing factors:

1. **Retry budget too small for vendor-side blip.** `_fetch_company_news` in `src/alphamind/data_sources/finnhub/news.py:112-116` uses `RetryShape.important` (2 attempts, 1s gap = 1s budget total). Both attempts hit the 502 because the vendor blip lasts longer than 1s.
2. **No per-ticker isolation.** `_gather_items` (`src/alphamind/data_sources/finnhub/news.py:124-142`) loops over tickers with no try/except, so the first ticker whose retries exhaust kills the whole cycle and discards every item already fetched from earlier tickers.

## Scope

Two options (not mutually exclusive):

**(A)** Bump `_fetch_company_news` to `RetryShape.vendor_outage_extended` (4 attempts, 5+15+45s = 65s budget). This is the same tier FRED uses for the same pattern. Risk: [news.py](<http://news.py>) iterates per-ticker, so worst-case wait scales by the failing-ticker count — but only when retries actually exhaust on that ticker, which is rare.

**(B)** Wrap the per-ticker call in `_gather_items` in try/except so one ticker's exhausted retries skip that ticker and continue the cycle. Failed-ticker count goes into the run's `error_summary` field. This converts a hard failure into degraded-but-functional collection.

Recommend doing both: (A) to recover from the daily 01:31/06:31 blip; (B) to make the collector resilient to any single-ticker persistent issue (rate limit hit, ticker delisted, etc.).

## Acceptance criteria

* `_fetch_company_news` uses `RetryShape.vendor_outage_extended`.
* `_gather_items` continues past a ticker whose retries exhaust; failed tickers are logged at WARNING and the count appears in the run's outcome.
* Unit test: mock SDK that 502s on ticker N → other tickers' items still persist; run completes (status=success) with a warning summary.
* After deploy, monitor 5-7 days for whether the 01:31 / 06:31 cycles now succeed.

## Verification

`uv run python scripts/verify_ongoing_collection.py` shows `finnhub.news` freshness within cadence consistently for a week post-deploy.

## Related

Depends on the underlying retry classifier being correct — see the `_is_retryable` httpx-family bug for the broader retry-coverage gap.