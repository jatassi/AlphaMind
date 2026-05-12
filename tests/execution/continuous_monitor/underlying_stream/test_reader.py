"""Tests for ``SqlOpenPositionsReader`` (story 02b / ALP-434).

The reader is the production seam the continuous monitor uses to determine
which underlyings to subscribe to. It reads OPEN positions directly from the
``positions`` table through a session factory — no invocation-id or
risk-parameter providers required, so it's safe to instantiate once at
process startup and reuse across pipeline invocations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.continuous_monitor.underlying_stream.reader import (
    SqlOpenPositionsReader,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield factory
    await async_engine.dispose()


def _open_equity_row(position_id: str, ticker: str) -> PositionRow:
    return PositionRow(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN.value,
        direction=Direction.LONG.value,
        entry_timestamp="2026-05-11T14:30:00Z",
        instrument_type=InstrumentType.EQUITY.value,
        details_json=(
            '{"instrument_type":"' + InstrumentType.EQUITY.value + '",'
            '"ticker":"' + ticker + '",'
            '"share_count":10.0,'
            '"average_cost_basis_per_share":100.0}'
        ),
        execution_history_json=(
            '[{"fill_timestamp":"2026-05-11T14:30:00Z",'
            '"fill_price":100.0,"fill_quantity":10.0,'
            '"slippage":0.0,"fees":0.0}]'
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


def _pending_equity_row(position_id: str, ticker: str) -> PositionRow:
    return PositionRow(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING.value,
        direction=Direction.LONG.value,
        entry_timestamp=None,
        instrument_type=InstrumentType.EQUITY.value,
        details_json=(
            '{"instrument_type":"' + InstrumentType.EQUITY.value + '",'
            '"ticker":"' + ticker + '",'
            '"share_count":10.0,'
            '"average_cost_basis_per_share":100.0}'
        ),
        execution_history_json="[]",
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=None,
        origin=None,
    )


class TestSqlOpenPositionsReader:
    async def test_empty_db_returns_empty_tuple(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        reader = SqlOpenPositionsReader(session_factory)
        assert await reader.get_open_positions() == ()

    async def test_returns_open_positions_only(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(_open_equity_row("p1", "SPY"))
            session.add(_pending_equity_row("p2", "AAPL"))
            await session.commit()

        reader = SqlOpenPositionsReader(session_factory)
        positions = await reader.get_open_positions()
        assert len(positions) == 1
        assert positions[0].position_id == "p1"
        assert positions[0].status == PositionStatus.OPEN

    async def test_returns_records_sorted_by_position_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(_open_equity_row("p3", "MSFT"))
            session.add(_open_equity_row("p1", "SPY"))
            session.add(_open_equity_row("p2", "AAPL"))
            await session.commit()

        reader = SqlOpenPositionsReader(session_factory)
        positions = await reader.get_open_positions()
        assert [p.position_id for p in positions] == ["p1", "p2", "p3"]
