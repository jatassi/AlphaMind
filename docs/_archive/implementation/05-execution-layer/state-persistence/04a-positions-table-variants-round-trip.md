# 04a — Positions table + variants round-trip

## Goal

Ship the `positions` SQL table with the discriminated-union `PositionDetailsPayload` (equity / options / strategy variants) stored as a typed sub-object, plus round-trip serialisation between `PositionRecord` Pydantic and the SQLAlchemy row. Position is the central business object — every other Tier 1 / Tier 2 entity references it. Status state machine (pending → open → closed) is enforced as a CHECK constraint. Execution history (`tuple[PositionFill, ...]`) and corporate-action provenance (`parent_position_id`, `origin`) round-trip. After this story, positions can be persisted, retrieved, and round-trip-compared with no information loss.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Positions — persistence-layer fields beyond the position model (status, realized P/L, position ID)
* `docs/design/05-execution-layer/position-model.md` — the position hierarchy stored in this table
* `src/alphamind/portfolio_state/records/positions.py` — the typed `PositionRecord` with `EquityPositionDetails` / `OptionsPositionDetails` / `StrategyPositionDetails` discriminated-union variants and `PositionFill` execution history items. The SQL column shape mirrors this Pydantic shape; do NOT redefine fields.
* `src/alphamind/portfolio_state/records/__init__.py` — the public re-export surface to consume
* `src/alphamind/persistence/models.py` — existing SQLAlchemy `Base` + table patterns to mirror

## Depends on

* <issue id="937a53c3-5acb-425c-9c56-99dd3305cd26">ALP-357</issue> (this work tree, story 03) — `activity_log` carries `position_id` references; this story's table is the FK target activity_log will eventually point to (FK constraint added later during integration per story 03's out-of-scope note).

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `PositionRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/positions.py`, define `PositionRow(Base)` mirroring `PositionRecord`:

* `position_id: TEXT PRIMARY KEY`
* `thesis_id: TEXT` — nullable; FK to `theses.thesis_id` once that table exists (no FK constraint at this story's time; integration adds it)
* `bracket_id: TEXT` — nullable; same FK pattern
* `status: TEXT NOT NULL` — CHECK in (`"PENDING"`, `"OPEN"`, `"CLOSED"`)
* `direction: TEXT NOT NULL` — CHECK in (`"LONG"`, `"SHORT"`)
* `entry_timestamp: TEXT` — nullable until OPEN
* `instrument_type: TEXT NOT NULL` — CHECK in (`"EQUITY"`, `"OPTIONS"`, `"STRATEGY"`); the discriminator value
* `details_json: TEXT NOT NULL` — JSON-serialised `PositionDetailsPayload` (one of the three variants per `instrument_type`)
* `execution_history_json: TEXT NOT NULL` — JSON array of `PositionFill` records (empty array allowed when status is PENDING)
* `realized_pnl_to_date_usd: REAL` — nullable; required when status is CLOSED
* `corporate_action_adjustment_needed: INTEGER NOT NULL` — 0/1
* `parent_position_id: TEXT` — nullable; for spin-offs
* `origin: TEXT` — nullable; non-null when parent_position_id is non-null

Indexes:

* `ix_positions_status` on `(status)` — drives `get_open_positions` / `get_pending_positions`
* `ix_positions_thesis_id` on `(thesis_id)` — drives lookup by thesis

### 2\. Round-trip helpers

In `src/alphamind/execution/state_persistence/tables/positions_codec.py`:

* `record_to_row(record: PositionRecord) -> PositionRow`
* `row_to_record(row: PositionRow) -> PositionRecord`

Both use `TypeAdapter[PositionDetailsPayload]` for the discriminated-union round-trip and `TypeAdapter[tuple[PositionFill, ...]]` for execution history. Faithful round-trip is the contract: `row_to_record(record_to_row(p)) == p` for every valid `PositionRecord`.

### 3\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_positions.py` creates the table + indexes + CHECK constraints. `down_revision` chains to story 03's migration.

### Out of scope

* No `theses`, `brackets`, or `orders` tables — those are sibling parallel stories.
* No FK constraints from `activity_log` (story 03) back to this table — integration phase.
* No write paths into this table (Phase 1 / Phase 2 are stories 07 / 08).
* No reads of this table from the repository (story 06).

## Acceptance criteria

- [ ] `positions` table exists with all 13 columns, the three CHECK constraints, and the two indexes.
- [ ] `record_to_row(record)` faithfully serialises every variant (equity, options, strategy) without information loss.
- [ ] `row_to_record(row)` faithfully deserialises every variant; `row_to_record(record_to_row(r)) == r` is property-tested across all three variants.
- [ ] Inserting a row with `status = "PENDING"` and a non-empty `execution_history_json` is rejected by the typed Pydantic validator at `record_to_row` time (mirrors the existing in-memory invariant).
- [ ] Inserting a row with `status = "CLOSED"` and a NULL `realized_pnl_to_date_usd` is rejected at `record_to_row` time.
- [ ] Inserting a row with mismatched `instrument_type` and `details_json.instrument_type` discriminator is rejected.
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_positions_table.py` covers each acceptance criterion above plus a fixture-driven round-trip for each of the three variants.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_positions_table.py -n auto`. Spot-check the round-trip test for the three variants (equity / options / strategy) by reading the test fixtures and confirming each is non-trivial (multiple fills, populated greeks for options, multi-leg for strategy).