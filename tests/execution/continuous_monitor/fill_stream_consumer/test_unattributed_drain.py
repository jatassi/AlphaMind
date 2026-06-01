"""Tests for consumer-side recovery of unattributed fills (ALP-763).

The fill-stream consumer used to silently drop a fill-bearing event that
arrived before its local ``orders`` row was committed (deferred Phase-2
writeback) — a permanent position/cash divergence. The fix never drops:

* ``_persist_one`` does a short in-process retry, then quarantines the raw
  ``FillReport`` to the ``unattributed_fills`` queue and emits a one-time
  loud ``log.warning`` alert.
* ``drain_unattributed_fills`` re-resolves each queued fill and integrates it
  the moment its order row exists; a fill that never resolves (out-of-band
  manual order) stays queued + alerted, never integrated.

These are sociable tests against a real in-process SQLite session and a fake
``FillReport`` built from the broker-adapter translator, mirroring
``test_task.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.models import Order, TradeUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import BracketId, OrderId, PositionId, ThesisId
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.fill_stream_consumer.task import _persist_one
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    derive_broker_fill_key,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.unattributed_drain import (
    drain_unattributed_fills,
)
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    list_unattributed_fills,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.records import FillRecord
from alphamind.state.tables.fill_records import FillRecordRow
from tests.state._fk_substrate import seed_position_cluster, stub_order_row


def _now_utc() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite engine with the full ORM schema and one seeded order."""
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        seed_position_cluster(
            sess,
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId("thesis-1"),
            bracket_id=BracketId("bracket-1"),
            entry_order_id=OrderId("order-1"),
        )
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _build_order(
    *,
    client_order_id: str,
    order_id: UUID,
    qty: str = "1",
    filled_qty: str = "1",
    status: AlpacaOrderStatus = AlpacaOrderStatus.FILLED,
) -> Order:
    return Order(
        id=order_id,
        client_order_id=client_order_id,
        created_at=_now_utc(),
        updated_at=_now_utc(),
        submitted_at=_now_utc(),
        symbol="AAPL",
        asset_class=AssetClass.US_EQUITY,
        order_class=OrderClass.SIMPLE,
        order_type=OrderType.LIMIT,
        type=OrderType.LIMIT,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        status=status,
        extended_hours=False,
        qty=qty,
        filled_qty=filled_qty,
    )


def _fill_report(*, order_id: UUID, client_order_id: str, price: float, qty: float) -> FillReport:
    update = TradeUpdate(
        event="fill",
        order=_build_order(order_id=order_id, client_order_id=client_order_id),
        timestamp=_now_utc(),
        price=price,
        qty=qty,
    )
    reports = translate_trade_update(update)
    assert len(reports) == 1
    return reports[0]


async def _read_fill_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[FillRecordRow]:
    async with session_factory() as session:
        result = await session.execute(select(FillRecordRow))
        return list(result.scalars().all())


async def _seed_order_row_for(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    alpaca_order_id: str,
) -> None:
    """Insert the matching local order row (the deferred Phase-2 writeback)."""
    async with session_factory() as session:
        session.add(
            stub_order_row(
                order_id,
                "bracket-1",
                position_id="pos-1",
                role="ENTRY",
                status="PENDING",
                alpaca_order_id=alpaca_order_id,
            )
        )
        await session.commit()


class TestFillBeforeOrderCommitRace:
    async def test_fill_is_quarantined_then_drained_when_order_appears(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A fill arrives with no matching orders row (entry UUID + command-id
        # client_order_id, neither resolves).
        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="inv-20260601.CMD-1.0.0",
            price=150.0,
            qty=1.0,
        )

        await _persist_one(report, session_factory=session_factory, enrichment_callable=None)

        # Not dropped: parked in the queue, zero fill_records.
        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].broker_fill_key == derive_broker_fill_key(report)
        assert queued[0].alerted is True

        # The deferred Phase-2 writeback lands: the order row now exists,
        # keyed by the captured broker UUID.
        await _seed_order_row_for(
            session_factory,
            order_id="ORD-DEFERRED-1",
            alpaca_order_id=str(entry_uuid),
        )

        integrated = await drain_unattributed_fills(session_factory=session_factory)

        assert integrated == 1
        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-DEFERRED-1"
        assert rows[0].processing_status == "unprocessed"
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []


class TestTrulyUnknownOrder:
    async def test_unknown_order_stays_queued_and_alerted(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A fill for an out-of-band manual order that never gets a local row.
        report = _fill_report(
            order_id=uuid4(),
            client_order_id="manual-out-of-band",
            price=200.0,
            qty=1.0,
        )

        await _persist_one(report, session_factory=session_factory, enrichment_callable=None)

        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].alerted is True
        assert queued[0].retry_count == 0

        integrated = await drain_unattributed_fills(session_factory=session_factory)

        # Still unresolved → not integrated, stays queued, retry bumped.
        assert integrated == 0
        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].retry_count == 1
        assert queued[0].alerted is True

    async def test_drain_applies_enrichment_callable_on_integrate(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from alphamind._kernel.money import money, price
        from alphamind.portfolio_state.records.positions import LiveExecutionEstimate

        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="inv-20260601.CMD-2.0.0",
            price=150.0,
            qty=1.0,
        )
        await _persist_one(report, session_factory=session_factory, enrichment_callable=None)
        await _seed_order_row_for(
            session_factory,
            order_id="ORD-DEFERRED-2",
            alpaca_order_id=str(entry_uuid),
        )

        async def fake_enrichment(record: FillRecord) -> FillRecord:
            return record.model_copy(
                update={
                    "live_execution_estimate": LiveExecutionEstimate(
                        estimated_spread_usd=money("0.01"),
                        estimated_impact_usd=money("0.02"),
                        estimated_regulatory_fees_usd=money("0.03"),
                        live_adjusted_fill_price=price("150.50"),
                    )
                }
            )

        integrated = await drain_unattributed_fills(
            session_factory=session_factory, enrichment_callable=fake_enrichment
        )

        assert integrated == 1
        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].live_execution_estimate_json is not None


class TestRetryAbsorbsRace:
    async def test_fill_persists_when_order_appears_within_retry_window(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the order row appears within the short in-process retry window,
        the fill persists straight to fill_records without ever queuing."""
        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="inv-20260601.CMD-3.0.0",
            price=150.0,
            qty=1.0,
        )

        # Simulate the order row committing mid-retry: the first resolution
        # attempt sees no row, then a "sleep" seeds it before the next attempt.
        seeded = {"done": False}

        async def fake_sleep(_seconds: float) -> None:
            if not seeded["done"]:
                await _seed_order_row_for(
                    session_factory,
                    order_id="ORD-RACE-1",
                    alpaca_order_id=str(entry_uuid),
                )
                seeded["done"] = True

        monkeypatch.setattr(
            "alphamind.execution.continuous_monitor.fill_stream_consumer.task.asyncio.sleep",
            fake_sleep,
        )

        await _persist_one(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-RACE-1"
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []
