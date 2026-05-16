"""
Session factory for AlphaMind's SQLite database.

Database path resolution order:
  1. Explicit ``path`` argument passed to :func:`make_engine`
  2. ``DATABASE_PATH`` environment variable
  3. ``main.yaml`` ``paths.database`` key

Raises :class:`RuntimeError` when none of the above resolves a path; a silent
default would let SQLAlchemy create an empty SQLite file and downstream
verifiers would mis-report "tables MISSING" instead of "no DB configured".

Pragmas applied on every new connection (from data-and-state.md):
  - ``journal_mode = WAL``
  - ``busy_timeout = 60000``
  - ``foreign_keys = ON``
  - ``synchronous = NORMAL``
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

_PERCENT_VAR_PATTERN = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def _resolve_path(path: str | None) -> str:
    """Resolve the database path using the documented priority chain."""
    if path is not None:
        return path
    env_path = os.environ.get("DATABASE_PATH")
    if env_path:
        return env_path
    try:
        import yaml

        config_file = Path(__file__).parents[3] / "config" / "main.yaml"
        if config_file.exists():
            with config_file.open(encoding="utf-8") as fh:
                cfg: dict[str, Any] = yaml.safe_load(fh) or {}
            db_path: str | None = cfg.get("paths", {}).get("database")
            if db_path:
                # ``os.path.expandvars`` only honors ``%VAR%`` on Windows; we
                # substitute manually so a YAML value like ``%USERPROFILE%/...``
                # expands identically on POSIX (test parity, replay harness).
                return _PERCENT_VAR_PATTERN.sub(
                    lambda m: os.environ.get(m.group(1), m.group(0)), db_path
                )
    except (yaml.YAMLError, OSError, ImportError):
        # Schema bugs in ``main.yaml`` (or yaml unavailable) shouldn't masquerade
        # as "DB not configured" — but we still fall through to the RuntimeError
        # below so the caller sees an actionable message. ``TypeError`` /
        # ``AttributeError`` (e.g., cfg returning the wrong shape) surface
        # naturally so a config-schema bug is not hidden as connectivity loss.
        pass
    raise RuntimeError(
        "AlphaMind database path is not configured. "
        "Pass an explicit path, set the DATABASE_PATH environment variable, "
        "or populate paths.database in config/main.yaml."
    )


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


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a :class:`~sqlalchemy.orm.sessionmaker` bound to *engine*."""
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return factory


def make_async_engine(path: str | None = None) -> AsyncEngine:
    """
    Create and return a SQLAlchemy :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.

    Mirrors :func:`make_engine` but uses the ``sqlite+aiosqlite`` driver and
    applies the same four pragmas on every fresh connection. Used by the
    state-persistence ``InvocationContext`` (story 02b) and downstream
    write paths that opt into the async session.
    """
    resolved = _resolve_path(path)
    url = (
        "sqlite+aiosqlite:///:memory:"
        if resolved == ":memory:"
        else f"sqlite+aiosqlite:///{resolved}"
    )
    engine = _sa_create_async_engine(url)
    # The sync ``Engine`` underlying an ``AsyncEngine`` exposes the same
    # ``connect`` event the sync helper hooks; pragmas fire on every fresh
    # DBAPI connection regardless of whether the caller is sync or async.
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def make_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an :class:`~sqlalchemy.ext.asyncio.async_sessionmaker` bound to *engine*."""
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    return factory


@dataclass(frozen=True, slots=True)
class EnginePair:
    """Paired sync + async engines and session factories bound to one DB path.

    The pipeline scheduler and verify script both need the pair: async writes
    in Phase 1 / Phase 2, sync reads inside the analysis subtree's
    ``asyncio.to_thread`` callsites. :func:`engine_pair_context` builds the
    pair as a unit and disposes both on exit.
    """

    async_engine: AsyncEngine
    async_session_factory: async_sessionmaker[AsyncSession]
    sync_engine: Engine
    sync_session_factory: sessionmaker[Session]


@contextlib.asynccontextmanager
async def engine_pair_context(path: str | None = None) -> AsyncIterator[EnginePair]:
    """Yield a paired sync/async :class:`EnginePair`; dispose both on exit.

    Both engines resolve through the same documented path chain (explicit
    argument → ``DATABASE_PATH`` env → ``main.yaml``) so the pair binds to one
    SQLite file. The sync engine is disposed before awaiting async dispose
    because sync dispose is non-awaitable; both run in the ``finally`` block
    so a partial-init failure still releases acquired connections.
    """
    async_engine = make_async_engine(path)
    sync_engine = make_engine(path)
    try:
        yield EnginePair(
            async_engine=async_engine,
            async_session_factory=make_async_session_factory(async_engine),
            sync_engine=sync_engine,
            sync_session_factory=make_session_factory(sync_engine),
        )
    finally:
        sync_engine.dispose()
        await async_engine.dispose()
