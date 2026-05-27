"""Shared fixtures for ``tests/command_center/control`` (story 04a / ALP-668).

Reuses the ``operator_invocation`` substrate fixtures so audit / proxy
tests open real ``InvocationHandle`` contexts against in-memory SQLite,
without having to wire the full ``operator_sessions`` + WebAuthn
substrate the auth tests use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.persistence.models import Base
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

PROCESS_LIFETIME_ID = "plt-command_center-control-test"


@pytest.fixture
async def production_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test in-memory production session factory + seeded process lifetime.

    Mirrors the ``session_factory`` fixture in
    ``tests/command_center/_kernel/test_operator_invocation.py``; the row
    seeded here resolves the ``invocations.process_lifetime_id`` FK that
    ``operator_invocation`` writes against.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=PROCESS_LIFETIME_ID,
                process_role="monitor",
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


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
