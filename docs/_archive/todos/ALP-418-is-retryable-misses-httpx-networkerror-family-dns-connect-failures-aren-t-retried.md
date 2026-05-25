## Symptom

When the host has a transient DNS or network blip, httpx-based collectors (kalshi, polymarket, sec_edgar.rss, and finnhub.news via its requests-backed SDK) fail their entire cycle without retrying — even though the failure is clearly transient and the next cycle succeeds normally.

Evidence — on 2026-05-07 alone, five separate multi-collector burst failures (all `httpx.ConnectError: [Errno 11001] getaddrinfo failed`):

* 01:00:00 — kalshi, finnhub.news, sec_edgar.rss, polymarket
* 03:30:00 — same four
* 04:00:11 — polymarket, finnhub.calendar, sec_edgar.rss, kalshi, finnhub.news, iborrowdesk
* 07:00:11 — bls.macro, kalshi, sec_edgar.rss, polymarket, finnhub.news
* 10:02:XX — five collectors

None retried; all marked failed in `collection_runs`. The DNS resolver recovered within seconds — a single retry would have salvaged every one of these cycles.

## Root cause

`src/alphamind/data_sources/_common.py:276-296` (`_is_retryable`) covers `httpx.TimeoutException` and `httpx.HTTPStatusError`, plus `urllib.error.URLError | ConnectionResetError`, but **does not** match the `httpx.NetworkError` family:

* `httpx.ConnectError` — DNS failures, connection refused (this is the one biting us)
* `httpx.ReadError` / `httpx.WriteError` / `httpx.CloseError` — mid-flight socket failures

The code comment at lines 285-288 explicitly claims "DNS failure surface as URLError" — that is only true for `urllib`-based clients. For httpx clients, DNS failures surface as `httpx.ConnectError`, an `httpx.NetworkError` subclass.

Related gap: finnhub's SDK uses `requests` internally. `collector.log.2026-05-08:190` shows a `TimeoutError` propagating from `urllib3/connectionpool.py` through `requests/adapters.py` — also unmatched. `requests.exceptions.ConnectionError` and `requests.exceptions.Timeout` should probably be retried too.

## Scope

1. Extend `_is_retryable` in `src/alphamind/data_sources/_common.py` to match `httpx.NetworkError` (covers ConnectError + ReadError + WriteError + CloseError in one shot). For symmetry with the existing `TimeoutException` branch, `httpx.TransportError` would also work and cover both at once.
2. Add `requests.exceptions.ConnectionError` and `requests.exceptions.Timeout` matching for SDKs that use `requests` (finnhub, fredapi).
3. Update the comment at lines 285-288 to no longer claim httpx DNS failures land in URLError.

## Acceptance criteria

* Unit test: `_is_retryable(httpx.ConnectError("getaddrinfo failed"))` returns `True`.
* Unit test: `_is_retryable(httpx.ReadError(...))` returns `True`.
* Unit test: `_is_retryable(requests.exceptions.ConnectionError(...))` returns `True`.
* Unit test: `_is_retryable(requests.exceptions.ReadTimeout(...))` returns `True`.
* Existing tests still pass.
* Comment block reflects the actual class coverage.

## Verification

After deploy, no further `getaddrinfo failed` errors should appear in `collector.log` for any collector unless the DNS outage exceeds the retry budget for that shape.