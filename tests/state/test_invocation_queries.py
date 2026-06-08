"""Window-scoped invocation-regime read helper (ALP-911 / story 06f).

``read_invocation_regimes_in_window`` returns the ``invocation_id -> active_regime``
map over invocations whose ``start_at`` falls in ``[start, end)`` — the REGIME
conditioning source the feedback-loop loader stamps onto the dataset. Mirrors the
second-prefix window comparison ``read_agent_calls_in_window`` uses.
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
from alphamind.state.repository.invocation_queries import (
    read_invocation_regimes_in_window,
)
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_TS_IN = "2026-06-01T09:00:00+00:00"  # inside the window
_TS_OUT = "2026-01-01T09:00:00+00:00"  # before the window

_PLT = "plt-invocation-regime-tests"


def _seed_invocation(sess: object, invocation_id: str, *, start_at: str, regime: str) -> None:
    row = stub_invocation_row(invocation_id, process_lifetime_id=_PLT)
    row.start_at = start_at
    row.active_regime = regime
    sess.add(row)  # type: ignore[attr-defined]


@pytest.fixture()
async def seeded_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with in/out-of-window invocations in two regimes."""
    db_path = tmp_path / "invocation_queries_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        _seed_invocation(sess, "inv-normal", start_at=_TS_IN, regime="normal")
        _seed_invocation(sess, "inv-elevated", start_at=_TS_IN, regime="elevated")
        _seed_invocation(sess, "inv-out", start_at=_TS_OUT, regime="normal")
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadInvocationRegimesInWindow:
    async def test_in_window_invocations_mapped_to_regime(
        self, seeded_session: AsyncSession
    ) -> None:
        regimes = await read_invocation_regimes_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END
        )
        assert regimes == {"inv-normal": "normal", "inv-elevated": "elevated"}

    async def test_out_of_window_excluded(self, seeded_session: AsyncSession) -> None:
        regimes = await read_invocation_regimes_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END
        )
        assert "inv-out" not in regimes

    async def test_empty_window_returns_empty_map(self, seeded_session: AsyncSession) -> None:
        regimes = await read_invocation_regimes_in_window(
            seeded_session,
            datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2030, 2, 1, tzinfo=UTC),
        )
        assert regimes == {}
