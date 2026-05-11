# 04c — Orders table + round-trip codec

## Goal

Ship the `orders` SQL table plus round-trip serialisation between `OrderRecord` Pydantic and the SQLAlchemy row. Orders are the most write-heavy entity in the system — status and fill fields update on every fill report, modifications update price parameters and Alpaca order IDs. The Alpaca order ID chain (ordered array of all replacement IDs) round-trips. After this story, orders can be persisted, retrieved, and round-trip-compared with no information loss.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Orders — full record shape (order ID, position FK, bracket FK, role, instrument spec, direction, type, quantity, price parameters, duration, status, Alpaca order ID + chain, timestamps, fill quantities, average fill price, modification count, metadata)
* `docs/design/05-execution-layer/orders-and-brackets.md` — order vocabulary the table mirrors
* `src/alphamind/portfolio_state/records/orders.py` — the typed `OrderRecord` with `OrderRole`, `OrderType`, `OrderDirection`, `OrderClass`, `OrderDuration`, `OrderStatus`, `InstrumentSpec`, `PriceParameters`
* `src/alphamind/portfolio_state/records/__init__.py` — public re-exports
* Sibling story 04a (positions table) for the round-trip-codec pattern

## Depends on

* <issue id="937a53c3-5acb-425c-9c56-99dd3305cd26">ALP-357</issue> (this work tree, story 03) — `activity_log` references `order_id`; this table is its FK target.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `OrderRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/orders.py`:

* `order_id: TEXT PRIMARY KEY` — OMS-assigned
* `position_id: TEXT` — nullable for entry orders rejected before a position was created; FK target only (constraint added during integration)
* `bracket_id: TEXT` — nullable; FK target only
* `order_role: TEXT NOT NULL` — CHECK in `OrderRole` values
* `order_class: TEXT NOT NULL` — CHECK in `OrderClass` values
* `instrument_spec_json: TEXT NOT NULL` — JSON-serialised `InstrumentSpec` (equity ticker, options contract, or strategy leg array)
* `direction: TEXT NOT NULL` — CHECK in `OrderDirection` values
* `order_type: TEXT NOT NULL` — CHECK in `OrderType` values
* `quantity: REAL NOT NULL` — shares or contracts (allows fractional)
* `price_parameters_json: TEXT NOT NULL` — JSON-serialised `PriceParameters` (limit price + stop price, nullables based on order type)
* `duration: TEXT NOT NULL` — CHECK in `OrderDuration` values (DAY / GTC / GTD)
* `status: TEXT NOT NULL` — CHECK in `OrderStatus` values
* `alpaca_order_id: TEXT` — nullable until first submission
* `alpaca_order_id_chain_json: TEXT NOT NULL` — JSON array of all Alpaca order IDs across replacements (empty until first submission)
* `submission_timestamp: TEXT` — nullable until submitted
* `last_update_timestamp: TEXT NOT NULL`
* `filled_quantity: REAL NOT NULL` — defaults 0
* `average_fill_price: REAL` — nullable until first fill
* `remaining_quantity: REAL NOT NULL`
* `modification_count: INTEGER NOT NULL` — defaults 0
* `metadata_json: TEXT NOT NULL` — JSON containing thesis_id, parent_bracket_id, pm_command_id

Indexes:

* `ix_orders_status` on `(status)` — drives `get_pending_orders`
* `ix_orders_position_id` on `(position_id)` — drives lookup by position
* `ix_orders_bracket_id` on `(bracket_id)` — drives lookup by bracket
* `ix_orders_alpaca_order_id` on `(alpaca_order_id)` — drives Alpaca-side reconciliation lookups

### 2\. Round-trip helpers

In `src/alphamind/execution/state_persistence/tables/orders_codec.py`:

* `record_to_row(record: OrderRecord) -> OrderRow`
* `row_to_record(row: OrderRow) -> OrderRecord`

Both use `TypeAdapter[InstrumentSpec]` for the discriminated-union round-trip (equity / options / strategy).

### 3\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_orders.py` creates the table + indexes + CHECK constraints. `down_revision` chains to story 03's migration.

### Out of scope

* No write paths emitting `order_*` activity log events — story 08.
* No fill-record FK back to orders — story 05.
* No bracket↔order linkage on the bracket side — story 04d.

## Acceptance criteria

- [ ] `orders` table exists with all 21 columns, the four indexes, and CHECK constraints on `order_role` / `order_class` / `direction` / `order_type` / `duration` / `status`.
- [ ] `record_to_row(record)` faithfully serialises every `OrderRole` / `OrderClass` / `OrderDirection` combination valid per the in-memory invariants.
- [ ] `row_to_record(row)` faithfully deserialises; `row_to_record(record_to_row(o)) == o` is property-tested across the order-role enum.
- [ ] Round-trip preserves the `alpaca_order_id_chain` ordering (a 3-element chain remains a 3-element chain in the same order).
- [ ] Round-trip preserves nullable fields correctly: a pending unsubmitted order with `alpaca_order_id = None` and empty chain round-trips faithfully.
- [ ] Round-trip preserves `instrument_spec`'s discriminated-union variants (equity / options / strategy).
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_orders_table.py` covers each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_orders_table.py -n auto`. Spot-check the round-trip test for at least these cases: pending unsubmitted order, partially-filled active order with Alpaca chain of length 2, fully-filled cancelled-after-fill order, options order, multi-leg strategy order.