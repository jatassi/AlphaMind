---
status: not_started
completed_date:
commit_id:
---

# 05f — Finnhub vendor adapter

## Goal

Implement `src/alphamind/data_sources/finnhub/` covering Qual1 news and Qual5 calendar (earnings + economic + IPO + FDA + filings) into `news_articles`, `event_calendar`, and `earnings_event_details`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `news_articles`, `news_article_tickers`, `event_calendar`, `earnings_event_details`
- `docs/design/01-data-layer/collector/lifecycle.md` § Bootstrap — calendar forward 90 days, news forward-only
- `docs/design/01-data-layer/api-key-checklist.md` § Finnhub — endpoint inventory
- `docs/design/01-data-layer/api-failure-handling.md` — Qual1 important, Qual5 important
- `config/news_outlets.yaml` — outlet credibility tier mapping

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/finnhub/` —
- `client.py` — wraps `finnhub-python` SDK with `with_retries(important)`, `RateLimiter`, `verify_connectivity()`.
- `news.py`:
  - `collect_news(ticker_scope, since)` — pulls `/api/v1/company-news` per ticker and `/api/v1/news?category=general` for market-wide. Writes `news_articles` (one row per article, body persisted to `%USERPROFILE%\AlphaMind\data\news\<YYYY>\<MM>\<article_id>.txt`) and `news_article_tickers`. No bootstrap function — forward-only.
  - `article_id` synthesized as SHA-256 of `(source='finnhub', url, published_at)` when Finnhub doesn't supply a stable ID.
  - `source_credibility_tier` stamped at ingestion by lookup against `news_outlets.yaml` keyed on the article's `source_outlet` (Finnhub field `source`).
- `calendar.py`:
  - `collect_earnings_calendar(since)` and `bootstrap_earnings_calendar()` — pulls `/api/v1/calendar/earnings`. Writes `event_calendar` with `event_type='earnings'` plus `earnings_event_details`.
  - `collect_economic_calendar(since)` and `bootstrap_economic_calendar()` — pulls `/api/v1/calendar/economic`. Writes `event_calendar` with `event_type` mapped from Finnhub's event taxonomy (e.g., `cpi_release`, `ppi_release`, `nfp_release`, `fomc`, `gdp_release`, `ism_release`).
  - `collect_ipo_calendar(since)` — `/api/v1/calendar/ipo`. Writes `event_calendar` with `event_type='other'` (or `ipo` if added to the storage enum).
  - `collect_fda_calendar(since)` — `/api/v1/fda-advisory-committee-calendar`. Writes `event_calendar` with `event_type='fda_advisory'`.
  - `bootstrap_*` variants pull forward 90 days.
- Unit tests with mocked SDK responses.

Out of scope:
- Sell-side recommendations / insider transactions (Q5 — deferred per `storage.md`).
- Filing search via `/api/v1/sec-filings` (use SEC EDGAR adapter in story 05h instead — the SEC source is authoritative).

## Notes

Finnhub free-tier rate limit: 60 req/min. Set `data_sources.yaml.providers.finnhub.rate_limit_per_minute = 60`.

Per-ticker company-news endpoint requires a date window. Forward-only collection: the function pulls `[since, now]` per ticker. The default `since=None` resolves via `_common.py`'s "since latest published_at in news_articles for source='finnhub' and ticker mention" fallback, capped at a sane default lookback (e.g., 24 hours) on cold start.

Body text from Finnhub's `summary` field. Some articles only have headlines; in that case `body_path` is null.

`event_type` mapping for the economic calendar: Finnhub's `event` field is free-text. Maintain a small mapping dict keyed on common Finnhub event names (`"CPI"` → `cpi_release`, `"FOMC Statement"` → `fomc`, etc.). Unknown events fall back to `other`.

Earnings calendar fills `earnings_event_details.eps_consensus`, `eps_actual`, `revenue_consensus_usd`, `revenue_actual_usd`, `expected_call_time` (Finnhub's `hour` field maps to `bmo`/`amc`/`dmh`).

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API key works.
- [ ] `client.py` integrates `with_retries(important)`, `RateLimiter`, and `track_run`.
- [ ] `news.collect_news()` writes `news_articles` rows with body text persisted on disk and `body_path` populated, plus `news_article_tickers` rows.
- [ ] `news.collect_news()` stamps `source_credibility_tier` from `news_outlets.yaml` lookup.
- [ ] `news.collect_news()` populates `vendor_sentiment_score` and `vendor_sentiment_label` as null (Finnhub doesn't pre-compute sentiment — Marketaux does).
- [ ] `calendar.collect_earnings_calendar()` writes `event_calendar` + `earnings_event_details` correctly.
- [ ] `calendar.bootstrap_earnings_calendar()` covers forward 90 days.
- [ ] `calendar.collect_economic_calendar()` maps Finnhub event names to the storage `event_type` enum via a documented dict.
- [ ] `calendar.collect_ipo_calendar()` and `calendar.collect_fda_calendar()` write `event_calendar` rows.
- [ ] Re-running any function on the same window produces no duplicate rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked SDK responses cover the above.
- [ ] `uv run pytest` passes.
