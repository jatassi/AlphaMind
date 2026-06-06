"""Single-writer contention: the monitor's append path cannot race the pipeline (ALP-855 / W4a).

ADR-0005 makes ``SQLITE_BUSY_SNAPSHOT`` *unrepresentable* by construction: the
race requires a cross-process read-modify-write on a **shared mutable row**, and
the redesign removes that pattern — the monitor's primary writes land on its *own*
single-writer ``position_greeks`` side table (keyed by ``position_id``), never an
RMW on the pipeline-owned ``positions`` rows.

**FS4 exception (ALP-836 / ALP-847):** ``precommit_monitor_close_order`` INSERTs a
fresh close ``orders`` row into the pipeline-owned ``orders`` table (durable-intent
pre-broker-submit).  This is a new-PK INSERT (not an RMW on a shared row) under
``BEGIN IMMEDIATE``, so ``SQLITE_BUSY_SNAPSHOT`` remains unreachable — only plain
``BUSY`` can occur, which serializes via ``busy_timeout``.  The contention test
:func:`test_monitor_close_precommit_and_pipeline_write_no_busy` covers this path.

This test reproduces the prior race's topology — two engines on one WAL DB
writing concurrently — but with the post-redesign write split (monitor →
``position_greeks`` / new close ``orders`` row; pipeline → ``positions``).
Because the writers touch disjoint rows, neither leaves a stale read snapshot
the other's write-upgrade collides with, so the run completes with **no**
``SQLITE_BUSY_SNAPSHOT``. The contrast test (both writers RMW'ing the *same*
shared row, the pre-redesign shape) is in
``tests/test_persistence.py::TestCrossWriterSnapshotConflict`` — that is the
race this design dissolves.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol, ThesisId
from alphamind.execution.continuous_monitor.bracket_stops.close_order_precommit import (
    precommit_monitor_close_order,
)
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
    BrokerEntryWindowCanceller,
    EntryWindowDeadlineOutcome,
)
from alphamind.execution.continuous_monitor.entry_window.wiring import (
    make_entry_cancel_target_resolver,
)
from alphamind.execution.continuous_monitor.greeks_refresh import SqlGreeksWriter
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    OrderStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row
from tests.state._fk_substrate import seed_position_cluster

_NOW = datetime(2026, 5, 27, 20, 0, tzinfo=UTC)


def _options_position(position_id: str) -> PositionRecord:
    from datetime import date

    from alphamind._kernel.ids import Symbol
    from alphamind._kernel.money import money, price, signed_money

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
                await session.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
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
                await session.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
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


# ---------------------------------------------------------------------------
# FS4 exception: monitor close-precommit INSERTs into pipeline-owned orders
# ---------------------------------------------------------------------------

_BRACKET_ID = "bracket-contention-1"
_THESIS_ID = "THE-AAPL-0123456789abcdef0123456789abcdef"
_ENTRY_ORDER_ID = "order-contention-entry-1"
_CLOSE_POSITION_ID = "pos-contention-1"


@pytest.fixture()
async def db_path_with_cluster(tmp_path: Path) -> AsyncIterator[str]:
    """DB seeded with the full FK cluster required for an orders INSERT.

    ``orders`` carries DEFERRABLE FKs to ``positions``, ``brackets``, and
    ``theses``, all of which are mutually referential and must land in a single
    deferred-FK transaction.
    """
    import alphamind.state.tables  # noqa: F401  — ensure FK tables are registered

    path = str(tmp_path / "contention_orders.db")
    sync_engine = make_engine(path)
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        seed_position_cluster(
            sess,
            position_id=_CLOSE_POSITION_ID,
            thesis_id=_THESIS_ID,
            bracket_id=_BRACKET_ID,
            entry_order_id=_ENTRY_ORDER_ID,
        )
        sess.commit()
    sync_engine.dispose()
    yield path


def _bracketed_position() -> PositionRecord:
    from datetime import date

    from alphamind._kernel.ids import Symbol
    from alphamind._kernel.money import money, price, signed_money

    return PositionRecord(
        position_id=PositionId(_CLOSE_POSITION_ID),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=BracketId(_BRACKET_ID),
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


async def test_monitor_close_precommit_and_pipeline_write_no_busy(
    db_path_with_cluster: str,
) -> None:
    """Monitor ``precommit_monitor_close_order`` (FS4 orders INSERT) serializes
    cleanly against a concurrent pipeline ``orders``/``positions`` write.

    The monitor's INSERT is a new-PK row under ``BEGIN IMMEDIATE`` — no
    deferred-read snapshot that a write-upgrade could race into
    ``SQLITE_BUSY_SNAPSHOT``.  Only plain ``BUSY`` is possible, and
    ``busy_timeout`` absorbs it.  Both commits succeed without error.
    """
    monitor_engine = make_async_engine(db_path_with_cluster)
    pipeline_engine = make_async_engine(db_path_with_cluster)
    monitor_factory = make_async_session_factory(monitor_engine)
    pipeline_factory = make_async_session_factory(pipeline_engine)

    pipeline_holds_lock = asyncio.Event()
    order: list[str] = []

    async def pipeline_write() -> None:
        async with pipeline_factory() as session:
            await begin_write_immediate(session)
            row = (
                await session.execute(
                    select(PositionRow).where(PositionRow.position_id == _CLOSE_POSITION_ID)
                )
            ).scalar_one()
            row.realized_pnl_to_date_usd = 42.0
            order.append("pipeline_wrote")
            pipeline_holds_lock.set()
            await asyncio.sleep(0.2)  # hold the lock while the monitor contends
            await session.commit()
            order.append("pipeline_committed")

    async def monitor_precommit() -> None:
        await pipeline_holds_lock.wait()
        # FS4: monitor inserts a new close orders row into the pipeline-owned table.
        await precommit_monitor_close_order(
            monitor_factory,
            position=_bracketed_position(),
            client_order_id="coid-contention-close-1",
        )
        order.append("monitor_precommitted")

    try:
        await asyncio.gather(pipeline_write(), monitor_precommit())
        async with monitor_factory() as sess:
            close_row = (
                await sess.execute(
                    select(OrderRow).where(OrderRow.client_order_id == "coid-contention-close-1")
                )
            ).scalar_one()
            pos_row = (
                await sess.execute(
                    select(PositionRow).where(PositionRow.position_id == _CLOSE_POSITION_ID)
                )
            ).scalar_one()
    finally:
        await monitor_engine.dispose()
        await pipeline_engine.dispose()

    # Both writes landed; the monitor serialized behind the pipeline's IMMEDIATE lock.
    assert order[0] == "pipeline_wrote"
    assert "monitor_precommitted" in order
    assert close_row.client_order_id == "coid-contention-close-1"
    assert pos_row.realized_pnl_to_date_usd == 42.0


# ---------------------------------------------------------------------------
# ALP-863: the entry-window cancel path is read-only — it cannot race the pipeline
# ---------------------------------------------------------------------------


def _pending_entry_bracket_record() -> BracketRecord:
    """A ``PENDING_ENTRY`` bracket over the seeded contention cluster — the input
    the canceller's ``cancel`` reads (``entry_order_id`` + ``bracket_id``)."""
    leg = BracketLeg(
        leg_id=f"{_BRACKET_ID}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{_BRACKET_ID}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(_BRACKET_ID),
        position_id=PositionId(_CLOSE_POSITION_ID),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(_ENTRY_ORDER_ID),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=_NOW,
    )


async def _confirming_broker_cancel(_alpaca_order_id: AlpacaOrderId) -> BrokerCancelClassification:
    """Fake broker round-trip: the cancel is confirmed (no DB, no network)."""
    return BrokerCancelClassification.CANCEL_CONFIRMED


async def test_entry_window_cancel_is_read_only_and_cannot_race_pipeline(
    db_path_with_cluster: str,
) -> None:
    """ALP-863 — the entry-window **cancel** path performs the broker cancel and
    NO DB write, so it cannot produce ``SQLITE_BUSY_SNAPSHOT`` against a concurrent
    pipeline write.

    The canceller's only DB touch is the target *resolve* (a read in its own
    session); the broker round-trip is faked. Run it while the pipeline holds an
    up-front IMMEDIATE write lock and neither ``SQLITE_BUSY_SNAPSHOT`` nor any
    other ``OperationalError`` is raised — there is no deferred-read→write-upgrade
    on a shared row to collide. The cancel returns ``CANCELLED`` and the entry
    order row is left **untouched**: the dissolve cascade is the pipeline's job,
    projected later from the ``TERMINAL_ORDER_STATUS`` event the broker cancel
    produces (the monitor's old in-place ``orders``/``brackets`` RMW is gone).
    """
    monitor_engine = make_async_engine(db_path_with_cluster)
    pipeline_engine = make_async_engine(db_path_with_cluster)
    monitor_factory = make_async_session_factory(monitor_engine)
    pipeline_factory = make_async_session_factory(pipeline_engine)

    pipeline_holds_lock = asyncio.Event()
    order: list[str] = []
    outcomes: list[EntryWindowDeadlineOutcome] = []

    async def pipeline_write() -> None:
        async with pipeline_factory() as session:
            await begin_write_immediate(session)
            row = (
                await session.execute(
                    select(PositionRow).where(PositionRow.position_id == _CLOSE_POSITION_ID)
                )
            ).scalar_one()
            row.realized_pnl_to_date_usd = 7.0
            order.append("pipeline_wrote")
            pipeline_holds_lock.set()
            await asyncio.sleep(0.2)  # hold the lock while the monitor contends
            await session.commit()
            order.append("pipeline_committed")

    async def monitor_cancel() -> None:
        await pipeline_holds_lock.wait()
        canceller = BrokerEntryWindowCanceller(
            resolve_target=make_entry_cancel_target_resolver(monitor_factory),
            broker_cancel=_confirming_broker_cancel,
        )
        outcomes.append(await canceller.cancel(bracket=_pending_entry_bracket_record(), now=_NOW))
        order.append("monitor_cancelled")

    try:
        # No SQLITE_BUSY_SNAPSHOT (or any OperationalError) is raised.
        await asyncio.gather(pipeline_write(), monitor_cancel())
        async with monitor_factory() as sess:
            entry_row = await sess.get(OrderRow, _ENTRY_ORDER_ID)
            pos_row = (
                await sess.execute(
                    select(PositionRow).where(PositionRow.position_id == _CLOSE_POSITION_ID)
                )
            ).scalar_one()
    finally:
        await monitor_engine.dispose()
        await pipeline_engine.dispose()

    assert order[0] == "pipeline_wrote"
    assert outcomes == [EntryWindowDeadlineOutcome.CANCELLED]
    # The monitor wrote NOTHING — the entry order keeps its seeded status (the
    # ``stub_order_row`` default), NOT the CANCELLED the old writeback would set.
    assert entry_row is not None
    assert entry_row.status == OrderStatus.FILLED.value
    assert entry_row.status != OrderStatus.CANCELLED.value
    # The pipeline write landed; the monitor read-only path did not block it.
    assert pos_row.realized_pnl_to_date_usd == 7.0
