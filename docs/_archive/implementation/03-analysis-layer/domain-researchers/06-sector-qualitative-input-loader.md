## Goal

Implement the loader that reads sector-specific qualitative inputs from the data layer and returns a typed `SectorQualitativeInput` value object containing the slice of qualitative data each sector researcher's design doc names. Story 08's input-bundle assembler renders this value object into the LLM's user message.

This story carries an expanded scope to fix two upstream gaps that block correct sector-tag matching:

1. The design intent for `news_articles.topic_tags` is a JSON array of canonical `HeadlineType` values, normalized at the collector boundary via `config/headline_tag_mapping.yaml`. The mapping yaml exists and is fully populated, but the producer adapters do not apply it — Marketaux writes raw vendor topic names, SEC EDGAR writes raw item codes. This story promotes `HeadlineType` from the design schema to production code and patches the two news adapters to normalize before writing.
2. The existing distillation-Q7 consumer (`src/alphamind/distillation/q7/correlation_regime_change.py:314–321`) splits `topic_tags` on comma — wrong format (the value is JSON-encoded) and dead code against current data. Fix it to JSON-parse. No backfill of historical data: old rows keep vendor-raw tags and lose tag-recall; new ingestion uses canonical tags.

The story also adds an Alembic migration introducing `event_calendar.sectors` since per-sector event filtering needs it, and drops the high-priority promote-to-top concept (the `is_high_priority` column does not exist in the data layer).

## Reading

* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — second row points at `qualitative.md §6a` (AI narrative, supply chain, product cycle, competitive dynamics, export controls).
* `docs/design/03-analysis-layer/domain-researchers/financials.md` § Inputs — `qualitative.md §6c` (credit, rate environment, M&A, regulatory posture, consumer/payment, crypto).
* `docs/design/03-analysis-layer/domain-researchers/energy.md` § Inputs — `qualitative.md §6b` (OPEC, inventory narrative, weather, pipeline).
* `docs/design/01-data-layer/external/qualitative.md` § 6 — sector-specific catalysts, the design-time signal taxonomy.
* `docs/design/01-data-layer/external/qualitative.md` § 1a — credibility tier tagging (`tier_1`/`tier_2`/`tier_3`).
* `docs/design/01-data-layer/collector/storage.md` § `news_articles`, § `event_calendar` — schema documentation; [storage.md](<http://storage.md>) is authoritative on `topic_tags` being JSON of canonical `HeadlineType`.
* `docs/design/01-data-layer/collector/data-sources.md` § News adapters — design-time intent that adapters normalize through `headline_tag_mapping.yaml`.
* `docs/design/01-data-layer/schema/_common.py` § `HeadlineType` — the canonical taxonomy enum (14 members) to promote to production.
* `config/headline_tag_mapping.yaml` — vendor → canonical mappings for marketaux, finnhub, sec_edgar_8k, generic rss; already populated.
* `src/alphamind/persistence/models.py` — actual `news_articles`, `news_article_tickers`, `event_calendar`, `sector_classification` ORM shapes (the authoritative ground truth).
* `src/alphamind/data_sources/_common.py` — where `RetryShape` and other production data-source enums live; `HeadlineType` will be promoted to this module.
* `src/alphamind/data_sources/marketaux/news.py:121–123` — current vendor-raw write of `topic_tags`.
* `src/alphamind/data_sources/sec_edgar/rss.py:303` — current item-code write of `topic_tags`.
* `src/alphamind/distillation/q7/correlation_regime_change.py:314–321` — broken split-on-comma consumer to fix.
* `src/alphamind/distillation/sector_assembly.py` § `load_sector_roster` — canonical sector→ticker reader.
* `src/alphamind/analysis/_shared.py` (story 02) — `Sector`, `_SECTOR_AUDIENCE_MAP` reused here.

## Depends on

* **02** (<issue id="d47bb088-b447-4f25-b67c-67d73739dad4">ALP-133</issue>) — `Sector` enum and `_SECTOR_AUDIENCE_MAP`.

## Scope

The work has seven labeled parts. They land in one PR (this story's worktree branch); ordering within the worktree is at the implementer's discretion as long as the resulting branch passes the test suite end-to-end.

**(A) Promote HeadlineType to production code.** Add `HeadlineType` as a `StrEnum` to `src/alphamind/data_sources/_common.py` (alongside `RetryShape`). The enum members are exactly those in `docs/design/01-data-layer/schema/_common.py` § `HeadlineType` — 14 values: `breaking`, `earnings_related`, `m_and_a`, `analyst_action`, `regulatory`, `geopolitical`, `macro_data`, `insider_activity`, `short_report`, `activist`, `product_launch`, `supply_chain`, `guidance`, `sector_rotation`. Re-export from the package's `__init__.py` if the existing convention does so for `RetryShape`.

**(B) headline_tag_mapping.yaml loader.** Add a small loader in `src/alphamind/data_sources/_common.py` (or a new sibling module if `_common.py` is already large) that reads `config/headline_tag_mapping.yaml` once at import time and exposes a typed mapping `dict[str, dict[str, HeadlineType]]` keyed by vendor name (`marketaux`, `finnhub`, `sec_edgar_8k`, `rss_topic`). Unmapped vendor tags drop silently per the mapping yaml's documented behavior. Surface a typed `normalize_vendor_tags(vendor: str, raw_tags: Iterable[str]) -> list[HeadlineType]` helper that the adapters call.

**(C) Patch news adapters.** In `src/alphamind/data_sources/marketaux/news.py`, replace the current `topic_tags = json.dumps(names) if names else None` block (around line 121–123) with a call to `normalize_vendor_tags("marketaux", names)`, then JSON-encode the resulting `[h.value for h in canonical]` list. Empty list serializes to `None` (preserve existing nullability semantics). In `src/alphamind/data_sources/sec_edgar/rss.py`, replace the `topic_tags=json.dumps(_extract_item_codes(description))` call (around line 303) with the analogous normalization: extract item codes, normalize through `normalize_vendor_tags("sec_edgar_8k", codes)`, JSON-encode `[h.value for h in canonical]`. The `"8k"` synthetic tag the current adapter prepends is not in the mapping yaml — drop it; downstream consumers detect 8-K via `event_type` rather than a tag. The Finnhub news adapter does not write `topic_tags` today; do not add a new write path here.

**(D) Fix distillation Q7 consumer.** In `src/alphamind/distillation/q7/correlation_regime_change.py` (around line 314–321), replace the comma-split with a JSON parse. Defensively handle malformed JSON (catch `json.JSONDecodeError` and treat as empty tags rather than crashing — existing rows in the database may still carry vendor-raw text). Update the comment to reflect the JSON storage shape. Match the resulting tag list against `NARRATIVE_LAG_REGIME_TAGS` after coercing the parsed strings into `HeadlineType` members (drop strings that don't resolve to a valid `HeadlineType`).

**(E) Alembic migration for event_calendar.sectors.** Add a small reversible Alembic migration adding a TEXT `sectors` column to `event_calendar`, defaulting NULL. The column stores comma-separated `Sector` values (e.g., `"tech_semis,financials"`) — *not* JSON, since this column is added fresh by this story and consumers parse it deterministically. Migration `up()` adds the column; `down()` removes it cleanly. The `event_calendar` ORM model in `src/alphamind/persistence/models.py` gets the corresponding `Mapped[str | None]` field.

**(F) Pydantic records.** Under `src/alphamind/analysis/domain_researchers/qualitative_input.py`:

```python
class HeadlineEntry(BaseModel, frozen=True):
    headline: str
    source_outlet: str            # outlet name from news_articles.source_outlet
    tier: Literal["tier_1", "tier_2", "tier_3"]   # NULL → "tier_3"
    published_at: datetime        # UTC, tz-aware
    tickers: tuple[str, ...]      # universe tickers mentioned per news_article_tickers
    tags: tuple[HeadlineType, ...]  # parsed from topic_tags JSON; canonical only

class EventEntry(BaseModel, frozen=True):
    event_id: str
    event_name: str               # description column
    event_time: datetime          # scheduled_at, parsed to UTC tz-aware
    sectors: frozenset[Sector]    # parsed from new sectors column (comma-split)
    tickers: tuple[str, ...]      # aggregated across event_calendar rows for this event_id
    consensus: str | None         # earnings_event_details join — eps/revenue when event_type='earnings'

class SectorQualitativeInput(BaseModel, frozen=True):
    sector: Sector
    as_of: datetime               # UTC, tz-aware — invocation's effective time
    lookback_window_hours: int
    headlines: tuple[HeadlineEntry, ...]   # ordered by composite score descending
    events: tuple[EventEntry, ...]         # next 72h, ascending by event_time
    data_freshness: datetime      # latest ingested_at across the underlying tables
```

**(G) Loader.** Function signature:

```python
def load_sector_qualitative_input(
    session: Session,
    sector: Sector,
    as_of: datetime,
    *,
    lookback_window_hours: int = 24,
    max_headlines: int = 30,
) -> SectorQualitativeInput:
    ...
```

Sector ticker membership is read via `alphamind.distillation.sector_assembly.load_sector_roster(session, DOMAIN_RESEARCHER_BY_AUDIENCE[_SECTOR_AUDIENCE_MAP[sector]])`. Do not invent a parallel reader.

### Headline selection

Pull `news_articles` rows with `published_at >= as_of - lookback_window_hours` AND (any of the article's `news_article_tickers.ticker` rows is in the sector roster OR any parsed `HeadlineType` tag is in the sector's relevant-tag set). Parse `topic_tags` as JSON; defensively treat malformed JSON as empty tags (same defensive posture as the Q7 fix); drop string values that don't resolve to a valid `HeadlineType`.

### Per-sector relevant-tag sets

Encode as `frozenset[HeadlineType]` constants at module top, informed by `qualitative.md § 6` and the canonical `HeadlineType` enum:

* `_TECH_SEMIS_TAGS = frozenset({HeadlineType.SUPPLY_CHAIN, HeadlineType.PRODUCT_LAUNCH, HeadlineType.REGULATORY, HeadlineType.ANALYST_ACTION, HeadlineType.M_AND_A, HeadlineType.GUIDANCE, HeadlineType.EARNINGS_RELATED})`
* `_FINANCIALS_TAGS = frozenset({HeadlineType.REGULATORY, HeadlineType.MACRO_DATA, HeadlineType.M_AND_A, HeadlineType.ANALYST_ACTION, HeadlineType.GUIDANCE, HeadlineType.EARNINGS_RELATED})`
* `_ENERGY_TAGS = frozenset({HeadlineType.GEOPOLITICAL, HeadlineType.MACRO_DATA, HeadlineType.REGULATORY, HeadlineType.ANALYST_ACTION, HeadlineType.M_AND_A, HeadlineType.GUIDANCE, HeadlineType.EARNINGS_RELATED})`

Adding a new tag to one set is a one-line change.

### Composite-score ranking

Score each candidate headline as `tier_score × 1.0 + recency_score × 1.0 + ticker_mention_boost`. Tier score: `tier_1=3, tier_2=2, tier_3=1`. Recency score: linear decay from `as_of` (weight 1.0) to `as_of - lookback_window_hours` (weight 0.0). Ticker-mention boost: `×1.5` multiplier on the running total when at least one ticker on the article is in the sector roster. Take the top `max_headlines` by composite score; ties broken by `published_at` descending. No high-priority bypass — every headline is ranked the same way.

### Event selection

Pull `event_calendar` rows where `scheduled_at` is in `[as_of, as_of + 72h]` AND (the row's `sectors` column parses to include `sector` OR is `NULL`/empty). NULL is treated as cross-sector (FOMC etc.); rows whose parsed `sectors` excludes the requested sector are filtered out. Group rows by `event_id`; aggregate tickers across rows. Emit one `EventEntry` per `event_id`. For `event_type = 'earnings'`, join `earnings_event_details` for `eps_consensus` / `revenue_consensus_usd`; render as `consensus` string (e.g., `"EPS $1.23, revenue $50.0B"`). Other event types have `consensus = None`.

## Schema reconciliation notes

The data layer's actual schema is authoritative; this story aligns to it.

* `news_articles.topic_tags` (TEXT, nullable) stores JSON-encoded list of strings (canonical `HeadlineType.value`s after Part C). NULL → empty.
* `news_articles.source_credibility_tier` may be `NULL`; default to `"tier_3"` (lowest credibility).
* `news_articles.source_outlet` is the operator-facing outlet name; the `source` column is a vendor-id string (`"polygon"`, `"marketaux"`).
* `news_articles` has no `is_high_priority` column — drop the high-priority promote-to-top concept entirely. Every headline is ranked uniformly.
* `news_article_tickers` columns: `article_id`, `ticker`, `is_primary`, `vendor_sentiment_score`, `vendor_sentiment_label`. The loader joins on `article_id` and aggregates tickers per article.
* `event_calendar` columns (post-migration): `event_id`, `event_type`, `ticker` (singular, nullable), `scheduled_at`, `description`, `status`, `source`, `ingested_at`, `last_updated`, `sectors` (NEW; comma-separated TEXT). Macro events have `ticker IS NULL` and may have `sectors IS NULL` (cross-sector default).

## Unit tests

Under `tests/analysis/domain_researchers/test_qualitative_input.py` and `tests/data_sources/test_headline_normalization.py`:

* Empty database produces a `SectorQualitativeInput` with empty `headlines` and `events`; `data_freshness` is `as_of - lookback_window_hours` (document the choice).
* Headlines outside the lookback window are excluded.
* Headlines with `topic_tags = NULL` do not crash; the parsed tag tuple is empty.
* Headlines with malformed JSON `topic_tags` do not crash; the parsed tag tuple is empty.
* Headlines with at least one sector ticker are included regardless of tags.
* Headlines with at least one sector-relevant `HeadlineType` tag are included regardless of tickers.
* Headlines with vendor-raw `topic_tags` (historical rows) parse cleanly but produce empty canonical tag tuples (the tags don't resolve as `HeadlineType` members).
* Composite-score ranking respects all factors (tier × recency × ticker-mention boost).
* `max_headlines` truncates the ordered list.
* Events outside the 72h forward window are excluded.
* Events with `sectors IS NULL` are included for every sector (cross-sector default).
* Events with `sectors` parsing to a set including this sector are included.
* Events with `sectors` excluding this sector are excluded.
* Events grouped by `event_id` aggregate tickers correctly.
* `event_type='earnings'` events render `consensus` from joined `earnings_event_details`; non-earnings events have `consensus = None`.
* `data_freshness` reflects the latest `ingested_at` across `news_articles` and `event_calendar`.
* Alembic migration `up()` adds the `event_calendar.sectors` column; `down()` removes it cleanly.
* `normalize_vendor_tags("marketaux", ["earnings", "guidance", "unknown_tag"])` returns `[HeadlineType.EARNINGS_RELATED, HeadlineType.GUIDANCE]` (unmapped silently dropped).
* `normalize_vendor_tags("sec_edgar_8k", ["2.02", "5.02", "9.99"])` returns `[HeadlineType.EARNINGS_RELATED, HeadlineType.INSIDER_ACTIVITY]`.
* Marketaux adapter test: stub a vendor response with `topics: [{"name": "earnings"}, {"name": "macro"}]` → `news_articles.topic_tags` written as `'["earnings_related", "macro_data"]'`.
* SEC EDGAR adapter test: stub a description with item-codes `2.02` and `5.02` → `news_articles.topic_tags` written as `'["earnings_related", "insider_activity"]'`.
* Distillation Q7 test: a row with `topic_tags = '["macro_data"]'` triggers the regime-change branch when `MACRO_DATA in NARRATIVE_LAG_REGIME_TAGS`. A row with malformed JSON does not crash.

## Out of scope

* Earnings call transcripts (qual §4 — Tier-2 of `earnings_commentary`). Transcript ingestion is not yet built.
* Industry trade publications (DigiTimes, Argus Media, etc. — not in v1 ingestion).
* Social sentiment, prediction markets, options-flow narrative — those are the qualitative researcher's territory, not domain researchers.
* Rendering the `SectorQualitativeInput` to LLM input-bundle text — that is story 08.
* Backfill of historical `news_articles.topic_tags` rows from vendor-raw to canonical. Old rows stay vendor-raw and lose tag-recall; new ingestion produces canonical. Backfill is a separate ticket if/when needed.
* Adding `topic_tags` writes to the Finnhub news adapter. Currently it doesn't write the column; this story keeps that behavior.
* Broader changes to `src/alphamind/distillation/q7/`. Only the topic_tags consumer fix is in scope.
* Promoting the rest of `docs/design/01-data-layer/schema/_common.py` to production code. Only `HeadlineType` is in scope (it's the bottleneck for this story); other types stay design-only.

## Notes

The 24-hour default lookback window on headlines is informed by `qualitative-research.md § News digest`. The harness can override per invocation type when the `run_types/` resolver wiring lands; for now this is the default.

The composite-score ranking is duplicated in concept from the qualitative researcher's news-digest ranking algorithm. Consolidating into a shared utility is a future refactor; the qualitative researcher's work tree may pull this back.

`HeadlineEntry.tags: tuple[HeadlineType, ...]` (typed) over `tuple[str, ...]` (free-form) — the canonical taxonomy is closed and the production enum exists post-Part A; type the field to make ill-formed values fail at construction rather than at filter time.

`HeadlineEntry.tier` is a `Literal["tier_1", "tier_2", "tier_3"]` rather than an enum because the data-layer column is `TEXT` and uses these exact strings; coercing through an enum adds drift surface without buying type-safety the `Literal` doesn't already provide.

The `event_calendar.sectors` column uses comma-separated TEXT (not JSON) deliberately. The column is added fresh by this story; consumers parse a known shape with no legacy rows to handle. JSON would be over-engineering for a small comma-separated list of three known values.

The Q7 fix is in scope because the existing comma-split is dead code today (split on a JSON-encoded value yields a single malformed token that doesn't match any tag). Leaving it broken until a separate ticket lands risks the wrong-but-harmless code becoming load-bearing if someone refactors around it. Fix it now while the JSON shape is fresh in mind.

## Acceptance criteria

- [ ] `HeadlineType` is added to `src/alphamind/data_sources/_common.py` as a `StrEnum` with the 14 canonical members from the design schema; re-exported per the package's existing convention.
- [ ] A `headline_tag_mapping.yaml` loader exposes `normalize_vendor_tags(vendor, raw_tags) -> list[HeadlineType]`; unmapped tags drop silently; vendor keys cover marketaux, finnhub, sec_edgar_8k, rss_topic.
- [ ] `src/alphamind/data_sources/marketaux/news.py` writes `topic_tags` as JSON of canonical `HeadlineType.value`s after normalization; the synthetic vendor tag set is dropped.
- [ ] `src/alphamind/data_sources/sec_edgar/rss.py` writes `topic_tags` as JSON of canonical `HeadlineType.value`s after item-code normalization; the synthetic `"8k"` tag is no longer prepended.
- [ ] `src/alphamind/distillation/q7/correlation_regime_change.py` parses `topic_tags` as JSON, defensively handles malformed JSON, and matches against `NARRATIVE_LAG_REGIME_TAGS` via `HeadlineType` membership.
- [ ] `event_calendar.sectors` (TEXT, nullable) added via Alembic migration; `up()`/`down()` reversible; ORM model updated.
- [ ] `HeadlineEntry`, `EventEntry`, `SectorQualitativeInput` defined as frozen Pydantic models with the field shapes above (`HeadlineEntry.tags: tuple[HeadlineType, ...]`).
- [ ] `load_sector_qualitative_input(session, sector, as_of, lookback_window_hours, max_headlines)` returns a `SectorQualitativeInput`.
- [ ] Sector roster is read via `alphamind.distillation.sector_assembly.load_sector_roster()` (no parallel reader invented).
- [ ] `news_articles.topic_tags` parsed as JSON; malformed JSON treated as empty.
- [ ] `news_articles.source_credibility_tier` `NULL` treated as `tier_3`.
- [ ] No `is_high_priority` field on `HeadlineEntry`; no high-priority promote-to-top logic.
- [ ] Per-sector relevant-tag sets are encoded as named `frozenset[HeadlineType]` constants at module top.
- [ ] Headlines outside `[as_of - lookback_window_hours, as_of]` are excluded.
- [ ] Headlines with at least one sector ticker are included regardless of tags.
- [ ] Headlines with at least one sector-relevant `HeadlineType` tag are included regardless of tickers.
- [ ] Composite-score ranking matches the documented weights (tier × 1.0 + recency × 1.0 + ticker-mention × 1.5 multiplier).
- [ ] `events` covers the 72-hour forward window; cross-sector events (`sectors IS NULL`) are included for every sector.
- [ ] `event_type='earnings'` events render `consensus` from `earnings_event_details`; other event types have `consensus=None`.
- [ ] `data_freshness` reflects the latest ingestion timestamp across `news_articles` and `event_calendar`.
- [ ] No backfill of historical `news_articles.topic_tags` rows.
- [ ] No new `topic_tags` write path in the Finnhub news adapter.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
