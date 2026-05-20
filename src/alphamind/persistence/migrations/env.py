"""Alembic environment — SQLAlchemy 2.0 metadata target."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Make sure the project package is importable when running alembic from the
# repository root.
sys.path.insert(0, str(Path(__file__).parents[4]))

# Side-effect imports: each package whose modules attach tables to ``Base``
# must be imported here so ``Base.metadata`` is fully populated before
# ``compare_type``/``autogenerate`` runs. The state-persistence tables ship
# their own model files under ``execution.state_persistence.tables``.
import alphamind.state.tables  # noqa: F401
from alphamind.persistence.models import Base
from alphamind.persistence.session import _resolve_path, make_engine

# ---------------------------------------------------------------------------
# Alembic Config
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disable_foreign_keys_for_migrations(engine: Engine) -> None:
    """Turn ``PRAGMA foreign_keys`` OFF on every connection the migration run uses.

    Several migrations alter a column on a table inside the
    ``positions`` / ``orders`` / ``brackets`` / ``theses`` FK cycle. On SQLite
    that is a table *recreate* — ``op.batch_alter_table`` builds a temp table,
    copies rows, ``DROP``s the original, then renames. When the database holds
    child rows, that intermediate ``DROP TABLE`` trips the incoming
    ``ON DELETE RESTRICT`` foreign keys and the migration fails with
    ``FOREIGN KEY constraint failed`` (e.g. ``d3f6a1c7e9b2``, which makes
    ``positions.direction`` nullable, against a populated production database).

    ``PRAGMA foreign_keys`` is silently ignored while a transaction is pending,
    and Alembic runs the whole migration set inside one transaction on one
    connection — so it cannot be toggled from inside a migration. Issuing it
    from a ``connect`` listener fires it on the raw DBAPI connection at connect
    time, before any transaction begins, which is the only point SQLite honors
    the change. Migrations are pure schema operations and never rely on FK
    *enforcement* while they run; application connections re-enable it via the
    session-layer pragma hook, so durable referential integrity is unaffected.

    **Standing invariant**: every Alembic migration in this repo runs with
    FK enforcement OFF. A migration must therefore not rely on FK constraints
    to fail-fast on bad data moves — any such integrity check must be written
    explicitly inside the migration itself.
    """

    def _pragma_off(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    event.listen(engine, "connect", _pragma_off)


def _resolve_db_path() -> str:
    """
    Resolve the database path for this alembic invocation.

    Priority:
    1. ``-x db=<path>`` CLI argument  (alembic -x db=path/to/db.sqlite ...)
    2. ``DATABASE_PATH`` env var
    3. ``main.yaml`` paths.database

    Raises :class:`RuntimeError` (via :func:`_resolve_path`) when none resolve.
    """
    x_args: dict[str, str] = context.get_x_argument(as_dictionary=True)
    return x_args["db"] if "db" in x_args else _resolve_path(None)


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Generate SQL without connecting to the database."""
    path = _resolve_db_path()
    url = "sqlite:///:memory:" if path == ":memory:" else f"sqlite:///{path}"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    engine = make_engine(_resolve_db_path())
    # FK enforcement must be OFF so SQLite table-recreate migrations
    # (``op.batch_alter_table``) survive their intermediate ``DROP TABLE`` on a
    # populated database. See :func:`_disable_foreign_keys_for_migrations`.
    _disable_foreign_keys_for_migrations(engine)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        # Without explicit disposal the pooled SQLite connection outlives the
        # alembic call and keeps a Windows file handle on the DB, blocking
        # ``shutil.rmtree`` of replay-harness temp dirs (ALP-408).
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
