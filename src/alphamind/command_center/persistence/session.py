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


_OWNED_TABLE_NAMES = frozenset(CommandCenterBase.metadata.tables)
"""Frozen set of owned table names — single source of truth for the
foreign-table-write guard. Derived from ``CommandCenterBase.metadata.tables``
so a future table added to the cc base appears here automatically.
"""


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
    """
    for instance in [*session.new, *session.dirty, *session.deleted]:
        # ``inspect`` returns an InstanceState; ``mapper.local_table`` is
        # the SQL table this row writes to.
        try:
            mapper = instance.__mapper__  # type: ignore[union-attr]
        except AttributeError:
            # Non-mapped object somehow ended up in the session — let
            # SQLAlchemy handle / raise downstream.
            continue
        table = mapper.local_table
        if table is None:
            continue
        if table.name not in _OWNED_TABLE_NAMES:
            allowed = sorted(_OWNED_TABLE_NAMES)
            msg = (
                f"cc_writer_session refuses to write foreign table "
                f"{table.name!r}; only {allowed} are writable through "
                f"this factory. Foreign-table state mutations must "
                f"route through the pipeline / monitor's loopback HTTP "
                f"surfaces — see docs/design/command-center.md "
                f"§ Interaction model."
            )
            raise ForeignTableWriteError(msg)


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
    url = (
        "sqlite+aiosqlite:///:memory:"
        if db_path == ":memory:"
        else f"sqlite+aiosqlite:///{db_path}"
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

    # ``aiosqlite`` accepts URI-form file paths via the ``uri=true``
    # query parameter; the read-only flag is the ``mode=ro`` parameter.
    # SQLAlchemy threads the query string through to the underlying
    # connect() call.
    query = urlencode({"mode": "ro", "uri": "true"})
    url = f"sqlite+aiosqlite:///file:{db_path}?{query}"
    engine = create_async_engine(url)
    return _make_factory(engine)


def _make_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Wrap an :class:`AsyncEngine` in an :class:`async_sessionmaker`."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)
