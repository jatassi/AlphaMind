"""Weekly-digest snapshot writer + CLI producer (ALP-891 story 08a).

Drives the snapshot seam against a real on-disk SQLite (no mocks) via an
``AsyncSession`` the test owns. The DB is empty-but-schema-complete, so the
generated digest's registry metrics read zero rows and degrade gracefully — the
exact surface the writer serializes.

Covers:

* ``snapshot_week`` writes one ``weekly_digest_snapshots`` row whose ``digest_json``
  deserializes back to a ``WeeklyDigest`` equal to a fresh generation, with
  ``digest_schema_version`` stamped.
* Idempotency: a second ``snapshot_week`` for the same ``week_start`` is a reported
  no-op — no duplicate row, ``written=False``.
* The ``snapshot`` CLI subcommand runs the producer and reports written / skipped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.feedback import FeedbackLoopConfig
from alphamind.feedback_loop.digest import snapshot
from alphamind.feedback_loop.digest.codec import (
    DIGEST_SCHEMA_VERSION,
    deserialize_digest,
)
from alphamind.feedback_loop.digest.generator import generate_digest
from alphamind.feedback_loop.digest.windows import load_week_inputs, trailing_weeks
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.state.tables.weekly_digest_snapshots import WeeklyDigestSnapshotsRow

# A fixed Monday so labels and bounds are deterministic.
_WEEK_START = date(2026, 5, 11)
_TRAJECTORY_WEEKS = 4
_FIXED_NOW = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """An on-disk SQLite with the full schema created and no rows."""
    path = tmp_path / "snapshot.db"
    engine = make_engine(str(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    return str(path)


@pytest.fixture()
async def session(db_path: str) -> AsyncIterator[AsyncSession]:
    """An ``AsyncSession`` over the schema-complete DB; the test owns its lifecycle."""
    engine = make_async_engine(db_path)
    try:
        factory = make_async_session_factory(engine)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


@pytest.fixture
def digest_config() -> DigestConfig:
    return DigestConfig.model_validate(read_yaml_file(Path("config/digest.yaml")))


@pytest.fixture
def feedback_config() -> FeedbackLoopConfig:
    return FeedbackLoopConfig.model_validate(read_yaml_file(Path("config/feedback.yaml")))


def _clock() -> datetime:
    return _FIXED_NOW


async def _row_count(session: AsyncSession) -> int:
    result = await session.run_sync(
        lambda s: s.execute(select(func.count()).select_from(WeeklyDigestSnapshotsRow)).scalar_one()
    )
    return int(result)


class TestSnapshotWeek:
    async def test_writes_row_round_tripping_to_fresh_digest(
        self,
        session: AsyncSession,
        digest_config: DigestConfig,
        feedback_config: FeedbackLoopConfig,
    ) -> None:
        outcome = await snapshot.snapshot_week(
            session,
            _WEEK_START,
            digest_config=digest_config,
            feedback_config=feedback_config,
            trajectory_weeks=_TRAJECTORY_WEEKS,
            clock=_clock,
        )

        assert outcome.written is True
        assert await _row_count(session) == 1

        # The stored payload deserializes to a digest equal to a fresh generation.
        mondays = trailing_weeks(_WEEK_START, _TRAJECTORY_WEEKS)
        weeks = await load_week_inputs(session, mondays, feedback_config)
        fresh = generate_digest(weeks, digest_config)

        row = await session.run_sync(
            lambda s: s.execute(select(WeeklyDigestSnapshotsRow)).scalars().one()
        )
        assert row.digest_schema_version == DIGEST_SCHEMA_VERSION
        assert deserialize_digest(row.digest_json) == fresh

    async def test_second_call_same_week_is_reported_noop(
        self,
        session: AsyncSession,
        digest_config: DigestConfig,
        feedback_config: FeedbackLoopConfig,
    ) -> None:
        first = await snapshot.snapshot_week(
            session,
            _WEEK_START,
            digest_config=digest_config,
            feedback_config=feedback_config,
            trajectory_weeks=_TRAJECTORY_WEEKS,
            clock=_clock,
        )
        second = await snapshot.snapshot_week(
            session,
            _WEEK_START,
            digest_config=digest_config,
            feedback_config=feedback_config,
            trajectory_weeks=_TRAJECTORY_WEEKS,
            clock=_clock,
        )

        assert first.written is True
        assert second.written is False
        assert await _row_count(session) == 1


class TestSnapshotCLI:
    def test_runs_producer_and_reports_written_then_skipped(
        self, db_path: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from alphamind.feedback_loop.digest import cli

        argv = ["--db-path", db_path, "snapshot", "--week", _WEEK_START.isoformat()]

        rc_first = cli.main(argv)
        out_first = capsys.readouterr().out
        assert rc_first == 0
        assert "written" in out_first.lower()

        rc_second = cli.main(argv)
        out_second = capsys.readouterr().out
        assert rc_second == 0
        assert "skip" in out_second.lower()
