---
status: in_progress
completed_date:
commit_id:
---

# 05g — Marketaux vendor adapter

## Goal

Implement `src/alphamind/data_sources/marketaux/` to pull Qual1 news with vendor pre-computed sentiment from Marketaux into `news_articles` (with `body_path` on disk) and `news_article_tickers`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `news_articles` (especially `vendor_sentiment_score`, `vendor_sentiment_label`), `news_article_tickers`
- `docs/design/01-data-layer/api-key-checklist.md` § Marketaux — endpoint inventory + 100 req/day cap
- `docs/design/01-data-layer/api-failure-handling.md` — Qual1 important
- `config/news_outlets.yaml` — outlet credibility tier mapping

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/marketaux/` —
- `client.py` — HTTP wrapper around Marketaux REST API (no SDK; use `httpx`). Integrates `with_retries(important)`, `RateLimiter` (configured for the daily-cap reality), `verify_connectivity()`.
- `news.py`:
  - `collect_news(ticker_scope, since)` — pulls `/v1/news/all?symbols=<ticker>&filter_entities=true` per ticker batch and `/v1/news/all?countries=us` for market-wide. Writes `news_articles` (with `vendor_sentiment_score` from Marketaux's `entities[].sentiment_score` and `vendor_sentiment_label` from a derived label using Marketaux's score thresholds — typically positive >0.15, negative <-0.15, neutral otherwise). Body text persists to disk; `body_path` populated.
  - `news_article_tickers` populated from Marketaux's `entities[]` array; primary ticker = the ticker that drove the query, others get `is_primary=0`.
  - `article_id` synthesized as SHA-256 of `(source='marketaux', url, published_at)`.
  - No bootstrap function — forward-only.
- Unit tests with mocked HTTP responses.

Out of scope:
- Marketaux's stock screener and other endpoints — only the news endpoints are in scope.
- Per-platform sentiment breakdown (covered by Qual2 social, deferred).

## Notes

Marketaux free-tier cap: **100 requests per day**. Set `data_sources.yaml.providers.marketaux.rate_limit_per_minute` low enough to spread across the day (e.g., 0.07/min ≈ 4/hour). The 30-min cron schedule from `runner.md` produces 48 fires/day at 30-min cadence — well under cap when batched per cron fire.

Batch tickers per query: Marketaux accepts comma-separated symbol lists (max 5 per request on free tier). The collector should batch the ~65-ticker universe into 13 calls per cron fire if it queries all tickers; or rotate (e.g., one sector per cron fire) to stay under daily cap.

Sentiment score is per-entity (per-ticker mention) in Marketaux's response. Use the score for the article's primary ticker (the one we queried) for `news_articles.vendor_sentiment_score`; the per-ticker variants flow into a TODO for future enrichment.

`source_credibility_tier` stamped at ingestion via `news_outlets.yaml` keyed on Marketaux's `source` field.

## Acceptance criteria

- [ ] `client.py` has `verify_connectivity()` that exits 0 when the API token works.
- [ ] `client.py` integrates `with_retries(important)`, `RateLimiter`, and `track_run`.
- [ ] `news.collect_news()` writes `news_articles` rows with `vendor_sentiment_score` populated from Marketaux's per-entity score.
- [ ] `vendor_sentiment_label` derived from the score per documented thresholds.
- [ ] Body text persists to `%USERPROFILE%\AlphaMind\data\news\<YYYY>\<MM>\<article_id>.txt`, `body_path` populated.
- [ ] `news_article_tickers` populated for every ticker mentioned in `entities[]`.
- [ ] `source_credibility_tier` stamped from `news_outlets.yaml` lookup.
- [ ] Re-running on the same window produces no duplicate rows (idempotent on `article_id`).
- [ ] Per-cron-fire request count stays inside the 100/day budget under steady-state cadence.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
