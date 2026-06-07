"""Shared fixtures for ``tests/command_center/alerts/`` (story 05a / ALP-671).

Two recurring fixtures:

* :func:`cc_writer_factory` — an in-memory cc_writer session factory
  used by the persistence + engine tests for alerts-table CRUD.
* :func:`production_factory` — an in-memory production-Base session
  factory + seeded process_lifetime + invocation so the
  ``operator_invocation()`` helper (used by ack/snooze audit) has a
  valid FK target for invocation_id rows.

The fixtures intentionally lean on plain in-memory SQLite so the test
substrate doesn't depend on the dual-engine machinery; the structural-
enforcement layer is exercised by the persistence-layer tests in
``tests/command_center/persistence/``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center.persistence.tables import CommandCenterBase
from alphamind.persistence.models import Base
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

PROCESS_LIFETIME_ID = "plt-alerts-test"
INVOCATION_ID = "inv-alerts-test"


@pytest.fixture
async def cc_writer_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory cc_writer factory bound to :data:`CommandCenterBase.metadata`."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def production_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory production-Base factory seeded with a process_lifetime + invocation.

    The seeded rows let ``operator_invocation()`` write a fresh
    InvocationRow with ``process_lifetime_id`` resolved.
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
                hostname="t",
                git_sha="d",
                git_branch="t",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="t",
            )
        )
        # Seed an invocation row so operator_invocation has a peer for
        # the FK on activity_log; operator_invocation will mint its own
        # invocation row in a separate transaction.
        session.add(
            InvocationRow(
                invocation_id=INVOCATION_ID,
                process_lifetime_id=PROCESS_LIFETIME_ID,
                start_at="2026-05-26T00:00:00Z",
                fill_collection_completed_at=None,
                command_execution_completed_at=None,
                trigger_type="manual",
                trigger_source="operator_console",
                trigger_reason="seed",
                active_mode="normal",
                git_sha_at_invocation="x",
                active_profile="x",
                active_regime="x",
                active_overlays_json="{}",
                resolved_config_hash="x",
                resolved_config_snapshot_path="x",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="x",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=None,
                snapshot_metadata_json=None,
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
