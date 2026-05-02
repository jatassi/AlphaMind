---
status: not_started
completed_date:
commit_id:
---

# 06 — Sector-qualitative input loader

## Goal

Implement the loader that reads sector-specific qualitative inputs from the data layer and returns them in the form the input-bundle assembler (story 08) consumes. The output is a `SectorQualitativeInput` value object containing the slice of qualitative data each sector researcher's design doc names — sector-tagged news headlines and sector-relevant scheduled events for v1.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — second row points at `qualitative.md §6a` (AI narrative, supply chain, product cycle, competitive dynamics, export controls)
- `docs/design/03-analysis-layer/domain-researchers/financials.md` § Inputs — `qualitative.md §6c` (credit, rate environment, M&A, regulatory posture, consumer/payment, crypto)
- `docs/design/03-analysis-layer/domain-researchers/energy.md` § Inputs — `qualitative.md §6b` (OPEC, inventory narrative, weather, pipeline)
- `docs/design/01-data-layer/external/qualitative.md` § 6 — the sector-specific catalysts; what the data layer ingests for each sector
- `docs/design/01-data-layer/external/qualitative.md` § 1a — credibility tier tagging (`tier_1`/`tier_2`/`tier_3`); used here for ranking
- `docs/design/01-data-layer/collector/storage.md` — `news_articles`, `event_calendar` table shapes
- `docs/implementation/01-data-layer/collector/05g-marketaux-vendor-adapter.md` — confirm the news-articles sector-tagging field

## Depends on

- 03 (`Sector` enum)
- The data-layer collector (already merged) for `news_articles` and `event_calendar` tables. No code dependency on a specific story; reads the tables produced by stories 05g (Marketaux) and 06a (bootstrap orchestrator) of the collector work tree.

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `qualitative_input.py` defining:
  - `class HeadlineEntry(BaseModel, frozen=True)` carrying:
    - `headline: str`
    - `source: str` (outlet name from `news_outlets.yaml`)
    - `tier: Literal["tier_1", "tier_2", "tier_3"]`
    - `published_at: datetime` (UTC, tz-aware)
    - `tickers: tuple[str, ...]` (universe tickers mentioned)
    - `tags: tuple[str, ...]` (type tags from `qualitative-research.md § Type tags`: `breaking`, `M&A`, `analyst_action`, `regulatory`, `geopolitical`, `macro_data`, `insider_activity`, `short_report`, `activist`, `product_launch`, `supply_chain`, `guidance`, `sector_rotation`, `earnings_related`)
    - `is_high_priority: bool` (from the data layer's first-class flag)
  - `class EventEntry(BaseModel, frozen=True)` carrying:
    - `event_name: str`
    - `event_time: datetime` (UTC, tz-aware)
    - `sectors: frozenset[Sector]`
    - `tickers: tuple[str, ...]` (when ticker-specific, e.g., earnings; empty for macro events like FOMC)
    - `consensus: str | None` (consensus expectation when known, e.g., `"+0.3% MoM"` for CPI)
  - `class SectorQualitativeInput(BaseModel, frozen=True)` carrying:
    - `sector: Sector`
    - `as_of: datetime` (UTC, tz-aware — the invocation's effective time)
    - `lookback_window_hours: int` (the headline-fetch window)
    - `headlines: tuple[HeadlineEntry, ...]` (ordered by composite score descending — see ranking below)
    - `events: tuple[EventEntry, ...]` (next 72 hours, ordered by `event_time` ascending)
    - `data_freshness: datetime` (latest `ingested_at` across the underlying tables; used by the harness to surface staleness in the brief's signal-quality flag)
  - `load_sector_qualitative_input(session: Session, sector: Sector, as_of: datetime, lookback_window_hours: int = 24, max_headlines: int = 30) -> SectorQualitativeInput` — the main entry point. SQLAlchemy session injected by the caller; `as_of` and lookback window injected by the harness.
- Headline selection logic:
  - Pull `news_articles` rows where `published_at >= as_of - lookback_window_hours` AND (any of `tickers` are members of the sector's ticker list per `config/sectors.yaml` OR any of `tags` are in the sector's relevant-tag set — the relevant-tag set is per-sector and codified as a constant in this module).
  - Per-sector relevant-tag sets (informed by qualitative.md § 6):
    - `TECH_SEMIS`: `supply_chain`, `product_launch`, `regulatory` (export controls), `analyst_action`, `M&A`, `guidance`, `earnings_related`
    - `FINANCIALS`: `regulatory`, `macro_data`, `M&A`, `analyst_action`, `guidance`, `earnings_related`
    - `ENERGY`: `geopolitical`, `macro_data`, `regulatory`, `analyst_action`, `M&A`, `guidance`, `earnings_related`
  - Composite-score ranking per `qualitative-research.md § Ranking algorithm`:
    - source credibility (tier_1=3, tier_2=2, tier_3=1) × 1.0
    - recency (linear decay from `as_of` to `as_of - lookback_window_hours`) × 1.0
    - universe-ticker mention boost: ×1.5 if any ticker in the sector's membership
    - high-priority flag: forces the row into the top of the ranking regardless of score
  - Take the top `max_headlines` by composite score after applying the high-priority overrides; ties broken by `published_at` descending.
- Event selection logic:
  - Pull `event_calendar` rows where `event_time` is in `[as_of, as_of + 72h]` AND the row's `sectors` includes `sector` (or is `null`/empty for cross-sector events like FOMC, in which case macro events are included for every sector).
  - The `sectors` column on `event_calendar` may not exist yet — if it doesn't, this story adds it via Alembic migration as part of the work. Document the dependency in the story Notes if the column is missing.
- Unit tests:
  - Empty database produces a `SectorQualitativeInput` with empty `headlines` and `events` and a `data_freshness` of `as_of - lookback_window_hours` (or `None` if the spec prefers; document the choice).
  - Headlines outside the lookback window are excluded.
  - Headlines with no sector ticker and no sector-relevant tag are excluded.
  - Headlines with at least one sector ticker are included regardless of tag.
  - Headlines with at least one sector-relevant tag are included regardless of ticker (cross-sector macro headlines).
  - Composite ranking respects all factors; high-priority headlines surface ahead of higher-scored non-priority ones.
  - `max_headlines` truncates the ordered list; truncation does not drop high-priority entries before non-priority ones.
  - Events outside the 72h forward window are excluded; events with `null`/empty sectors are included for every sector.
  - `data_freshness` reflects the latest ingestion timestamp across both tables.

Out of scope:
- Earnings call transcripts (qual §4 — Tier 2 of the `earnings_commentary` tool). Transcript ingestion is not yet built; sectorual-research feature parity with the design doc's full §6 surface comes online incrementally as ingestion expands.
- Industry trade publications (DigiTimes, Argus Media, etc. — not in v1 ingestion).
- Social sentiment, prediction markets, options-flow narrative — those are the qualitative researcher's territory, not domain researchers.
- Rendering the `SectorQualitativeInput` to the LLM input-bundle text — that is story 08.
- Writing to the database — this loader is read-only.

## Notes

The sector membership for ticker matching is read from `config/sectors.yaml` per story 02. The loader takes the sector membership as a constructor argument (or accepts a `sectors_config` parameter on the entry function) so tests can inject fixture membership without YAML.

The 24-hour default lookback window on headlines is informed by `qualitative-research.md § News digest` (which uses an inter-invocation window). Domain researchers run on the same invocation cadence; 24 hours is a reasonable default that sees overnight developments. The harness can override per invocation type (pre-open might want longer; intraday might want shorter) via the [`run_types/` overlay](../../../design/configuration-management.md#run_typestriggeryaml) once the resolver wiring lands.

The `event_calendar` row may carry sectors as a `tags` field, an `applicable_sectors` array, or a separate join table — depending on the existing schema. Read the actual schema (story 03b of the collector work tree's persistence layer) and align. If the column does not exist, do not invent it silently — surface the gap as part of dispatch and add it as a small Alembic migration step within this story's scope.

The composite-score ranking is duplicated from the qualitative researcher's news-digest ranking algorithm. Consolidating into a shared utility is a future refactor (the qualitative researcher work tree may pull this back); for now, encode the ranking here as a private function and write a test asserting the score components match the documented weights.

`HeadlineEntry.tags` uses a `tuple[str, ...]` rather than a closed `Literal` enum because the tag taxonomy is data-layer-defined and may grow without a domain-researcher schema bump. The relevant-tag sets above are encoded as `frozenset[str]` constants in the module — adding a new tag to one set is a one-line change.

`HeadlineEntry.tier` is a `Literal["tier_1", "tier_2", "tier_3"]` rather than an enum because the data-layer YAML uses these exact strings (see `config/news_outlets.yaml`); coercing through an enum adds drift surface without buying type-safety the `Literal` doesn't already provide.

## Acceptance criteria

- [ ] `HeadlineEntry`, `EventEntry`, `SectorQualitativeInput` defined as frozen Pydantic models.
- [ ] `load_sector_qualitative_input(session, sector, as_of, lookback_window_hours, max_headlines)` returns a `SectorQualitativeInput`.
- [ ] Headlines outside `[as_of - lookback_window_hours, as_of]` are excluded.
- [ ] Headlines with at least one sector ticker are included regardless of tags.
- [ ] Headlines with at least one sector-relevant tag are included regardless of tickers.
- [ ] Composite-score ranking matches the documented weights (tier × 1.0 + recency × 1.0 + ticker-mention × 1.5).
- [ ] High-priority headlines appear at the top of `headlines` regardless of composite score; truncation does not drop them before non-priority entries.
- [ ] `events` covers the 72-hour forward window; cross-sector events (`null`/empty sectors) are included for every sector.
- [ ] `data_freshness` reflects the latest ingestion timestamp across `news_articles` and `event_calendar`.
- [ ] Tests use an injected sector-membership map rather than reading `config/sectors.yaml`.
- [ ] If `event_calendar` schema lacks a sector column, the story includes the Alembic migration adding it; the migration is reversible (down() implemented).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
