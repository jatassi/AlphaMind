"""Tests for the ``unattributed_fills`` retry-queue persistence helpers (ALP-763).

Sociable tests against a real in-process SQLite session (no mocks): exercise
the idempotent append, the ordered list read, deletion, the alerted flag, and
the retry-touch counter through the async session boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
    delete_unattributed_fill,
    list_unattributed_fills,
    mark_unattributed_fill_alerted,
    touch_unattributed_fill_retry,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.state.records import UnattributedFill
from alphamind.state.tables.unattributed_fills import UnattributedFillRow

FILL_AT = datetime(2026, 6, 1, 14, 30, 4, tzinfo=UTC)


def _unattributed_fill(
    *,
    broker_fill_key: str = "bfk-1",
    alpaca_order_id: str = "alpaca-ord-1",
    first_seen_at: datetime | None = None,
    raw_report_json: str = '{"event":"fill"}',
) -> UnattributedFill:
    return UnattributedFill(
        broker_fill_key=broker_fill_key,
        alpaca_order_id=alpaca_order_id,
        client_order_id="client-ord-1",
        event_type="fill",
        fill_timestamp=FILL_AT,
        fill_price=150.25,
        fill_quantity=10.0,
        raw_report_json=raw_report_json,
        first_seen_at=first_seen_at or datetime(2026, 6, 1, 14, 30, 5, tzinfo=UTC),
        last_retry_at=None,
        retry_count=0,
        alerted=False,
    )


@pytest.fixture()
async def factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Async session factory over an on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    yield make_async_session_factory(async_engine)
    await async_engine.dispose()


class TestAppendUnattributedFill:
    async def test_inserts_new_row(self, factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session:
            await append_unattributed_fill(session, _unattributed_fill())
            await session.commit()
            rows = (await session.execute(select(UnattributedFillRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].broker_fill_key == "bfk-1"
        assert rows[0].alerted == 0
        assert rows[0].retry_count == 0

    async def test_idempotent_on_broker_fill_key(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with factory() as session:
            await append_unattributed_fill(session, _unattributed_fill())
            await session.commit()
        async with factory() as session:
            # Same key, different payload — must NOT overwrite or duplicate.
            await append_unattributed_fill(
                session, _unattributed_fill(raw_report_json='{"event":"changed"}')
            )
            await session.commit()
        async with factory() as session:
            rows = (await session.execute(select(UnattributedFillRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].raw_report_json == '{"event":"fill"}'


class TestListUnattributedFills:
    async def test_lists_ordered_by_first_seen_at(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        later = _unattributed_fill(
            broker_fill_key="bfk-late",
            first_seen_at=datetime(2026, 6, 1, 15, 0, 0, tzinfo=UTC),
        )
        earlier = _unattributed_fill(
            broker_fill_key="bfk-early",
            first_seen_at=datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC),
        )
        async with factory() as session:
            await append_unattributed_fill(session, later)
            await append_unattributed_fill(session, earlier)
            await session.commit()
        async with factory() as session:
            records = await list_unattributed_fills(session)
        assert [r.broker_fill_key for r in records] == ["bfk-early", "bfk-late"]
        assert all(isinstance(r, UnattributedFill) for r in records)


class TestDeleteUnattributedFill:
    async def test_removes_matching_row(self, factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session:
            await append_unattributed_fill(session, _unattributed_fill())
            await session.commit()
        async with factory() as session:
            await delete_unattributed_fill(session, "bfk-1")
            await session.commit()
        async with factory() as session:
            rows = (await session.execute(select(UnattributedFillRow))).scalars().all()
        assert rows == []


class TestMarkAlerted:
    async def test_sets_alerted_flag(self, factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session:
            await append_unattributed_fill(session, _unattributed_fill())
            await session.commit()
        async with factory() as session:
            await mark_unattributed_fill_alerted(session, "bfk-1")
            await session.commit()
        async with factory() as session:
            records = await list_unattributed_fills(session)
        assert records[0].alerted is True


class TestTouchRetry:
    async def test_increments_count_and_sets_last_retry_at(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        observed = datetime(2026, 6, 1, 16, 0, 0, tzinfo=UTC)
        async with factory() as session:
            await append_unattributed_fill(session, _unattributed_fill())
            await session.commit()
        async with factory() as session:
            await touch_unattributed_fill_retry(session, "bfk-1", observed_at=observed)
            await touch_unattributed_fill_retry(session, "bfk-1", observed_at=observed)
            await session.commit()
        async with factory() as session:
            records = await list_unattributed_fills(session)
        assert records[0].retry_count == 2
        assert records[0].last_retry_at == observed
