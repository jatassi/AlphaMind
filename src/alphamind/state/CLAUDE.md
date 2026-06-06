# state/ — invocation & run-state persistence (audit trail)

Data layer (internal). The pipeline's own state and audit substrate: `tables/` (ORM
tables for invocations, briefs, activity), `repository/` (read/write access),
`invocation_context/` (per-run context). Backs the immutable audit trail every layer
writes to. Design intent (historical): `docs/architecture/data-and-state.md`.

## Key invariants

- Append-mostly audit trail — invocation records are the system's ground truth for "what happened"; don't mutate history.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- `tests/conftest.py` imports `alphamind.state.tables` so FK targets register before scoped runs; without it, scoped pytest used to fail on a `briefs`→`invocations` FK error.
- `BrokerEventLogRow.event_seq` (ALP-865) and `ActivityLogRow.event_seq` (ALP-870) are both the SQLite implicit `rowid` mapped via `column_property(literal_column("<table>.rowid"))` — emits no DDL, but a query referencing **only** `event_seq` has no `FROM` anchor (`select(func.max(event_seq))` fails "no such column"). Always co-select/co-filter a real column (the emergency receiver's `func.max(event_seq)` is anchored by its `event_type` WHERE), or take the max of scanned rows in Python.
