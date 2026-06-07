"""Window-scoped resolved-thesis read helper (ALP-885 / story 06c).

``read_resolved_theses_in_window`` returns only RESOLVED theses whose
``resolution_timestamp`` falls in ``[start, end)``, decoded with their component
rows, and excludes ACTIVE theses and theses resolved outside the window.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.repository.outcome_queries import read_resolved_theses_in_window
from alphamind.state.tables.theses_codec import record_to_rows
from tests.feedback_loop.metrics._outcome_fixtures import make_resolved_thesis_record
from tests.state._fk_substrate import (
    stub_position_row,
    stub_thesis_row,
)

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_RES_IN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # inside the window
_RES_OUT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # before the window


def _seed_resolved(sess: object, thesis_id: str, position_id: str, resolved_at: datetime) -> None:
    sess.add(stub_position_row(position_id))  # type: ignore[attr-defined]
    sess.flush()  # type: ignore[attr-defined]
    record = make_resolved_thesis_record(thesis_id, position_id, resolution_timestamp=resolved_at)
    thesis_row, comp_rows = record_to_rows(record)
    sess.add(thesis_row)  # type: ignore[attr-defined]
    for crow in comp_rows:
        sess.add(crow)  # type: ignore[attr-defined]


@pytest.fixture()
async def seeded_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with in/out-of-window resolved theses + one ACTIVE."""
    db_path = tmp_path / "outcome_queries_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        _seed_resolved(sess, "thes-in", "pos-in", _RES_IN)
        _seed_resolved(sess, "thes-out", "pos-out", _RES_OUT)
        # An ACTIVE thesis must never be returned, even resolved-in-window time.
        sess.add(stub_position_row("pos-active"))
        sess.flush()
        sess.add(
            stub_thesis_row("thes-active", "pos-active", status=ThesisRecordStatus.ACTIVE.value)
        )
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadResolvedThesesInWindow:
    async def test_in_window_resolved_thesis_returned(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert {t.thesis_id for t in theses} == {"thes-in"}

    async def test_components_decoded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        (thesis,) = theses
        assert len(thesis.components) == 3
        assert thesis.resolution_pnl_usd == 100.0

    async def test_out_of_window_excluded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert all(t.thesis_id != "thes-out" for t in theses)

    async def test_active_thesis_excluded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert all(t.thesis_id != "thes-active" for t in theses)

    async def test_empty_window_returns_empty_tuple(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(
            seeded_session,
            datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2030, 2, 1, tzinfo=UTC),
        )
        assert theses == ()
