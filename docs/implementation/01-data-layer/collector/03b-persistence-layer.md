---
status: not_started
completed_date:
commit_id:
---

# 03b — Persistence layer

## Goal

Implement SQLAlchemy 2.0 declarative models, Alembic migrations, and a session factory for every table in `storage.md`.

## Reading

- `docs/design/01-data-layer/collector/storage.md` — authoritative schema spec
- `docs/architecture/data-and-state.md` — pragma settings, WAL mode, session split
- `docs/design/configuration-management.md § main.yaml` — database path

## Depends on

- 02 (project skeleton — `src/alphamind/persistence/` directory and `migrations/`)

## Scope

In scope:
- One SQLAlchemy 2.0 declarative model per table in `storage.md` § Tables:
  - `asset_universe`, `sector_classification`, `etf_membership`, `ticker_change_history`
  - `ohlcv_bars`, `corporate_actions`
  - `options_contracts`, `options_contract_snapshots`
  - `macro_observations`, `treasury_auctions`
  - `event_calendar`, `earnings_event_details`
  - `news_articles`, `news_article_tickers`
  - `prediction_market_contracts`, `prediction_market_snapshots`
  - `collection_runs`
- Composite primary keys, foreign keys, indexes, and `NOT NULL` constraints exactly as specified in `storage.md`.
- `src/alphamind/persistence/session.py` exposing an `engine` constructor and a `Session` factory. The engine accepts a database path (resolved from `main.yaml` or a `DATABASE_PATH` env override), opens with the four pragmas from `data-and-state.md` (`journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`).
- Alembic configured under `src/alphamind/persistence/migrations/`. Initial migration creates every table.
- Unit tests:
  - Round-trip every model against an in-memory SQLite database.
  - Verify composite-key uniqueness on `ohlcv_bars`, `macro_observations`, `options_contract_snapshots`.
  - Verify foreign-key constraints fire on violations.
  - Verify pragma settings are applied on session open.

Out of scope:
- Vendor adapters that write to these tables (stories 05*).
- Bootstrap orchestration (story 06a).
- Retention pruning (operational concern; deferred).
- Phase 4 portfolio-state tables (`positions`, `theses`, etc.) — separate work, not in the data-layer build.

## Notes

Match column-name-to-attribute exactly with `storage.md` — distillation queries reference column names from that doc.

For `ohlcv_bars`, declare the paired `adj_*` and `unadj_*` columns individually (more readable and easier to introspect than programmatic generation).

`event_calendar.last_updated` is mutable. Use SQLAlchemy's `onupdate=func.now()` so it self-maintains.

`news_articles.body_path` is filesystem path; not validated at the DB layer.

The Alembic env should configure SQLAlchemy 2.0 metadata target with `compare_type=True` for downstream column-type-change detection.

Database path resolution: `DATABASE_PATH` env var > `main.yaml` `paths.database` > default `%USERPROFILE%\AlphaMind\data\alphamind.db`. Tests use `:memory:`.

## Acceptance criteria

- [ ] One declarative model per table from `storage.md` § Tables exists under `src/alphamind/persistence/` (single `models.py` or split — operator's call).
- [ ] Every column matches type, nullability, and primary-key participation in `storage.md`.
- [ ] Every index from `storage.md` is declared.
- [ ] Every foreign key from `storage.md` is declared.
- [ ] `src/alphamind/persistence/session.py` exposes a `Session` factory and an engine constructor; the engine applies the four pragmas from `data-and-state.md`.
- [ ] `alembic upgrade head` against an empty SQLite file creates the full schema.
- [ ] `alembic downgrade base` reverses cleanly.
- [ ] Unit test covers round-trip insert + select for every table.
- [ ] Unit test verifies composite-key uniqueness on `ohlcv_bars`, `macro_observations`, `options_contract_snapshots`.
- [ ] Unit test verifies a foreign-key violation raises `IntegrityError`.
- [ ] Unit test verifies `PRAGMA journal_mode` returns `wal`, `PRAGMA foreign_keys` returns `1`, `PRAGMA busy_timeout` returns `5000`.
- [ ] `uv run pytest` passes.
