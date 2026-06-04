"""Self-attributing fill → broker-event log (story 02a / ALP-845).

A fill's thesis/position attribution rides the broker-carried link embedded in
``client_order_id`` (ALP-844 / ADR-0002), so the fill appends to the append-only
``broker_event_log`` with the decoded ``thesis_id`` / ``invocation_id`` /
``position_id`` and **no local ``orders`` row is required**. The strand path
(``unattributed_fills``) is reachable only for a genuinely out-of-band fill that
carries no parseable link.

These tests exercise :func:`persist_fill_report` directly against an on-disk
SQLite schema (the DB is one of the four sanctioned mock boundaries; here it is
real). The link is built through the canonical
:func:`derive_pm_command_id` / :func:`derive_engine_command_id` so the fixtures
never hand-write the id grammar.
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

from alphamind._kernel.ids import (
    BracketId,
    InvocationId,
    OrderId,
    PositionId,
    ThesisId,
)
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.oms.command_ids import (
    derive_engine_command_id,
    derive_open_thesis_id,
    derive_pm_command_id,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from tests.state._fk_substrate import (
    seed_position_cluster,
    stub_invocation_row,
    stub_process_lifetime_row,
)

# ---------------------------------------------------------------------------
# A linked command id built through the canonical derivation (no hand-writing).
# ---------------------------------------------------------------------------

_INVOCATION_ID = "20260603T120000Z-abc123"
_BASE_COMMAND_ID = f"inv-{_INVOCATION_ID}.ENV-SA-1.0.0"
_THESIS_ID = derive_open_thesis_id("AAPL", _BASE_COMMAND_ID)
_PM_LINKED_COMMAND_ID = derive_pm_command_id(
    invocation_id=_INVOCATION_ID,
    envelope_id="ENV-SA-1",
    command_ordinal=0,
    attempt_seq=0,
    thesis_id=_THESIS_ID,
)
_ENGINE_LINKED_COMMAND_ID = derive_engine_command_id(
    monitor_session_id="mon-20260511T120000Z-deadbeef",
    trigger_id=1,
    thesis_id=_THESIS_ID,
    invocation_id=_INVOCATION_ID,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _build_order(
    *,
    client_order_id: str,
    order_id: UUID | None = None,
    qty: str = "1",
    filled_qty: str = "1",
    status: AlpacaOrderStatus = AlpacaOrderStatus.FILLED,
) -> Order:
    return Order(
        id=order_id or uuid4(),
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


def _fill_report(
    *,
    client_order_id: str,
    order_id: UUID | None = None,
    price: float | None = 189.42,
    qty: float | None = 1.0,
    event: str = "fill",
):
    reports = translate_trade_update(
        TradeUpdate(
            event=event,
            order=_build_order(client_order_id=client_order_id, order_id=order_id),
            timestamp=_now_utc(),
            price=price,
            qty=qty,
        )
    )
    assert len(reports) == 1
    return reports[0]


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite engine with the full ORM schema.

    Seeds the thesis/position/invocation FK targets the broker-carried link
    points at, but deliberately leaves the ``orders`` row absent in the
    no-order-row tests (the entry-order in the cluster is reused as the link's
    position/thesis edge, NOT as the fill's resolution hop).
    """
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        # Seed the position/thesis/bracket cluster keyed on the LINK's thesis id
        # and a position the thesis points at, plus the invocation FK target.
        seed_position_cluster(
            sess,
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId(_THESIS_ID),
            bracket_id=BracketId("bracket-1"),
            entry_order_id=OrderId("order-cluster-1"),
        )
        sess.add(stub_process_lifetime_row())
        sess.flush()
        sess.add(stub_invocation_row(InvocationId(f"inv-{_INVOCATION_ID}")))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


async def _read_event_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[BrokerEventLogRow]:
    async with session_factory() as session:
        result = await session.execute(select(BrokerEventLogRow))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# AC 1 — self-attribute via the link, no order row, append to the event log
# ---------------------------------------------------------------------------


class TestSelfAttribution:
    async def test_pm_linked_fill_appends_event_log_row_with_no_order_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A PM-originated fill whose ``client_order_id`` carries the link
        attributes to its thesis / invocation / position and appends a
        ``broker_event_log`` row — with no ``orders`` row resolving it."""
        report = _fill_report(client_order_id=_PM_LINKED_COMMAND_ID, order_id=uuid4())

        await persist_fill_report(
            report, session_factory=session_factory, enrichment_callable=None
        )

        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "FILL"
        assert row.thesis_id == _THESIS_ID
        assert row.invocation_id == f"inv-{_INVOCATION_ID}"
        assert row.position_id == "pos-1"
