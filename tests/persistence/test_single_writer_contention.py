"""Single-writer contention: the monitor's append path cannot race the pipeline (ALP-855 / W4a).

ADR-0005 makes ``SQLITE_BUSY_SNAPSHOT`` *unrepresentable* by construction: the
race requires a cross-process read-modify-write on a **shared mutable row**, and
the redesign removes that pattern — the monitor's only writes land on its *own*
single-writer ``position_greeks`` side table (keyed by ``position_id``), never an
RMW on the pipeline-owned ``positions`` / ``orders`` rows.

This test reproduces the prior race's topology — two engines on one WAL DB
writing concurrently — but with the post-redesign write split (monitor →
``position_greeks``; pipeline → ``positions``). Because the writers touch
disjoint rows, neither leaves a stale read snapshot the other's write-upgrade
collides with, so the run completes with **no** ``SQLITE_BUSY_SNAPSHOT``. The
contrast test (both writers RMW'ing the *same* shared row, the pre-redesign
shape) is in ``tests/test_persistence.py::TestCrossWriterSnapshotConflict`` —
that is the race this design dissolves.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId
from alphamind.execution.continuous_monitor.greeks_refresh import SqlGreeksWriter
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row

_NOW = datetime(2026, 5, 27, 20, 0, tzinfo=UTC)


def _options_position(position_id: str) -> PositionRecord:
    from datetime import date

    from alphamind._kernel.money import money, price, signed_money
    from alphamind._kernel.ids import Symbol
    from alphamind.portfolio_state.records.positions import PositionFill

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
            greeks=OptionGreeks(delta=0.4, gamma=0.02, theta=-0.01, vega=0.10),
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


@pytest.fixture()
async def db_path(tmp_path: Path) -> AsyncIterator[str]:
    path = str(tmp_path / "contention.db")
    engine = make_async_engine(path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    async with factory() as sess:
        sess.add(position_record_to_row(_options_position("pos-1")))
        await sess.commit()
    await engine.dispose()
    yield path


async def test_monitor_append_and_pipeline_write_no_busy_snapshot(db_path: str) -> None:
    """Monitor greeks upsert (its own side table) concurrent with a pipeline
    write to ``positions`` completes without ``SQLITE_BUSY_SNAPSHOT``.

    The pipeline writer takes the write lock up front via ``begin_write_immediate``
    and holds it while the monitor's greeks append runs against its own row; with
    no shared-row RMW the monitor write simply serializes (``busy_timeout``) and
    both commit — the stale-snapshot upgrade that produced the race is never
    reachable.
    """
    monitor_engine = make_async_engine(db_path)
    pipeline_engine = make_async_engine(db_path)
    monitor_factory = make_async_session_factory(monitor_engine)
    pipeline_factory = make_async_session_factory(pipeline_engine)

    pipeline_holds_lock = asyncio.Event()
    order: list[str] = []

    async def pipeline_write() -> None:
        # The pipeline is the single writer of the projection; it RMW's the
        # positions row under an up-front IMMEDIATE write lock (ALP-824 belt).
        async with pipeline_factory() as session:
            await begin_write_immediate(session)
            row = (
                await session.execute(
                    select(PositionRow).where(PositionRow.position_id == "pos-1")
                )
            ).scalar_one()
            row.realized_pnl_to_date_usd = 1.0
            order.append("pipeline_wrote")
            pipeline_holds_lock.set()
            await asyncio.sleep(0.2)  # hold the write lock while the monitor contends
            await session.commit()
            order.append("pipeline_committed")

    async def monitor_append() -> None:
        await pipeline_holds_lock.wait()
        # The monitor's only write: an upsert on its own position_greeks row.
        writer = SqlGreeksWriter(monitor_factory)
        await writer.update_options_greeks(
            position_id=PositionId("pos-1"),
            greeks=OptionGreeks(
                delta=0.6,
                gamma=0.03,
                theta=-0.015,
                vega=0.13,
                as_of_timestamp=_NOW,
                iv_used=0.28,
            ),
        )
        order.append("monitor_appended")

    try:
        # No SQLITE_BUSY_SNAPSHOT (or any OperationalError) is raised.
        await asyncio.gather(pipeline_write(), monitor_append())
        async with monitor_factory() as sess:
            greeks_row = (
                await sess.execute(
                    select(PositionGreeksRow).where(PositionGreeksRow.position_id == "pos-1")
                )
            ).scalar_one()
            pos_row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
    finally:
        await monitor_engine.dispose()
        await pipeline_engine.dispose()

    # Both writers' effects are durable; the monitor serialized behind the
    # pipeline's lock rather than racing it.
    assert order[0] == "pipeline_wrote"
    assert "monitor_appended" in order
    assert greeks_row.delta == 0.6
    assert pos_row.realized_pnl_to_date_usd == 1.0


async def test_concurrent_writes_never_raise_operational_error(db_path: str) -> None:
    """Many interleaved monitor appends + pipeline writes raise no transient lock
    error — the contention is structurally absent, not merely retried away."""
    monitor_engine = make_async_engine(db_path)
    pipeline_engine = make_async_engine(db_path)
    monitor_factory = make_async_session_factory(monitor_engine)
    pipeline_factory = make_async_session_factory(pipeline_engine)
    writer = SqlGreeksWriter(monitor_factory)

    async def one_monitor_append(i: int) -> None:
        await writer.update_options_greeks(
            position_id=PositionId("pos-1"),
            greeks=OptionGreeks(
                delta=0.5 + i * 0.01,
                gamma=0.02,
                theta=-0.01,
                vega=0.1,
                as_of_timestamp=_NOW,
                iv_used=0.25,
            ),
        )

    async def one_pipeline_write(i: int) -> None:
        async with pipeline_factory() as session:
            await begin_write_immediate(session)
            row = (
                await session.execute(
                    select(PositionRow).where(PositionRow.position_id == "pos-1")
                )
            ).scalar_one()
            row.realized_pnl_to_date_usd = float(i)
            await session.commit()

    try:
        tasks = [
            *(one_monitor_append(i) for i in range(8)),
            *(one_pipeline_write(i) for i in range(8)),
        ]
        # gather raises if any task raises OperationalError (BUSY/BUSY_SNAPSHOT).
        await asyncio.gather(*tasks)
    except OperationalError as exc:  # pragma: no cover - failure path
        await monitor_engine.dispose()
        await pipeline_engine.dispose()
        pytest.fail(f"unexpected transient lock under disjoint-row writers: {exc}")
    finally:
        await monitor_engine.dispose()
        await pipeline_engine.dispose()
