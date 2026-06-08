"""Unit tests for the feedback-loop spine verify script (ALP-896 / story 10).

The script's *seed builder* (``build_test_seed``) and its pure *assertion helpers*
are the unit-testable surface; the full end-to-end run is exercised by invoking
``scripts/verify_feedback_loop.py`` manually (it is the verify-of-the-verify, and it
runs ``alembic upgrade head`` against a scratch DB — embedding that in pytest would
just duplicate the script). These tests pin:

* the seed builder inserts the documented closed-position scenario; and
* each pure ``assert_*`` helper returns ``None`` on the conforming, genuinely-loaded
  stage result and a rule-naming failure message on a perturbed one.

The database is the sanctioned mock boundary; here it is a real on-disk SQLite DB
(``tmp_path``) with the ORM schema materialised via ``create_all`` — faster than the
script's own ``alembic upgrade head`` while exercising the real stage code against a
genuine record set, so the helpers are checked on real loaded objects rather than
hand-built imitations. The resolver's LLM fallback is satisfied by the script's own
injected SDK stub (the one sanctioned LLM-boundary fake).
"""

from __future__ import annotations

import dataclasses
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register state tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus, ThesisResolutionCategory
from alphamind.scripts.verify_feedback_loop import (
    FeedbackLoopSeed,
    StageResults,
    assert_digest_generated,
    assert_outcome_metric_nonempty,
    assert_recent_resolutions_regression,
    assert_replay_join_computes,
    assert_retrospective_saved,
    assert_snapshot_written,
    assert_supersession_detected,
    assert_thesis_resolved,
    assert_validation_evaluated,
    assert_window_loaded,
    build_test_seed,
)
from alphamind.scripts.verify_feedback_loop import _drive_stages as drive_stages
from alphamind.state.tables.theses import ThesisRow

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An async session factory over a fresh on-disk SQLite DB at ORM-schema head."""
    db_path = tmp_path / "alphamind-fbl-test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    yield make_async_session_factory(async_engine)
    await async_engine.dispose()


class TestSeedBuilder:
    async def test_seeds_closed_position_active_thesis(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The seed inserts the ACTIVE thesis linked to a CLOSED position."""
        seed = await build_test_seed(factory)

        async with factory() as session:
            thesis_row = (
                await session.execute(
                    select(ThesisRow).where(ThesisRow.thesis_id == seed.thesis_id)
                )
            ).scalar_one()

        assert thesis_row.status == ThesisRecordStatus.ACTIVE.value
        assert thesis_row.position_id == seed.position_id


@pytest.fixture()
async def driven(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[StageResults, FeedbackLoopSeed]]:
    """Seed + drive the full spine once, yielding ``(results, seed)``.

    Exercises the real stage code against the controlled seed so the ``assert_*``
    helpers are checked on a genuine loaded record set, not hand-built imitations.
    """
    seed = await build_test_seed(factory)
    with tempfile.TemporaryDirectory() as data_tmp:
        results = await drive_stages(factory, seed, data_root=Path(data_tmp))
    yield results, seed


class TestAssertionHelpersOnRealStageResults:
    """Every ``assert_*`` helper passes (returns ``None``) on the real run."""

    async def test_thesis_resolved_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert assert_thesis_resolved(results.resolved, seed) is None

    async def test_recent_resolutions_regression_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert (
            assert_recent_resolutions_regression(
                results.recent_resolutions, results.metric_result, seed
            )
            is None
        )

    async def test_window_loaded_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert assert_window_loaded(results.window, seed) is None

    async def test_outcome_metric_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert assert_outcome_metric_nonempty(results.metric_result, seed) is None

    async def test_digest_and_snapshot_pass(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert assert_digest_generated(results.digest, seed) is None
        assert assert_snapshot_written(results.snapshot, seed) is None

    async def test_validation_passes(self, driven: tuple[StageResults, FeedbackLoopSeed]) -> None:
        results, seed = driven
        assert assert_validation_evaluated(results.evaluation, seed) is None

    async def test_supersession_passes(self, driven: tuple[StageResults, FeedbackLoopSeed]) -> None:
        results, _ = driven
        assert assert_supersession_detected(results.superseded_count) is None

    async def test_retrospective_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        assert assert_retrospective_saved(results.ingestion, results.report_file_ref, seed) is None

    async def test_resolved_thesis_category_is_validated(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        """The TARGET_REACHED + profitable scenario resolves to VALIDATED."""
        results, _ = driven
        assert results.resolved[0].record.resolution_category is (
            ThesisResolutionCategory.VALIDATED
        )

    async def test_replay_join_computes_passes(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        """The seeded MODIFICATION_ORIGINAL_FORM replay flows through the join: the
        window's replays are non-empty and pm_modification_effectiveness computes."""
        results, _ = driven
        assert assert_replay_join_computes(results.window) is None


class TestAssertionHelpersRejectMismatches:
    """Each pure helper names the rule it enforces on a perturbed input."""

    async def test_thesis_resolved_rejects_empty(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        _, seed = driven
        message = assert_thesis_resolved((), seed)
        assert message is not None
        assert "expected exactly 1" in message

    async def test_recent_resolutions_rejects_missing_thesis(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        message = assert_recent_resolutions_regression((), results.metric_result, seed)
        assert message is not None
        assert seed.thesis_id in message

    async def test_outcome_metric_rejects_none_value(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        empty = dataclasses.replace(results.metric_result, value=None)
        message = assert_outcome_metric_nonempty(empty, seed)
        assert message is not None
        assert "None" in message

    async def test_snapshot_rejects_not_written(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        results, seed = driven
        skipped = dataclasses.replace(results.snapshot, written=False)
        message = assert_snapshot_written(skipped, seed)
        assert message is not None
        assert "written=False" in message

    async def test_supersession_rejects_nonzero(self) -> None:
        message = assert_supersession_detected(1)
        assert message is not None
        assert "0 supersessions" in message

    async def test_replay_join_rejects_empty_replays(
        self, driven: tuple[StageResults, FeedbackLoopSeed]
    ) -> None:
        """A window with no loaded replays fails the join assertion — the seed did
        not flow through, so the modification metric has nothing to compute over."""
        from alphamind.feedback_loop.dataset import ReplaysBundle

        results, _ = driven
        empty = dataclasses.replace(results.window, replays=ReplaysBundle())
        message = assert_replay_join_computes(empty)
        assert message is not None
        assert "replays" in message
