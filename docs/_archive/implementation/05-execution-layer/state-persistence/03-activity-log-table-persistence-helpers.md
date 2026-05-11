# 03 — Activity log table + persistence helpers

## Goal

Ship the `activity_log` SQL table plus two persistence helpers that round-trip the in-memory `ActivityLogEntry` family — `append_activity_log_entry(handle, entry)` writes one entry inside the open `InvocationContext` transaction; `query_activity_log` exposes the read APIs the snapshot assembler and the three blocked distillation stories need (intra-invocation changelog, recent PM decisions sliding window, position-modification trail, "most recent distillation_config_change new_hash"). The table stores the polymorphic `EventType` discriminator + the JSON-serialized event detail payload (every `*Detail` Pydantic class round-trips faithfully). Activity-log entry creation flows through `InvocationContext`'s session handle so emissions commit atomically with the invocation row.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Activity log entries — the entry shape and the full event-type catalog (position lifecycle, order lifecycle, bracket events, thesis events, cash and margin events, risk and guardrail events, PM decision events, corporate action events, configuration events)
* `docs/design/05-execution-layer/state-persistence.md` § Read paths — defines the three activity-log projections the snapshot read consumes (intra-invocation changelog, PM decision sliding window, position modification trail)
* `src/alphamind/portfolio_state/events/activity_log.py` — the typed `ActivityLogEntry` + every `*Detail` Pydantic class (positions, orders, brackets, theses, cash, capital, margin, guardrail, PM-decision, corporate-action, configuration). The SQL round-trip MUST preserve every field.
* `src/alphamind/portfolio_state/events/__init__.py` — the discriminated-union shape (`EventType` enum + per-type detail mapping)
* `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — defines the `distillation_config_change` event detail and the `prior_hash`/`new_hash` semantics the read API supports
* <issue id="6535bdb3-1142-4b6c-9539-b7ae0121c013">ALP-100</issue> (sibling work tree) — the consumer of the "most recent distillation_config_change new_hash" read API; verify the API shape the loader expects
* `src/alphamind/execution/state_persistence/invocation_context/context.py` (from story 02b) — the session handle this story's emission helper accepts
* `src/alphamind/persistence/migrations/versions/3fbbfd3049f5_add_regime_adaptation_state_table.py` — sibling-feature migration shape

## Depends on

* <issue id="970152af-549c-4d0d-b61c-d47ca6930098">ALP-356</issue> (this work tree, story 02b) — `invocations` table is the FK target and `InvocationContext` is the transaction substrate this story extends.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `ActivityLogRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/activity_log.py`, define `ActivityLogRow(Base)`:

* `entry_id: TEXT PRIMARY KEY` — monotonically increasing string
* `invocation_id: TEXT NOT NULL FK invocations.invocation_id ON DELETE RESTRICT`
* `entry_at: TEXT NOT NULL` — ISO 8601 UTC of the event
* `event_type: TEXT NOT NULL` — value of the `EventType` StrEnum (CHECK constraint enumerating every catalog entry)
* `event_group: TEXT NOT NULL` — value of the `EventGroup` StrEnum
* `position_id: TEXT` — nullable; when set, FK to `positions.position_id` ON DELETE RESTRICT once that table exists (initially no FK; story 04a adds the constraint via a follow-up migration in this story IF the order is fixed during integration — otherwise leave as a plain nullable TEXT and document the integration-time follow-up in the migration's docstring)
* `order_id: TEXT` — nullable; same FK pattern as `position_id`
* `thesis_id: TEXT` — nullable; same FK pattern
* `source: TEXT NOT NULL` — value of the `EventSource` StrEnum (CHECK constraint)
* `detail_json: TEXT NOT NULL` — JSON-serialized event detail payload (the per-EventType Pydantic detail's `model_dump_json()`)

Indexes:

* `ix_activity_log_invocation_id` on `(invocation_id)` — drives the intra-invocation changelog query
* `ix_activity_log_event_type_invocation_id` on `(event_type, invocation_id)` — drives the PM decision sliding window query
* `ix_activity_log_position_id_entry_at` on `(position_id, entry_at)` — drives the position modification trail query

### 2\. Emission helper: `append_activity_log_entry`

In `src/alphamind/execution/state_persistence/invocation_context/activity_log.py`:

```python
async def append_activity_log_entry(
    handle: InvocationHandle,
    entry: ActivityLogEntry,
) -> None:
    """Persist one activity log entry inside the open InvocationContext transaction."""
```

Behavior:

* Validates `entry.invocation_id == handle.invocation_id` (refuse to append cross-invocation entries).
* Serializes `entry.detail` to JSON via `model_dump_json()`.
* Constructs the `ActivityLogRow` and adds it to the open session — does NOT commit (the surrounding `InvocationContext` commits when its transaction exits cleanly).

### 3\. Read API: query helpers

In `src/alphamind/execution/state_persistence/repository/activity_log_queries.py`, ship four async read functions taking a SQLAlchemy session and returning `tuple[ActivityLogEntry, ...]` (rehydrated to the typed shape):

* `read_intra_invocation_changelog(session, invocation_id) -> tuple[ActivityLogEntry, ...]` — entries whose `invocation_id == invocation_id`, ordered by `entry_at` ascending.
* `read_recent_pm_decision_log(session, sliding_window_invocations) -> tuple[ActivityLogEntry, ...]` — entries with `event_type == EventType.PM_DECISION` from the most recent N invocations (joining against `invocations` on `start_at` desc, taking the top N).
* `read_position_modification_trail(session, position_ids) -> dict[str, tuple[ActivityLogEntry, ...]]` — entries scoped to each given position_id, ordered chronologically per position.
* `read_most_recent_config_change_new_hash(session, config_file) -> str | None` — the `new_hash` from the most recent `distillation_config_change` event whose detail's `config_file` matches the supplied path, or None if no entry exists. Consumed by [ALP-100](https://linear.app/alphamind-jatassi/issue/ALP-100).

Each query must rehydrate every `*Detail` payload through the typed Pydantic shape (use `TypeAdapter[ActivityLogEntry]` for the discriminated-union dispatch).

### 4\. Alembic migration

A single migration at `src/alphamind/persistence/migrations/versions/<rev>_add_activity_log.py` creating the table + the three indexes. `down_revision` chains to story 02b's migration.

### Out of scope

* No FK constraints to positions/orders/theses — those tables don't exist yet (stories 04a–04d). Add as nullable columns now; FKs land in a follow-up migration during integration.
* No emission from the configuration loader — that's [ALP-100](https://linear.app/alphamind-jatassi/issue/ALP-100)'s job.
* No emission from Phase 1 / Phase 2 write paths — stories 07 and 08.
* No `agent_calls` table (deferred per parent issue Pre-resolved decision (B)).

## Acceptance criteria

- [ ] `activity_log` table exists with all nine columns named above, the three indexes, and CHECK constraints on `event_type`, `event_group`, and `source`.
- [ ] `append_activity_log_entry(handle, entry)` validates the invocation_id match and rejects cross-invocation appends with a clear error.
- [ ] An emitted entry's `detail_json` round-trips through Pydantic without information loss for at least one detail class from each `EventGroup` (position lifecycle, order lifecycle, bracket, thesis, cash/margin, risk/guardrail, PM decision, corporate action, configuration).
- [ ] `read_intra_invocation_changelog` returns only entries with the matching `invocation_id`, ordered by `entry_at` ascending.
- [ ] `read_recent_pm_decision_log` returns only `PM_DECISION` entries from the most recent N invocations.
- [ ] `read_position_modification_trail` returns a dict keyed by position_id, with entries ordered chronologically.
- [ ] `read_most_recent_config_change_new_hash` returns the new_hash from the most recent matching entry, or None when no match exists.
- [ ] An emission inside `InvocationContext` that exits with an exception does NOT persist the activity log entry (transactional atomicity test).
- [ ] The Alembic migration is idempotent (`upgrade` then `downgrade` then `upgrade` succeeds).
- [ ] `tests/execution/state_persistence/test_activity_log.py` covers each acceptance criterion above.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run `uv run pytest tests/execution/state_persistence/test_activity_log.py -n auto`. Verify that the round-trip test exercises at least one detail class from every `EventGroup` and that the four read APIs each have a happy-path + edge-case test (empty result, multiple invocations, sliding-window boundary).