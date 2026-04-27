---
status: done
completed_date: 2026-04-26
commit_id: 034e302
---

# 05h — SEC EDGAR vendor adapter

## Goal

Implement `src/alphamind/data_sources/sec_edgar/` to pull 8-K filings (material events) from SEC EDGAR's RSS feed into `news_articles`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — `news_articles`, `news_article_tickers`
- `docs/design/01-data-layer/api-key-checklist.md` § SEC EDGAR — User-Agent header requirement, endpoint inventory
- `docs/design/01-data-layer/external/qualitative.md` § 1a — 8-K and short-report context
- `docs/design/01-data-layer/api-failure-handling.md` — Qual1 important

## Depends on

- 04 (shared library `_common.py`)

## Scope

In scope: under `src/alphamind/data_sources/sec_edgar/` —
- `client.py` — HTTP wrapper around SEC EDGAR (no auth; requires `User-Agent: AlphaMind <ops_email>` header on every request). Integrates `with_retries(important)`, `RateLimiter` (SEC requests max 10 req/sec), `verify_connectivity()`.
- `rss.py`:
  - `collect_8k_filings(since)` — pulls 8-K filings via the EDGAR full-text search RSS or the daily filings index; filters by universe-ticker CIKs from `asset_universe.cik`. Writes `news_articles` with `source='sec_edgar_8k_rss'`, `source_outlet='SEC EDGAR'`, `headline_text` set to the 8-K item description, `body_path` populated when the filing's primary document is fetched and stored to disk.
  - `news_article_tickers` populated with the filing's ticker (resolved via CIK → ticker join from `asset_universe`); `is_primary=1`.
  - `article_id` is the EDGAR accession number (always unique).
  - No bootstrap function — forward-only.
- Unit tests with mocked HTTP responses.

Out of scope:
- 10-K, 10-Q, Form 4, Form 13F, etc. — Q5 fundamentals and Q5g insider data, deferred.
- XBRL companyfacts parsing — Q5 fundamentals, deferred.
- Filing body content extraction beyond storing the primary HTML document.

## Notes

SEC requires identifying `User-Agent` header on every request: `<application name> <admin email>`. Set via `data_sources.yaml.providers.sec_edgar.user_agent` (read from env if needed for ops email).

Rate limit: SEC's stated maximum is 10 req/sec. Set `data_sources.yaml.providers.sec_edgar.rate_limit_per_minute = 600` (= 10/sec). The token-bucket prevents bursts.

CIK → ticker resolution: read from `asset_universe.cik`. Filings for tickers without a populated `cik` skip silently (warn-log).

8-K item types (Item 1.01, 2.02, 5.02, etc.) appear in the filing description. Capture the item codes as `topic_tags` JSON (e.g., `["8k", "item_2.02"]`) for downstream filtering.

`headline_text` should be a synthesized one-line summary: `<ticker> 8-K filed <date> — <item description>`. The structured EDGAR feed provides the item description.

Body retrieval: the 8-K's primary document URL is in the filing index. Fetch the HTML, strip to plain text, write to `%USERPROFILE%\AlphaMind\data\news\<YYYY>\<MM>\<article_id>.txt`. Skip body retrieval if the rate budget is tight; `body_path` left null.

## Acceptance criteria

- [ ] `client.py` sends a `User-Agent` header on every request.
- [ ] `client.py` has `verify_connectivity()` that exits 0 on a successful EDGAR endpoint reach.
- [ ] `client.py` integrates `with_retries(important)`, `RateLimiter`, and `track_run`.
- [ ] `rss.collect_8k_filings()` writes `news_articles` rows for 8-K filings on universe tickers, with `source='sec_edgar_8k_rss'`.
- [ ] `news_article_tickers` populated correctly via CIK resolution.
- [ ] `topic_tags` JSON includes the 8-K item codes when present.
- [ ] `article_id` is the EDGAR accession number.
- [ ] Body text persisted to disk when fetched; `body_path` populated.
- [ ] Re-running on the same window produces no duplicate rows.
- [ ] On failure, `collection_runs` records `failed`; no data rows.
- [ ] Unit tests with mocked HTTP responses cover the above.
- [ ] `uv run pytest` passes.
