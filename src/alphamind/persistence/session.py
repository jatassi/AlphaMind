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

from sqlalchemy import Connection, Engine, event
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


def _substitute_percent_var(match: re.Match[str]) -> str:
    """Expand ``%VAR%`` against the environment, with ``%USERPROFILE%`` falling
    back to :func:`Path.home` on POSIX — matches the convention used in every
    other module (collector / scheduler / logging) and avoids writing a
    literal-named SQLite file in cwd when run on macOS / Linux.
    """
    name = match.group(1)
    value = os.environ.get(name)
    if value is not None:
        return value
    if name == "USERPROFILE":
        return str(Path.home())
    return match.group(0)


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
                # Any ``%VAR%`` still present after substitution raises so we
                # don't silently write a literal-named junk file in cwd.
                expanded = _PERCENT_VAR_PATTERN.sub(_substitute_percent_var, db_path)
                unresolved = _PERCENT_VAR_PATTERN.findall(expanded)
                if unresolved:
                    raise RuntimeError(
                        f"main.yaml paths.database resolved to {expanded!r} with "
                        f"unexpanded variables {unresolved!r}; set the "
                        f"corresponding environment variable or pass an explicit "
                        f"path / DATABASE_PATH override."
                    )
                return expanded
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


# ALP-824 — selective per-transaction BEGIN-mode control, ASYNC engine only.
# The scheduler's fill-collection write unit races the continuous monitor on one WAL DB;
# :func:`begin_write_immediate` opts that one transaction into ``BEGIN IMMEDIATE``
# so the write lock is taken up front and ``busy_timeout`` governs contention. To
# control the BEGIN statement, the async engine disables pysqlite's implicit BEGIN
# (``isolation_level = None``) and a ``begin`` hook emits ``BEGIN <mode>`` from the
# ``sqlite_begin_mode`` execution option (default ``DEFERRED``).
#
# These two hooks are registered ONLY on the async engine, never the sync one:
# ``begin_write_immediate`` is async-only (the async fill-collection unit is its sole
# caller), and the sync engine must keep pysqlite's lazy implicit BEGIN so that a
# ``PRAGMA foreign_keys=OFF`` issued inside an Alembic batch migration still runs
# in autocommit (SQLite silently *ignores* that pragma once a transaction is
# pending, which would re-enable FK enforcement mid-table-recreate).
_SQLITE_BEGIN_MODE_OPTION = "sqlite_begin_mode"
_DEFAULT_BEGIN_MODE = "DEFERRED"
_VALID_BEGIN_MODES = frozenset({"DEFERRED", "IMMEDIATE"})


def _apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Fire the four required pragmas on every fresh DBAPI connection (sync + async).

    Pragmas run in pysqlite's autocommit state (it does not implicitly begin a
    transaction on a ``PRAGMA``), which is where ``PRAGMA journal_mode=WAL`` must
    run — it cannot change the journal mode inside a transaction.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _disable_implicit_begin(dbapi_connection: Any, _connection_record: Any) -> None:
    """Hand BEGIN emission to SQLAlchemy so a transaction can opt into IMMEDIATE.

    Async-engine only (ALP-824). Setting ``isolation_level = None`` disables
    pysqlite's implicit, deferred ``BEGIN`` so the ``begin`` hook
    (:func:`_emit_begin`) can issue an explicit ``BEGIN <mode>``. Deliberately NOT
    applied to the sync engine — see the module-level note above on the
    ``PRAGMA foreign_keys=OFF`` migration interaction.
    """
    dbapi_connection.isolation_level = None


def _emit_begin(conn: Connection) -> None:
    """Emit an explicit ``BEGIN <mode>`` honoring the ``sqlite_begin_mode`` option.

    Fires on transaction start now that pysqlite's implicit ``BEGIN`` is disabled
    (:func:`_disable_implicit_begin`). The mode defaults to ``DEFERRED`` (a read
    snapshot taken on the first SELECT, upgraded on the first write — the prior
    locking behavior), so an unmarked async transaction keeps its deferred
    semantics. A caller opts one transaction into the up-front write lock by
    setting the ``sqlite_begin_mode="IMMEDIATE"`` execution option (ALP-824).
    """
    mode = conn.get_execution_options().get(_SQLITE_BEGIN_MODE_OPTION, _DEFAULT_BEGIN_MODE)
    if mode not in _VALID_BEGIN_MODES:
        msg = (
            f"invalid {_SQLITE_BEGIN_MODE_OPTION}={mode!r}; "
            f"expected one of {sorted(_VALID_BEGIN_MODES)}"
        )
        raise ValueError(msg)
    conn.exec_driver_sql(f"BEGIN {mode}")


def _register_immediate_begin_hooks(sync_engine: Engine) -> None:
    """Enable selective ``BEGIN IMMEDIATE`` on an engine (ALP-824, async only).

    For the async engine, pass its ``sync_engine``. Pairs the implicit-begin
    disable with the BEGIN-mode emitter so :func:`begin_write_immediate` can take
    the write lock up front. Never registered on the sync engine.
    """
    event.listen(sync_engine, "connect", _disable_implicit_begin)
    event.listen(sync_engine, "begin", _emit_begin)


async def begin_write_immediate(session: AsyncSession) -> None:
    """Open *session*'s transaction with ``BEGIN IMMEDIATE`` (write lock up front).

    Call before the first read or write of any write unit that shares the WAL
    database with another committer. ``BEGIN IMMEDIATE`` takes the SQLite write
    lock eagerly, so a concurrent write makes this transaction *wait* (governed
    by ``PRAGMA busy_timeout``) instead of leaving a deferred read snapshot that
    a later write-upgrade would race into an immediate ``SQLITE_BUSY_SNAPSHOT``.
    IMMEDIATE is opt-in per transaction: pure-read transactions stay DEFERRED
    (readers must never take the write lock), while every write transaction
    opts in — the scheduler/command-execution units directly (ALP-824) and the
    monitor's write transactions through
    :func:`alphamind.persistence.write_unit.run_immediate_write_unit` (ALP-942).

    Must run before any other statement on *session*: the execution option is
    consumed when the transaction begins, so a transaction already opened by a
    prior read/write would silently ignore it (re-exposing the
    ``SQLITE_BUSY_SNAPSHOT`` race). The guard below turns that silent footgun into
    a loud failure rather than a deferred transaction masquerading as IMMEDIATE.
    """
    if session.in_transaction():
        msg = (
            "begin_write_immediate must run before the session opens its "
            "transaction; one is already active, so BEGIN IMMEDIATE would be "
            "silently ignored. Call it as the first operation on a fresh session."
        )
        raise RuntimeError(msg)
    # Typed as a plain mapping so mypy resolves the ``Mapping[str, Any]`` overload
    # of ``execution_options`` rather than the options TypedDict (which rejects a
    # non-literal key — and ``sqlite_begin_mode`` is a custom key SQLAlchemy passes
    # through to the begin hook, not a declared option).
    exec_opts: dict[str, Any] = {_SQLITE_BEGIN_MODE_OPTION: "IMMEDIATE"}
    await session.connection(execution_options=exec_opts)


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
    # Sync engine keeps pysqlite's implicit BEGIN (no begin-mode hooks) — see the
    # ALP-824 module note: migrations rely on autocommit-timed PRAGMA foreign_keys.
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
    # The sync ``Engine`` underlying an ``AsyncEngine`` exposes the same ``connect``
    # / ``begin`` events. Pragmas fire on every fresh DBAPI connection (as on the
    # sync engine); the async engine additionally gets the BEGIN-mode hooks so the
    # fill-collection write unit can take the write lock up front via ``BEGIN IMMEDIATE``
    # (ALP-824).
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    _register_immediate_begin_hooks(engine.sync_engine)
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
    in fill collection / command execution, sync reads inside the analysis subtree's
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
