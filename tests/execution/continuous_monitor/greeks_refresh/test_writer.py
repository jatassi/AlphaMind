"""Tests for ``SqlGreeksWriter`` (story 03a / ALP-436).

The narrow position-update writer the refresh task uses. Each call opens
a fresh AsyncSession via the injected factory, loads the position row,
re-projects the typed record with the new greeks, and commits — one row,
no batching, one transaction per write so a crash mid-cycle never leaves
a torn position record.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind.execution.continuous_monitor.greeks_refresh import SqlGreeksWriter
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)


@pytest.fixture()
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    engine = make_async_engine(str(db_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _options_position(
    *,
    position_id: str = "pos-1",
    iv_used: float | None = 0.25,
    as_of: datetime | None = None,
) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=200.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=OptionGreeks(
                delta=0.4,
                gamma=0.02,
                theta=-0.01,
                vega=0.10,
                as_of_timestamp=as_of,
                iv_used=iv_used,
                refresh_failed=False,
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
                fill_price=2.5,
                fill_quantity=1.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(*, position_id: str = "strat-1") -> PositionRecord:
    leg_one_options = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=500.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(delta=0.5, gamma=0.01, theta=-0.02, vega=0.15),
    )
    leg_two_options = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=510.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=OptionGreeks(delta=0.3, gamma=0.01, theta=-0.015, vega=0.12),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(
                StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=leg_one_options),
                StrategyLeg(leg_id="leg-2", direction=Direction.SHORT, options=leg_two_options),
            ),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=300.0,
            breakeven_levels=(503.0,),
            strategy_greeks=OptionGreeks(delta=0.2, gamma=0.0, theta=-0.005, vega=0.03),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
                fill_price=3.0,
                fill_quantity=1.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


async def _insert_position(
    factory: async_sessionmaker[AsyncSession], position: PositionRecord
) -> None:
    async with factory() as sess:
        sess.add(position_record_to_row(position))
        await sess.commit()


async def _load_position(
    factory: async_sessionmaker[AsyncSession], position_id: str
) -> PositionRecord:
    async with factory() as sess:
        row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == position_id))
        ).scalar_one()
        return position_row_to_record(row)


class TestSqlGreeksWriterOptions:
    async def test_overwrites_options_greeks(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _options_position(position_id="pos-1"))
        new_greeks = OptionGreeks(
            delta=0.6,
            gamma=0.03,
            theta=-0.015,
            vega=0.13,
            as_of_timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            iv_used=0.28,
            refresh_failed=False,
        )
        writer = SqlGreeksWriter(async_factory)
        await writer.update_options_greeks(position_id="pos-1", greeks=new_greeks)
        reloaded = await _load_position(async_factory, "pos-1")
        details = reloaded.details
        assert isinstance(details, OptionsPositionDetails)
        assert details.greeks.delta == 0.6
        assert details.greeks.iv_used == 0.28
        assert details.greeks.as_of_timestamp == datetime(2026, 5, 11, 14, 30, tzinfo=UTC)

    async def test_missing_position_raises(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        writer = SqlGreeksWriter(async_factory)
        with pytest.raises(LookupError, match="no such position"):
            await writer.update_options_greeks(
                position_id="missing-pos",
                greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
            )


class TestSqlGreeksWriterStrategy:
    async def test_overwrites_per_leg_and_aggregate(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _strategy_position(position_id="strat-1"))
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        per_leg = {
            "leg-1": OptionGreeks(
                delta=0.55,
                gamma=0.011,
                theta=-0.021,
                vega=0.16,
                as_of_timestamp=as_of,
                iv_used=0.22,
            ),
            "leg-2": OptionGreeks(
                delta=0.32,
                gamma=0.011,
                theta=-0.016,
                vega=0.13,
                as_of_timestamp=as_of,
                iv_used=0.20,
            ),
        }
        aggregated = OptionGreeks(
            delta=0.23,
            gamma=0.0,
            theta=-0.005,
            vega=0.03,
            as_of_timestamp=as_of,
            iv_used=0.21,
        )
        writer = SqlGreeksWriter(async_factory)
        await writer.update_strategy_greeks(
            position_id="strat-1", per_leg=per_leg, aggregated=aggregated
        )
        reloaded = await _load_position(async_factory, "strat-1")
        details = reloaded.details
        assert isinstance(details, StrategyPositionDetails)
        assert details.strategy_greeks.delta == 0.23
        assert details.strategy_greeks.as_of_timestamp == as_of
        leg_greeks = {leg.leg_id: leg.options.greeks for leg in details.legs}
        assert leg_greeks["leg-1"].delta == 0.55
        assert leg_greeks["leg-2"].delta == 0.32
