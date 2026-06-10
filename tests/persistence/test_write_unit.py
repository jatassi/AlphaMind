"""``run_immediate_write_unit`` — fresh-session IMMEDIATE write units (ALP-942).

The monitor's read-then-write transactions ran ``BEGIN DEFERRED`` (the async
engine default), so the first SELECT pinned a WAL read snapshot and the later
write-upgrade collided with any concurrent committer as an immediate
``SQLITE_BUSY_SNAPSHOT`` — a class ``busy_timeout`` structurally cannot cover.
:func:`run_immediate_write_unit` composes the two ALP-824 primitives
(``begin_write_immediate`` + ``run_with_sqlite_busy_retry``) so every attempt
takes the write lock up front on a fresh session.

The DB here is real (one of the four sanctioned boundaries): a temp-file WAL
database, because in-memory SQLite cannot reproduce cross-connection snapshot
upgrades.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.persistence.write_unit import run_immediate_write_unit
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.monitor_halt_mode import (
    MONITOR_HALT_MODE_SINGLETON_ID,
    MonitorHaltModeRow,
)


@pytest.fixture()
async def db_path(tmp_path: Path) -> str:
    """Temp-file WAL DB with the full ORM schema (no in-memory: snapshot
    semantics need real cross-connection WAL)."""
    import alphamind.state.tables  # noqa: F401  — register every FK table

    path = str(tmp_path / "write_unit.db")
    engine = make_async_engine(path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return path


def _halt_row(*, enabled: int = 1, reason: str = "test") -> MonitorHaltModeRow:
    return MonitorHaltModeRow(
        id=MONITOR_HALT_MODE_SINGLETON_ID,
        enabled=enabled,
        reason=reason,
        applied_at=None,
    )


async def _read_halt_reason(factory: async_sessionmaker[AsyncSession]) -> str | None:
    async with factory() as session:
        row = (await session.execute(select(MonitorHaltModeRow))).scalar_one_or_none()
        return None if row is None else row.reason


def _transient_lock_error() -> OperationalError:
    return OperationalError(
        "INSERT INTO t", {}, sqlite3.OperationalError("database is locked")
    )


def _event_row(event_key: str) -> BrokerEventLogRow:
    """Minimal valid append-only event row (the link FKs are nullable)."""
    return BrokerEventLogRow(
        event_key=event_key,
        event_type="FILL",
        thesis_id=None,
        invocation_id=None,
        position_id=None,
        raw_payload_json="{}",
        broker_timestamp=None,
        captured_at="2026-06-09T17:00:05.911Z",
    )


async def test_commits_unit_writes_and_returns_unit_result(db_path: str) -> None:
    """The helper opens a session, runs the unit under BEGIN IMMEDIATE, commits,
    and returns the unit's value — the write is durable for a later session."""
    engine = make_async_engine(db_path)
    factory = make_async_session_factory(engine)

    async def unit(session: AsyncSession) -> str:
        assert session.in_transaction()  # IMMEDIATE transaction already open
        conn = await session.connection()
        sync_conn = conn.sync_connection
        assert sync_conn is not None
        assert sync_conn.get_execution_options().get("sqlite_begin_mode") == "IMMEDIATE"
        session.add(_halt_row(reason="written-by-unit"))
        return "unit-result"

    try:
        result = await run_immediate_write_unit(factory, unit)
        assert result == "unit-result"
        assert await _read_halt_reason(factory) == "written-by-unit"
    finally:
        await engine.dispose()


async def test_transient_lock_error_retries_on_a_fresh_session(db_path: str) -> None:
    """A transient lock error re-runs the whole unit on a NEW session; the
    eventual success commits exactly one durable copy of the write."""
    engine = make_async_engine(db_path)
    factory = make_async_session_factory(engine)
    seen_sessions: list[AsyncSession] = []

    async def unit(session: AsyncSession) -> None:
        seen_sessions.append(session)
        if len(seen_sessions) < 3:
            raise _transient_lock_error()
        session.add(_halt_row(reason="retried"))

    try:
        await run_immediate_write_unit(factory, unit)
        assert len(seen_sessions) == 3
        assert len(set(map(id, seen_sessions))) == 3  # fresh session per attempt
        assert await _read_halt_reason(factory) == "retried"
    finally:
        await engine.dispose()


async def test_non_transient_error_propagates_without_retry(db_path: str) -> None:
    """Only transient lock errors retry; anything else aborts on attempt 1 and
    nothing the unit staged is committed."""
    engine = make_async_engine(db_path)
    factory = make_async_session_factory(engine)
    calls = 0

    async def unit(session: AsyncSession) -> None:
        nonlocal calls
        calls += 1
        session.add(_halt_row(reason="must-not-commit"))
        msg = "boom"
        raise ValueError(msg)

    try:
        with pytest.raises(ValueError, match="boom"):
            await run_immediate_write_unit(factory, unit)
        assert calls == 1
        assert await _read_halt_reason(factory) is None  # rolled back, not committed
    finally:
        await engine.dispose()


async def test_retry_budget_exhaustion_reraises_the_transient_error(db_path: str) -> None:
    """A persistently locked DB propagates the final transient error after
    exactly ``attempts`` tries."""
    engine = make_async_engine(db_path)
    factory = make_async_session_factory(engine)
    calls = 0

    async def unit(_session: AsyncSession) -> None:
        nonlocal calls
        calls += 1
        raise _transient_lock_error()

    try:
        with pytest.raises(OperationalError):
            await run_immediate_write_unit(factory, unit, attempts=2)
        assert calls == 2
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The load-bearing regression pair: a genuine cross-connection
# SQLITE_BUSY_SNAPSHOT under the pre-fix DEFERRED shape, and the same
# interleave succeeding through run_immediate_write_unit.
# ---------------------------------------------------------------------------


async def test_deferred_read_then_write_upgrade_fails_busy_snapshot(db_path: str) -> None:
    """The pre-fix monitor shape genuinely reproduces SQLITE_BUSY_SNAPSHOT.

    One connection pins a deferred read snapshot (BEGIN DEFERRED + SELECT);
    another connection commits; the first connection's write-upgrade then fails
    IMMEDIATELY — the busy handler is never invoked, so ``busy_timeout=60000``
    is irrelevant (the prod failures took ~2 ms). This is the four-times-in-18h
    production failure of 2026-06-09.
    """
    reader_engine = make_async_engine(db_path)
    committer_engine = make_async_engine(db_path)
    reader_factory = make_async_session_factory(reader_engine)
    committer_factory = make_async_session_factory(committer_engine)

    try:
        async with reader_factory() as session:
            # BEGIN DEFERRED (the async-engine default) + first SELECT pins the
            # WAL read snapshot.
            (await session.execute(select(MonitorHaltModeRow))).all()

            # A concurrent committer advances the WAL head past the snapshot.
            async with committer_factory() as committer:
                committer.add(_halt_row(reason="concurrent-commit"))
                await committer.commit()

            # The write-upgrade on the stale snapshot fails instantly.
            session.add(_event_row("tevt-busy-snapshot-repro"))
            with pytest.raises(OperationalError) as excinfo:
                await session.flush()
        assert (
            getattr(excinfo.value.orig, "sqlite_errorname", None) == "SQLITE_BUSY_SNAPSHOT"
        )
    finally:
        await reader_engine.dispose()
        await committer_engine.dispose()


async def test_immediate_write_unit_survives_the_same_concurrent_committer(
    db_path: str,
) -> None:
    """The converted shape completes under the interleave that kills the
    deferred shape: with the write lock held from BEGIN, the concurrent
    committer serializes behind ``busy_timeout`` instead of invalidating a read
    snapshot, and both writes land.
    """
    import asyncio

    unit_engine = make_async_engine(db_path)
    committer_engine = make_async_engine(db_path)
    unit_factory = make_async_session_factory(unit_engine)
    committer_factory = make_async_session_factory(committer_engine)

    async def concurrent_commit() -> None:
        async with committer_factory() as committer:
            committer.add(_halt_row(reason="concurrent-commit"))
            await committer.commit()

    committer_task: asyncio.Task[None] | None = None

    async def unit(session: AsyncSession) -> None:
        nonlocal committer_task
        # The read that previously pinned a stale snapshot — now under the
        # up-front write lock.
        (await session.execute(select(MonitorHaltModeRow))).all()
        # Launch the concurrent committer mid-unit; it must wait on the lock
        # (busy_timeout), not invalidate our snapshot.
        committer_task = asyncio.get_running_loop().create_task(concurrent_commit())
        await asyncio.sleep(0.1)  # let the committer genuinely contend
        session.add(_event_row("tevt-immediate-survives"))

    try:
        await run_immediate_write_unit(unit_factory, unit)
        assert committer_task is not None
        await committer_task
        async with unit_factory() as session:
            assert await _read_halt_reason(unit_factory) == "concurrent-commit"
            event = (
                await session.execute(select(BrokerEventLogRow))
            ).scalar_one()
        assert event.event_key == "tevt-immediate-survives"
    finally:
        await unit_engine.dispose()
        await committer_engine.dispose()
