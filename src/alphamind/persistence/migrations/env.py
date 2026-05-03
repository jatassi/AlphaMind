"""Alembic environment — SQLAlchemy 2.0 metadata target."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make sure the project package is importable when running alembic from the
# repository root.
sys.path.insert(0, str(Path(__file__).parents[4]))

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


def _resolve_db_path() -> str:
    """
    Resolve the database path for this alembic invocation.

    Priority:
    1. ``-x db=<path>`` CLI argument  (alembic -x db=path/to/db.sqlite ...)
    2. ``DATABASE_PATH`` env var
    3. ``main.yaml`` paths.database
    4. Platform default
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
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
