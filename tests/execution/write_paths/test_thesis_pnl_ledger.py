"""Shell tests for the per-thesis PnL-ledger writer (ALP-851 / W2b).

The single writer of ``thesis_pnl_ledger`` (ADR-0005): reads a thesis's
``broker_event_log`` events, folds them via the pure derivation, and upserts the
ledger row. The DB is real (a sanctioned boundary); no internal collaborator is
mocked. The two load-bearing invariants:

* per-thesis realized PnL reproduces from the event log alone (no ``orders``
  join);
* a simulated quantity checkpoint / projection rebuild never alters the ledger
  (invariant 3) — re-deriving from the same event set is idempotent.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.execution.write_paths.thesis_pnl_ledger import rederive_thesis_pnl_ledger
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from alphamind.state.tables.thesis_pnl_ledger_codec import row_to_record
from tests.state._fk_substrate import (
    seed_position_cluster,
    stub_invocation_row,
    stub_process_lifetime_row,
)

_THESIS = "thesis-1"
_POS = "pos-1"
_INV = "inv-1"
_T0 = dt.datetime(2026, 6, 1, 14, 0, 0, tzinfo=dt.UTC)


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    db_path = tmp_path / "alphamind_pnl.db"
    import alphamind.state.tables  # noqa: F401 — registers all tables

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed the process-lifetime + invocation + position/thesis FK substrate.

    The FK-substrate builders are sync row factories; ``run_sync`` lets the async
    session apply them through the synchronous ``seed_position_cluster`` against
    the real on-disk DB.
    """

    def _populate(sync_session: object) -> None:
        from sqlalchemy.orm import Session

        assert isinstance(sync_session, Session)
        sync_session.add(stub_process_lifetime_row())
        sync_session.flush()
        sync_session.add(stub_invocation_row(_INV))
        seed_position_cluster(sync_session)

    async with factory() as sess:
        await sess.run_sync(lambda s: _populate(s))
        await sess.commit()


def _at(seconds: int) -> dt.datetime:
    return _T0 + dt.timedelta(seconds=seconds)


def _fill_event(
    *, event_key: str, side: str, fill_price: float, fill_quantity: float, at_seconds: int = 0
) -> BrokerEventRecord:
    payload = {
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"order": {"side": side}},
    }
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.FILL,
        thesis_id=ThesisId(_THESIS),
        invocation_id=InvocationId(_INV),
        position_id=PositionId(_POS),
        raw_payload_json=json.dumps(payload, sort_keys=True),
        broker_timestamp=_at(at_seconds),
        captured_at=_at(at_seconds),
    )


def _expiry_event(
    *, event_key: str, realized_pnl_delta_usd: str, at_seconds: int
) -> BrokerEventRecord:
    payload = {"activity_id": event_key, "realized_pnl_delta_usd": realized_pnl_delta_usd}
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.OPEXP,
        thesis_id=ThesisId(_THESIS),
        invocation_id=InvocationId(_INV),
        position_id=PositionId(_POS),
        raw_payload_json=json.dumps(payload, sort_keys=True),
        broker_timestamp=_at(at_seconds),
        captured_at=_at(at_seconds),
    )


async def _append(factory: async_sessionmaker[AsyncSession], *events: BrokerEventRecord) -> None:
    async with factory() as sess:
        for event in events:
            await append_broker_event(sess, event)
        await sess.commit()


async def test_realized_pnl_reproduces_from_event_log_alone(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC#1: a thesis's realized PnL derives from its event log with no orders join."""
    _engine, factory = db
    await _seed(factory)
    await _append(
        factory,
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-close", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
        _expiry_event(event_key="activity:exp", realized_pnl_delta_usd="-50.00", at_seconds=120),
    )

    async with factory() as sess:
        record = await rederive_thesis_pnl_ledger(sess, ThesisId(_THESIS), InvocationId(_INV))
        await sess.commit()

    assert record.realized_pnl_usd == signed_money("250.00")  # +300 round-trip, -50 expiry
    assert record.cost_basis_usd == money("0")

    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, _THESIS)
        assert row is not None
        assert row_to_record(row).realized_pnl_usd == signed_money("250.00")


async def test_rederive_is_idempotent_under_checkpoint(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC#2: a simulated quantity checkpoint/rebuild leaves the ledger unchanged.

    Re-deriving from the same event set must reproduce the same figures — a
    projection rebuild reruns the derivation and the ledger does not drift.
    """
    _engine, factory = db
    await _seed(factory)
    await _append(
        factory,
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-close", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
    )

    async with factory() as sess:
        first = await rederive_thesis_pnl_ledger(sess, ThesisId(_THESIS), InvocationId(_INV))
        await sess.commit()
    # A checkpoint/rebuild reruns the derivation over the same events.
    async with factory() as sess:
        second = await rederive_thesis_pnl_ledger(sess, ThesisId(_THESIS), InvocationId(_INV))
        await sess.commit()

    assert first.realized_pnl_usd == second.realized_pnl_usd == signed_money("300.00")
    assert first.cost_basis_usd == second.cost_basis_usd == money("0")
