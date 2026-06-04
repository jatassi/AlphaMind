"""Tests for the periodic fill-backfill backstop ``run_fill_backfill`` (ALP-763).

A fast Phase-2 entry fill can be dropped / quarantined before its ``orders``
row commits. The websocket disconnect-recovery only runs on a reconnect and
keys its ``since`` off ``max(fill_records.fill_timestamp)`` — which permanently
excludes an earlier dropped fill once a later one lands. This backstop sweeps
ON AN INTERVAL with an INDEPENDENT, generous lookback bound, feeding any Alpaca
fill missing from ``fill_records`` through the normal persist path and then
draining the unattributed-fills queue.

Sociable tests against a real in-process SQLite session and a fake ``queries``
whose ``get_orders`` yields ``OrderSnapshot``s, mirroring the
``fill_stream_consumer`` test style.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import BracketId, OrderId, PositionId, ThesisId
from alphamind._kernel.money import price
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter import FillReport, OrderSnapshot
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.activities_backfill import (
    run_fill_backfill,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    derive_broker_fill_key,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
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
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.fill_records import FillRecordRow
from tests.state._fk_substrate import seed_position_cluster, stub_order_row

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _config(
    *,
    fill_backfill_interval_seconds: int = 900,
    fill_backfill_lookback_seconds: int = 259_200,
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
        fill_backfill_interval_seconds=fill_backfill_interval_seconds,
        fill_backfill_lookback_seconds=fill_backfill_lookback_seconds,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260601T120000Z-deadbeef",
        started_at=_now_utc(),
        mode="paper",
    )


def _order_snapshot(
    *,
    order_id: str,
    client_order_id: str,
    filled_avg_price: float = 100.0,
    filled_qty: float = 1.0,
    filled_at: datetime | None = None,
) -> OrderSnapshot:
    fa = filled_at or _now_utc()
    return OrderSnapshot(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol="AAPL",
        asset_class="us_equity",
        qty=filled_qty,
        filled_qty=filled_qty,
        side="buy",
        order_type="market",
        time_in_force="day",
        order_class="simple",
        status="filled",
        submitted_at=fa,
        filled_at=fa,
        canceled_at=None,
        expired_at=None,
        filled_avg_price=price(filled_avg_price),
        replaced_by=None,
        replaces=None,
        legs=None,
    )


class _FakeAccountStateQueries:
    """``get_orders`` async-iterator substitute; records each invocation."""

    def __init__(self, snapshots: Sequence[OrderSnapshot] = ()) -> None:
        self._snapshots = list(snapshots)
        self.calls: list[dict[str, Any]] = []

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncIterator[OrderSnapshot]:
        self.calls.append({"status": status, "since": since, "until": until})
        for snap in self._snapshots:
            yield snap


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------


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


async def _read_fill_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[FillRecordRow]:
    async with session_factory() as session:
        result = await session.execute(select(FillRecordRow))
        return list(result.scalars().all())


async def _read_broker_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[BrokerEventLogRow]:
    async with session_factory() as session:
        result = await session.execute(select(BrokerEventLogRow))
        return list(result.scalars().all())


async def _seed_order_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    alpaca_order_id: str,
) -> None:
    """Insert a local order row keyed by the captured broker UUID."""
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


def _fill_report(*, order_id: str, client_order_id: str, price_: float, qty: float) -> FillReport:
    from alpaca.trading.enums import (
        AssetClass,
        OrderClass,
        OrderSide,
        OrderType,
        TimeInForce,
    )
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
    from alpaca.trading.models import Order, TradeUpdate

    order = Order(
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
        status=AlpacaOrderStatus.FILLED,
        extended_hours=False,
        qty=str(qty),
        filled_qty=str(qty),
    )
    update = TradeUpdate(event="fill", order=order, timestamp=_now_utc(), price=price_, qty=qty)
    reports = translate_trade_update(update)
    assert len(reports) == 1
    return reports[0]


async def _fast_loop() -> AsyncIterator[None]:
    """Stand-in for ``MonitorSupervisor.supervised_loop`` (ALP-826).

    The real seam beats the watchdog then paces the loop at the task's cadence;
    these tests exercise the run-forever loop's *behavior* (sweeps run, errors
    are tolerated, cancellation propagates), not the pacing, so this iterates
    forever with a near-zero yield. The cadence→watchdog-bound mapping is tested
    at the supervisor level in ``test_supervisor.py``.
    """
    while True:
        yield
        await asyncio.sleep(0)


def _run_kwargs(
    session_factory: async_sessionmaker[AsyncSession],
    queries: _FakeAccountStateQueries,
) -> dict[str, Any]:
    return {
        "session_factory": session_factory,
        "trading_client_factory": lambda _mode: object(),
        "account_state_queries_factory": lambda _client: queries,
        "loop": _fast_loop,
    }


# ---------------------------------------------------------------------------
# Headline AC — recover a dropped fill
# ---------------------------------------------------------------------------


class TestBackfillRecoversDroppedFill:
    async def test_one_sweep_appends_a_dropped_fill(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # The local orders row exists (keyed by the captured broker UUID), but
        # the fill never made it to fill_records (dropped before its row
        # committed; a later fill then advanced max(fill_timestamp) past it).
        entry_uuid = str(uuid4())
        await _seed_order_row(session_factory, order_id="ORD-DROPPED-1", alpaca_order_id=entry_uuid)
        queries = _FakeAccountStateQueries(
            snapshots=[
                _order_snapshot(order_id=entry_uuid, client_order_id="inv.CMD-1.0.0"),
            ]
        )
        assert await _read_fill_records(session_factory) == []

        # Drive exactly one sweep, then cancel the run-forever loop.
        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                _config(),
                **_run_kwargs(session_factory, queries),
            )
        )
        rows = await _wait_for_rows(session_factory, expected=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        assert rows[0].order_id == "ORD-DROPPED-1"
        assert rows[0].processing_status == "unprocessed"
        # The sweep used an independent lookback `since` (NOT max-fill-timestamp).
        assert queries.calls
        first = queries.calls[0]
        assert first["status"] == "all"
        assert first["since"] is not None
        assert first["until"] is not None


# ---------------------------------------------------------------------------
# Idempotency — re-feeding an already-persisted fill is a no-op
# ---------------------------------------------------------------------------


class TestIdempotent:
    async def test_repeated_sweeps_do_not_duplicate_a_fill(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The same Alpaca order surfaces in every sweep (its terminal snapshot
        persists in the broker's get_orders window). The first sweep recovers it
        into the gap-free ``broker_event_log`` exactly once; subsequent sweeps
        over the identical snapshot collapse on the ``event_key`` PK and add no
        duplicate row — neither in the event log (the AC's named substrate) nor
        in the ``fill_records`` projection."""
        entry_uuid = str(uuid4())
        await _seed_order_row(session_factory, order_id="ORD-IDEM-1", alpaca_order_id=entry_uuid)
        # A single stable snapshot returned on EVERY get_orders call — its
        # filled_at (and thus the derived fill_timestamp) is fixed, so the
        # dedupe inputs are identical across sweeps.
        snapshot = _order_snapshot(
            order_id=entry_uuid,
            client_order_id="inv.CMD-2.0.0",
            filled_at=_now_utc(),
        )
        queries = _FakeAccountStateQueries(snapshots=[snapshot])

        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                # Tiny interval so multiple sweeps run before we cancel.
                _config(fill_backfill_interval_seconds=1),
                **_run_kwargs(session_factory, queries),
            )
        )
        # Wait for the first sweep to append, then for several more sweeps.
        await _wait_for_rows(session_factory, expected=1)
        await _wait_for_calls(queries, expected=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Many sweeps over the same snapshot → recovered exactly once.
        assert len(await _read_broker_events(session_factory)) == 1
        assert len(await _read_fill_records(session_factory)) == 1


# ---------------------------------------------------------------------------
# Queue drain — a previously-quarantined fill integrates after a sweep
# ---------------------------------------------------------------------------


class TestBackfillQuarantinesOutOfBand:
    """A backfill-recovered fill that carries no broker-carried link AND has no
    local order row (a genuinely out-of-band / manually-placed order) is parked
    on ``unattributed_fills`` — never dropped — by the shared persist path."""

    async def test_unresolved_out_of_band_fill_is_quarantined(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A fill with a link-less client_order_id and no local order row: there
        # is nothing to self-attribute it to, so it is quarantined.
        entry_uuid = str(uuid4())
        queries = _FakeAccountStateQueries(
            snapshots=[
                _order_snapshot(order_id=entry_uuid, client_order_id="out-of-band-manual"),
            ]
        )

        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                _config(),
                **_run_kwargs(session_factory, queries),
            )
        )
        # Wait for the sweep to park the fill on the queue.
        await _wait_for_queue(session_factory, expected=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1


class TestDrainsQueue:
    async def test_quarantined_fill_integrates_after_sweep(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Quarantine a fill whose order row did not exist at first sight.
        entry_uuid = str(uuid4())
        report = _fill_report(
            order_id=entry_uuid, client_order_id="inv.CMD-3.0.0", price_=150.0, qty=1.0
        )
        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)
        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].broker_fill_key == derive_broker_fill_key(report)

        # The deferred order row now exists. A sweep (which also drains the
        # queue) integrates and dequeues it. get_orders returns nothing here —
        # the integration is via the drain leg of the sweep.
        await _seed_order_row(session_factory, order_id="ORD-DRAIN-1", alpaca_order_id=entry_uuid)
        queries = _FakeAccountStateQueries(snapshots=[])

        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                _config(),
                **_run_kwargs(session_factory, queries),
            )
        )
        rows = await _wait_for_rows(session_factory, expected=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        assert rows[0].order_id == "ORD-DRAIN-1"
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []


# ---------------------------------------------------------------------------
# Run-forever loop: cancellation + sweep-error tolerance
# ---------------------------------------------------------------------------


class TestLoopLifecycle:
    async def test_clean_cancellation(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        queries = _FakeAccountStateQueries(snapshots=[])
        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                _config(),
                **_run_kwargs(session_factory, queries),
            )
        )
        await _wait_for_calls(queries, expected=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_sweep_error_does_not_crash_loop(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A sweep that raises must be swallowed; the loop continues to the
        next interval rather than propagating and crashing the task."""

        calls = {"n": 0}

        class _ExplodingQueries(_FakeAccountStateQueries):
            async def get_orders(
                self,
                *,
                status: Literal["open", "closed", "all"] = "all",
                since: datetime | None = None,
                until: datetime | None = None,
                symbols: tuple[str, ...] | None = None,
            ) -> AsyncIterator[OrderSnapshot]:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient broker outage")
                # Second sweep yields nothing, proving the loop survived.
                for _ in ():  # pragma: no cover - empty generator body
                    yield  # type: ignore[misc]

        queries = _ExplodingQueries()
        task = asyncio.create_task(
            run_fill_backfill(
                _session(),
                # Tiny interval so the second sweep follows quickly.
                _config(fill_backfill_interval_seconds=1),
                **_run_kwargs(session_factory, queries),
            )
        )
        # The loop must reach a second sweep despite the first one raising.
        await _wait_for(lambda: calls["n"] >= 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Real-time polling helpers (bound on wall clock, not iteration count)
# ---------------------------------------------------------------------------


async def _wait_for_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> list[FillRecordRow]:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    rows: list[FillRecordRow] = []
    while asyncio.get_event_loop().time() < deadline:
        rows = await _read_fill_records(session_factory)
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.01)
    return rows


async def _wait_for_queue(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as session:
            if len(await list_unattributed_fills(session)) >= expected:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"unattributed_fills never reached {expected} rows")


async def _wait_for_calls(
    queries: _FakeAccountStateQueries,
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if len(queries.calls) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"get_orders never reached {expected} calls")


async def _wait_for(predicate: Any, *, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("predicate never became true")


# ``timedelta`` kept imported for since-bound assertions in future extensions.
_ = timedelta
