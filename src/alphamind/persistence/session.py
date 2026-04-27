"""
Session factory for AlphaMind's SQLite database.

Database path resolution order:
  1. Explicit ``path`` argument passed to :func:`make_engine`
  2. ``DATABASE_PATH`` environment variable
  3. ``main.yaml`` ``paths.database`` key
  4. Platform default: ``%USERPROFILE%\\AlphaMind\\data\\alphamind.db``

Pragmas applied on every new connection (from data-and-state.md):
  - ``journal_mode = WAL``
  - ``busy_timeout = 60000``
  - ``foreign_keys = ON``
  - ``synchronous = NORMAL``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.orm import Session, sessionmaker


def _default_db_path() -> str:
    """Return the platform-default database path."""
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return str(Path(userprofile) / "AlphaMind" / "data" / "alphamind.db")


def _resolve_path(path: str | None) -> str:
    """Resolve the database path using the documented priority chain."""
    if path is not None:
        return path
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return env_path
    try:
        import yaml  # type: ignore[import-untyped]

        config_file = Path(__file__).parents[3] / "config" / "main.yaml"
        if config_file.exists():
            with config_file.open() as fh:
                cfg: dict[str, Any] = yaml.safe_load(fh) or {}
            db_path: str | None = cfg.get("paths", {}).get("database")
            if db_path:
                return db_path
    except Exception:
        pass
    return _default_db_path()


def _apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Fire all four required pragmas on every fresh DBAPI connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_engine(path: str | None = None) -> Engine:
    """
    Create and return a SQLAlchemy :class:`~sqlalchemy.engine.Engine`.

    Parameters
    ----------
    path:
        SQLite database path.  Pass ``":memory:"`` for an in-memory database.
        When *None* the documented resolution chain is used.
    """
    resolved = _resolve_path(path)
    url = "sqlite:///:memory:" if resolved == ":memory:" else f"sqlite:///{resolved}"
    engine = _sa_create_engine(url)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def make_session_factory(engine: Engine) -> type[Session]:
    """Return a :class:`~sqlalchemy.orm.sessionmaker` bound to *engine*."""
    factory: type[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return factory
