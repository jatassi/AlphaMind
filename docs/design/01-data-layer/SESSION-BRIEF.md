# Session brief: data layer build

> **Transient document.** This is a session brief for standing up AlphaMind's data layer. Delete or move once the work is complete.

## What this is

A focused build session whose only goal is to **collect external market data and persist it durably**. No analysis, no LLM agents, no OMS, no dashboard, no feedback loop — those layers come later. The mental model is: *plumbing the data lake, with the rest of the house yet to be built*.

## Context

AlphaMind is in design phase. The pipeline (distillation → analysis → decision → execution) reads from the data layer's persistence; downstream layers come online over time. This session builds the data layer in isolation — external sources collected, raw payloads persisted into the schema you'll define, ready for downstream consumers when they arrive.

## Three pre-build design decisions

The design has gaps in three specific areas that need explicit decisions before writing collection code. Each lands a design doc that becomes part of the AlphaMind spec.

### Decision 1: Raw data persistence schema

**The gap:** The design specifies what LLM agents *consume* (presentation models in `docs/design/01-data-layer/schema/*.py`, denormalized per-ticker per-invocation packages) but not what the collector *writes* to the database. There's a brief mention of an `ohlcv_bars` table in `docs/architecture/data-and-state.md` and "per-category tables for order flow, options, macro readings" — that's it. No DDL, no columns, no indexes.

**Why it matters before building:** Schema choices made here determine the shape of all data the collector writes. Retroactively migrating across categories is involved work; landing the schema thoughtfully up front is worth the design time.

**What to produce:** A new doc at `docs/design/01-data-layer/raw-data-persistence-schema.md` that specifies, per data category:
- Table name (or names if multiple per category)
- Columns: name, type, nullable, primary key participation, indexes
- Partitioning strategy (single table with composite key vs. per-date partitioned tables — likely single table is fine at AlphaMind's volume)
- Cross-cutting rules (timestamp timezone, how missing data is represented, adjusted vs. unadjusted prices, extended-hours handling)

**Required coverage:**
- Q1 equity OHLCV (multi-timeframe: 15min, 1hr, 4hr, daily, weekly per `schema/price_volume.py`)
- Q3 derivatives — options chain snapshots
- Q6 macro series (FRED, EIA, BLS)
- Q7 cross-asset / correlation reference data
- Qual1 news articles (raw text)
- Qual3 prediction market snapshots
- Qual5 catalyst calendar / earnings calendar

**Deferrable but worth flagging in the doc as TBD:**
- Q2 order flow (microstructure data — schema depends on Polygon's tick data shape)
- Q4 short selling
- Q5 fundamental data (earnings revisions, expectations vs. reality)
- Q8 commodities
- Qual2 social sentiment
- Qual4 earnings transcripts (likely a hybrid: metadata in DB, text on disk)
- Qual6 regulatory events

**Source material:**
- Field inventory: `docs/design/01-data-layer/schema/*.py` (one file per category, well-documented)
- Volume estimates: `docs/architecture/data-and-state.md`
- Distillation's read patterns: `docs/design/02-distillation-layer/external.md` (constrains your indexing)

### Decision 2: Collector architecture

**The gap:** The design treats data collection as Phase 1 of each pipeline invocation (Trigger → Data Layer → Distillation → ...). It doesn't address running collection *before* the rest of the pipeline exists.

**The choice:** Three viable shapes:

1. **Standalone collector** — a separate Python script (or NSSM Windows service) that runs on its own schedule, hits APIs, writes to the same SQLite database the eventual pipeline will use. Smallest scaffolding. Eventual pipeline absorbs or replaces it.
2. **Pipeline shell** — build the pipeline harness now with just the data-layer phase implemented; remaining phases are no-ops. Runs on the existing scheduler from `config/scheduler.yaml`. More architecturally pure (matches the eventual design exactly) but more upfront scaffolding.
3. **Hybrid** — standalone collector for continuous polling (prediction markets, news), pipeline-invocation-driven collection for the rest.

**Why it matters before building:** Decides the entry point, the process supervision pattern, and how the eventual pipeline integrates.

**Recommendation:** Likely (1) — lowest scaffolding for this isolated build, and the eventual pipeline can read from the same tables without caring how data got there. (2) is a defensible choice if building the pipeline harness now is preferable to building a collector that the harness later absorbs or replaces.

**What to produce:** A short section in `docs/design/01-data-layer/README.md` (or a new `collector-architecture.md`) naming the choice, the entry point, and the invocation cadence. One to three paragraphs is enough.

### Decision 3: Backfill strategy

**The gap:** Distillation needs rolling baselines (20–252 days). Day-1 collection has zero history. The design doesn't say how to bootstrap. The threshold-calibration doc lands the calibration-state-tagging concept (`bootstrap` / `calibrated`) but doesn't specify where the bootstrap data comes from.

**The choice:** Which sources backfill at first deployment and how far back?

- **Polygon equity bars** — supports historical pulls. Recommend backfilling 252 trading days (covers gap_fill_baseline_days, the longest window) for all universe tickers on first run.
- **FRED, EIA, BLS macro** — support deep history. Recommend backfilling whatever the relevant series' max-baseline window requires (60–90 days for most).
- **Polygon options chains** — historical options data is expensive and lower-priority at first deployment. Recommend starting fresh; accept that options-related distillation baselines come online 30–60 days after collection begins.
- **News / prediction markets / earnings transcripts** — ephemeral; can't backfill in any meaningful way. Start collecting forward; baseline windows fill organically.

**What to produce:** A new doc at `docs/design/01-data-layer/bootstrap-and-backfill.md` covering:
- Per-category backfill scope (how far back, which API call)
- Backfill execution order (dependencies, e.g., reference data before bars)
- Backfill timing (one-time on first deploy, or periodic catch-up if collection has gaps)
- How distillation handles still-bootstrapping baselines (cross-references `02-distillation-layer/threshold-calibration.md`)

## Build to-do

In order:

1. **Land the three design decisions above** as the three docs named.
2. **Create the SQLite database** at `%USERPROFILE%\AlphaMind\data\alphamind.db` with the schema from Decision 1.
3. **Add to existing `.env`** with all API keys per `docs/design/01-data-layer/api-key-checklist.md`. Verify connectivity to each provider.
4. **Implement collectors** for the in-scope categories. One module per data source is the natural shape (one for Polygon equity, one for FRED, one for Polymarket, etc.). Each module knows: how to query the source, how to map the response to the storage schema, how to apply the failure semantics from `api-failure-handling.md`.
5. **Implement the backfill script** per Decision 3. Test against a couple of tickers / a short window before running the full backfill.
6. **Run the backfill.** Verify row counts match expectations before moving on.
7. **Wire the collector into its scheduling mechanism** per Decision 2 (NSSM service, APScheduler trigger, whatever).
8. **Start ongoing collection.** Verify it runs on schedule and persists rows successfully across at least one full cycle.

## Smaller decisions you'll make as you build

These don't need pre-build documents but be conscious you're making choices:

- **Collection cadence per source.** The design has freshness SLAs (Q1 <5min, Q2/Q3 <30min, Q5/Q6 <1h) but the eventual pipeline runs every 2–4h. For day-1 collection you can collect once per scheduled invocation and accept staleness on the <30min categories. Document the choice in the collector module's docstring.
- **Failure handling at collector level.** When a source fails, write a sentinel row (NULLs with a `failure_reason` field), skip the row entirely, or quarantine the partial response? Pick one and apply consistently.
- **Multi-source failover.** `data_sources.yaml` defines `primary` and `failover` providers per category. Initially, collect from primary only — failover logic adds complexity for marginal benefit until a provider actually fails.

## Verification

How to know things are working:

- Database file exists at the expected path with all the tables from your schema doc.
- After backfill: row counts match expectations (e.g., ~70 tickers × 252 days × 5 timeframes ≈ 88K rows in `ohlcv_bars`, scaled by however many of those timeframes you backfill).
- After 1 trading day of live collection: each table has new rows with today's date, no obvious gaps.
- API failure handling: introduce a deliberate failure (revoke a key temporarily) and verify the collector handles it per spec — logs the failure, doesn't crash, keeps collecting from healthy sources.
- Mental check: imagine the distillation layer querying these tables. Could it compute a 20-day volume baseline for AAPL? A 60-day correlation matrix across the universe? Does the schema and indexing support those queries efficiently?

## Scope discipline

In scope:
- External data collection (Polygon, FRED, Finnhub, Marketaux, Kalshi, Polymarket, Alpaca, EIA, BLS, SEC EDGAR, CBOE, FINRA, etc. per `api-key-checklist.md`)
- Persistence schema and tables for raw data
- Backfill script
- Scheduling and process supervision for the collector
- Logging and operational basics

Out of scope:
- Distillation logic (no anomaly detection, no rolling baselines computation, no regime classification)
- Any LLM agent infrastructure (no Claude SDK wiring, no agent prompts)
- OMS, broker adapter, guardrails (no Alpaca trading account integration beyond reading market data)
- Continuous monitor (the websocket-driven fill processor)
- Command center / dashboard
- Feedback loop infrastructure (counterfactual replay engine, validations table, retrospective reports)
- Provenance fields per `state-persistence.md` (process lifetimes, agent calls — those are for the LLM pipeline, not data collection)

If a question arises about something out-of-scope, defer with a TODO comment and bring it back to the design conversation.

## What to report back about

When you check back in, the most useful things to share:

- The three docs you produced (schema, architecture, backfill) — particularly anywhere your decisions diverged from what this brief recommended
- Any new gaps in the broader AlphaMind design you noticed while building (the audit that produced this brief was thorough but not exhaustive)
- Surprises during API integration — provider quirks, undocumented behaviors, rate limits that hit harder than expected
- When data has accumulated enough that we can start talking seriously about distillation

## Reference docs (in order of likely usefulness)

- `docs/design/01-data-layer/README.md` — layer overview
- `docs/design/01-data-layer/schema/*.py` — field inventory per category, well-commented
- `docs/design/01-data-layer/api-key-checklist.md` — exhaustive provider/endpoint reference
- `docs/design/configuration-management.md` — `data_sources.yaml`, `assets.yaml` schemas
- `docs/design/01-data-layer/api-failure-handling.md` — tier-based retry semantics
- `docs/design/02-distillation-layer/external.md` — what the distillation layer will eventually read (constrains your schema)
- `docs/design/02-distillation-layer/threshold-calibration.md` — bootstrap policy (constrains your backfill)
- `docs/architecture/data-and-state.md` — high-level storage architecture
- `docs/architecture/infrastructure.md` — process supervision pattern (NSSM Windows Services)
- `config/assets.yaml` — the ticker list to collect for
