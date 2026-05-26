"""Dual session factories for the command center (story 02 / ALP-666).

The dual-session split (parent issue pre-resolved H) keeps the command
center's writes scoped to the three command-center-owned tables and its
reads of foreign tables provably free of writes:

* :func:`build_cc_writer_session_factory` — writes scoped to the three
  command-center-owned tables. Two layers of enforcement:

  1. *Structural.* The writer engine and its sessions are wired against
     ``CommandCenterBase``'s :class:`~sqlalchemy.MetaData`, which carries
     only the three owned tables. Reflected via
     :attr:`CommandCenterBase.metadata.tables`.
  2. *Runtime.* A ``before_flush`` event listener inspects every pending
     ``add`` and ``deletion`` on the writer's sessions and raises
     :class:`ForeignTableWriteError` if any of them target a table whose
     name is NOT in the owned-table allow-list. This catches callers who
     constructed a foreign mapper (e.g. ``InvocationRow``) and called
     ``session.add(...)`` against the writer session — the structural
     property alone does not block it, because SQLAlchemy resolves
     mappers from a global registry, not from the engine's metadata.
  3. *Runtime, defense-in-depth.* A ``do_orm_execute`` listener catches
     Core-level INSERT / UPDATE / DELETE statements (including the bulk
     ``session.execute(insert(ForeignTable), [...])`` shape) that
     bypass the ``before_flush`` ORM path. The session's
     :meth:`~sqlalchemy.orm.Session.bulk_insert_mappings` /
     :meth:`~sqlalchemy.orm.Session.bulk_update_mappings` /
     :meth:`~sqlalchemy.orm.Session.bulk_save_objects` family bypass
     ``before_flush`` and the typed-ORM unit-of-work entirely; the
     ``do_orm_execute`` listener defends against those callers landing
     on this factory (F8). Direct Core SQL (``session.execute(text(...))``)
     against foreign tables is still not blocked at the application
     layer — that path is acceptable for SELECTs (the writer can read
     anything) and only meaningful for writes if the caller has
     explicitly bypassed both the ORM AND the typed bindings, which
     no AlphaMind code does.

* :func:`build_foreign_reader_session_factory` — read-only against the
  same DB via the ``?mode=ro`` URI parameter. SQLite rejects any DDL/DML
  with ``OperationalError: attempt to write a readonly database``
  regardless of which table is touched. The reader can SELECT against
  any table whose schema is created in the DB (reflected on demand).

Both factories return :class:`~sqlalchemy.ext.asyncio.async_sessionmaker`
instances bound to fresh engines; callers shutting down the daemon
release the engines via ``engine.dispose()`` (the factory's
``.kw['bind']`` exposes the bound engine).

The two factories are *separate engines* against the same DB file — they
do NOT share a connection pool. SQLite handles the concurrency: the
writer uses ``journal_mode=WAL`` (per the production pragma hook so the
underlying readers, including the OMS, see consistent snapshots);
``foreign_reader`` opens the DB with ``mode=ro`` so its connections
hold only shared locks. Concurrent reads + the command center's
narrow writes coexist without contention.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from alphamind.command_center.persistence.tables import CommandCenterBase

__all__ = [
    "ForeignTableWriteError",
    "build_cc_writer_session_factory",
    "build_foreign_reader_session_factory",
]


_PERCENT_VAR_PATTERN = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def _substitute_percent_var(match: re.Match[str]) -> str:
    """Expand ``%VAR%`` against the environment; ``%USERPROFILE%`` falls
    back to :func:`Path.home` on POSIX.

    Mirrors :func:`alphamind.persistence.session._substitute_percent_var`
    so cc YAML values that use ``%USERPROFILE%`` resolve identically on
    Windows production and macOS / Linux test machines.
    """
    name = match.group(1)
    value = os.environ.get(name)
    if value is not None:
        return value
    if name == "USERPROFILE":
        return str(Path.home())
    return match.group(0)


def _resolve_db_path(db_path: str) -> str:
    """Expand ``%VAR%`` placeholders (and tilde) in *db_path*.

    The cc YAML carries paths such as ``%USERPROFILE%/AlphaMind/data/alphamind.db``
    (verbatim from production); on macOS / Linux that expands to
    ``~/AlphaMind/data/alphamind.db`` via the ``USERPROFILE`` fallback.
    Raises :class:`RuntimeError` if a placeholder remains after substitution
    so a typo'd variable name fails loud instead of writing a literal
    ``%TYPO%/...`` file in cwd.
    """
    if db_path == ":memory:":
        return db_path
    expanded = _PERCENT_VAR_PATTERN.sub(_substitute_percent_var, db_path)
    unresolved = _PERCENT_VAR_PATTERN.findall(expanded)
    if unresolved:
        msg = (
            f"command_center db_path resolved to {expanded!r} with unexpanded "
            f"variables {unresolved!r}; set the corresponding environment "
            f"variable or fix the YAML."
        )
        raise RuntimeError(msg)
    # ``~`` expansion mirrors ``Path.expanduser`` so a tilde-form path
    # from a hand-edited YAML resolves identically to the ``%USERPROFILE%``
    # form.
    return str(Path(expanded).expanduser())


class ForeignTableWriteError(RuntimeError):
    """Raised when a writer session is asked to mutate a foreign table.

    The command-center writer is scoped to the three owned tables only
    (``alerts``, ``webauthn_credentials``, ``operator_sessions``); foreign
    state mutations route through the pipeline / monitor's loopback
    HTTP surfaces, not through the command center's own writer.
    """


def _apply_writer_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Apply the standard four pragmas to every fresh writer connection.

    Mirrors :func:`alphamind.persistence.session._apply_pragmas` so the
    command center writer shares the production pragma posture: WAL
    journal, 60s busy_timeout, FK enforcement, NORMAL synchronous.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _owned_table_names() -> frozenset[str]:
    """Return the current set of cc-owned table names.

    Evaluated lazily at listener-invocation time rather than at module
    import (F13): the listener fires on flush, which happens long after
    SQLAlchemy mapper-registry initialization completes; reading the
    metadata snapshot at module-import time creates a load-order
    dependency where any future cc table declared in a module imported
    after this one would be silently omitted from the allow-list.

    Cheap to call repeatedly — :attr:`MetaData.tables` is a plain dict
    on a long-lived module-global, and :class:`frozenset` construction
    on the four-element set takes microseconds.
    """
    names = frozenset(CommandCenterBase.metadata.tables)
    if not names:
        # If this fires the cc tables haven't been registered yet —
        # almost certainly a circular-import bug. Fail loud so the cause
        # is obvious rather than silently allowing every foreign write.
        msg = (
            "_owned_table_names is empty — CommandCenterBase.metadata "
            "has no tables registered. Check the import order: the cc "
            "tables module must be importable before any session "
            "flushes against the cc writer factory."
        )
        raise RuntimeError(msg)
    return names


def _reject_foreign_table_writes(
    session: Session,
    _flush_context: Any,
    _instances: Any,
) -> None:
    """``before_flush`` listener: reject mutations of foreign tables.

    Inspects ``session.new`` / ``session.deleted`` / ``session.dirty``;
    for each pending instance, checks whether the mapped table's name is
    in :data:`_OWNED_TABLE_NAMES`. If not, raises
    :class:`ForeignTableWriteError`.

    The check uses the mapper's local table (``mapper.local_table``)
    rather than ``__table__`` so inheritance hierarchies resolve to the
    correct table name; for the command-center owned tables and the
    foreign tables alike, both forms agree because every mapper is
    declarative-flat (no joined-table inheritance).

    Bulk operations (``Session.bulk_insert_mappings`` /
    ``bulk_update_mappings`` / ``bulk_save_objects``) bypass the ORM
    unit-of-work and therefore bypass this listener — see
    :func:`_reject_foreign_table_core_writes` for the defense-in-depth
    listener that catches those (F8).
    """
    owned = _owned_table_names()
    for instance in [*session.new, *session.dirty, *session.deleted]:
        # Each instance is an ORM-mapped row class; ``__mapper__`` is the
        # SQLAlchemy mapper; ``mapper.local_table`` is the SQL table this
        # row writes to. Untyped attribute access via ``getattr`` so mypy
        # doesn't fall back to ``Any`` on the dynamic ORM surface.
        mapper = getattr(instance, "__mapper__", None)
        if mapper is None:
            continue
        table = mapper.local_table
        if table is None:
            continue
        if table.name not in owned:
            allowed = sorted(owned)
            msg = (
                f"cc_writer_session refuses to write foreign table "
                f"{table.name!r}; only {allowed} are writable through "
                f"this factory. Foreign-table state mutations must "
                f"route through the pipeline / monitor's loopback HTTP "
                f"surfaces — see docs/design/command-center.md "
                f"§ Interaction model."
            )
            raise ForeignTableWriteError(msg)


def _reject_foreign_table_core_writes(orm_execute_state: Any) -> None:
    """``do_orm_execute`` listener: catch bulk + Core INSERT/UPDATE/DELETE
    to foreign tables (F8 defense-in-depth).

    ``Session.bulk_insert_mappings`` and friends bypass the unit-of-work
    and so bypass :func:`_reject_foreign_table_writes`. So does direct
    ``session.execute(insert(ForeignTable), [...])`` — the Core-level
    statement path. This listener fires on every ORM-executed statement
    and rejects writes whose target table is not owned by the cc.

    Direct ``session.execute(text("INSERT INTO foreign ..."))`` (raw SQL
    via ``text()``) is NOT caught by this listener — that route bypasses
    both the ORM unit-of-work and the typed-execute hook entirely. It
    is acceptable for the writer factory because no AlphaMind callsite
    issues raw-SQL writes via the cc writer; if a future callsite does,
    it should use a different (production) session factory.
    """
    statement = orm_execute_state.statement
    is_dml = getattr(statement, "is_dml", False)
    if not is_dml:
        # SELECT / other read shapes — no enforcement needed; reads are
        # already permitted on the cc writer factory.
        return
    # The target table for a Core insert/update/delete is exposed on
    # ``.table``; for ORM bulk-execute it threads through the same
    # attribute. Walk every targeted table — a JOIN-form UPDATE / DELETE
    # could touch multiple.
    owned = _owned_table_names()
    tables = _walk_dml_tables(statement)
    for table in tables:
        if table.name not in owned:
            allowed = sorted(owned)
            msg = (
                f"cc_writer_session refuses to write foreign table "
                f"{table.name!r} via Core/bulk path; only {allowed} are "
                f"writable through this factory. Foreign-table state "
                f"mutations must route through the pipeline / monitor's "
                f"loopback HTTP surfaces — see "
                f"docs/design/command-center.md § Interaction model."
            )
            raise ForeignTableWriteError(msg)


def _walk_dml_tables(statement: Any) -> list[Any]:
    """Return every table referenced by an INSERT / UPDATE / DELETE.

    Most DML statements expose a single target via ``.table``; some
    UPDATE / DELETE forms can target multiple. The helper coerces both
    shapes to a list so the caller iterates uniformly.
    """
    tables: list[Any] = []
    target = getattr(statement, "table", None)
    if target is not None:
        tables.append(target)
    # ``.entity_description`` / ``.target`` are alternative spellings on
    # certain ORM-bulk and bulk-insert constructions; check both before
    # giving up so we don't accept an unguarded write by accident.
    for attr in ("target", "entity_description"):
        extra = getattr(statement, attr, None)
        if extra is None:
            continue
        if isinstance(extra, dict):
            mapper = extra.get("mapper")
            if mapper is not None:
                local = getattr(mapper, "local_table", None)
                if local is not None and local not in tables:
                    tables.append(local)
        elif extra not in tables:
            tables.append(extra)
    return tables


def build_cc_writer_session_factory(
    db_path: str,
) -> async_sessionmaker[AsyncSession]:
    """Return an async-session factory bound to a writer engine.

    The writer engine binds to ``CommandCenterBase``'s :class:`MetaData`
    (carried implicitly through the ORM mapper registry). Callers that
    construct rows of the three owned tables and ``session.add(...)``
    them flush correctly; callers that attempt to operate on a foreign
    mapper find no mapping on this engine.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database (``alphamind.db`` in
        production). Pass ``":memory:"`` for an in-memory database (the
        same SQLite connection is shared across all sessions on the
        same engine, so the schema survives session boundaries; intended
        for tests).
    """
    resolved = _resolve_db_path(db_path)
    url = (
        "sqlite+aiosqlite:///:memory:"
        if resolved == ":memory:"
        else f"sqlite+aiosqlite:///{resolved}"
    )
    engine = create_async_engine(url)
    # The sync ``Engine`` underlying an ``AsyncEngine`` exposes the
    # ``connect`` event the sync pragma helper hooks; pragmas fire on
    # every fresh DBAPI connection regardless of sync / async usage.
    event.listen(engine.sync_engine, "connect", _apply_writer_pragmas)

    # Subclass the synchronous ``Session`` so the foreign-table-write
    # guard listens only on sessions this factory mints — NOT on every
    # ``Session`` in the process (which would block legitimate writes
    # elsewhere). ``async_sessionmaker(sync_session_class=...)`` threads
    # the sync subclass through; ``AsyncSession`` routes flushes through
    # its bound sync session, so the listener fires on every flush this
    # factory's sessions perform.
    class _CommandCenterWriterSync(Session):
        """Sync Session subclass scoped to the cc writer factory."""

    event.listen(_CommandCenterWriterSync, "before_flush", _reject_foreign_table_writes)
    # Defense-in-depth: bulk_insert_mappings / bulk_save_objects /
    # session.execute(insert(ForeignTable), [...]) bypass before_flush.
    # do_orm_execute fires on every ORM-routed statement and lets us
    # reject DML against foreign tables before SQLAlchemy renders the
    # SQL (F8).
    event.listen(_CommandCenterWriterSync, "do_orm_execute", _reject_foreign_table_core_writes)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine,
        sync_session_class=_CommandCenterWriterSync,
        expire_on_commit=False,
    )
    return factory


def build_foreign_reader_session_factory(
    db_path: str,
) -> async_sessionmaker[AsyncSession]:
    """Return an async-session factory bound to a read-only engine.

    ``?mode=ro`` opens the DB read-only at the SQLite layer; any DDL or
    DML raises ``OperationalError`` regardless of which table is touched.
    The reader can SELECT against any table whose schema is created in
    the DB.

    ``:memory:`` is not supported for the reader factory because SQLite
    rejects ``?mode=ro`` on in-memory databases; tests requiring a
    reader factory must use an on-disk DB.
    """
    if db_path == ":memory:":
        msg = (
            "foreign_reader_session_factory does not support :memory: — "
            "SQLite rejects ?mode=ro on in-memory databases. Use an "
            "on-disk DB path for tests."
        )
        raise ValueError(msg)

    resolved = _resolve_db_path(db_path)
    # ``aiosqlite`` accepts URI-form file paths via the ``uri=true``
    # query parameter; the read-only flag is the ``mode=ro`` parameter.
    # SQLAlchemy threads the query string through to the underlying
    # connect() call. Backslashes in a Windows path
    # (``C:\Users\jacks\...\alphamind.db``) break the URI parse — SQLite's
    # URI form treats backslash as the escape character and the path
    # component is mangled. Normalize to forward slashes so the resolved
    # path survives the URI round-trip on both POSIX and Windows (F11).
    uri_path = resolved.replace("\\", "/")
    query = urlencode({"mode": "ro", "uri": "true"})
    url = f"sqlite+aiosqlite:///file:{uri_path}?{query}"
    engine = create_async_engine(url)
    return _make_factory(engine)


def _make_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Wrap an :class:`AsyncEngine` in an :class:`async_sessionmaker`."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)
