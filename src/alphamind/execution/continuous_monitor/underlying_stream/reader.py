"""Lifetime-scoped SQL open-positions reader (story 02b / ALP-434).

The continuous monitor lives across pipeline invocations, so it cannot share
the invocation-scoped :class:`SqlPortfolioStateRepository` (which requires an
``invocation_id`` + active-risk-parameters providers). It also only needs the
narrow ``get_open_positions`` slice — every other repository method is dead
weight for the monitor's purposes.

This module ships a minimal :class:`SqlOpenPositionsReader` that satisfies the
:class:`OpenPositionsReader` Protocol via the same SQLAlchemy query the SQL
repository uses, plus the same row-to-record codec for consistency.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.portfolio_state.records.positions import PositionRecord, PositionStatus


class SqlOpenPositionsReader:
    """Reads currently-open positions through a fresh session per call.

    Satisfies :class:`OpenPositionsReader` without the wider repository's
    invocation-scoping requirements; safe to reuse across the monitor
    process lifetime.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        async with self._session_factory() as session:
            stmt = (
                select(PositionRow)
                .where(PositionRow.status == PositionStatus.OPEN.value)
                .order_by(PositionRow.position_id.asc())
            )
            result = await session.execute(stmt)
            return tuple(position_row_to_record(row) for row in result.scalars())
