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

- The squashed baseline (`a000000000aa`) is **metadata-driven** (`Base.metadata.create_all`), so on a fresh DB it already creates whatever the current models declare. An incremental migration that adds a new model table/column must therefore be **idempotent** (guard `create_table`/`add_column` with `sa.inspect(op.get_bind())`), or `alembic upgrade head` double-creates ("table already exists") and `test_mapper_fk_autogenerate`'s head-equals-metadata check fails. See `a865wm0000bb`.
