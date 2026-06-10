"""The shared append helpers refuse non-IMMEDIATE transactions (ALP-942 Scope F).

``append_broker_event`` / ``append_fill_record`` / ``append_unattributed_fill``
are the cross-process write seams every fill/event fact lands through. A caller
that reaches them on a deferred transaction has re-opened the
``SQLITE_BUSY_SNAPSHOT`` race ALP-942 closed, so the helpers raise loudly at
the first test run instead of shipping a latent production race.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import OrderId
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.execution.write_paths.fill_persistence import append_fill_record
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
)
from alphamind.state.records import FillProcessingStatus, FillRecord, UnattributedFill
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType

_NOW = datetime(2026, 6, 9, 17, 0, 5, tzinfo=UTC)


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    import alphamind.state.tables  # noqa: F401  — register every FK table

    engine = make_async_engine(str(tmp_path / "guard.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_async_session_factory(engine)
    finally:
        await engine.dispose()


def _event_record() -> BrokerEventRecord:
    return BrokerEventRecord(
        event_key="tevt-guard-1",
        event_type=BrokerEventType.FILL,
        thesis_id=None,
        invocation_id=None,
        position_id=None,
        raw_payload_json="{}",
        broker_timestamp=None,
        captured_at=_NOW,
    )


def _fill_record() -> FillRecord:
    from alphamind._kernel.money import money, price
    from alphamind.portfolio_state.records.orders import OrderStatus

    return FillRecord(
        fill_id="fill-guard-1",
        order_id=OrderId("order-guard-1"),
        fill_timestamp=_NOW,
        fill_price=price(100.0),
        fill_quantity=1.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=None,
        fees_usd=money(0.0),
        execution_venue=None,
        gateway_reference="alp-guard",
        persistence_timestamp=_NOW,
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def _unattributed_fill() -> UnattributedFill:
    return UnattributedFill(
        broker_fill_key="bfk-guard-1",
        alpaca_order_id="alp-guard-1",
        client_order_id="coid-guard-1",
        event_type="fill",
        fill_timestamp=_NOW,
        fill_price=100.0,
        fill_quantity=1.0,
        raw_report_json="{}",
        first_seen_at=_NOW,
        last_retry_at=None,
        retry_count=0,
        alerted=False,
    )


async def test_each_append_helper_rejects_a_deferred_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every shared append helper raises before staging anything when the
    session's transaction did not begin IMMEDIATE."""
    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="append_broker_event"):
            await append_broker_event(session, _event_record())
    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="append_fill_record"):
            await append_fill_record(session, _fill_record())
    async with session_factory() as session:
        with pytest.raises(RuntimeError, match="append_unattributed_fill"):
            await append_unattributed_fill(session, _unattributed_fill())


async def test_append_helpers_accept_an_immediate_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Under ``begin_write_immediate`` (the run_immediate_write_unit shape) the
    helpers stage normally — the event row commits and is durable."""
    async with session_factory() as session:
        await begin_write_immediate(session)
        inserted = await append_broker_event(session, _event_record())
        await append_unattributed_fill(session, _unattributed_fill())
        await session.commit()
    assert inserted is True
