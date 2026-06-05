"""Tests for ``SqlGreeksWriter`` (story 04b / ALP-855).

The narrow greeks writer the refresh task uses. Single-writer (monitor-owned)
``position_greeks`` side table, keyed by ``position_id`` (ADR-0005): each call
opens a fresh AsyncSession via the injected factory, upserts the one greeks row
for the position, and commits — **never** an RMW/UPDATE on the pipeline-owned
``positions`` row. The greeks are the lone computed decoration; isolating them
in their own table is what makes the monitor's refresh a clean upsert on its
own table rather than a cross-process read-modify-write (the second-writer
pattern that generated ALP-824).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.greeks_refresh import SqlGreeksWriter
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
from alphamind.state.records_position_greeks import PositionGreeksRecord
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.position_greeks_codec import row_to_record
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
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
                fill_price=price(2.5),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
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
        direction=None,
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
                fill_price=price(3.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
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


async def _load_greeks(
    factory: async_sessionmaker[AsyncSession], position_id: str
) -> PositionGreeksRecord | None:
    async with factory() as sess:
        row = (
            await sess.execute(
                select(PositionGreeksRow).where(PositionGreeksRow.position_id == position_id)
            )
        ).scalar_one_or_none()
        return None if row is None else row_to_record(row)


async def _position_row_details_json(
    factory: async_sessionmaker[AsyncSession], position_id: str
) -> str:
    async with factory() as sess:
        row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == position_id))
        ).scalar_one()
        return row.details_json


class TestSqlGreeksWriterWritesSideTable:
    async def test_options_greeks_land_in_side_table(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _options_position(position_id=PositionId("pos-1")))
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        new_greeks = OptionGreeks(
            delta=0.6,
            gamma=0.03,
            theta=-0.015,
            vega=0.13,
            as_of_timestamp=as_of,
            iv_used=0.28,
            refresh_failed=False,
        )
        writer = SqlGreeksWriter(async_factory)
        await writer.update_options_greeks(position_id=PositionId("pos-1"), greeks=new_greeks)

        side = await _load_greeks(async_factory, "pos-1")
        assert side is not None
        assert side.delta == 0.6
        assert side.gamma == 0.03
        assert side.theta == -0.015
        assert side.vega == 0.13
        assert side.iv == 0.28
        assert side.updated_at == as_of

    async def test_options_greeks_write_does_not_touch_positions_row(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ADR-0005: the refresh writes the side table, never an RMW on positions."""
        await _insert_position(async_factory, _options_position(position_id=PositionId("pos-1")))
        before = await _position_row_details_json(async_factory, "pos-1")
        writer = SqlGreeksWriter(async_factory)
        await writer.update_options_greeks(
            position_id=PositionId("pos-1"),
            greeks=OptionGreeks(
                delta=0.6,
                gamma=0.03,
                theta=-0.015,
                vega=0.13,
                as_of_timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
                iv_used=0.28,
            ),
        )
        after = await _position_row_details_json(async_factory, "pos-1")
        assert after == before

    async def test_repeated_options_write_upserts_one_row(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _options_position(position_id=PositionId("pos-1")))
        writer = SqlGreeksWriter(async_factory)
        first_as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        second_as_of = datetime(2026, 5, 11, 15, 30, tzinfo=UTC)
        await writer.update_options_greeks(
            position_id=PositionId("pos-1"),
            greeks=OptionGreeks(
                delta=0.6, gamma=0.03, theta=-0.015, vega=0.13, as_of_timestamp=first_as_of
            ),
        )
        await writer.update_options_greeks(
            position_id=PositionId("pos-1"),
            greeks=OptionGreeks(
                delta=0.7, gamma=0.04, theta=-0.02, vega=0.14, as_of_timestamp=second_as_of
            ),
        )
        async with async_factory() as sess:
            rows = (
                await sess.execute(
                    select(PositionGreeksRow).where(PositionGreeksRow.position_id == "pos-1")
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].delta == 0.7
        assert rows[0].updated_at == second_as_of.isoformat()

    async def test_missing_position_raises(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        writer = SqlGreeksWriter(async_factory)
        with pytest.raises(LookupError, match="no such position"):
            await writer.update_options_greeks(
                position_id=PositionId("missing-pos"),
                greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
            )


class TestSqlGreeksWriterStrategy:
    async def test_strategy_aggregate_greeks_land_in_side_table(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _strategy_position(position_id=PositionId("strat-1")))
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        per_leg = {
            "leg-1": OptionGreeks(
                delta=0.55, gamma=0.011, theta=-0.021, vega=0.16, as_of_timestamp=as_of, iv_used=0.22
            ),
            "leg-2": OptionGreeks(
                delta=0.32, gamma=0.011, theta=-0.016, vega=0.13, as_of_timestamp=as_of, iv_used=0.20
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
            position_id=PositionId("strat-1"), per_leg=per_leg, aggregated=aggregated
        )

        side = await _load_greeks(async_factory, "strat-1")
        assert side is not None
        assert side.delta == 0.23
        assert side.iv == 0.21
        assert side.updated_at == as_of

    async def test_strategy_write_does_not_touch_positions_row(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _insert_position(async_factory, _strategy_position(position_id=PositionId("strat-1")))
        before = await _position_row_details_json(async_factory, "strat-1")
        as_of = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        writer = SqlGreeksWriter(async_factory)
        await writer.update_strategy_greeks(
            position_id=PositionId("strat-1"),
            per_leg={
                "leg-1": OptionGreeks(delta=0.55, gamma=0.011, theta=-0.021, vega=0.16),
                "leg-2": OptionGreeks(delta=0.32, gamma=0.011, theta=-0.016, vega=0.13),
            },
            aggregated=OptionGreeks(
                delta=0.23, gamma=0.0, theta=-0.005, vega=0.03, as_of_timestamp=as_of
            ),
        )
        after = await _position_row_details_json(async_factory, "strat-1")
        assert after == before


class TestSqlGreeksWriterNoPositionsOrOrdersRmw:
    """ADR-0005 invariant 1: the greeks refresh's only write is its own side
    table — it issues no ``UPDATE`` / ``INSERT`` on the pipeline-owned
    ``positions`` or ``orders`` rows (the cross-process RMW that generated
    ALP-824). Asserts directly on the SQL the writer emits, so the no-RMW
    contract is structural rather than incidental to the codec round-trip.
    """

    async def test_options_refresh_emits_no_positions_or_orders_write(
        self, tmp_path: Path
    ) -> None:
        from alphamind.persistence.session import (
            make_async_engine,
            make_async_session_factory,
        )

        engine = make_async_engine(str(tmp_path / "norwm.db"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = make_async_session_factory(engine)

        statements: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture(
            _conn: Connection,
            _cursor: object,
            statement: str,
            *_rest: object,
        ) -> None:
            statements.append(statement)

        try:
            await _insert_position(factory, _options_position(position_id=PositionId("pos-1")))
            statements.clear()  # ignore the seed insert; only watch the refresh
            writer = SqlGreeksWriter(factory)
            await writer.update_options_greeks(
                position_id=PositionId("pos-1"),
                greeks=OptionGreeks(
                    delta=0.6,
                    gamma=0.03,
                    theta=-0.015,
                    vega=0.13,
                    as_of_timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
                    iv_used=0.28,
                ),
            )
        finally:
            await engine.dispose()

        write_kinds = ("INSERT", "UPDATE", "DELETE")
        mutations = [
            s
            for s in statements
            if any(s.lstrip().upper().startswith(k) for k in write_kinds)
        ]
        # The only mutation is on the side table.
        assert mutations, "expected at least one write to position_greeks"
        for stmt in mutations:
            upper = stmt.upper()
            assert "POSITIONS" not in upper, f"greeks refresh RMW'd positions: {stmt!r}"
            assert "ORDERS" not in upper, f"greeks refresh RMW'd orders: {stmt!r}"
            assert "POSITION_GREEKS" in upper, f"unexpected write target: {stmt!r}"
