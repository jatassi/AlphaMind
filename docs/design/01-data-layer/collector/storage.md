# Storage

The data layer's schema spec — tables, columns, cross-cutting rules, retention. Defines what the [data sources library](data-sources.md)'s collection functions write and what the distillation layer (and eventually the pipeline) reads.

## Storage choice

SQLite (WAL mode) accessed via SQLAlchemy. Schema migrations via Alembic. Models live in `alphamind.persistence.models`. Coexists with the eventual portfolio-state schema ([state-persistence.md](../../05-execution-layer/state-persistence.md)) in the same `%USERPROFILE%\AlphaMind\data\alphamind.db` — table names are disjoint.

The choice and pragma settings inherit from [data-and-state.md](../../../architecture/data-and-state.md): `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`.

## Cross-cutting rules

| Rule | Choice |
|---|---|
| **Timestamps** | UTC throughout, stored as ISO 8601 TEXT (`2026-04-26T13:30:00Z`). SQLAlchemy `DateTime` maps cleanly. ET conversion happens at presentation, never in storage. |
| **Missing data** | Nullable columns where absence is meaningful. Required fields are NOT NULL — a missing value is a collection bug. No sentinel rows on collection failure: the next scheduled fire retries; absence in the data tables is interpreted via [api-failure-handling.md](../api-failure-handling.md)'s fail-closed policy. |
| **Adjustments** | Q1 OHLCV bars stored with paired `adj_*` and `unadj_*` price/volume columns from a single Polygon pull. Adjusted values reflect adjustments-known-at-ingestion-time; corporate actions occurring after ingestion do not retroactively rewrite stored rows. |
| **Extended hours** | Single `ohlcv_bars` table; `session` column distinguishes `pre_market` / `regular` / `after_hours` / `overnight`. The distillation layer applies the confidence discount per [external.md § 1](../../02-distillation-layer/external.md#1-normalization-and-formatting). |
| **Idempotency** | Every time-series table has a natural composite primary key. Collection functions UPSERT; re-running on the same window produces no duplicates. |
| **Partitioning** | Single tables, composite indexes. SQLite has no native partitioning; volumes (estimated below) are well within range for single-table queries. |
| **Provenance** | Every row carries `source` (e.g., `polygon`, `fred`) and `ingested_at` UTC. |
| **Universe scoping** | Most tables FK to `asset_universe.ticker`. `asset_universe` holds both trading-universe tickers and benchmark instruments via the `asset_role` column. |
| **Forward-only** | Time-series tables append; collection functions never rewrite past rows. The two narrow exceptions are macro revisions (new row with incremented `revision_number`) and options snapshot retention pruning at 120 days. Reference tables hold current snapshots and overwrite in place. |

## Tables

### Reference

#### `asset_universe`

One row per ticker. Includes both trading-universe entries (`asset_role='universe'`, populated from `assets.yaml`'s `sectors:`) and benchmark instruments (`asset_role='benchmark'`, populated from `assets.yaml`'s `benchmarks:`).

| Column | Type | Notes |
|---|---|---|
| `asset_id` | TEXT PRIMARY KEY | Stable UUID — survives ticker changes. |
| `ticker` | TEXT NOT NULL UNIQUE | Current symbol. |
| `full_name` | TEXT NOT NULL | Legal entity name. |
| `asset_class` | TEXT NOT NULL | `equity` / `etf` / `index`. |
| `asset_role` | TEXT NOT NULL | `universe` / `benchmark`. |
| `exchange` | TEXT NOT NULL | `NYSE` / `NASDAQ` / `NYSE_ARCA`. |
| `cik` | TEXT | SEC CIK, zero-padded 10 char. Nullable for ETFs and non-SEC reporters. |
| `figi` | TEXT | Optional. |
| `isin` | TEXT | Optional. |
| `shares_outstanding` | INTEGER | |
| `float_shares` | INTEGER | |
| `market_cap_usd` | REAL | |
| `avg_daily_volume_shares` | INTEGER | 60-day trailing per [asset-universe-validation.md](../../asset-universe-validation.md). |
| `avg_daily_volume_notional_usd` | REAL | |
| `beta_spy` | REAL | 90-day trailing. |
| `analyst_count` | INTEGER | Per Finnhub recommendation count. |
| `options_chain_liquid` | INTEGER | Boolean. |
| `ipo_date` | TEXT | ISO date. |
| `is_active` | INTEGER NOT NULL | Boolean. |
| `added_date` | TEXT NOT NULL | ISO date. |
| `removed_date` | TEXT | |
| `removal_reason` | TEXT | |
| `last_updated` | TEXT NOT NULL | ISO datetime UTC. |

Indexes: `(asset_role, is_active)`.

#### `sector_classification`

Ticker → AlphaMind sector mapping with peer group and sector ETF.

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT PRIMARY KEY | FK to `asset_universe.ticker`. |
| `asset_id` | TEXT NOT NULL | FK to `asset_universe.asset_id`. |
| `alphamind_sector` | TEXT NOT NULL | `tech` / `semis` / `financials` / `energy`. |
| `domain_researcher` | TEXT NOT NULL | `tech_semis` / `financials` / `energy`. |
| `gics_sector` | TEXT | |
| `gics_industry_group` | TEXT | |
| `gics_industry` | TEXT | |
| `gics_sub_industry` | TEXT | |
| `sector_etf` | TEXT NOT NULL | `XLK` / `SMH` / `XLF` / `XLE`. |
| `peer_group` | TEXT | Per [reference.py](../schema/reference.py) peer-group enumeration. |
| `classification_source` | TEXT NOT NULL | `polygon` / `manual` / `finnhub`. |
| `last_updated` | TEXT NOT NULL | |

Indexes: `(alphamind_sector)`, `(peer_group)`.

#### `etf_membership`

Many-to-many: ticker × ETF × weight as of a rebalance date.

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT NOT NULL | FK to `asset_universe.ticker`. |
| `etf_ticker` | TEXT NOT NULL | |
| `etf_name` | TEXT NOT NULL | |
| `weight_pct` | REAL NOT NULL | 0.0–100.0. |
| `weight_as_of` | TEXT NOT NULL | ISO date of last confirmed weight. |
| `is_top_10` | INTEGER NOT NULL | Boolean. |

Primary key: `(ticker, etf_ticker, weight_as_of)`. Indexes: `(etf_ticker, weight_as_of)`, `(ticker)`.

#### `ticker_change_history`

Historical symbol changes per asset_id.

| Column | Type | Notes |
|---|---|---|
| `asset_id` | TEXT NOT NULL | FK to `asset_universe.asset_id`. |
| `previous_ticker` | TEXT NOT NULL | |
| `new_ticker` | TEXT NOT NULL | |
| `effective_date` | TEXT NOT NULL | ISO date. |
| `reason` | TEXT NOT NULL | `rebrand` / `merger` / `spin_off` / `other`. |

Primary key: `(asset_id, effective_date)`.

### Market data — Q1

#### `ohlcv_bars`

Multi-timeframe bars. Paired adjusted/unadjusted columns from a single Polygon pull per row.

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT NOT NULL | FK to `asset_universe.ticker`. |
| `timeframe` | TEXT NOT NULL | `15min` / `1h` / `4h` / `1d` / `1w`. (`1min` collected on demand, same table.) |
| `period_start` | TEXT NOT NULL | ISO datetime UTC, bar start. |
| `period_end` | TEXT NOT NULL | ISO datetime UTC, bar end. |
| `session` | TEXT NOT NULL | `pre_market` / `regular` / `after_hours` / `overnight`. |
| `adj_open` / `adj_high` / `adj_low` / `adj_close` | REAL NOT NULL | Split-and-dividend-adjusted as of ingestion. |
| `adj_volume` | INTEGER NOT NULL | Adjusted share volume. |
| `adj_vwap` | REAL | |
| `unadj_open` / `unadj_high` / `unadj_low` / `unadj_close` | REAL NOT NULL | As-traded prices. |
| `unadj_volume` | INTEGER NOT NULL | As-traded share volume. |
| `unadj_vwap` | REAL | |
| `trade_count` | INTEGER | |
| `source` | TEXT NOT NULL | `polygon` / `alpaca`. |
| `ingested_at` | TEXT NOT NULL | UTC. |

Primary key: `(ticker, timeframe, period_start)`. Indexes: SQLite's clustered PK serves trailing-window queries; an additional `(timeframe, period_start)` index supports cross-ticker queries by date.

### Market data — Q12 corporate actions

#### `corporate_actions`

Dividends, splits, spin-offs, mergers, symbol changes. Required for adjusted-bar correctness on backfilled history and for the OMS layer's eventual corporate-action handling per [corporate-actions.md](../../05-execution-layer/corporate-actions.md).

| Column | Type | Notes |
|---|---|---|
| `action_id` | TEXT PRIMARY KEY | Polygon-assigned where available, else hash of source + ticker + ex_date + type. |
| `ticker` | TEXT NOT NULL | FK to `asset_universe.ticker`. |
| `action_type` | TEXT NOT NULL | `cash_dividend` / `stock_dividend` / `split` / `reverse_split` / `spin_off` / `merger` / `symbol_change` / `other`. |
| `declaration_date` | TEXT | ISO date. |
| `ex_date` | TEXT NOT NULL | ISO date. |
| `record_date` | TEXT | |
| `payable_date` | TEXT | |
| `ratio` | REAL | For splits — `4.0` means 4-for-1. |
| `cash_amount_per_share` | REAL | For cash dividends, USD. |
| `new_ticker` | TEXT | For `symbol_change`. |
| `acquirer_ticker` | TEXT | For `merger`. |
| `spin_off_ticker` | TEXT | For `spin_off`. |
| `description` | TEXT | |
| `source` | TEXT NOT NULL | `polygon`. |
| `ingested_at` | TEXT NOT NULL | UTC. |

Indexes: `(ticker, ex_date)`, `(action_type, ex_date)`.

### Options — Q3

#### `options_contracts`

Per-contract reference (slow-changing). Populated from Polygon `/v3/reference/options/contracts`. Reference rows are not pruned.

| Column | Type | Notes |
|---|---|---|
| `contract_ticker` | TEXT PRIMARY KEY | Polygon OCC symbol, e.g., `O:AAPL250117C00200000`. |
| `underlying_ticker` | TEXT NOT NULL | FK to `asset_universe.ticker`. |
| `expiration_date` | TEXT NOT NULL | ISO date. |
| `strike_price` | REAL NOT NULL | |
| `contract_type` | TEXT NOT NULL | `call` / `put`. |
| `first_seen_at` | TEXT NOT NULL | UTC. |
| `last_seen_at` | TEXT NOT NULL | UTC. |
| `source` | TEXT NOT NULL | `polygon`. |

Indexes: `(underlying_ticker, expiration_date)`.

#### `options_contract_snapshots`

Time-series. Populated from Polygon `/v3/snapshot/options/{ticker}`. Rows older than 120 days are pruned by an ops-time policy; per-ticker daily ATM-IV history (for IV-rank's 252-day window) is maintained separately by the distillation layer in its own state tables per [threshold-calibration.md](../../02-distillation-layer/threshold-calibration.md).

| Column | Type | Notes |
|---|---|---|
| `snapshot_ts` | TEXT NOT NULL | UTC. |
| `contract_ticker` | TEXT NOT NULL | FK to `options_contracts.contract_ticker`. |
| `underlying_ticker` | TEXT NOT NULL | Denormalized for query efficiency. |
| `open_interest` | INTEGER | |
| `volume_today` | INTEGER | |
| `last_price` | REAL | |
| `bid` | REAL | |
| `ask` | REAL | |
| `implied_volatility` | REAL | |
| `delta` | REAL | |
| `gamma` | REAL | |
| `theta` | REAL | |
| `vega` | REAL | |
| `rho` | REAL | |
| `underlying_price` | REAL | Spot at snapshot. |
| `source` | TEXT NOT NULL | `polygon`. |
| `ingested_at` | TEXT NOT NULL | |

Primary key: `(snapshot_ts, contract_ticker)`. Indexes: `(underlying_ticker, snapshot_ts)`.

### Macro — Q6

#### `macro_observations`

Generic time-series for FRED, EIA, BLS, Treasury fiscal data, and other series-shaped sources. Each PMI subcomponent, each CPI component, each yield series gets its own `series_id`.

| Column | Type | Notes |
|---|---|---|
| `source` | TEXT NOT NULL | `fred` / `eia` / `bls` / `treasury`. |
| `series_id` | TEXT NOT NULL | Vendor-native identifier (e.g., `DGS10`, `CPIAUCSL`, `EPC0`). |
| `observation_date` | TEXT NOT NULL | ISO date — the date the observation refers to. |
| `release_date` | TEXT | ISO date — when published. Nullable for series without a distinct release. |
| `revision_number` | INTEGER NOT NULL | 0 = first publication, 1+ = subsequent revisions. |
| `value` | REAL | Nullable when missing. |
| `units` | TEXT | `pct` / `bp` / `bbl` / `usd` / etc. |
| `frequency` | TEXT | `daily` / `weekly` / `monthly` / `quarterly` / `annual`. |
| `ingested_at` | TEXT NOT NULL | |

Primary key: `(source, series_id, observation_date, revision_number)`. Indexes: `(series_id, observation_date)`, `(release_date)`.

#### `treasury_auctions`

Specialized table — the field set doesn't fit the generic series shape.

| Column | Type | Notes |
|---|---|---|
| `auction_id` | TEXT PRIMARY KEY | Composite, e.g., `2026-03-15_10Y`. |
| `tenor` | TEXT NOT NULL | `2Y` / `5Y` / `10Y` / `30Y`. |
| `auction_date` | TEXT NOT NULL | ISO date. |
| `auction_yield_bp` | REAL | |
| `bid_to_cover` | REAL | |
| `tail_bp` | REAL | |
| `primary_dealer_pct` | REAL | |
| `indirect_pct` | REAL | |
| `direct_pct` | REAL | |
| `auction_size_usd` | REAL | Billions. |
| `source` | TEXT NOT NULL | `treasury`. |
| `ingested_at` | TEXT NOT NULL | |

Indexes: `(auction_date, tenor)`.

### Events

#### `event_calendar`

All scheduled events with an `event_type` discriminator. Consolidates Q6g (macro events), Qual5 (regulatory / policy / geopolitical), and Qual5e earnings calendar entries into one shape.

| Column | Type | Notes |
|---|---|---|
| `event_id` | TEXT PRIMARY KEY | Vendor-assigned where available, else hash. |
| `event_type` | TEXT NOT NULL | `earnings` / `fomc` / `cpi_release` / `ppi_release` / `pce_release` / `nfp_release` / `ism_release` / `gdp_release` / `opec_meeting` / `investor_day` / `conference` / `treasury_auction` / `fda_advisory` / `regulatory_decision` / `product_launch` / `other`. |
| `ticker` | TEXT | Nullable for market-wide events. FK to `asset_universe.ticker`. |
| `scheduled_at` | TEXT NOT NULL | ISO datetime UTC. |
| `description` | TEXT | |
| `status` | TEXT NOT NULL | `scheduled` / `completed` / `cancelled` / `postponed`. |
| `source` | TEXT NOT NULL | `finnhub` / `polygon` / `sec_edgar` / `manual`. |
| `ingested_at` | TEXT NOT NULL | |
| `last_updated` | TEXT NOT NULL | Status field can mutate as events transition. |

Indexes: `(scheduled_at)`, `(ticker, scheduled_at)`, `(event_type, scheduled_at)`.

#### `earnings_event_details`

One-to-one extension to `event_calendar` rows where `event_type='earnings'`.

| Column | Type | Notes |
|---|---|---|
| `event_id` | TEXT PRIMARY KEY | FK to `event_calendar.event_id`. |
| `ticker` | TEXT NOT NULL | |
| `fiscal_period` | TEXT NOT NULL | `Q1` / `Q2` / `Q3` / `Q4` / `FY`. |
| `fiscal_year` | INTEGER NOT NULL | |
| `expected_call_time` | TEXT | `bmo` (before market open) / `amc` (after market close) / `dmh` (during market hours). |
| `eps_consensus` | REAL | |
| `eps_actual` | REAL | |
| `revenue_consensus_usd` | REAL | |
| `revenue_actual_usd` | REAL | |
| `reported_at` | TEXT | UTC. |
| `source` | TEXT NOT NULL | |

Indexes: `(ticker, fiscal_year, fiscal_period)`.

### News — Qual1

#### `news_articles`

Article metadata. Body text stored on disk at `%USERPROFILE%\AlphaMind\data\news\<YYYY>\<MM>\<article_id>.txt` to keep it `grep`-able by adaptive-research agents (per [qualitative.md § 1](../external/qualitative.md)).

| Column | Type | Notes |
|---|---|---|
| `article_id` | TEXT PRIMARY KEY | Vendor-assigned where available, else SHA-256 of `(source, url, published_at)`. |
| `source` | TEXT NOT NULL | `finnhub` / `marketaux` / `sec_edgar_8k_rss` / `rss`. |
| `source_outlet` | TEXT | E.g., `Reuters`, `CNBC`. |
| `source_credibility_tier` | TEXT | `tier_1` / `tier_2` / `tier_3`. Stamped by the collector at ingestion from `config/news_outlets.yaml`. |
| `url` | TEXT | |
| `language` | TEXT NOT NULL | Default `en`. |
| `headline_text` | TEXT NOT NULL | |
| `body_path` | TEXT | Filesystem path to body content. Nullable for headline-only sources. |
| `published_at` | TEXT NOT NULL | UTC. |
| `ingested_at` | TEXT NOT NULL | |
| `vendor_sentiment_score` | REAL | Marketaux pre-computed (-1 to +1). Nullable. |
| `vendor_sentiment_label` | TEXT | `positive` / `negative` / `neutral`. Nullable. |
| `topic_tags` | TEXT | JSON array of vendor tags (`earnings`, `m_and_a`, `regulatory`, etc.). |

Indexes: `(published_at DESC)`, `(source, published_at)`.

#### `news_article_tickers`

Many-to-many: articles to mentioned tickers. The primary subject ticker is flagged.

| Column | Type | Notes |
|---|---|---|
| `article_id` | TEXT NOT NULL | FK to `news_articles.article_id` ON DELETE CASCADE. |
| `ticker` | TEXT NOT NULL | FK to `asset_universe.ticker`. |
| `is_primary` | INTEGER NOT NULL | Boolean. |

Primary key: `(article_id, ticker)`. Indexes: `(ticker, article_id)`.

### Prediction markets — Qual3

#### `prediction_market_contracts`

Per-contract reference (slow-changing).

| Column | Type | Notes |
|---|---|---|
| `contract_id` | TEXT PRIMARY KEY | Platform-assigned (Polymarket condition-id, Kalshi ticker). |
| `platform` | TEXT NOT NULL | `polymarket` / `kalshi` / `metaculus`. |
| `description` | TEXT NOT NULL | |
| `category` | TEXT NOT NULL | `monetary_policy` / `antitrust` / `trade` / `financial_reg` / `tax` / `election` / `opec` / `conflict` / `sanctions`. |
| `resolution_date` | TEXT | ISO date. |
| `resolution_outcome` | TEXT | `yes` / `no` / `undecided`. Nullable while open. |
| `created_at` | TEXT NOT NULL | UTC. |
| `last_seen_at` | TEXT NOT NULL | |

Indexes: `(platform, category)`, `(resolution_date)`.

#### `prediction_market_snapshots`

Per-contract probability and liquidity over time.

| Column | Type | Notes |
|---|---|---|
| `contract_id` | TEXT NOT NULL | FK to `prediction_market_contracts.contract_id`. |
| `snapshot_ts` | TEXT NOT NULL | UTC. |
| `yes_probability` | REAL NOT NULL | 0.0–1.0. |
| `volume_24h_usd` | REAL | |
| `liquidity_usd` | REAL | |
| `bid` | REAL | |
| `ask` | REAL | |
| `ingested_at` | TEXT NOT NULL | |

Primary key: `(contract_id, snapshot_ts)`. Indexes: `(snapshot_ts)`.

### Operations

#### `collection_runs`

One row per collection-function invocation for ops visibility and failure debugging.

| Column | Type | Notes |
|---|---|---|
| `run_id` | TEXT PRIMARY KEY | UUID. |
| `collector` | TEXT NOT NULL | Module path, e.g., `polygon.equity`. |
| `started_at` | TEXT NOT NULL | UTC. |
| `completed_at` | TEXT | UTC. Nullable while in flight. |
| `status` | TEXT NOT NULL | `running` / `success` / `partial_failure` / `failed`. |
| `rows_written` | INTEGER | |
| `error_summary` | TEXT | Short error description on failure. |

Indexes: `(collector, started_at)`.

## Deferred categories

Categories the [SESSION-BRIEF](../SESSION-BRIEF.md) flagged as deferrable, not implemented in v1:

| Category | Reason |
|---|---|
| Q2 order flow / microstructure / dark pool | Schema depends on Polygon tick-data shape; tick-data subscription not yet active. |
| Q4 short selling | Bi-monthly FINRA cycle; daily short volume + borrow cost can fit `macro_observations` later. |
| Q5 fundamental data (earnings revisions, expectations vs. reality) | Requires a separate revision-tracking shape; deferred until decision layer needs it. |
| Q8 commodities specialized (futures curves, CFTC positioning) | WTI / nat-gas spot fits `macro_observations`; futures curves and COT need their own table. |
| Qual2 social sentiment | StockTwits + Google Trends; deferred until distillation needs the input. |
| Qual4 earnings transcripts | Hybrid pattern: metadata in DB, transcript text on disk per Qual1's news pattern. |
| Qual6 sector-specific qualitative catalysts | LLM-derived; lands when the qualitative research layer is built. |

When a deferred category lands, it gets its own table designed against the same cross-cutting rules.

## Storage volume

After bootstrap (252-day equity / 24-month monthly macro / 90-day daily macro / no historical options or news):

| Table family | Approximate rows | Approximate size |
|---|---|---|
| `ohlcv_bars` (universe + benchmarks, 5 timeframes, paired adj/unadj) | ~600K | ~150 MB |
| `corporate_actions` | ~3K | <1 MB |
| `macro_observations` | ~50K | ~5 MB |
| `treasury_auctions` | ~50 | <1 MB |
| `event_calendar` + `earnings_event_details` | ~500 | <1 MB |
| Reference tables | ~100 | <1 MB |

Total post-bootstrap: ~150–200 MB. Steady-state daily growth: ~25 MB/day (mostly `ohlcv_bars` + `options_contract_snapshots` before pruning + `news_articles`). One year of operation lands around 10 GB before any retention policy applies.

## Retention

| Table | Policy |
|---|---|
| `ohlcv_bars` | Retained indefinitely. |
| `options_contract_snapshots` | Rows older than 120 days pruned by an ops-time policy. `options_contracts` reference rows are not pruned. |
| `news_articles` + body files on disk | Retained indefinitely; revisit if growth exceeds projections. |
| `prediction_market_snapshots` | Retained indefinitely for v1; revisit when `prediction_market_history_days=30` calibration window grows. |
| `macro_observations` | Retained indefinitely. |
| `event_calendar` | Past-event rows retained for reconciliation. |
| `collection_runs` | Trim after 90 days. |

---

*Cross-references:*

- [Data sources library](data-sources.md) — the collection functions that write these tables.
- [Runner](runner.md) — the long-running process that schedules the writes.
- [Lifecycle](lifecycle.md) — bootstrap, catch-up, retention application.
- [data-and-state.md](../../../architecture/data-and-state.md) — SQLite + WAL + SQLAlchemy choice.
- [external/quantitative.md](../external/quantitative.md), [external/qualitative.md](../external/qualitative.md) — what each signal is and why it matters.
- [02-distillation-layer/external.md](../../02-distillation-layer/external.md) — read patterns and indexing implications.
- [02-distillation-layer/threshold-calibration.md](../../02-distillation-layer/threshold-calibration.md) — calibration windows that drove backfill depth.
- [api-failure-handling.md](../api-failure-handling.md) — fail-closed semantics on absence.
