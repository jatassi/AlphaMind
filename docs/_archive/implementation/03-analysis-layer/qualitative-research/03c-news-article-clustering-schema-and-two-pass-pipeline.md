# 03c — News-article clustering schema and two-pass pipeline

## Goal

Implement the deterministic two-pass headline clustering pipeline the news digest depends on, plus the schema additions that persist its output and the collector cron entry that runs the pipeline at ingestion. Stage 1 collapses syndicated wire copies (the same Reuters or AP item republished by Yahoo, MarketWatch, CNBC within minutes) using normalized-Levenshtein matching; Stage 2 clusters source-canonical headlines covering the same event from different angles using SimHash similarity plus a shared-ticker constraint. The pipeline writes one row per cluster to a new `news_article_clusters` table and stamps each member's `cross_ticker_cluster_id` on `news_articles`. Story 04a's digest renderer reads these rows at invocation time. Per the operator-confirmed resolution baked into this story: every clustering threshold is encoded as a named module constant (definitional cutoff, not a Class A tunable), and the pipeline runs as a collector cron entry every 30 minutes during market hours rather than at qualitative-researcher invocation time.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Headline clustering — the authoritative algorithm: Stage 1 (normalized-Levenshtein 0.90 within 30 minutes), Stage 2 (SimHash Jaccard 0.60 over 64-bit fingerprints + shared-ticker requirement within 6 hours), 24-hour seal, single-link agglomeration, cluster ID = earliest source-canonical member's `article_id`.
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Cluster metadata — the `news_article_clusters` columns and the `BreakingHeadline.cross_ticker_cluster_id` linkage.
* `docs/design/01-data-layer/external/qualitative.md` § 1a — source credibility tiers and ticker tagging that the clustering algorithm operates over.
* `docs/design/01-data-layer/schema/news_sentiment.py` § `BreakingHeadline`, `HeadlineCluster` — the schema-level shapes the design references; if these are documentation rather than runtime artifacts, ignore — the runtime tables are in `src/alphamind/persistence/models.py`.
* `src/alphamind/persistence/models.py` § `NewsArticles`, `NewsArticleTickers` — the existing tables Stage 1 and Stage 2 read from.
* `src/alphamind/data_sources/_common.py` § `HeadlineType` — the canonical type-tag enum referenced in cluster `primary_theme`.
* `src/alphamind/distillation/qualitative_derived.py` § `NON_NEUTRAL_DOMINANCE_THRESHOLD` — the canonical precedent for "definitional cutoff encoded as a named module constant"; this story applies the same pattern.
* `alembic/versions/` (latest `*.py`) — the most recent collector migration; mirror its style for the new migration.
* `config/collector_schedule.yaml` — the existing collector cron entries; mirror their style for the new clustering entry.
* `src/alphamind/distillation/sector_assembly.py` § `load_sector_roster` — sector membership the clustering pipeline references when assigning a cluster's primary sector (for the digest renderer's sector-bucketing).

## Depends on

* ALP-241 — story 01 lands the package skeletons that house the new module's tests.

## Scope

In scope under `src/alphamind/data_sources/news/clustering.py` (new module), `alembic/versions/<timestamp>_news_article_clusters.py`, `config/collector_schedule.yaml`, and `tests/data_sources/news/test_clustering.py`.

### 1\. Alembic migration

Add a new migration with two changes.

**Add table** `news_article_clusters`:

* `cluster_id: TEXT PRIMARY KEY` — the earliest source-canonical member's `article_id`.
* `primary_theme: TEXT NOT NULL` — modal `HeadlineType.value` across cluster members; ties resolve to the modal member's first `topic_tags` entry.
* `headline_count: INTEGER NOT NULL` — number of source-canonical headlines (post-syndication, distinct outlets only); the count Step 2 of the digest ranking algorithm scores on.
* `first_seen_at: TEXT NOT NULL` — UTC ISO8601 of the earliest member.
* `last_seen_at: TEXT NOT NULL` — UTC ISO8601 of the most recent member.
* `tickers_involved: TEXT NOT NULL` — comma-separated union of `(primary_ticker, tickers_mentioned)` across cluster members. (TEXT chosen for consistency with `news_articles.topic_tags`; later refactor to a join table if querying by ticker becomes a hot path.)
* `sealed_at: TEXT NOT NULL` — `first_seen_at + 24h`. Headlines arriving past this seal that match the cluster's similarity profile start a new cluster.
* Index on `(first_seen_at)` for the renderer's invocation-windowed reads.

**Add column** `news_articles.cross_ticker_cluster_id: TEXT NULL` with foreign-key reference to `news_article_clusters.cluster_id` (`ON DELETE SET NULL`). Index on this column for the renderer's join.

The migration is reversible (`upgrade` and `downgrade` both implemented).

### 2\. Clustering primitives — definitional constants

Encode every threshold from the design doc as a named module-level constant. These are definitional cutoffs, not Class A tunables — they are not subject to ongoing calibration the way the threshold-calibration framework's tunables are. Mirrors the `NON_NEUTRAL_DOMINANCE_THRESHOLD` precedent in `alphamind.distillation.qualitative_derived`:

```python
STAGE_1_LEVENSHTEIN_RATIO_MIN = 0.90
STAGE_1_TIME_WINDOW_MINUTES = 30
STAGE_2_SIMHASH_JACCARD_MIN = 0.60
STAGE_2_TIME_WINDOW_HOURS = 6
CLUSTER_SEAL_HOURS = 24
```

Each constant's docstring quotes the design-doc § Headline clustering line that motivated it. If paper trading later reveals one of these thresholds is wrong, raise it as a future story rather than tuning silently in code.

### 3\. Stage 1 — source canonicalization

`canonicalize_syndication(articles: Sequence[NewsArticles]) -> Mapping[str, str]` — input is articles published within a time window; output maps `article_id` → `source_canonical_id` (the earliest member's `article_id` for each match group).

Match condition (from the design): normalized-Levenshtein ratio of `headline_text` ≥ 0.90 within 30 minutes. Normalization: lowercase, strip punctuation, normalize whitespace, normalize ticker references (`$AAPL` → `AAPL`), drop outlet-specific lead tokens (`BREAKING:`, `UPDATE:`, `EXCLUSIVE:`).

Implementation: pure function (no I/O); takes pre-fetched articles and returns the mapping. The persistence-side caller assembles the input set from the database. Use `python-Levenshtein` if it is already a dependency, otherwise `difflib.SequenceMatcher.ratio()` (built-in, sufficient for short strings).

### 4\. Stage 2 — event clustering

`cluster_events(canonical_articles: Sequence[NewsArticles], ticker_index: Mapping[str, frozenset[str]]) -> list[HeadlineCluster]` — input is source-canonical articles plus a per-article ticker set (built from `news_article_tickers`); output is a list of `HeadlineCluster` Pydantic records (the in-memory shape; the caller persists them as `news_article_clusters` rows).

Match condition (all three required, per the design):

1. SimHash Jaccard similarity over 64-bit fingerprints of normalized `headline_text` ≥ 0.60. Token set after Stage-1 normalization, hashed via SimHash.
2. At least one shared ticker between the headlines' ticker sets. Ticker-less headlines cluster only with other ticker-less headlines whose `topic_tags` intersect by at least one tag.
3. Both members fall within 6 hours of the cluster's `first_seen_at`.

Cluster ID: the `article_id` of the earliest source-canonical member. Single-link agglomeration on the matched-pair graph — a headline joins an existing cluster if it matches any current member.

SimHash implementation: write a tiny `_simhash` helper in this module (≤ 30 LOC) producing a 64-bit fingerprint via the standard `hashlib.md5`-then-bit-vote algorithm; `_jaccard_64bit(a, b) -> float` returns the bit-overlap ratio. Do not introduce a third-party SimHash dependency for ~30 lines of math.

### 5\. Persistence layer

`refresh_news_clusters(session: Session, *, as_of: datetime, lookback_window_hours: int = 48) -> RefreshResult` — top-level function. Reads `news_articles` rows where `published_at >= as_of - lookback_window_hours`, runs Stage 1 + Stage 2, and writes `news_article_clusters` rows + `news_articles.cross_ticker_cluster_id` updates inside one transaction. Idempotent: re-running on the same window does not create duplicate cluster rows or change cluster IDs (a cluster's ID is the earliest member's `article_id`, which is stable).

`RefreshResult` is a small Pydantic record with `articles_processed: int`, `clusters_written: int`, `clusters_updated: int`, `articles_unclustered: int` for observability.

### 6\. Collector cron wiring

Add a `collector_schedule.yaml` entry that runs `refresh_news_clusters` every 30 minutes during US equity market hours (9:30 ET to 16:00 ET) plus an hourly run during the 4:00 ET – 9:30 ET pre-market window when overnight news accumulates. Mirror the cron-string format the existing collector entries use. The renderer (story 04a) reads cluster rows at qualitative-researcher invocation time without re-running the pipeline — clustering is the producer, the renderer is the consumer, and they run on independent cadences.

If the collector framework requires a top-level Python entry point (e.g., a `__main__`-style script), add it under `scripts/refresh_news_clusters.py` calling `refresh_news_clusters(session, as_of=datetime.now(UTC))`.

Tests at `tests/data_sources/news/test_clustering.py` cover:

* Stage 1: identical headlines from `BREAKING:` / `UPDATE:` prefixes and different outlets within 30 minutes collapse to one canonical.
* Stage 1: headlines published 31 minutes apart do not match even at high Levenshtein ratio.
* Stage 2: two source-canonical headlines about the same NVDA event published 4 hours apart with SimHash Jaccard ≥ 0.60 cluster together.
* Stage 2: same SimHash but no shared ticker → no cluster.
* Stage 2: ticker-less macro headlines with overlapping `topic_tags` cluster; ticker-less without tag overlap do not.
* Cluster ID stability: re-running `refresh_news_clusters` produces identical cluster IDs for the same input window.
* `cross_ticker_cluster_id` is `NULL` for unclustered headlines.

### Out of scope

* The digest renderer (composite-score ranking, sector buckets, output text format) — story 04a.
* Tagging headlines as high-priority — derivative; story 04a computes membership at render time from `topic_tags` plus tier.
* Backfill of `cross_ticker_cluster_id` for historical headlines beyond the lookback window — defer; the digest is invocation-windowed.

## Acceptance criteria

- [ ] Alembic migration upgrades cleanly on a fresh database and on a populated production-shaped database, and downgrades without error.
- [ ] `news_article_clusters` table exists with the named columns and the `(first_seen_at)` index.
- [ ] `news_articles.cross_ticker_cluster_id` column exists with the foreign-key reference and is indexed.
- [ ] `STAGE_1_LEVENSHTEIN_RATIO_MIN`, `STAGE_1_TIME_WINDOW_MINUTES`, `STAGE_2_SIMHASH_JACCARD_MIN`, `STAGE_2_TIME_WINDOW_HOURS`, `CLUSTER_SEAL_HOURS` are module constants in `clustering.py` with docstrings citing § Headline clustering.
- [ ] `canonicalize_syndication`, `cluster_events`, and `refresh_news_clusters` are importable from `alphamind.data_sources.news.clustering`.
- [ ] `refresh_news_clusters` is idempotent: running twice on the same window produces identical cluster rows.
- [ ] `config/collector_schedule.yaml` carries an entry that invokes `refresh_news_clusters` every 30 minutes during market hours plus hourly during pre-market.
- [ ] `tests/data_sources/news/test_clustering.py` exists and covers each test case named above. Tests pass under `uv run pytest tests/data_sources/news/test_clustering.py -n auto`.
- [ ] No test or production code introduces hard-coded threshold values; every threshold reads from the named constant.
- [ ] No third-party SimHash library is added to `pyproject.toml`.

## Verification

Run `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` against the test SQLite database to confirm the migration is reversible. Run `uv run pytest tests/data_sources/news/test_clustering.py -n auto`. Run `uv run ruff check . && uv run mypy` clean. Spot-check `refresh_news_clusters` against a production-shaped database fixture (small handful of recent `news_articles` rows) and confirm the produced cluster rows match an expected manual clustering by inspection. Confirm the new `collector_schedule.yaml` entry parses via the existing collector-config bootstrap.
