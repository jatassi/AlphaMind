# 05 — Fill records + corporate-action integration ledger tables

## Goal

Ship two append-only Tier-2 lifecycle tables: `fill_records` (one row per fill event with the unprocessed/processed status flag and Reg T attribution metadata) and `corporate_action_integration_ledger` (one row per Alpaca CA activity tracked by its `alpaca_activity_id`). Both feed the Phase 1 fill-integration write path: the continuous monitor appends new fills as `unprocessed`, Phase 1 marks them `processed` inside the same transaction that mutates positions/orders/cash. After this story, the durable substrate exists for the immediate-fill-persistence path (story 07).

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Fill records — full record shape: fill ID, order FK, fill timestamp, fill price + quantity, remaining quantity, order status after fill, slippage, fees, execution venue, gateway reference, persistence timestamp, processing status, processing invocation ID, processing timestamp, Reg T margin attribution
* `docs/design/05-execution-layer/state-persistence.md` § Corporate action integration ledger — `alpaca_activity_id` (PK, dedupe anchor), processing invocation ID, processing timestamp, processing status
* `docs/design/05-execution-layer/state-persistence.md` § Fill persistence path — defines the append-only contract this story's write path must satisfy
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path — defines the processing-status transition this story enables
* `docs/design/05-execution-layer/regt-margin-attribution.md` — defines the Reg T attribution sub-record stored on each fill record
* `docs/design/05-execution-layer/corporate-actions.md` — defines the integration mechanics for the CA ledger
* Story 04c (orders table) — the FK target for fill_records
* Story 02b (invocations) — the FK target for processing_invocation_id

## Depends on

* <issue id="7049d546-26ff-4d45-a1ac-455c6093e97b">ALP-360</issue> (this work tree, story 04c) — `fill_records.order_id` is FK to `orders.order_id`.
* <issue id="970152af-549c-4d0d-b61c-d47ca6930098">ALP-356</issue> (this work tree, story 02b) — `fill_records.processing_invocation_id` and `corporate_action_integration_ledger.processing_invocation_id` are FKs to `invocations`.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `FillRecordRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/fill_records.py`:

* `fill_id: TEXT PRIMARY KEY`
* `order_id: TEXT NOT NULL FK orders.order_id ON DELETE RESTRICT`
* `fill_timestamp: TEXT NOT NULL` — ISO 8601 UTC of the actual market event
* `fill_price: REAL NOT NULL`
* `fill_quantity: REAL NOT NULL`
* `remaining_quantity_after: REAL NOT NULL`
* `order_status_after: TEXT NOT NULL` — CHECK in `OrderStatus` values
* `slippage_usd: REAL` — nullable
* `fees_usd: REAL NOT NULL`
* `execution_venue: TEXT` — nullable; live-mode only
* `gateway_reference: TEXT` — nullable
* `persistence_timestamp: TEXT NOT NULL`
* `processing_status: TEXT NOT NULL` — CHECK in (`"unprocessed"`, `"processed"`, `"quarantined"`)
* `processing_invocation_id: TEXT` — nullable until processed; FK to `invocations.invocation_id` ON DELETE RESTRICT
* `processing_timestamp: TEXT` — nullable until processed
* `regt_attribution_json: TEXT` — nullable on fills predating the attribution module
* `live_execution_estimate_json: TEXT` — nullable; populated only in paper mode per the paper-evaluation harness

UNIQUE constraint `uq_fill_records_dedupe` on `(order_id, fill_timestamp, fill_quantity, fill_price)` — implements the design doc's deduplication contract.

Indexes:

* `ix_fill_records_processing_status` on `(processing_status)` — drives the Phase 1 "fetch unprocessed fills" query
* `ix_fill_records_order_id` on `(order_id)` — drives reconstruction queries

### 2\. `CorporateActionIntegrationLedgerRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/corporate_action_integration_ledger.py`:

* `alpaca_activity_id: TEXT PRIMARY KEY` — Alpaca's identifier; serves as the dedupe anchor
* `processing_invocation_id: TEXT NOT NULL FK invocations.invocation_id ON DELETE RESTRICT`
* `processing_timestamp: TEXT NOT NULL`
* `processing_status: TEXT NOT NULL` — CHECK in (`"processed"`)

Index:

* `ix_ca_ledger_processing_invocation_id` on `(processing_invocation_id)` — drives per-invocation lookup

### 3\. Append-only write helpers

In `src/alphamind/execution/state_persistence/write_paths/fill_persistence.py`:

* `async def append_fill_record(session, fill: FillRecord) -> None` — INSERT one row with `processing_status = "unprocessed"`, `persistence_timestamp = now`. Idempotent on the dedupe key (silently skip duplicates per the design doc). Does NOT touch any other entity.

In `src/alphamind/execution/state_persistence/write_paths/ca_integration_ledger.py`:

* `async def mark_ca_activity_processed(handle, alpaca_activity_id) -> None` — INSERT inside the open `InvocationContext` transaction; idempotent on the PK.

### 4\. Typed `FillRecord` Pydantic façade

In `src/alphamind/execution/state_persistence/write_paths/records.py`, define a frozen Pydantic `FillRecord` mirroring the SQLAlchemy column shape (one of the rare cases where the typed shape doesn't yet exist in `portfolio_state/`; this story may be the first place it lands). If `portfolio_state/events/activity_log.py` already has a `FillRecord` shape, prefer reusing it rather than introducing a duplicate.

### 5\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_fill_records_and_ca_ledger.py` creates both tables in one migration. `down_revision` chains to story 04c (or whichever 04\* migration the integration phase ordered last).

### Out of scope

* No Phase 1 fill-integration mutation logic — story 07.
* No FK constraints from `activity_log.position_id` or `activity_log.order_id` to their respective tables — integration phase.
* No CA-specific event generation (`corporate_action_applied` activity log entries) — story 07.

## Acceptance criteria

- [ ] `fill_records` table exists with all 17 columns, the UNIQUE dedupe constraint, two indexes, FK on `order_id`, and FK on `processing_invocation_id`.
- [ ] `corporate_action_integration_ledger` table exists with all 4 columns, one index, FK on `processing_invocation_id`, and CHECK on `processing_status`.
- [ ] `append_fill_record` inserts a fresh fill with `processing_status = "unprocessed"` and a non-null `persistence_timestamp`.
- [ ] `append_fill_record` is idempotent on the dedupe key — calling twice with the same `(order_id, fill_timestamp, fill_quantity, fill_price)` does NOT create a duplicate row.
- [ ] `mark_ca_activity_processed` inside an `InvocationContext` is idempotent on `alpaca_activity_id`.
- [ ] Inserting a fill referencing a nonexistent `order_id` is rejected by the FK constraint.
- [ ] Inserting a fill with `processing_status = "processed"` and a NULL `processing_invocation_id` is rejected by an application-level invariant (or a CHECK constraint).
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_fill_records_table.py` and `test_ca_integration_ledger_table.py` cover each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/ -n auto`. Spot-check the dedup test: insert the same fill twice, assert one row exists. Spot-check the FK test: insert a fill with nonexistent order_id and confirm an `IntegrityError` is raised.