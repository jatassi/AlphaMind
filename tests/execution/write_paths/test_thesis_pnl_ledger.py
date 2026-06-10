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
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.execution.position_model.thesis_pnl_derivation import ThesisPnlDerivation
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.execution.write_paths.thesis_pnl_ledger import (
    _to_ledger_record,
    rederive_thesis_pnl_ledger,
    rederive_thesis_pnl_ledgers,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
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
    *,
    event_key: str,
    side: str,
    fill_price: float,
    fill_quantity: float,
    at_seconds: int = 0,
    thesis_id: str = _THESIS,
    position_id: str = _POS,
) -> BrokerEventRecord:
    payload = {
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"order": {"side": side}},
    }
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.FILL,
        thesis_id=ThesisId(thesis_id),
        invocation_id=InvocationId(_INV),
        position_id=PositionId(position_id),
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


async def _seed_extra_cluster(
    factory: async_sessionmaker[AsyncSession],
    *,
    position_id: str,
    thesis_id: str,
    bracket_id: str,
    entry_order_id: str,
) -> None:
    """Seed a second position/thesis/bracket/order cluster (FK targets for events)."""

    def _populate(sync_session: object) -> None:
        from sqlalchemy.orm import Session

        assert isinstance(sync_session, Session)
        seed_position_cluster(
            sync_session,
            position_id=position_id,
            thesis_id=thesis_id,
            bracket_id=bracket_id,
            entry_order_id=entry_order_id,
        )

    async with factory() as sess:
        await sess.run_sync(lambda s: _populate(s))
        await sess.commit()


async def _append(factory: async_sessionmaker[AsyncSession], *events: BrokerEventRecord) -> None:
    async with factory() as sess:
        await begin_write_immediate(sess)
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


async def test_rederive_stamps_last_derived_event_seq_to_max_folded(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-865: a re-derivation stamps ``last_derived_event_seq`` to the max
    ``event_seq`` (rowid) it folded, and the value round-trips through the codec.

    This is the per-thesis watermark the dirty-set selector reads to skip a clean
    thesis: it must equal the latest log position the thesis's figures reflect.
    """
    _engine, factory = db
    await _seed(factory)
    await _append(
        factory,
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-add", side="buy", fill_price=120.0, fill_quantity=10.0, at_seconds=60
        ),
    )

    # The max event_seq (SQLite rowid) among the thesis's two events.
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(BrokerEventLogRow).where(BrokerEventLogRow.thesis_id == _THESIS)
                )
            )
            .scalars()
            .all()
        )
        expected_max_seq = max(row.event_seq for row in rows)

    async with factory() as sess:
        records = await rederive_thesis_pnl_ledgers(sess, (ThesisId(_THESIS),), InvocationId(_INV))
        await sess.commit()

    assert records[0].last_derived_event_seq == expected_max_seq
    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, _THESIS)
        assert row is not None
        assert row.last_derived_event_seq == expected_max_seq
        # The codec round-trips the cursor.
        assert row_to_record(row).last_derived_event_seq == expected_max_seq


async def test_derived_cost_basis_and_provenance_round_trip_through_codec(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC#3: the derived cost basis + provenance round-trip through the ledger codec."""
    _engine, factory = db
    await _seed(factory)
    await _append(
        factory,
        _fill_event(event_key="fevt-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="fevt-add", side="buy", fill_price=120.0, fill_quantity=10.0, at_seconds=60
        ),
    )

    async with factory() as sess:
        await rederive_thesis_pnl_ledger(sess, ThesisId(_THESIS), InvocationId(_INV))
        await sess.commit()

    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, _THESIS)
        assert row is not None
        record = row_to_record(row)

    # 20 shares at an avg cost of 110 → 2200 capital tied up.
    assert record.cost_basis_usd == money("2200.00")
    assert json.loads(record.provenance_json)["event_keys"] == ["fevt-open", "fevt-add"]
    assert record.derived_from_invocation_id == InvocationId(_INV)


async def test_batched_rederive_matches_unbatched_and_is_query_bounded(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """PR2 — ``rederive_thesis_pnl_ledgers`` over N theses fetches events in a
    bounded number of broker-event-log SELECTs (not one-per-thesis) while writing
    per-thesis ledgers byte-identical to the unbatched per-thesis path.

    The N+1 it replaces issues one ``SELECT … WHERE thesis_id = ?`` per distinct
    thesis; the batched entry point issues a small constant regardless of the
    thesis count. Per-thesis output must be unchanged, so the two ledger rows the
    batch writes are asserted against the figures the unbatched single-thesis
    derivation produces over the same events.
    """
    engine, factory = db
    await _seed(factory)  # pos-1 / thesis-1
    await _seed_extra_cluster(
        factory,
        position_id="pos-2",
        thesis_id="thesis-2",
        bracket_id="bracket-2",
        entry_order_id="order-2",
    )
    # thesis-1: a closed round-trip (+300 realized, flat) — same as the unbatched tests.
    await _append(
        factory,
        _fill_event(event_key="t1-open", side="buy", fill_price=100.0, fill_quantity=10.0),
        _fill_event(
            event_key="t1-close", side="sell", fill_price=130.0, fill_quantity=10.0, at_seconds=60
        ),
    )
    # thesis-2: an open lot (20 shares @ avg 110 → 2200 basis, 0 realized).
    await _append(
        factory,
        _fill_event(
            event_key="t2-open",
            side="buy",
            fill_price=100.0,
            fill_quantity=10.0,
            thesis_id="thesis-2",
            position_id="pos-2",
        ),
        _fill_event(
            event_key="t2-add",
            side="buy",
            fill_price=120.0,
            fill_quantity=10.0,
            at_seconds=60,
            thesis_id="thesis-2",
            position_id="pos-2",
        ),
    )

    event_log_selects: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        normalized = " ".join(statement.split()).lower()
        if "from broker_event_log" in normalized and normalized.startswith("select"):
            event_log_selects.append(statement)

    async with factory() as sess:
        records = await rederive_thesis_pnl_ledgers(
            sess, (ThesisId("thesis-1"), ThesisId("thesis-2")), InvocationId(_INV)
        )
        await sess.commit()

    # Bounded: a per-thesis fetch would issue >= 2 event SELECTs over 2 theses; the
    # batch issues a single IN-clause read regardless of the thesis count.
    assert len(event_log_selects) == 1, event_log_selects

    by_thesis = {r.thesis_id: r for r in records}
    assert by_thesis[ThesisId("thesis-1")].realized_pnl_usd == signed_money("300.00")
    assert by_thesis[ThesisId("thesis-1")].cost_basis_usd == money("0")
    assert by_thesis[ThesisId("thesis-2")].realized_pnl_usd == signed_money("0")
    assert by_thesis[ThesisId("thesis-2")].cost_basis_usd == money("2200.00")

    # The persisted ledger rows match the returned records, byte-for-byte on the figures.
    async with factory() as sess:
        row1 = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        row2 = await sess.get(ThesisPnlLedgerRow, "thesis-2")
        assert row1 is not None and row2 is not None
        assert row_to_record(row1).realized_pnl_usd == signed_money("300.00")
        assert row_to_record(row1).cost_basis_usd == money("0")
        assert row_to_record(row2).cost_basis_usd == money("2200.00")


def test_provenance_serialization_tolerates_non_str_values() -> None:
    """F6: non-str provenance values (Decimal, datetime) must not raise TypeError.

    ``_to_ledger_record`` uses ``serialize_event_payload`` (sort_keys+default=str)
    so a Decimal or datetime sneaking into provenance coerces via str() instead of
    raising.  The round-trip must preserve the coerced string in the JSON.
    """
    decimal_key = Decimal("1.23")
    datetime_key = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
    derivation = ThesisPnlDerivation(
        realized_pnl_usd=money("0"),
        cost_basis_usd=money("0"),
        provenance_event_keys=(decimal_key, datetime_key),  # type: ignore[arg-type]
    )

    # Must not raise TypeError despite non-str values in provenance_event_keys.
    record = _to_ledger_record(ThesisId(_THESIS), derivation, None, None)

    parsed = json.loads(record.provenance_json)
    assert parsed["event_keys"] == [str(decimal_key), str(datetime_key)]
