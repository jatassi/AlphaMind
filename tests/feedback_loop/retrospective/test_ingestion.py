"""``ingest_window`` — the retrospective Phase-1 data pull (ALP-890 / story 07c).

Composes ``load_window`` (invocations + provenance via agent_calls, thesis
resolutions via outcomes, PM envelopes via pm_decision_log, validation/replay
bundles) with the unresolved ``optional_pending_retrospective`` follow-up pull.
Replays ingest gracefully empty until ALP-129.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.dataset import WindowDataset
from alphamind.feedback_loop.retrospective.ingestion import (
    RetrospectiveIngestion,
    ingest_window,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    OutcomeId,
    RollbackStatus,
    ValidationId,
    ValidationOutcomeRecord,
    ValidationRecord,
)
from alphamind.feedback_loop.validation.records import (
    Verdict as OutcomeVerdict,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.validation_queries import (
    insert_validation,
    insert_validation_outcome,
)

_WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 4, 1, tzinfo=UTC)


def _validation(validation_id: str, edited_artifact: str) -> ValidationRecord:
    return ValidationRecord(
        validation_id=ValidationId(validation_id),
        registered_at=datetime(2026, 2, 1, tzinfo=UTC),
        registered_by_session_id=None,
        edited_artifact=edited_artifact,
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime="normal",
        registered_model_id="claude-sonnet-4-5",
        watched_metric_ids=(MetricId("win_rate"),),
        window_length_days=7,
        expected_direction=ExpectedDirection.IMPROVED,
        expected_magnitude="5% relative",
        success_criterion="win_rate up 5%",
        failure_criterion="win_rate down 3%",
        evaluation_due_at=datetime(2026, 2, 8, tzinfo=UTC),
        superseded_at=None,
        superseded_reason=None,
    )


def _outcome(
    outcome_id: str,
    validation_id: str,
    rollback_status: RollbackStatus,
) -> ValidationOutcomeRecord:
    return ValidationOutcomeRecord(
        outcome_id=OutcomeId(outcome_id),
        validation_id=ValidationId(validation_id),
        evaluated_at=datetime(2026, 2, 15, tzinfo=UTC),
        evaluated_by_session_id=None,
        verdict=OutcomeVerdict.DEGRADED,
        posterior_summary={"delta": -0.01},
        confounder_notes="regime straddle",
        narrative="degraded with confounder",
        rollback_status=rollback_status,
    )


@pytest.fixture()
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    db_path = tmp_path / "ingestion.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        insert_validation(sess, _validation("v-opt", "prompts/decision/strategist.md"))
        insert_validation(sess, _validation("v-mand", "prompts/analysis/analyst.md"))
        sess.flush()
        insert_validation_outcome(
            sess,
            _outcome("o-opt", "v-opt", RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE),
        )
        insert_validation_outcome(
            sess,
            _outcome("o-mand", "v-mand", RollbackStatus.MANDATORY_CLEAN_FAILURE),
        )
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestIngestWindow:
    async def test_returns_ingestion_with_window_dataset(self, session: AsyncSession) -> None:
        ingestion = await ingest_window(session, _WINDOW_START, _WINDOW_END)
        assert isinstance(ingestion, RetrospectiveIngestion)
        assert isinstance(ingestion.window, WindowDataset)
        assert ingestion.window.start == _WINDOW_START
        assert ingestion.window.end == _WINDOW_END

    async def test_pending_rollbacks_only_optional_pending(self, session: AsyncSession) -> None:
        ingestion = await ingest_window(session, _WINDOW_START, _WINDOW_END)
        assert {p.outcome.outcome_id for p in ingestion.pending_rollbacks} == {OutcomeId("o-opt")}

    async def test_pending_rollback_carries_canonical_identifier(
        self, session: AsyncSession
    ) -> None:
        ingestion = await ingest_window(session, _WINDOW_START, _WINDOW_END)
        (pending,) = ingestion.pending_rollbacks
        assert pending.edited_artifact == "prompts/decision/strategist.md"
        assert pending.item_identifier == "follow_up.rollback_prompts/decision/strategist.md"

    async def test_replays_ingest_gracefully_empty(self, session: AsyncSession) -> None:
        """Replays sub-bundle is empty until ALP-129 lands — no error."""
        ingestion = await ingest_window(session, _WINDOW_START, _WINDOW_END)
        assert ingestion.window.replays.replays == ()
