"""Tests for ``command_center.persistence.session`` (story 02 / ALP-666).

The dual-session-factory split (parent issue pre-resolved H):

* ``cc_writer_session_factory()`` — full read/write against
  ``alphamind.db`` for the three command-center-owned tables; structural
  enforcement of foreign-table-write-rejection via a dedicated
  :class:`~sqlalchemy.orm.DeclarativeBase` whose MetaData carries only
  the three owned tables.
* ``foreign_reader_session_factory()`` — read-only against the same DB
  (``?mode=ro`` URI parameter); SQLite-layer rejection of any DDL/DML.

Tests verify both halves of the split: the writer can write its own
tables, raises on a foreign mapper add; the reader can SELECT foreign
tables but raises on INSERT.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center.persistence.codecs import (
    AlertSeverity,
    AlertStatus,
)
from alphamind.command_center.persistence.session import (
    build_cc_writer_session_factory,
    build_foreign_reader_session_factory,
)
from alphamind.command_center.persistence.tables import (
    AlertRow,
    CommandCenterBase,
)
from alphamind.persistence.models import Base as ProductionBase


@pytest.fixture
async def populated_db(tmp_path: Path) -> AsyncIterator[Path]:
    """Per-test SQLite DB with the production + command-center schemas created.

    The test DB is on-disk (not ``:memory:``) so the ``?mode=ro`` reader
    factory can open it in read-only mode — ``:memory:`` does not support
    a read-only URI parameter.
    """
    db_path = tmp_path / "test.db"

    # Bootstrap the schema via a non-readonly async engine.
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(ProductionBase.metadata.create_all)
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    await engine.dispose()

    yield db_path


class TestWriterSessionFactory:
    async def test_can_insert_alert_row(self, populated_db: Path) -> None:
        factory = build_cc_writer_session_factory(str(populated_db))
        try:
            async with factory() as session:
                session.add(
                    AlertRow(
                        alert_id="alert-001",
                        rule_name="pipeline_aborted",
                        severity=AlertSeverity.CRITICAL.value,
                        status=AlertStatus.FIRING.value,
                        fired_at="2026-05-26T00:00:00Z",
                        acknowledged_at=None,
                        snoozed_until=None,
                        context_json="{}",
                    )
                )
                await session.commit()

            async with factory() as session:
                row = await session.get(AlertRow, "alert-001")
                assert row is not None
                assert row.rule_name == "pipeline_aborted"
        finally:
            await _dispose_factory(factory)

    async def test_metadata_carries_only_owned_tables(self, populated_db: Path) -> None:
        """The writer Base's MetaData lists only the three owned tables.

        Structural property: the writer engine's mapper registry only
        knows about the three command-center-owned tables. The runtime
        ``before_flush`` guard (tested separately) is the active
        rejection mechanism for ``session.add(InvocationRow(...))``-
        style attempts.
        """
        # Document the structural property regardless of any engine
        # construction.
        assert "invocations" not in CommandCenterBase.metadata.tables
        assert "alerts" in CommandCenterBase.metadata.tables
        assert "webauthn_credentials" in CommandCenterBase.metadata.tables
        assert "operator_sessions" in CommandCenterBase.metadata.tables

        factory = build_cc_writer_session_factory(str(populated_db))
        try:
            async with factory() as session:
                bind = session.get_bind()
                assert bind is not None
        finally:
            await _dispose_factory(factory)

    async def test_rejects_session_add_of_foreign_mapper(self, populated_db: Path) -> None:
        """Runtime enforcement: adding a foreign mapper raises before flush.

        SQLAlchemy resolves mappers from a global registry rather than
        from the engine's metadata, so the structural property alone
        does not block ``session.add(InvocationRow(...))``. The
        ``before_flush`` listener wired by ``build_cc_writer_session_factory``
        is the runtime guard — it inspects ``session.new`` /
        ``session.deleted`` / ``session.dirty`` and raises
        ``ForeignTableWriteError`` on the first foreign mapper found.
        """
        from alphamind.command_center.persistence.session import (
            ForeignTableWriteError,
        )
        from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

        factory = build_cc_writer_session_factory(str(populated_db))
        try:
            async with factory() as session:
                session.add(
                    ProcessLifetimeRow(
                        process_lifetime_id="plt-x",
                        process_role="pipeline",
                        process_start_at="2026-05-26T00:00:00Z",
                        process_pid=1,
                        hostname="h",
                        git_sha="sha",
                        git_branch="b",
                        git_dirty=0,
                        python_version="3.13",
                        pip_freeze_hash="0",
                        pip_freeze_snapshot_path="/p",
                        anthropic_sdk_version="na",
                        claude_agent_sdk_version="na",
                        os_release="os",
                    )
                )
                with pytest.raises(ForeignTableWriteError, match="process_lifetimes"):
                    await session.commit()
        finally:
            await _dispose_factory(factory)

    async def test_other_async_sessions_not_affected(self, populated_db: Path) -> None:
        """The foreign-table guard is scoped to the cc writer factory.

        The listener attaches to a per-factory sync session subclass, NOT
        to the global :class:`Session` class — so an unrelated
        ``AsyncSession`` built on a different engine writes whatever
        mappers it likes. This guards against the listener accidentally
        breaking legitimate writes elsewhere in the codebase.
        """
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker as plain_async_sessionmaker,
        )
        from sqlalchemy.ext.asyncio import (
            create_async_engine as plain_create_async_engine,
        )

        from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

        # Stand up our own engine + factory, NOT via build_cc_writer.
        plain_engine = plain_create_async_engine(f"sqlite+aiosqlite:///{populated_db}")
        plain_factory = plain_async_sessionmaker(bind=plain_engine, expire_on_commit=False)

        # Also build a cc writer factory to confirm both can coexist.
        cc_factory = build_cc_writer_session_factory(str(populated_db))
        try:
            async with plain_factory() as session:
                # No listener fires on this session — the plain factory's
                # sync session class is the unmodified base class.
                session.add(
                    ProcessLifetimeRow(
                        process_lifetime_id="plt-z",
                        process_role="pipeline",
                        process_start_at="2026-05-26T00:00:00Z",
                        process_pid=1,
                        hostname="h",
                        git_sha="sha",
                        git_branch="b",
                        git_dirty=0,
                        python_version="3.13",
                        pip_freeze_hash="0",
                        pip_freeze_snapshot_path="/p",
                        anthropic_sdk_version="na",
                        claude_agent_sdk_version="na",
                        os_release="os",
                    )
                )
                await session.commit()
        finally:
            await plain_engine.dispose()
            await _dispose_factory(cc_factory)


class TestReaderSessionFactory:
    async def test_can_select_foreign_tables(self, populated_db: Path) -> None:
        factory = build_foreign_reader_session_factory(str(populated_db))
        try:
            async with factory() as session:
                # The production schema's ``asset_universe`` table exists
                # in the DB; the reader factory can SELECT it (returns
                # empty result, but the query plans and executes).
                result = await session.execute(text("SELECT COUNT(*) FROM asset_universe"))
                assert result.scalar() == 0
        finally:
            await _dispose_factory(factory)

    async def test_can_select_command_center_tables(self, populated_db: Path) -> None:
        factory = build_foreign_reader_session_factory(str(populated_db))
        try:
            async with factory() as session:
                result = await session.execute(select(AlertRow))
                assert result.scalars().all() == []
        finally:
            await _dispose_factory(factory)

    async def test_cannot_insert_via_raw_sql(self, populated_db: Path) -> None:
        """SQLite-level read-only enforcement.

        ``?mode=ro`` opens the DB read-only; any DDL or DML raises
        ``OperationalError: attempt to write a readonly database``
        regardless of which table is touched.
        """
        factory = build_foreign_reader_session_factory(str(populated_db))
        try:
            async with factory() as session:
                with pytest.raises(OperationalError, match="readonly database"):
                    await session.execute(
                        text(
                            "INSERT INTO alerts "
                            "(alert_id, rule_name, severity, status, fired_at, context_json) "
                            "VALUES ('a', 'pipeline_aborted', 'critical', 'firing', "
                            "'2026-05-26T00:00:00Z', '{}')"
                        )
                    )
                    await session.commit()
        finally:
            await _dispose_factory(factory)

    async def test_cannot_insert_into_foreign_table_via_raw_sql(self, populated_db: Path) -> None:
        """Foreign-table writes also fail at the SQLite layer."""
        factory = build_foreign_reader_session_factory(str(populated_db))
        try:
            async with factory() as session:
                with pytest.raises(OperationalError, match="readonly database"):
                    await session.execute(
                        text(
                            "INSERT INTO process_lifetimes "
                            "(process_lifetime_id, process_role, process_start_at, "
                            "process_pid, hostname, git_sha, git_branch, git_dirty, "
                            "python_version, pip_freeze_hash, pip_freeze_snapshot_path, "
                            "anthropic_sdk_version, claude_agent_sdk_version, os_release) "
                            "VALUES ('plt-x', 'pipeline', '2026-05-26T00:00:00Z', 1, "
                            "'h', 'sha', 'b', 0, '3.13', '0', '/p', 'na', 'na', 'os')"
                        )
                    )
                    await session.commit()
        finally:
            await _dispose_factory(factory)


class TestEngineDisposal:
    async def test_factory_carries_its_engine(self, populated_db: Path) -> None:
        # Smoke: the factory's bound engine is accessible so callers can
        # dispose it when shutting down the daemon.
        factory = build_cc_writer_session_factory(str(populated_db))
        try:
            async with factory() as session:
                assert session.get_bind() is not None
        finally:
            await _dispose_factory(factory)


async def _dispose_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    """Dispose the underlying engine of a session factory.

    The session factory carries its engine via ``factory.kw['bind']``;
    callers shutting down the daemon need to release the engine pool.
    """
    engine = factory.kw["bind"]
    await engine.dispose()
