"""Tests for ``sync_terminal_order_status`` (ALP-739).

The helper reflects a broker terminal non-fill disposition
(``canceled`` / ``expired``) onto the local ``orders`` row. Tests exercise
the transition, the source-status guard (a late cancel must not clobber a
FILLED order), the unknown-order no-op, and idempotent re-processing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import alphamind.state.tables  # noqa: F401  -- register every table on Base
from alphamind.execution.write_paths.order_status_sync import (
    sync_terminal_order_status,
)
from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.tables.orders import OrderRow

_OBSERVED_AT = datetime(2026, 5, 28, 20, 30, tzinfo=UTC)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory production-Base factory (FK enforcement off — no cluster seed)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _order_row(*, order_id: str, status: str, filled_quantity: float = 0.0) -> OrderRow:
    return OrderRow(
        order_id=order_id,
        position_id=None,
        bracket_id="brk-1",
        order_role="ENTRY",
        order_class="BRACKET",
        instrument_spec_json='{"instrument_type": "EQUITY", "ticker": "ZS"}',
        direction="SELL",
        order_type="LIMIT",
        quantity=10.0,
        price_parameters_json='{"limit_price": "61.50", "stop_trigger_price": null}',
        duration="DAY",
        status=status,
        alpaca_order_id="alp-1",
        alpaca_order_id_chain_json='["alp-1"]',
        submission_timestamp="2026-05-28T13:30:00+00:00",
        last_update_timestamp="2026-05-28T13:30:00+00:00",
        filled_quantity=filled_quantity,
        average_fill_price=None,
        remaining_quantity=10.0 - filled_quantity,
        modification_count=0,
        metadata_json='{"originating_thesis_id": null, '
        '"originating_pm_command_id": null, "age_hours": 0.0}',
    )


async def _seed(factory: async_sessionmaker[AsyncSession], row: OrderRow) -> None:
    async with factory() as session:
        session.add(row)
        await session.commit()


async def _status_and_ts(
    factory: async_sessionmaker[AsyncSession], order_id: str
) -> tuple[str, str]:
    async with factory() as session:
        row = await session.get(OrderRow, order_id)
        assert row is not None
        return row.status, row.last_update_timestamp


async def _sync(
    factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    terminal_status: OrderStatus,
    observed_at: datetime = _OBSERVED_AT,
) -> bool:
    async with factory() as session:
        transitioned = await sync_terminal_order_status(
            session,
            order_id=order_id,
            terminal_status=terminal_status,
            observed_at=observed_at,
        )
        await session.commit()
    return transitioned


async def test_pending_entry_transitions_to_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, _order_row(order_id="ord-1", status="PENDING"))
    transitioned = await _sync(
        session_factory, order_id="ord-1", terminal_status=OrderStatus.EXPIRED
    )
    assert transitioned is True
    status, ts = await _status_and_ts(session_factory, "ord-1")
    assert status == "EXPIRED"
    assert ts == _OBSERVED_AT.isoformat()


async def test_pending_entry_transitions_to_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, _order_row(order_id="ord-c", status="PENDING"))
    transitioned = await _sync(
        session_factory, order_id="ord-c", terminal_status=OrderStatus.CANCELLED
    )
    assert transitioned is True
    status, _ = await _status_and_ts(session_factory, "ord-c")
    assert status == "CANCELLED"


async def test_partially_filled_order_is_not_transitioned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The consumer gates this path on cumulative_filled_quantity == 0, so a
    # partially-filled order never reaches the helper; the PENDING-only guard
    # is the defense-in-depth backstop, leaving the row untouched (the fill
    # path + Phase 1 own a partially-filled order's status).
    await _seed(
        session_factory,
        _order_row(order_id="ord-2", status="PARTIALLY_FILLED", filled_quantity=3.0),
    )
    transitioned = await _sync(
        session_factory, order_id="ord-2", terminal_status=OrderStatus.CANCELLED
    )
    assert transitioned is False
    status, _ = await _status_and_ts(session_factory, "ord-2")
    assert status == "PARTIALLY_FILLED"


async def test_filled_order_is_not_clobbered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Guard: a late canceled event (e.g. an OCO sibling cancel after the
    # entry already filled) must not overwrite a terminal FILLED status.
    await _seed(
        session_factory,
        _order_row(order_id="ord-3", status="FILLED", filled_quantity=10.0),
    )
    transitioned = await _sync(
        session_factory, order_id="ord-3", terminal_status=OrderStatus.CANCELLED
    )
    assert transitioned is False
    status, _ = await _status_and_ts(session_factory, "ord-3")
    assert status == "FILLED"


async def test_unknown_order_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    transitioned = await _sync(
        session_factory, order_id="ghost", terminal_status=OrderStatus.EXPIRED
    )
    assert transitioned is False


async def test_reprocessing_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Live + recovery overlap can deliver the same terminal event twice; the
    # second pass finds a terminal status and no-ops, so the observation
    # timestamp is not bumped a second time.
    await _seed(session_factory, _order_row(order_id="ord-4", status="PENDING"))
    later = datetime(2026, 5, 28, 21, 0, tzinfo=UTC)
    first = await _sync(session_factory, order_id="ord-4", terminal_status=OrderStatus.EXPIRED)
    second = await _sync(
        session_factory,
        order_id="ord-4",
        terminal_status=OrderStatus.EXPIRED,
        observed_at=later,
    )
    assert first is True
    assert second is False
    status, ts = await _status_and_ts(session_factory, "ord-4")
    assert status == "EXPIRED"
    assert ts == _OBSERVED_AT.isoformat()
