# 04b — Theses + thesis components tables

## Goal

Ship `theses` (parent record) + `thesis_components` (one-to-many child entities) SQL tables, plus round-trip serialisation between `ThesisRecord` Pydantic and the SQLAlchemy rows. The parent-child split is mandated by the design's three consumption modes (programmatic-query metadata, single-component LLM evaluation, full-thesis evaluation). Status state machine (active → resolved | cancelled) and per-component resolution outcomes (validated / wrong / inconclusive) round-trip. After this story, theses can be persisted with their components, retrieved, and round-trip-compared with no information loss.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Theses — persistence-layer fields beyond the thesis model (thesis ID, status, resolution timestamp/category, position FK)
* `docs/design/05-execution-layer/state-persistence.md` § Thesis components — the child entity shape (component ID, thesis FK, resolution outcome, resolution notes)
* `docs/design/05-execution-layer/thesis-model.md` — the structured thesis stored across the parent + child entities
* `src/alphamind/portfolio_state/records/theses.py` — the typed `ThesisRecord` + `ThesisComponent` + supporting types (`KeyAssumption`, `SupportingSignal`, `ThesisResolutionCategory`, `ThesisStatus`, etc.)
* `src/alphamind/portfolio_state/records/__init__.py` — public re-exports
* Sibling story 04a (positions table) for the round-trip-codec pattern

## Depends on

* <issue id="937a53c3-5acb-425c-9c56-99dd3305cd26">ALP-357</issue> (this work tree, story 03) — `activity_log` references `thesis_id`; this table is its FK target.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `ThesisRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/theses.py`:

* `thesis_id: TEXT PRIMARY KEY`
* `position_id: TEXT NOT NULL` — FK target only (constraint added during integration with positions table)
* `status: TEXT NOT NULL` — CHECK in (`"ACTIVE"`, `"RESOLVED"`, `"CANCELLED"`)
* `resolution_timestamp: TEXT` — nullable while active
* `resolution_category: TEXT` — CHECK in the four `ThesisResolutionCategory` values + `"CANCELLED_NEVER_ENTERED"`; nullable while active
* `summary: TEXT NOT NULL`
* `time_expectation_hours: REAL`
* `position_size_rationale: TEXT`
* `generation_timestamp: TEXT NOT NULL`
* `narrative_json: TEXT NOT NULL` — JSON-serialised non-component fields preserving the full Pydantic shape

Indexes:

* `ix_theses_status` on `(status)` — drives `get_active_theses`
* `ix_theses_position_id` on `(position_id)` — drives lookup by position
* `ix_theses_resolution_timestamp` on `(resolution_timestamp)` — drives `get_recent_thesis_resolutions`

### 2\. `ThesisComponentRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/thesis_components.py`:

* `component_id: TEXT PRIMARY KEY`
* `thesis_id: TEXT NOT NULL FK theses.thesis_id ON DELETE RESTRICT`
* `component_type: TEXT NOT NULL` — CHECK in the `ThesisComponentType` enum values
* `linked_bracket_leg: TEXT` — nullable; references a bracket leg identifier
* `instrument_reference: TEXT` — nullable
* `narrative: TEXT NOT NULL`
* `key_assumptions_json: TEXT NOT NULL` — JSON array of `KeyAssumption` records
* `supporting_signals_json: TEXT NOT NULL` — JSON array of `SupportingSignal` records
* `resolution_outcome: TEXT` — CHECK in (`"VALIDATED"`, `"WRONG"`, `"INCONCLUSIVE"`); nullable while thesis active
* `resolution_notes: TEXT` — nullable while active

Indexes:

* `ix_thesis_components_thesis_id` on `(thesis_id)` — drives parent → children lookup

### 3\. Round-trip helpers

In `src/alphamind/execution/state_persistence/tables/theses_codec.py`:

* `record_to_rows(record: ThesisRecord) -> tuple[ThesisRow, tuple[ThesisComponentRow, ...]]`
* `rows_to_record(thesis_row: ThesisRow, component_rows: tuple[ThesisComponentRow, ...]) -> ThesisRecord`

Faithful round-trip: `rows_to_record(*record_to_rows(t)) == t` for every valid `ThesisRecord`.

### 4\. Alembic migration

`src/alphamind/persistence/migrations/versions/<rev>_add_theses_and_components.py` creates both tables in one migration (FK requires the parent to exist within the same migration). `down_revision` chains to story 03's migration.

### Out of scope

* No `RecentThesisResolution` materialisation — that's a derived view computed at snapshot read time (story 06).
* No `thesis_quality_aggregates` table (parent issue Pre-resolved decision (D)).
* No write paths emitting `thesis_*` activity log events — story 08.

## Acceptance criteria

- [ ] `theses` table exists with all 10 columns, three indexes, and CHECK constraints on `status` and `resolution_category`.
- [ ] `thesis_components` table exists with all 10 columns, one index, FK to `theses` ON DELETE RESTRICT, and CHECK on `component_type` + `resolution_outcome`.
- [ ] `record_to_rows` decomposes a `ThesisRecord` into one parent row + N component rows in component-order-preserving fashion.
- [ ] `rows_to_record` reconstructs a `ThesisRecord` faithfully; round-trip equality property-tested across multiple component counts (0, 1, many).
- [ ] Inserting a `thesis_components` row referencing a nonexistent `thesis_id` is rejected by the FK constraint.
- [ ] Inserting a thesis with `status = "ACTIVE"` and a non-null `resolution_timestamp` is rejected by the typed validator.
- [ ] The Alembic migration is idempotent.
- [ ] `tests/execution/state_persistence/test_theses_table.py` covers each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_theses_table.py -n auto`. Spot-check that the round-trip test exercises a multi-component thesis with both ACTIVE and RESOLVED statuses, and that resolution outcome propagates per-component.