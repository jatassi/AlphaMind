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
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.modes import Mode
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.execution.state_persistence.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.scheduler.invocation import insert_invocation_record

REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"

_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)

_INVOCATION_ID_RE = re.compile(r"^inv-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-open-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-open-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to an initialized SQLite DB.

    Seeds the FK parent ``process_lifetimes`` row so
    :func:`insert_invocation_record` can satisfy the foreign-key constraint
    without further setup.
    """
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _baseline_runtime() -> RuntimeDimensions:
    return RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
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
            process_lifetime_id="proc-open-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.pre_open,
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
            process_lifetime_id="proc-open-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.pre_open,
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
        assert row.phase1_completed_at is None
        assert row.phase2_completed_at is None

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
            process_lifetime_id="proc-open-1",
            trigger_type="scheduled",
            trigger_source="morning-cron",
            trigger_reason="0 9 * * 1-5",
            firing_run_type=RunType.pre_open,
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

    async def test_fk_violation_raises_and_no_row_lands(
        self,
        env_path: Path,
        archive_root: Path,
        tmp_path: Path,
    ) -> None:
        """FK violation on the ``process_lifetime`` reference aborts the insert.

        Bypasses the seeded fixture so the parent ``process_lifetimes`` row
        does not exist; the row insertion must raise ``IntegrityError`` and
        leave no row in the DB.
        """
        db_path = tmp_path / "alphamind.db"

        import alphamind.execution.state_persistence.tables  # noqa: F401

        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            now = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
            with pytest.raises(IntegrityError):
                await insert_invocation_record(
                    session_factory=factory,
                    process_lifetime_id="proc-does-not-exist",
                    trigger_type="scheduled",
                    trigger_source="morning-cron",
                    trigger_reason="0 9 * * 1-5",
                    firing_run_type=RunType.pre_open,
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
