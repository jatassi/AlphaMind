# 04a — News digest renderer

## Goal

Implement the deterministic news digest renderer the qualitative researcher consumes as in-context input. Reads clustered headlines from `news_articles` joined to `news_article_clusters` (produced by story 03c) over a since-last-invocation window, applies the design doc's three-step ranking algorithm (deduplication → composite scoring → top-N selection per sector bucket), and produces the structured digest text format with the contracted reference IDs (`ND-M*`, `ND-T*`, `ND-F*`, `ND-E*`, `ND-EC*`, `ND-HP*`). The renderer is a pure function over a SQLAlchemy session and `as_of` / `last_invocation_time` parameters; story 05's bundle assembler concatenates its output into the user message.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Format — the literal section markers and per-headline line shapes the renderer emits.
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Sections — sector buckets (MACRO, TECH/SEMIS, FINANCIALS, ENERGY), earnings calls section, high-priority flags section, and the cap rules per section.
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Reference IDs — the `ND-*` reference scheme; section-prefix per bucket; sequential indexing within each section starting at 1.
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Ranking algorithm — Step 1 deduplication (one cluster representative per cluster), Step 2 composite scoring (source credibility + recency + universe-ticker mention + cluster size + high-priority override), Step 3 selection (top 5 per sector bucket).
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Type tags — the `HeadlineType` taxonomy; ties into the high-priority section's eligibility rules.
* `docs/design/03-analysis-layer/qualitative-research.md` § News digest § Token budget — the ~1,000–1,200-token target; renderer asserts a soft warning above this.
* `src/alphamind/data_sources/news/clustering.py` (story 03c) — the `STAGE_*`, `CLUSTER_SEAL_HOURS`, and `news_article_clusters` rows the renderer reads.
* `src/alphamind/persistence/models.py` § `NewsArticles`, `NewsArticleTickers`, `EarningsEventDetails`, `EventCalendar` — the read targets for sector buckets and the earnings-calls section.
* `src/alphamind/distillation/sector_assembly.py` § `load_sector_roster` — sector membership for ticker-to-bucket assignment.
* `src/alphamind/data_sources/_common.py` § `HeadlineType` — the canonical type-tag enum.

## Depends on

* ALP-245 — story 03c lands the clustering pipeline, the `news_article_clusters` table, and `news_articles.cross_ticker_cluster_id`. The renderer reads cluster representatives.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/news_digest.py` and `tests/analysis/qualitative_research/test_news_digest.py`.

### 1\. Records

`DigestEntry(BaseModel, frozen=True)` — one rendered headline carrying its assigned reference ID and per-headline metadata: `reference_id: str` (e.g., `"ND-T1"`), `cluster_id: str | None`, `published_at: datetime`, `tier: Literal["tier_1", "tier_2", "tier_3"]`, `headline: str`, `source_outlet: str`, `tickers: tuple[str, ...]`, `tags: tuple[HeadlineType, ...]`.

`NewsDigest(BaseModel, frozen=True)` — the renderer's output: `as_of: datetime`, `last_invocation_time: datetime`, `total_collected: int` (count before deduplication and selection), `total_shown: int`, `entries: tuple[DigestEntry, ...]`, `digest_text: str`. The `digest_text` is what the bundle assembler embeds into the user message; the structured `entries` are kept for downstream auditing and for the validator's reference-ID set (story 03b's catalyst-watch validator does not currently consume them, but the wider validation framework may).

### 2\. Composite-score weights

Encode every weight as a named module-level constant per the design doc § Ranking algorithm § Step 2:

```python
TIER_SCORE_BY_TIER: Mapping[str, float] = {"tier_1": 3.0, "tier_2": 2.0, "tier_3": 1.0}
RECENCY_WEIGHT = 1.0
UNIVERSE_TICKER_BOOST = 1.5
CLUSTER_SIZE_WEIGHT = 0.5  # log-scaled per design
TOP_N_PER_SECTOR_BUCKET = 5
HIGH_PRIORITY_MAX = 3
DIGEST_TOTAL_TOKEN_SOFT_CAP = 1200
```

Each constant's docstring quotes the design-doc § Ranking algorithm § Step 2 line that motivated it.

### 3\. Renderer

`render_news_digest(session: Session, *, invocation_id: str, as_of: datetime, last_invocation_time: datetime, sector_roster: Mapping[Sector, frozenset[str]]) -> NewsDigest`

Procedure:

* Pull `news_articles` rows where `published_at >= last_invocation_time` joined to `news_article_clusters` for cluster size. Headlines without a `cross_ticker_cluster_id` are treated as singleton clusters (`headline_count = 1`) for ranking purposes.
* Step 1 — deduplicate within each sector bucket. For each cluster, retain exactly one representative — the highest-credibility-tier member. Ties resolve to the earliest `published_at`.
* Step 2 — score each surviving representative per the weight constants.
* Step 3 — bucket into sectors via `sector_roster`. A headline goes to the highest-mentioned sector by primary-ticker membership; cross-sector headlines (matching multiple sector tickers) fall to MACRO; ticker-less headlines bucket by `topic_tags` (e.g., `macro_data` → MACRO; `regulatory` not tied to a sector → MACRO).
* Take top 5 per bucket; if fewer exist, show what's available (no padding).
* Earnings-calls section: pull `event_calendar` joined to `earnings_event_details` for events with `event_type='earnings'` and `reported_at` in `[last_invocation_time, as_of]`. Render each as `[ND-EC{N}] {ticker} reported {timestamp}: EPS ${actual} vs ${est} est, rev ${actual}b vs ${est}b est`.
* High-priority section: derive eligibility at render time from the union of `topic_tags` ∈ `{m_and_a, short_report, activist, regulatory, geopolitical}` plus `tier == "tier_1"` plus a tag-specific rule (e.g., `m_and_a` requires the headline text to indicate "rumor" rather than completed; the renderer's docstring names the per-tag eligibility rule). Cap at 3. High-priority items also appear in their sector bucket if top-5 — duplication is intentional per the design.
* Reference-ID assignment: per-section sequential starting at 1. Prefix per bucket: `ND-M`, `ND-T`, `ND-F`, `ND-E`, `ND-EC`, `ND-HP`.
* Render the structured text per § Format. The header line counts `total_collected` (all in-window articles) vs `total_shown` (assigned a reference ID).
* Determinism check: identical inputs produce a byte-identical `digest_text`.

### 4\. Per-section rendering helpers

Small helpers to keep the top-level function readable: `_render_header`, `_render_macro_section`, `_render_sector_section(bucket_name, entries)`, `_render_earnings_section`, `_render_high_priority_section`. Each produces a string ending in `\n`. Bucket-name labels match the design literal markers (`MACRO / CROSS-SECTOR`, `TECH / SEMIS`, `FINANCIALS`, `ENERGY`, `EARNINGS CALLS SINCE LAST INVOCATION`, `HIGH-PRIORITY FLAGS`).

### 5\. Token-budget warning

Approximate the rendered text's token count using a simple character-count heuristic (`len(text) / 4` is a workable approximation; if `tiktoken` is already a dep use it). When the count exceeds `DIGEST_TOTAL_TOKEN_SOFT_CAP`, log a warning at INFO level naming the count and the configured cap. Do not truncate — the agent's prompt handles oversized digests by triaging harder; the renderer produces the full rendered shape.

### Out of scope

* Implementing the clustering pipeline — story 03c.
* Pre-computing or caching the digest per invocation — story 06's runner calls the renderer fresh each invocation.
* Unicode normalization beyond what story 03c's clustering already applies.
* The headline-clustering "primary_theme" assignment — already produced by story 03c on `news_article_clusters.primary_theme`; the renderer reads it.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.news_digest import render_news_digest, NewsDigest, DigestEntry` resolves.
- [ ] `render_news_digest` against a populated test database produces a `NewsDigest` whose `digest_text` carries the literal section markers (`--- MACRO / CROSS-SECTOR (top 5) ---`, etc.) per design § Format.
- [ ] Reference IDs are sequential within each section starting at 1 (`ND-T1`, `ND-T2`, …).
- [ ] A cluster of 4 syndicated headlines is represented by exactly one digest entry (the highest-credibility-tier member).
- [ ] Tier-1 sources outrank Tier-2 with all else equal (per `TIER_SCORE_BY_TIER`).
- [ ] Recency boost is monotonic in `published_at`.
- [ ] Universe-ticker mention applies the configured boost.
- [ ] High-priority section caps at 3 entries; high-priority items also appear in their sector bucket if otherwise top-5.
- [ ] Earnings-calls section is empty when no universe ticker reported in the window.
- [ ] Identical inputs produce a byte-identical `digest_text` (determinism check via two consecutive renders).
- [ ] When the rendered text exceeds the configured soft cap, a warning is logged but no exception is raised.
- [ ] `tests/analysis/qualitative_research/test_news_digest.py` covers each acceptance criterion above. Tests pass under `uv run pytest tests/analysis/qualitative_research/test_news_digest.py -n auto`.
- [ ] No magic numeric thresholds; all weights and caps are module constants.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_news_digest.py -n auto`. Run `uv run mypy src/alphamind/analysis/qualitative_research/news_digest.py` clean. Spot-check the rendered text against a small fixture by manual reading — every section header is present, reference IDs are sequential, the header line accurately reports collected vs shown counts.
