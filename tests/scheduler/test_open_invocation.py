"""Tests for ``alphamind.scheduler.invocation.insert_invocation_record`` (ALP-449).

The function mints the invocation id, loads + persists the resolved config,
persists the data-calibration snapshot, computes the freshness JSON, builds
the ``InvocationRecord``, and inserts it via the short-transaction
:func:`insert_invocation_row`. Returns ``(invocation_id, pipeline_config)``.

Replaces the pre-ALP-449 ``open_invocation`` async-context-manager which
conflated row insertion with a long-running transaction spanning every
phase — incompatible with the design's snapshot-isolation contract.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.modes import Mode
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.scheduler.invocation import insert_invocation_record
from alphamind.state.tables.invocations import InvocationRow
from tests.scheduler.conftest import (
    SHIPPED_CONFIG_DIR,
)

_INVOCATION_ID_RE = re.compile(r"^inv-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _baseline_runtime() -> RuntimeDimensions:
    return RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.market_open,
    )


class TestInsertInvocationRecord:
    async def test_minted_invocation_id_matches_id_pattern(
        self,
        env_path: Path,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
        invocation_id, _ = await insert_invocation_record(
            session_factory=async_factory,
            process_lifetime_id="proc-driver-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.market_open,
            runtime=_baseline_runtime(),
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            now=now,
        )
        assert _INVOCATION_ID_RE.match(invocation_id) is not None

    async def test_inserts_one_row_visible_to_fresh_session(
        self,
        env_path: Path,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The row must be committed + visible to fresh-session reads on return.

        The downstream snapshot read (between Phase 1 and Phase 2) reads
        the row via fresh sessions through the SQL repository — the
        function's contract is that the row is durable by the time it
        returns, not later.
        """
        now = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
        invocation_id, _ = await insert_invocation_record(
            session_factory=async_factory,
            process_lifetime_id="proc-driver-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.market_open,
            runtime=_baseline_runtime(),
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            now=now,
        )

        async with async_factory() as session:
            rows = (await session.execute(select(InvocationRow))).scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.invocation_id == invocation_id
        assert row.start_at == "2026-05-07T14:30:00Z"
        assert row.fill_collection_completed_at is None
        assert row.command_execution_completed_at is None

    async def test_returns_resolved_pipeline_config(
        self,
        env_path: Path,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The returned pipeline_config matches what was persisted on the row.

        Callers reuse the returned ``PipelineConfig`` without re-loading;
        the persisted ``resolved_config_snapshot_path`` on the row points
        to the same resolved snapshot.
        """
        now = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
        invocation_id, pipeline_config = await insert_invocation_record(
            session_factory=async_factory,
            process_lifetime_id="proc-driver-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.market_open,
            runtime=_baseline_runtime(),
            archive_root=archive_root,
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            now=now,
        )

        async with async_factory() as session:
            row = await session.get(InvocationRow, invocation_id)

        assert row is not None
        assert row.resolved_config_hash == pipeline_config.snapshot.hash
        assert row.resolved_config_snapshot_path == str(pipeline_config.snapshot.path)

    async def test_unknown_process_lifetime_id_raises_and_no_row_lands(
        self,
        env_path: Path,
        archive_root: Path,
        tmp_path: Path,
    ) -> None:
        """An unknown process_lifetime_id aborts the invocation with no row written.

        Bypasses the seeded fixture so the parent ``process_lifetimes`` row
        does not exist. Previously the error surfaced as ``IntegrityError`` on
        the ``invocations`` insert; now ``_fetch_process_git_sha`` raises
        ``ValueError`` earlier (before any insert), but the observable contract
        is unchanged: no invocation row lands.
        """
        db_path = tmp_path / "alphamind.db"

        import alphamind.state.tables  # noqa: F401

        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            now = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
            with pytest.raises(ValueError, match="not found in process_lifetimes"):
                await insert_invocation_record(
                    session_factory=factory,
                    process_lifetime_id="proc-does-not-exist",
                    trigger_type="scheduled",
                    trigger_source="morning-cron",
                    trigger_reason="0 9 * * 1-5",
                    firing_run_type=RunType.market_open,
                    runtime=_baseline_runtime(),
                    archive_root=archive_root,
                    config_dir=SHIPPED_CONFIG_DIR,
                    env_path=env_path,
                    now=now,
                )

            async with factory() as session:
                rows = (await session.execute(select(InvocationRow))).scalars().all()
            assert rows == []
        finally:
            await async_engine.dispose()
