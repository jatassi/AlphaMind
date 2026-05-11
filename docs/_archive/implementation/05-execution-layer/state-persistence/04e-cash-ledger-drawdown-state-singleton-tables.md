# 04e — Cash ledger + drawdown state singleton tables

## Goal

Ship two singleton-row SQL tables — `cash_ledger` (running cash state) and `drawdown_state` (running portfolio high-water-mark + current drawdown) — plus round-trip serialisation between the typed Pydantic models and the rows. Both are mutable singletons; historical reconstruction comes from the activity log. After this story, cash and drawdown state can be persisted, retrieved, and round-trip-compared with no information loss.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Cash ledger — singleton record shape (current cash, settled cash, reserved capital, available buying power, margin held, unsettled proceeds with per-transaction settlement dates)
* `docs/design/05-execution-layer/state-persistence.md` § Tier 3 — Portfolio summary lists drawdown fields; per parent issue Pre-resolved decision (D), `drawdown_state` is its own singleton table
* `src/alphamind/portfolio_state/records/cash.py` — typed `CashLedger` + `UnsettledProceedsEntry`
* `src/alphamind/portfolio_state/aggregates/drawdown.py` — typed `DrawdownState`
* `src/alphamind/portfolio_state/records/__init__.py` and `aggregates/__init__.py` — public re-exports
* Sibling story 04a (positions table) for the round-trip-codec pattern

## Depends on

* <issue id="937a53c3-5acb-425c-9c56-99dd3305cd26">ALP-357</issue> (this work tree, story 03) — `activity_log` references cash + capital events; this story's tables are reachable indirectly via the activity log.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `CashLedgerRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/cash_ledger.py`:

Singleton-row pattern: a single row with `id = "current"` (PK, CHECK constraint enforcing the value).

* `id: TEXT PRIMARY KEY` — CHECK `id = "current"` (singleton invariant)
* `current_cash_balance_usd: REAL NOT NULL`
* `settled_cash_balance_usd: REAL NOT NULL`
* `reserved_capital_usd: REAL NOT NULL`
* `available_buying_power_usd: REAL NOT NULL`
* `margin_held_usd: REAL NOT NULL`
* `unsettled_proceeds_json: TEXT NOT NULL` — JSON array of `UnsettledProceedsEntry` records (each with amount + settlement date)
* `last_updated_at: TEXT NOT NULL` — ISO 8601 UTC

### 2\. `DrawdownStateRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/drawdown_state.py`:

Singleton-row pattern: `id = "current"` (PK, CHECK constraint).

* `id: TEXT PRIMARY KEY` — CHECK `id = "current"`
* `equity_high_water_mark_usd: REAL NOT NULL`
* `current_portfolio_value_usd: REAL NOT NULL`
* `current_drawdown_pct: REAL NOT NULL`
* `peak_timestamp: TEXT NOT NULL` — when the HWM was last established
* `last_updated_at: TEXT NOT NULL`
* `drawdown_by_source_json: TEXT NOT NULL` — JSON map of position_id → drawdown contribution; defaults `{}`

### 3\. Round-trip helpers

In `src/alphamind/execution/state_persistence/tables/cash_ledger_codec.py` and `drawdown_state_codec.py`:

* `record_to_row(record: CashLedger) -> CashLedgerRow` and inverse
* `record_to_row(state: DrawdownState) -> DrawdownStateRow` and inverse

Faithful round-trip for both, including the unsettled-proceeds array and drawdown-by-source map.

### 4\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_cash_ledger_and_drawdown_state.py` creates both tables in one migration. `down_revision` chains to story 03's migration.

### Out of scope

* No write paths updating either table from fills / commands — stories 07 and 08.
* No `portfolio_summary` Tier-3 table (parent issue Pre-resolved decision (D): P/L computed on-the-fly).
* No `risk_budget_consumption` table (computed at read time per Pre-resolved decision (D)).

## Acceptance criteria

- [ ] `cash_ledger` table exists with 8 columns and the `id = "current"` CHECK constraint.
- [ ] `drawdown_state` table exists with 7 columns and the `id = "current"` CHECK constraint.
- [ ] Inserting a second row with `id != "current"` is rejected by the CHECK constraint on each table.
- [ ] `CashLedger ↔ CashLedgerRow` round-trip is faithful, including an unsettled-proceeds list of multiple entries with distinct settlement dates.
- [ ] `DrawdownState ↔ DrawdownStateRow` round-trip is faithful, including a populated `drawdown_by_source` map.
- [ ] An empty `unsettled_proceeds` list and an empty `drawdown_by_source` map both round-trip correctly (typed-record default values preserved).
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_cash_ledger_table.py` and `test_drawdown_state_table.py` cover each acceptance criterion.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_cash_ledger_table.py tests/execution/state_persistence/test_drawdown_state_table.py -n auto`. Confirm the singleton CHECK constraint is exercised in at least one test per table.