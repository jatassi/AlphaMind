"""Tests for ``command_center._kernel.operator_invocation`` (story 02 / ALP-666).

Verifies the ``operator_invocation`` async context manager:

* Opens one row in ``invocations`` with ``trigger_source='operator_console'``
  and ``trigger_reason`` carrying the verb name + operator session ID.
* Yields a ``InvocationHandle`` whose ``invocation_id`` matches the
  committed row.
* The row is durable (visible from a fresh session) on exit; the handle
  is closed cleanly.

The AC's ``run_type=operator_console`` requirement is realized via
``trigger_source='operator_console'`` on the existing ``invocations``
schema — there is no separate ``run_type`` column today, and the
``trigger_source`` field is the schema's contract for "where did this
trigger come from" (existing values include ``cli`` and ``debug_e2e_cli``).
The verb name + operator session ID land in ``trigger_reason`` as a
structured ``key=value`` string so the activity-log explorer can filter
operator actions by them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center._kernel.control import ControlVerb
from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center._kernel.operator_invocation import (
    RUN_TYPE_OPERATOR_CONSOLE,
    operator_invocation,
)
from alphamind.persistence.models import Base
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test in-memory async SQLite engine with the state tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    # Insert a process-lifetimes row so the invocation row's FK resolves.
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id="plt-command_center-test",
                process_role="monitor",  # closest existing role; tests don't care
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=42,
                hostname="test-host",
                git_sha="deadbeef",
                git_branch="test",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="not-installed",
                claude_agent_sdk_version="not-installed",
                os_release="test-os",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


class TestOperatorInvocation:
    async def test_yields_a_handle_with_invocation_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with operator_invocation(
            session_factory=session_factory,
            process_lifetime_id="plt-command_center-test",
            operator_session_id_=operator_session_id("session-abc"),
            verb=ControlVerb.PAUSE,
        ) as handle:
            assert isinstance(handle, InvocationHandle)
            assert handle.invocation_id.startswith("inv-")

    async def test_persists_row_with_operator_console_trigger_source(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with operator_invocation(
            session_factory=session_factory,
            process_lifetime_id="plt-command_center-test",
            operator_session_id_=operator_session_id("session-abc"),
            verb=ControlVerb.PAUSE,
        ) as handle:
            invocation_id = handle.invocation_id

        # Re-read the row from a fresh session — confirms the commit ran in
        # its own short transaction (mirroring `insert_invocation_row`).
        async with session_factory() as session:
            row = await session.get(InvocationRow, invocation_id)
            assert row is not None
            assert row.trigger_source == RUN_TYPE_OPERATOR_CONSOLE
            assert row.trigger_type == "manual"

    async def test_trigger_reason_carries_verb_and_session_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with operator_invocation(
            session_factory=session_factory,
            process_lifetime_id="plt-command_center-test",
            operator_session_id_=operator_session_id("session-abc"),
            verb=ControlVerb.SWITCH_PROFILE,
        ) as handle:
            invocation_id = handle.invocation_id

        async with session_factory() as session:
            row = await session.get(InvocationRow, invocation_id)
            assert row is not None
            # The verb name and the operator session ID must be present in
            # trigger_reason verbatim so the activity-log explorer can grep
            # them out for the "operator actions" saved filter view.
            assert "switch_profile" in row.trigger_reason
            assert "session-abc" in row.trigger_reason

    async def test_session_closed_after_exit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with operator_invocation(
            session_factory=session_factory,
            process_lifetime_id="plt-command_center-test",
            operator_session_id_=operator_session_id("session-abc"),
            verb=ControlVerb.PAUSE,
        ) as handle:
            inner_session = handle.session
            assert inner_session.is_active

        # SQLAlchemy `AsyncSession.close()` puts the session in a non-active
        # state; subsequent operations would raise. Use the new_transaction
        # method or in_transaction property to inspect.
        assert not inner_session.in_transaction()

    async def test_exception_rolls_back_phase_but_invocation_row_survives(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        with pytest.raises(RuntimeError, match="caller raised"):
            async with operator_invocation(
                session_factory=session_factory,
                process_lifetime_id="plt-command_center-test",
                operator_session_id_=operator_session_id("session-abc"),
                verb=ControlVerb.PAUSE,
            ) as handle:
                invocation_id = handle.invocation_id
                raise RuntimeError("caller raised")

        # The invocations row was committed before the body ran (three-tx
        # model). Caller exceptions roll back only the phase session — the
        # row stays.
        async with session_factory() as session:
            row = await session.get(InvocationRow, invocation_id)
            assert row is not None

    async def test_independent_invocations_get_distinct_ids(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = set()
        for _ in range(3):
            async with operator_invocation(
                session_factory=session_factory,
                process_lifetime_id="plt-command_center-test",
                operator_session_id_=operator_session_id("session-abc"),
                verb=ControlVerb.PAUSE,
            ) as handle:
                ids.add(handle.invocation_id)

        # The id-mint helper uses a timestamp prefix + secrets token; three
        # back-to-back calls all yield distinct values.
        assert len(ids) == 3

    async def test_each_invocation_id_is_recorded_with_operator_session_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with operator_invocation(
            session_factory=session_factory,
            process_lifetime_id="plt-command_center-test",
            operator_session_id_=operator_session_id("session-xyz"),
            verb=ControlVerb.CANCEL_ORDER,
        ) as handle:
            invocation_id = handle.invocation_id

        async with session_factory() as session:
            result = await session.execute(
                select(InvocationRow).where(InvocationRow.invocation_id == invocation_id)
            )
            row = result.scalar_one()
            assert "session-xyz" in row.trigger_reason
            assert "cancel_order" in row.trigger_reason


class TestRunTypeOperatorConsoleConstant:
    def test_constant_value_matches_documented_string(self) -> None:
        # AC: "open a row in invocations with run_type=operator_console".
        # We carry this literal on `trigger_source` (the existing schema's
        # "where did this trigger come from" field) — the constant pins
        # the value the activity-log explorer's "Operator actions" saved
        # filter view queries against.
        assert RUN_TYPE_OPERATOR_CONSOLE == "operator_console"
