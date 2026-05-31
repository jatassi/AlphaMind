# persistence/ — SQLAlchemy models + Alembic migrations

Infrastructure. ORM models and the Alembic migration tree (`migrations/versions/`) over
the single SQLite database (WAL mode). Design intent (historical):
`docs/architecture/data-and-state.md`.

## Key invariants

- A new SQLAlchemy model needs a matching Alembic migration **and** a migration test — the lint+test chain passes green without one, but production (alembic-managed) never gets the table.
- Single database, single writer (execution layer); WAL mode means dev-Mac access over SMB is read-only (`immutable=1`).

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- Early creation-migrations build CHECK constraints from the live enum, so adding an enum member retroactively changes them on fresh DBs — test a status-widening migration via downgrade→reject→upgrade→accept, not by upgrading forward.
