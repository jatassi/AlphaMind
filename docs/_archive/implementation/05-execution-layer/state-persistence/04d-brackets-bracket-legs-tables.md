# 04d — Brackets + bracket legs tables

## Goal

Ship `brackets` (parent record, 1:1 with positions) + `bracket_legs` (one-to-many child entities) SQL tables, plus round-trip serialisation between `BracketRecord` Pydantic and the SQLAlchemy rows. The bracket bridges the thesis (which justifies each exit condition) and the orders (which implement the conditions mechanically). Modification history (an ordered array of modification events: timestamp, PM command ID, field, old, new, rationale) round-trips. Each leg's discriminated trigger payload (`PriceTrigger` / `TimeTrigger` / `EventTrigger`) plus optional `PLAnchorSpec` round-trips.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Brackets — full record shape (bracket ID, position FK, status, entry order ID, protective legs, modification history)
* `docs/design/05-execution-layer/orders-and-brackets.md` — bracket structure + leg types (take-profit, price-stop, time-expiration, event-invalidation), enforcement (mechanical/advisory)
* `src/alphamind/portfolio_state/records/orders.py` § Bracket types — `BracketRecord`, `BracketLeg`, `BracketStatus`, `BracketLegType`, `BracketLegEnforcement`, `BracketLegStatus`, `PriceTrigger` / `TimeTrigger` / `EventTrigger` discriminated union, `PLAnchorSpec`
* Sibling story 04a (positions table) for the round-trip-codec pattern

## Depends on

* <issue id="937a53c3-5acb-425c-9c56-99dd3305cd26">ALP-357</issue> (this work tree, story 03) — `activity_log` references bracket events but not `bracket_id` directly; this story's table is reachable via position_id chain.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `BracketRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/brackets.py`:

* `bracket_id: TEXT PRIMARY KEY`
* `position_id: TEXT NOT NULL` — FK target only (constraint added during integration with positions table); UNIQUE (1:1 with position)
* `status: TEXT NOT NULL` — CHECK in `BracketStatus` values (`PENDING_ENTRY`, `ACTIVE`, `COMPLETED`, `DISSOLVED`)
* `entry_order_id: TEXT NOT NULL` — FK target only (constraint added during integration with orders table)
* `entry_window_deadline: TEXT` — nullable
* `modification_history_json: TEXT NOT NULL` — JSON array of modification events; defaults `[]`

Indexes:

* `ix_brackets_position_id` on `(position_id)` — drives 1:1 lookup
* `ix_brackets_status` on `(status)` — drives status-filtered queries

### 2\. `BracketLegRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/bracket_legs.py`:

* `bracket_leg_id: TEXT PRIMARY KEY` — composite identifier within a bracket (e.g., `<bracket_id>::<leg_index>`)
* `bracket_id: TEXT NOT NULL FK brackets.bracket_id ON DELETE RESTRICT`
* `leg_index: INTEGER NOT NULL` — preserves leg ordering within a bracket
* `leg_type: TEXT NOT NULL` — CHECK in `BracketLegType` values
* `order_id: TEXT` — nullable for `event-invalidation` legs; FK target only (constraint added during integration)
* `trigger_kind: TEXT NOT NULL` — CHECK in (`"PRICE"`, `"TIME"`, `"EVENT"`); the discriminator
* `trigger_payload_json: TEXT NOT NULL` — JSON-serialised `PriceTrigger | TimeTrigger | EventTrigger`
* `pl_anchor_json: TEXT` — JSON-serialised `PLAnchorSpec` when present (price-stop legs); nullable
* `enforcement: TEXT NOT NULL` — CHECK in `BracketLegEnforcement` values
* `leg_status: TEXT NOT NULL` — CHECK in `BracketLegStatus` values

Indexes:

* `ix_bracket_legs_bracket_id` on `(bracket_id)` — drives parent → children lookup
* UNIQUE constraint `uq_bracket_legs_bracket_id_leg_index` on `(bracket_id, leg_index)` — preserves ordering invariants

### 3\. Round-trip helpers

In `src/alphamind/execution/state_persistence/tables/brackets_codec.py`:

* `record_to_rows(record: BracketRecord) -> tuple[BracketRow, tuple[BracketLegRow, ...]]`
* `rows_to_record(bracket_row: BracketRow, leg_rows: tuple[BracketLegRow, ...]) -> BracketRecord`

Both use `TypeAdapter` for the trigger discriminated union and `PLAnchorSpec` (when present). Faithful round-trip across all four leg types and all three trigger kinds.

### 4\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_brackets_and_legs.py` creates both tables in one migration. `down_revision` chains to story 03's migration.

### Out of scope

* No write paths emitting `bracket_*` activity log events — story 08.
* No FK on `entry_order_id` to `orders.order_id` — added during integration.

## Acceptance criteria

- [ ] `brackets` table exists with all 6 columns, two indexes, CHECK on `status`.
- [ ] `bracket_legs` table exists with all 10 columns, two indexes (one UNIQUE), FK to `brackets`, and CHECK constraints on `leg_type` / `trigger_kind` / `enforcement` / `leg_status`.
- [ ] `record_to_rows` decomposes a `BracketRecord` into one parent row + N leg rows preserving order via `leg_index`.
- [ ] `rows_to_record` reconstructs faithfully; round-trip equality property-tested across multi-leg brackets with mixed leg types.
- [ ] Each of the three trigger kinds (PRICE / TIME / EVENT) round-trips through `trigger_payload_json` faithfully.
- [ ] `PLAnchorSpec` round-trips when present and is NULL when absent.
- [ ] The UNIQUE constraint on `(bracket_id, leg_index)` rejects duplicate-index inserts.
- [ ] Inserting a `bracket_legs` row referencing a nonexistent `bracket_id` is rejected by the FK constraint.
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_brackets_table.py` covers each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_brackets_table.py -n auto`. Spot-check the round-trip test for at least: a take-profit + price-stop + time-expiration 3-leg bracket, an event-invalidation advisory leg with no order_id, and a bracket with non-empty modification history.