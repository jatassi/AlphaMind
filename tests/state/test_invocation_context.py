"""Tests for the transactional ``InvocationContext`` async context manager.

Story 02b: ``InvocationContext`` is the substrate the configuration loader
and the three blocked distillation stories opt into. On enter, it opens an
async transaction and INSERTs the supplied ``InvocationRecord``; downstream
stories' write paths (story 03 activity-log emission, stories 07-08
Phase 1/Phase 2 writes) join the same transaction via the handle's session.
On clean exit, the transaction commits; on exception, it rolls back and
re-raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.invocation_context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield an async engine + factory backed by a fresh on-disk SQLite DB.

    Tables are materialized once via the sync engine (the codebase's
    ``Base.metadata.create_all`` workflow), then reads/writes go through
    the async engine — same DB file, same pragmas via the same shared
    pragma listener.
    """
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    # Seed a parent process_lifetime row using the sync engine — InvocationContext
    # requires a parent FK row to exist before any invocation is inserted.
    sync_engine = make_engine(str(db_path))
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


def _make_invocation_record(
    invocation_id: str = "inv-2026-05-07T14:30:00Z-abcd",
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at="2026-05-07T14:30:00Z",
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json='["pre-event"]',
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/resolved_config.json"
        ),
        feature_flags_snapshot_json='{"foo": true}',
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration_state.json"
        ),
        data_source_freshness_json='{"polygon": "2026-05-07T14:00:00Z"}',
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


class TestInvocationContext:
    async def test_clean_exit_commits_invocation_row(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        record = _make_invocation_record()

        ctx = InvocationContext(session_factory=factory, record=record)
        async with ctx as handle:
            assert isinstance(handle, InvocationHandle)
            assert handle.invocation_id == record.invocation_id
            assert isinstance(handle.session, AsyncSession)

        # Verify the row landed.
        async with factory() as sess:
            result = await sess.execute(
                select(InvocationRow).where(InvocationRow.invocation_id == record.invocation_id)
            )
            persisted = result.scalar_one_or_none()
            assert persisted is not None
            assert persisted.trigger_type == "scheduled"
            assert persisted.process_lifetime_id == "proc-1"

    async def test_exception_inside_context_leaves_row_committed(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Under the three-tx model the row commits up-front; phase aborts don't undo it.

        The pre-ALP-449 InvocationContext rolled the row back on any
        exception. Per ``docs/design/05-execution-layer/state-persistence.md``
        § Snapshot isolation the row commit precedes phase work, so an
        exception inside the ``async with`` body rolls back only the
        phase's writes — the invocation row stays for the next invocation
        to see (and the SQL repository's ``fill_collection_completed_at IS NULL``
        guard refuses snapshot reads against it).
        """
        _, factory = async_engine_and_factory
        record = _make_invocation_record(invocation_id="inv-rollback-1")

        class _BoomError(RuntimeError):
            pass

        ctx = InvocationContext(session_factory=factory, record=record)
        with pytest.raises(_BoomError):
            async with ctx:
                raise _BoomError("simulated downstream failure")

        # The row stays — its commit happened before the phase opened.
        async with factory() as sess:
            result = await sess.execute(
                select(InvocationRow).where(InvocationRow.invocation_id == "inv-rollback-1")
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.fill_collection_completed_at is None
            assert row.command_execution_completed_at is None

    async def test_context_handle_session_can_join_same_transaction(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """The handle's session must let downstream callers join the same
        transaction — emulates story 03's activity-log write joining the
        InvocationContext-opened transaction.
        """
        _, factory = async_engine_and_factory
        record = _make_invocation_record(invocation_id="inv-joined-1")

        async with InvocationContext(session_factory=factory, record=record) as handle:
            # A second write on the same session — modelled after story 03's
            # activity-log emission. Use a raw INSERT so we don't depend on a
            # later story's table definition.
            await handle.session.execute(
                text("UPDATE invocations SET fill_collection_completed_at = :ts WHERE invocation_id = :iid"),
                {"ts": "2026-05-07T14:31:00Z", "iid": record.invocation_id},
            )

        async with factory() as sess:
            result = await sess.execute(
                select(InvocationRow).where(InvocationRow.invocation_id == record.invocation_id)
            )
            persisted = result.scalar_one_or_none()
            assert persisted is not None
            assert persisted.fill_collection_completed_at == "2026-05-07T14:31:00Z"

    async def test_fk_violation_raises_and_no_row_lands(
        self,
        tmp_path: Path,
    ) -> None:
        """FK violation on row insert raises before the phase opens; no orphan.

        Per ALP-356 acceptance criterion: the row-insert short transaction
        (``insert_invocation_row``) raises ``IntegrityError`` when the FK
        to ``process_lifetimes`` does not resolve. Under the three-tx
        model this happens *before* the phase session opens, so no orphan
        row lands in the DB.
        """
        # Build a fresh DB without the parent process_lifetime row.
        db_path = tmp_path / "alphamind.db"

        import alphamind.state.tables  # noqa: F401

        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            record = _make_invocation_record(invocation_id="inv-orphan-1")
            with pytest.raises(IntegrityError):
                async with InvocationContext(session_factory=factory, record=record):
                    pass

            async with factory() as sess:
                result = await sess.execute(
                    select(InvocationRow).where(InvocationRow.invocation_id == "inv-orphan-1")
                )
                assert result.scalar_one_or_none() is None
        finally:
            await async_engine.dispose()
