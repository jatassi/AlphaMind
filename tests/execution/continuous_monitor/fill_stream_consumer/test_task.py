"""Tests for ``run_fill_stream_consumer`` (story ALP-435 / 02c).

The run-forever task composes three existing primitives:

* :func:`alphamind.execution.broker_adapter.subscribe_trade_updates` —
  yields ``FillReport`` per websocket event;
* :func:`alphamind.execution.broker_adapter.recover_missed_fills_since` —
  the GET-based recovery routine called on startup + disconnect;
* :func:`alphamind.execution.write_paths.append_fill_record` —
  durable, idempotent append-only write.

Tests inject fakes for the trading stream + trading client + session factory
so the entire path is exercised without alpaca-py / network round-trips and
without touching a real database file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
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
from alphamind._kernel.money import money, price
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter import OrderSnapshot
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.fill_stream_consumer import (
    run_fill_stream_consumer,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow
from tests.state._fk_substrate import seed_position_cluster, stub_order_row

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _config(
    *, max_reconnect_attempts: int = 5, fill_stream_stale_timeout_seconds: int = 900
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=max_reconnect_attempts,
        supervisor_shutdown_timeout_seconds=5,
        fill_stream_stale_timeout_seconds=fill_stream_stale_timeout_seconds,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T120000Z-deadbeef",
        started_at=_now_utc(),
        mode="paper",
    )


def _build_order(
    *,
    client_order_id: str = "order-1",
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


def _trade_update(
    *,
    event: str = "fill",
    order: Order | None = None,
    timestamp: datetime | None = None,
    price: float | None = 189.42,
    qty: float | None = 1.0,
) -> TradeUpdate:
    return TradeUpdate(
        event=event,
        order=order or _build_order(),
        timestamp=timestamp or _now_utc(),
        price=price,
        qty=qty,
    )


def _order_snapshot(
    *,
    order_id: str = "alp-recovery-1",
    client_order_id: str = "order-1",
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


class _FakeStream:
    """Stream stub recording the handler and parking on ``_run_forever``.

    Mirrors the fake in ``tests/execution/broker_adapter/test_fill_stream.py``
    so the ``subscribe_trade_updates`` protocol path runs without alpaca-py.
    """

    def __init__(self) -> None:
        self.handler: Any = None
        self.run_cancelled = False

    def subscribe_trade_updates(self, handler: Any) -> None:
        self.handler = handler

    async def inject(self, update: TradeUpdate) -> None:
        assert self.handler is not None
        await self.handler(update)

    async def _run_forever(self) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise


class _FakeTradingClient:
    """Sentinel; the queries factory builds the real query surface from it."""


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
    """On-disk SQLite engine with the full ORM schema and one seeded order.

    aiosqlite needs an on-disk file for cross-session visibility under
    pragma WAL; an in-memory DB would create a fresh schema in each
    connection.
    """
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    # Seed the order_id=OrderId("order-1") cluster so the fill_records.order_id FK
    # is satisfied; tests reuse this id for every injected fill.
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


async def _wait_for_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> list[FillRecordRow]:
    """Poll the fill_records table until *expected* rows exist or we time out.

    Real-time bound (not iteration-count) so we don't have to guess how many
    event-loop turns aiosqlite's executor pool consumes per query.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    rows: list[FillRecordRow] = []
    while asyncio.get_event_loop().time() < deadline:
        rows = await _read_fill_records(session_factory)
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.01)
    return rows


async def _wait_for_handler(stream: _FakeStream, *, timeout_seconds: float = 5.0) -> None:
    """Wait until the stream's handler is registered (real-time bound)."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if stream.handler is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream handler never registered")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _build_run_kwargs(
    session_factory: async_sessionmaker[AsyncSession],
    stream: _FakeStream,
    queries: _FakeAccountStateQueries,
    *,
    max_reconnect_attempts: int = 5,
) -> dict[str, Any]:
    """Shared factory wiring for every TestX.test_* case."""
    return {
        "session_factory": session_factory,
        "stream_factory": lambda _mode: stream,
        "trading_client_factory": lambda _mode: _FakeTradingClient(),
        "account_state_queries_factory": lambda _client: queries,
    }


class TestHappyPath:
    async def test_persists_fill_from_websocket_event(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-1"),
                price=189.42,
                qty=1.0,
            )
        )

        rows = await _wait_for_rows(session_factory, expected=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        (row,) = rows
        assert row.order_id == "order-1"
        # ALP-462 — the fill_price column is ``Numeric`` (Decimal); compare
        # against the canonical ``Decimal('189.42')`` representation.
        assert row.fill_price == Decimal("189.42")
        assert row.fill_quantity == pytest.approx(1.0)
        assert row.processing_status == "unprocessed"

    async def test_skips_recovery_on_fresh_db(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Fresh DB → no prior fills → startup recovery is NOT invoked."""
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Recovery is gated on prior fills; an empty DB must not trigger it.
        assert queries.calls == []


class TestStartupRecovery:
    async def test_invokes_recovery_when_prior_fills_exist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Seed a prior fill so the startup-recovery path activates with
        # ``since=<prior fill timestamp>``.
        prior_ts = _now_utc() - timedelta(minutes=10)
        await _seed_prior_fill(session_factory, fill_timestamp=prior_ts)

        stream = _FakeStream()
        recovered_ts = _now_utc() - timedelta(minutes=5)
        queries = _FakeAccountStateQueries(
            snapshots=[
                _order_snapshot(
                    order_id=OrderId("alp-recovery-1"),
                    client_order_id="order-1",
                    filled_avg_price=99.0,
                    filled_qty=1.0,
                    filled_at=recovered_ts,
                ),
            ]
        )

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        # The recovery path adds one extra fill_records row beyond the
        # seeded prior; wait for it.
        rows = await _wait_for_rows(session_factory, expected=2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # get_orders was invoked once with ``since`` matching the seeded
        # prior fill's timestamp.
        assert len(queries.calls) >= 1
        first_call = queries.calls[0]
        assert first_call["status"] == "all"
        assert first_call["since"] is not None
        # Allow microsecond-level isoformat round-trip drift but require
        # the same UTC second.
        assert first_call["since"].replace(microsecond=0) == prior_ts.replace(microsecond=0)
        # The recovered fill landed alongside the seeded one.
        assert len(rows) == 2

    async def test_recovery_appends_only_the_residual_gap_over_logged_partials(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """FS1 wiring — the consumer's REST-recovery path passes ``recovered=True``
        so a cumulative snapshot reconciles to the residual gap over what the live
        websocket already logged. A 60-share partial is on ``broker_event_log``;
        startup recovery sees the order's cumulative 100 → ONE 40-share residual
        lands, not a full-100 duplicate, so the 03c fold totals 100 exactly once.

        Were ``recovered=True`` dropped at the caller, the cumulative would append
        as a second 100-share FILL and the logged total would be 160."""
        order_uuid = uuid4()
        # A live 60-share partial, logged per-event into broker_event_log AND
        # fill_records (the latter activates startup recovery via its timestamp).
        prior_ts = _now_utc() - timedelta(minutes=10)
        partial = _report_from_order(
            _build_order(
                client_order_id="order-1",
                order_id=order_uuid,
                qty="100",
                filled_qty="60",
                status=AlpacaOrderStatus.PARTIALLY_FILLED,
            ),
            event="partial_fill",
            price=189.42,
            qty=60.0,
        )
        partial = partial.model_copy(update={"fill_timestamp": prior_ts})
        await persist_fill_report(
            partial, session_factory=session_factory, enrichment_callable=None
        )

        # Startup recovery sees the order's CUMULATIVE state (100 filled @ avg)
        # for the SAME alpaca_order_id.
        recovered_ts = _now_utc() - timedelta(minutes=5)
        stream = _FakeStream()
        queries = _FakeAccountStateQueries(
            snapshots=[
                _order_snapshot(
                    order_id=str(order_uuid),
                    client_order_id="order-1",
                    filled_avg_price=189.50,
                    filled_qty=100.0,
                    filled_at=recovered_ts,
                ),
            ]
        )

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        # The partial (1) + the 40-share residual (2) — wait for the second.
        rows = await _wait_for_event_log(session_factory, expected=2)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        fills = [r for r in rows if r.event_type == "FILL"]
        total = sum(float(json.loads(r.raw_payload_json)["fill_quantity"]) for r in fills)
        assert total == 100.0, "recovery must append only the residual gap, not the cumulative"
        residuals = [
            r for r in fills if float(json.loads(r.raw_payload_json)["fill_quantity"]) == 40.0
        ]
        assert len(residuals) == 1


class TestDisconnect:
    async def test_websocket_failure_triggers_recovery_then_reconnect(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Translation error in the stream generator triggers recovery + retry.

        The :func:`subscribe_trade_updates` primitive propagates translation
        errors to the consumer (per ``test_translation_exception_propagates_to_consumer``)
        but suppresses websocket-level exceptions because alpaca-py reconnects
        internally. Either path lands in the task's try/except — the test
        simulates the case the primitive surfaces.

        Two stream instances simulate the disconnect lifecycle: the first
        receives a malformed payload that propagates an :class:`AttributeError`
        out of the generator; the second mints a fresh handler and accepts a
        live event. Both share the queries fake so the recovery invocation
        count is observable.
        """
        # No real backoff sleep — keep the test fast.
        monkeypatch.setattr(
            "alphamind.execution.continuous_monitor.fill_stream_consumer.task._backoff_seconds",
            lambda _attempt: 0.0,
        )

        # Seed a prior fill so the disconnect's recovery call has a non-None
        # ``since`` to use. (Recovery is a no-op on a fresh DB by design.)
        prior_ts = _now_utc() - timedelta(minutes=30)
        await _seed_prior_fill(session_factory, fill_timestamp=prior_ts)

        stream_a = _FakeStream()
        stream_b = _FakeStream()
        streams = iter([stream_a, stream_b])
        queries = _FakeAccountStateQueries()

        kwargs = _build_run_kwargs(session_factory, stream_a, queries)
        # Override stream_factory to return the iterator's next stream.
        kwargs["stream_factory"] = lambda _mode: next(streams)

        task = asyncio.create_task(run_fill_stream_consumer(_session(), _config(), **kwargs))

        # Wait for the first stream's handler, then inject a malformed
        # payload to force a translation error in the primitive.
        await _wait_for_handler(stream_a)

        class _Broken:
            pass

        await stream_a.inject(_Broken())  # type: ignore[arg-type]

        # Wait for the second stream's handler to register — that confirms
        # reconnect happened after the recovery + backoff path.
        await _wait_for_handler(stream_b)

        await stream_b.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-1"),
                price=50.0,
                qty=1.0,
            )
        )

        # Seeded prior + post-disconnect live fill = 2 rows.
        rows = await _wait_for_rows(session_factory, expected=2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 2
        # Recovery routine was invoked on the disconnect path — at least
        # the startup call AND the post-failure call (both with non-None
        # ``since`` derived from the seeded prior).
        assert len(queries.calls) >= 2

    async def test_budget_exhaustion_propagates_exception(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When max_reconnect_attempts is exhausted, the task raises."""
        monkeypatch.setattr(
            "alphamind.execution.continuous_monitor.fill_stream_consumer.task._backoff_seconds",
            lambda _attempt: 0.0,
        )

        # Stream factory returns a fresh stream on each reconnect; a
        # parallel poisoner injects a malformed payload as soon as each
        # stream's handler is registered, forcing the consumer's loop to
        # propagate an ``AttributeError`` and consume one reconnect attempt.
        streams_used: list[_FakeStream] = []

        def stream_factory(_mode: str) -> _FakeStream:
            s = _FakeStream()
            streams_used.append(s)
            return s

        async def poisoner() -> None:
            """Push a malformed event into each new stream as it appears."""

            class _Broken:
                pass

            last_seen = 0
            while True:
                if len(streams_used) > last_seen:
                    fresh = streams_used[last_seen]
                    # Wait until the stream's handler is registered.
                    for _ in range(50):
                        if fresh.handler is not None:
                            break
                        await asyncio.sleep(0.01)
                    if fresh.handler is not None:
                        await fresh.inject(_Broken())  # type: ignore[arg-type]
                    last_seen += 1
                await asyncio.sleep(0.01)

        queries = _FakeAccountStateQueries()
        kwargs = _build_run_kwargs(session_factory, _FakeStream(), queries)
        kwargs["stream_factory"] = stream_factory

        poisoner_task = asyncio.create_task(poisoner())
        try:
            with pytest.raises(AttributeError):
                await run_fill_stream_consumer(
                    _session(), _config(max_reconnect_attempts=2), **kwargs
                )
        finally:
            poisoner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poisoner_task


class TestCancellation:
    async def test_cancellation_exits_cleanly(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # On cancellation, the underlying stream's ``_run_forever`` task
        # must have been cancelled too — the subscribe_trade_updates
        # generator's cleanup path takes care of this.
        assert stream.run_cancelled


class TestIdempotency:
    async def test_duplicate_fill_does_not_create_second_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Same FillReport delivered twice yields exactly one fill_records row.

        ``append_fill_record`` is idempotent on the natural UNIQUE constraint
        ``(order_id, fill_timestamp, fill_quantity, fill_price)`` — the
        translator's deterministic ``fill_id`` derivation backstops this so
        the primary key also matches on retry.
        """
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)

        order = _build_order(client_order_id="order-1")
        ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
        update_a = _trade_update(event="fill", order=order, timestamp=ts, price=189.42, qty=1.0)
        update_b = _trade_update(event="fill", order=order, timestamp=ts, price=189.42, qty=1.0)
        await stream.inject(update_a)
        await stream.inject(update_b)

        # Give the consumer plenty of time to drain both events; expected=1
        # is intentional — we want to prove the dedupe held.
        rows = await _wait_for_rows(session_factory, expected=1)
        # Pause a little longer to let any second insert that *would* have
        # happened actually happen, then re-read.
        await asyncio.sleep(0.05)
        rows = await _read_fill_records(session_factory)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1


class TestEnrichmentCallable:
    """Story ALP-528 — ``enrichment_callable`` wedge wires in paper mode only.

    Two cases:

    * Default ``enrichment_callable=None`` (live mode): the record reaches
      ``append_fill_record`` with ``live_execution_estimate IS NULL`` — same
      shape as before the wedge landed.
    * Fake callable (paper mode): the record reaches ``append_fill_record``
      with ``live_execution_estimate`` non-None, populated by the wedge.
    """

    async def test_enrichment_callable_none_persists_null_estimate(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
                enrichment_callable=None,
            )
        )

        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-1"),
                price=189.42,
                qty=1.0,
            )
        )

        rows = await _wait_for_rows(session_factory, expected=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        (row,) = rows
        assert row.live_execution_estimate_json is None

    async def test_enrichment_callable_populates_estimate(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from alphamind.portfolio_state.records.positions import LiveExecutionEstimate
        from alphamind.state.records import FillRecord

        async def fake_enrichment(record: FillRecord) -> FillRecord:
            return record.model_copy(
                update={
                    "live_execution_estimate": LiveExecutionEstimate(
                        estimated_spread_usd=money("0.01"),
                        estimated_impact_usd=money("0.02"),
                        estimated_regulatory_fees_usd=money("0.03"),
                        live_adjusted_fill_price=price("189.50"),
                    )
                }
            )

        stream = _FakeStream()
        queries = _FakeAccountStateQueries()

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
                enrichment_callable=fake_enrichment,
            )
        )

        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-1"),
                price=189.42,
                qty=1.0,
            )
        )

        rows = await _wait_for_rows(session_factory, expected=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        (row,) = rows
        assert row.live_execution_estimate_json is not None


async def _seed_prior_fill(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fill_timestamp: datetime,
) -> None:
    """Insert one fill_records row so the startup-recovery path activates."""
    from alphamind.portfolio_state.records.orders import OrderStatus
    from alphamind.state.records import (
        FillProcessingStatus,
        FillRecord,
    )
    from alphamind.state.tables.fill_records_codec import (
        record_to_row,
    )

    record = FillRecord(
        fill_id="prior-fill-1",
        order_id=OrderId("order-1"),
        fill_timestamp=fill_timestamp,
        fill_price=price(100.0),
        fill_quantity=1.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=None,
        fees_usd=money(0.0),
        execution_venue=None,
        gateway_reference="alp-prior",
        persistence_timestamp=fill_timestamp,
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )
    async with session_factory() as db:
        db.add(record_to_row(record))
        await db.commit()


# ---------------------------------------------------------------------------
# Terminal non-fill status sync (ALP-739)
# ---------------------------------------------------------------------------


async def _set_order_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    status: str,
) -> None:
    async with session_factory() as session:
        row = await session.get(OrderRow, order_id)
        assert row is not None
        row.status = status
        await session.commit()


async def _wait_for_event_log(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> list[BrokerEventLogRow]:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    rows: list[BrokerEventLogRow] = []
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as session:
            rows = list((await session.execute(select(BrokerEventLogRow))).scalars().all())
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.01)
    return rows


class TestTerminalStatusEventLog:
    """ALP-849 / W1c — a broker terminal non-fill event lands as an append-only
    ``TERMINAL_ORDER_STATUS`` event; the monitor does NOT RMW ``orders.status``
    (invariant 1: one writer per fact)."""

    async def test_expired_event_appends_terminal_event_and_leaves_status(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # The seeded entry is FILLED by default; reset to PENDING so it is a
        # plausible zero-fill terminal source. Its status must NOT change.
        await _set_order_status(session_factory, order_id="order-1", status="PENDING")

        stream = _FakeStream()
        queries = _FakeAccountStateQueries()
        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(
                event="expired",
                order=_build_order(
                    client_order_id="order-1",
                    qty="1",
                    filled_qty="0",
                    status=AlpacaOrderStatus.EXPIRED,
                ),
            )
        )

        rows = await _wait_for_event_log(session_factory, expected=1)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert [r.event_type for r in rows] == ["TERMINAL_ORDER_STATUS"]
        # Attribution rides the resolved order's position edge (thesis-1/pos-1).
        assert rows[0].thesis_id == "thesis-1"
        assert rows[0].position_id == "pos-1"
        # A terminal non-fill event appends no fill_records row.
        assert await _read_fill_records(session_factory) == []
        # Invariant 1: the monitor never RMW'd the shared orders row.
        status, _ = await _status_and_ts(session_factory, "order-1")
        assert status == "PENDING"

    async def test_partial_fill_then_expire_appends_no_event(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A partially-filled-then-expired entry appends NO terminal event: the
        # fill path + fill collection own filled_quantity and integrate the partials.
        await _set_order_status(session_factory, order_id="order-1", status="PENDING")
        # A second PENDING entry expires cleanly (zero fill) and acts as a
        # processing barrier: once its event lands, the earlier one is drained.
        async with session_factory() as db:
            db.add(stub_order_row("order-2", "bracket-1", position_id="pos-1", status="PENDING"))
            await db.commit()

        stream = _FakeStream()
        queries = _FakeAccountStateQueries()
        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **_build_run_kwargs(session_factory, stream, queries),
            )
        )

        await _wait_for_handler(stream)
        # order-1: expired with a non-zero cumulative fill → gated out.
        await stream.inject(
            _trade_update(
                event="expired",
                order=_build_order(
                    client_order_id="order-1",
                    qty="10",
                    filled_qty="3",
                    status=AlpacaOrderStatus.EXPIRED,
                ),
            )
        )
        # order-2: clean zero-fill expire → appends the terminal event (barrier).
        await stream.inject(
            _trade_update(
                event="expired",
                order=_build_order(
                    client_order_id="order-2",
                    qty="1",
                    filled_qty="0",
                    status=AlpacaOrderStatus.EXPIRED,
                ),
            )
        )

        rows = await _wait_for_event_log(session_factory, expected=1)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Exactly one terminal event — order-2's. order-1 (partial) appended none.
        assert len(rows) == 1
        payload = rows[0].raw_payload_json
        assert "order-2" in payload
        assert "order-1" not in payload


async def _status_and_ts(
    session_factory: async_sessionmaker[AsyncSession], order_id: str
) -> tuple[str, str]:
    async with session_factory() as session:
        row = await session.get(OrderRow, order_id)
        assert row is not None
        return row.status, row.last_update_timestamp


# ---------------------------------------------------------------------------
# ALP-746 — resolve the local orders row by captured broker UUID when the
# client_order_id doesn't name a local PK (native-bracket protective children,
# OCO sibling-cancels, and equity entries whose client_order_id is the command
# id).
# ---------------------------------------------------------------------------


async def _seed_leg_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    role: str,
    status: str,
    alpaca_order_id: str,
    client_order_id: str | None = None,
) -> None:
    """Add a protective-leg / entry order to the seeded bracket-1 cluster."""
    async with session_factory() as session:
        session.add(
            stub_order_row(
                order_id,
                "bracket-1",
                position_id="pos-1",
                role=role,
                direction="SELL",
                status=status,
                alpaca_order_id=alpaca_order_id,
                client_order_id=client_order_id,
            )
        )
        await session.commit()


def _report_from_order(order: Order, *, event: str, price: float | None, qty: float | None) -> Any:
    """Translate a single alpaca order event into its FillReport (equity → one)."""
    reports = translate_trade_update(_trade_update(event=event, order=order, price=price, qty=qty))
    assert len(reports) == 1
    return reports[0]


class TestUuidResolution:
    async def test_bracket_protective_fill_resolves_to_leg_row_by_uuid(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A take-profit fill arrives keyed on the child's Alpaca UUID with an
        Alpaca-generated client_order_id; it resolves to the local leg row whose
        captured ``alpaca_order_id`` matches, and the fill_records row carries
        the OMS leg id (so fill collection integrates it)."""
        tp_uuid = uuid4()
        await _seed_leg_order(
            session_factory,
            order_id="ORD-NVDA-target-1",
            role="TAKE_PROFIT",
            status="PENDING",
            alpaca_order_id=str(tp_uuid),
        )
        report = _report_from_order(
            _build_order(order_id=tp_uuid, client_order_id="alpaca-generated-child-tp"),
            event="fill",
            price=200.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-NVDA-target-1"
        # The broker UUID is preserved on gateway_reference for reconciliation.
        assert rows[0].gateway_reference == str(tp_uuid)

    async def test_oco_sibling_cancel_resolves_to_leg_row_by_uuid(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When the take-profit fills, Alpaca OCO-cancels the price-stop sibling;
        that ``canceled`` event (Alpaca client_order_id, zero fills) resolves to
        the local stop row by captured UUID and appends a TERMINAL_ORDER_STATUS
        event (attributing via the leg's position edge) — without RMW-ing the
        shared orders row (invariant 1)."""
        sl_uuid = uuid4()
        await _seed_leg_order(
            session_factory,
            order_id="ORD-NVDA-inv0-1",
            role="PRICE_STOP",
            status="PENDING",
            alpaca_order_id=str(sl_uuid),
        )
        report = _report_from_order(
            _build_order(
                order_id=sl_uuid,
                client_order_id="alpaca-generated-child-sl",
                qty="1",
                filled_qty="0",
                status=AlpacaOrderStatus.CANCELED,
            ),
            event="canceled",
            price=None,
            qty=None,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _wait_for_event_log(session_factory, expected=1)
        assert [r.event_type for r in rows] == ["TERMINAL_ORDER_STATUS"]
        assert rows[0].position_id == "pos-1"
        # Invariant 1: the leg row's status is left untouched by the monitor.
        status, _ = await _status_and_ts(session_factory, "ORD-NVDA-inv0-1")
        assert status == "PENDING"
        # A cancel appends no fill record.
        assert await _read_fill_records(session_factory) == []

    async def test_entry_fill_resolves_by_uuid_when_client_order_id_is_command_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An entry fill's client_order_id is the command id (not the OMS PK);
        resolution falls back to the captured entry UUID to find the row."""
        entry_uuid = uuid4()
        await _seed_leg_order(
            session_factory,
            order_id="ORD-NVDA-entry-1",
            role="ENTRY",
            status="PENDING",
            alpaca_order_id=str(entry_uuid),
        )
        report = _report_from_order(
            _build_order(order_id=entry_uuid, client_order_id="inv-20260529.ENV-REC-1.0.0"),
            event="fill",
            price=150.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-NVDA-entry-1"

    async def test_in_window_fill_resolves_to_pending_submit_row_by_client_order_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """ALP-836 — a fast fill arriving in the submit→backfill window resolves to
        the durable pre-committed row by ``client_order_id`` even though the row
        still carries the synthetic placeholder (real ``alpaca_order_id`` not yet
        backfilled), so the fill attributes instead of stranding."""
        command_id = "inv-20260603.ENV-SA-1.0.0"
        real_uuid = uuid4()
        # Pre-committed row: PENDING_SUBMIT, client_order_id set, synthetic alpaca
        # id (the real broker UUID has NOT been backfilled yet).
        await _seed_leg_order(
            session_factory,
            order_id="ORD-CLOSE-pos-1-1",
            role="CLOSE",
            status="PENDING_SUBMIT",
            alpaca_order_id="alp-ORD-CLOSE-pos-1-1",
            client_order_id=command_id,
        )
        # The fill carries the command_id as its client_order_id and the real
        # broker UUID — the UUID lookup would miss the synthetic placeholder.
        report = _report_from_order(
            _build_order(order_id=real_uuid, client_order_id=command_id),
            event="fill",
            price=150.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-CLOSE-pos-1-1"

    async def test_fill_for_unknown_order_is_quarantined_not_dropped(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A fill that matches no local order by PK or UUID is never dropped
        (ALP-763): it is parked on the ``unattributed_fills`` queue and alerted,
        not inserted into ``fill_records`` (the FK would reject it there)."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        report = _report_from_order(
            _build_order(order_id=uuid4(), client_order_id="totally-unknown"),
            event="fill",
            price=200.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].alerted is True


# ---------------------------------------------------------------------------
# Connected-but-silent stream recovery (ALP-819)
# ---------------------------------------------------------------------------


class _StepClock:
    """Monotonic substitute advancing ``step`` seconds per call (ALP-819).

    Lets the staleness branch trip deterministically without real sleeps —
    every read jumps forward, so the elapsed-since-last-frame comparison
    crosses ``fill_stream_stale_timeout_seconds`` on the first poll.
    """

    def __init__(self, step: float) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


async def _wait_for_count(items: list[Any], *, at_least: int, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if len(items) >= at_least:
            return
        await asyncio.sleep(0.01)


class TestSilentStreamRecovery:
    async def test_rth_silent_stream_reconnects_without_consuming_budget(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """ALP-819: a connected stream that never delivers a frame and never
        raises is detected during RTH and forces a reconnect — and that
        reconnect is budget-neutral, so a persistently silent stream keeps
        recovering instead of exhausting ``max_reconnect_attempts`` and exiting.

        Each ``_FakeStream`` parks on ``_run_forever`` (alive, no exception)
        and receives no frame — the exact library-internal-reconnect signature
        the ALP-768 done-callback never catches.
        """
        streams_built: list[_FakeStream] = []

        def stream_factory(_mode: str) -> _FakeStream:
            stream = _FakeStream()
            streams_built.append(stream)
            return stream

        beats: list[int] = []
        queries = _FakeAccountStateQueries()
        kwargs = _build_run_kwargs(session_factory, _FakeStream(), queries)
        kwargs["stream_factory"] = stream_factory

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                # Explicit 60s timeout (the floor) so the test does not depend on
                # the config default; _StepClock(1000) exceeds it on every poll.
                _config(max_reconnect_attempts=2, fill_stream_stale_timeout_seconds=60),
                **kwargs,
                is_market_open=lambda _at: True,
                beat=lambda: beats.append(1),
                monotonic=_StepClock(1000.0),
                stream_poll_interval=0.01,
            )
        )

        # Three reconnects exceeds the 2-attempt reconnect budget — only
        # possible if a stale-driven reconnect does NOT count as a failure.
        await _wait_for_count(streams_built, at_least=3)

        assert not task.done()  # budget never exhausted
        assert len(streams_built) >= 3
        assert beats  # watchdog heartbeats fired while the stream was silent

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_off_hours_silent_stream_does_not_reconnect(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Outside RTH a silent stream must NOT churn reconnects — fills are
        legitimately sparse off-hours. The single connected stream stays put.
        """
        streams_built: list[_FakeStream] = []

        def stream_factory(_mode: str) -> _FakeStream:
            stream = _FakeStream()
            streams_built.append(stream)
            return stream

        queries = _FakeAccountStateQueries()
        kwargs = _build_run_kwargs(session_factory, _FakeStream(), queries)
        kwargs["stream_factory"] = stream_factory

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(fill_stream_stale_timeout_seconds=60),
                **kwargs,
                is_market_open=lambda _at: False,  # market closed
                monotonic=_StepClock(1000.0),
                stream_poll_interval=0.01,
            )
        )

        # Let many poll intervals elapse; no stale-driven reconnect must occur.
        await asyncio.sleep(0.1)

        assert len(streams_built) == 1
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Watchdog binding via 01a's per-cadence seam (ALP-828)
# ---------------------------------------------------------------------------


class _SupervisorClock:
    """Injectable monotonic + sleep for the supervisor (sanctioned clock fake).

    ``monotonic`` returns accumulated virtual time; ``sleep`` cooperatively
    yields then advances it, exactly like the supervisor's own test clock, so
    the watchdog can be tripped at an exact virtual elapsed time.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(0)
        self._now += delay

    def advance(self, delay: float) -> None:
        self._now += delay


class TestWatchdogBinding:
    """The fill consumer adopts 01a's heartbeat seam (ALP-828).

    Proves the consumer DECLARES its poll cadence to the watchdog (so a stall
    bound derives) and BEATS through ``supervisor.beat`` — exercised through
    the real ``run_fill_stream_consumer`` body, which owns the binding. The
    test does not call ``register_watch`` itself; it asserts the consumer does.
    """

    async def test_consumer_binds_poll_cadence_and_beats_via_supervisor_seam(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

        clock = _SupervisorClock()
        supervisor = MonitorSupervisor(
            session=_session(),
            config=_config(),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        stream = _FakeStream()
        queries = _FakeAccountStateQueries()
        kwargs = _build_run_kwargs(session_factory, stream, queries)

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **kwargs,
                beat=lambda: supervisor.beat("fill_stream_consumer"),
                register_watch=lambda cadence: supervisor.register_watch(
                    "fill_stream_consumer", cadence
                ),
                stream_poll_interval=2.0,
            )
        )

        await _wait_for_handler(stream)
        # Inject a fill so the consumer beats at least once through the kernel.
        await stream.inject(
            _trade_update(event="fill", order=_build_order(client_order_id="order-1"))
        )
        await _wait_for_rows(session_factory, expected=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        entry = supervisor._watch["fill_stream_consumer"]
        # Bound derives from the declared poll cadence (2.0s) * the multiplier
        # (10.0) — NOT a zero bound, which the watchdog could never trip.
        assert entry.watched is True
        assert entry.bound_seconds == pytest.approx(2.0 * 10.0)
        # The consumer beat through the supervisor seam (last_beat is set).
        assert entry.last_beat is not None

    async def test_starved_consumer_trips_the_watchdog(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from unittest import mock

        from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

        clock = _SupervisorClock()
        supervisor = MonitorSupervisor(
            session=_session(),
            config=_config(),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        supervisor._stop_event = asyncio.Event()

        stream = _FakeStream()
        queries = _FakeAccountStateQueries()
        kwargs = _build_run_kwargs(session_factory, stream, queries)

        task = asyncio.create_task(
            run_fill_stream_consumer(
                _session(),
                _config(),
                **kwargs,
                beat=lambda: supervisor.beat("fill_stream_consumer"),
                register_watch=lambda cadence: supervisor.register_watch(
                    "fill_stream_consumer", cadence
                ),
                stream_poll_interval=2.0,
            )
        )

        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(event="fill", order=_build_order(client_order_id="order-1"))
        )
        await _wait_for_rows(session_factory, expected=1)

        # Freeze the consumer: it can no longer beat (a genuinely-blocked
        # consumer). Cancel its task so no further beats arrive, then advance
        # the clock past its derived stall bound (2.0s * 10.0 = 20s).
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        clock.advance(25.0)

        exits: list[int] = []
        passes = 0

        async def _one_pass(_delay: float) -> None:
            # Let exactly one watchdog check pass run, then cancel the loop.
            nonlocal passes
            passes += 1
            if passes > 1:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        supervisor._sleep = _one_pass
        with (
            mock.patch(
                "alphamind.execution.continuous_monitor.supervisor.os._exit",
                side_effect=exits.append,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await supervisor._watchdog_loop()

        # The starved fill consumer (beat then silence past its bound) tripped
        # os._exit(1) → NSSM restart, exactly as ALP-819 required.
        assert exits == [1]


# ---------------------------------------------------------------------------
# ALP-942 Scope C — persist failures degrade, they do not crash the consumer
# ---------------------------------------------------------------------------


class TestPersistFailureTolerance:
    """A transient SQLite lock error escaping persist (retry budget spent) must
    not unwind ``run_fill_stream_consumer`` — the 2026-06-09 17:00:05Z monitor
    crash propagated exactly this error from ``_replay_recovery`` through the
    supervisor TaskGroup and took the whole process down."""

    async def test_replay_recovery_persist_failure_does_not_crash_consumer(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from tests.execution.continuous_monitor.fill_stream_consumer._flaky_commit import (
            flaky_commit_factory,
        )

        ts = _now_utc() - timedelta(minutes=5)
        await _seed_prior_fill(session_factory, fill_timestamp=ts)
        # Recovery yields one report whose persist commit ALWAYS fails with the
        # transient lock error — the write unit's retry budget is spent and the
        # OperationalError escapes persist_fill_report.
        queries = _FakeAccountStateQueries(
            [_order_snapshot(filled_at=_now_utc(), filled_qty=2.0)]
        )
        stream = _FakeStream()
        flaky, _counters = flaky_commit_factory(session_factory, failures=1_000)
        kwargs = _build_run_kwargs(session_factory, stream, queries)
        kwargs["session_factory"] = flaky

        task = asyncio.create_task(
            run_fill_stream_consumer(_session(), _config(), **kwargs)
        )

        # The consumer must survive the failed replay and reach the stream
        # (registering its handler proves the cycle continued past recovery).
        await _wait_for_handler(stream, timeout_seconds=15.0)
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_live_consume_loop_drops_report_on_transient_lock_and_continues(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from tests.execution.continuous_monitor.fill_stream_consumer._flaky_commit import (
            flaky_commit_factory,
        )

        stream = _FakeStream()
        queries = _FakeAccountStateQueries()
        # Exactly 5 failing commits: report A's write unit exhausts its retry
        # budget (5 attempts) and the transient error escapes persist; report B
        # then persists normally on the same consumer connection cycle.
        flaky, counters = flaky_commit_factory(session_factory, failures=5)
        kwargs = _build_run_kwargs(session_factory, stream, queries)
        kwargs["session_factory"] = flaky

        task = asyncio.create_task(
            run_fill_stream_consumer(_session(), _config(), **kwargs)
        )

        await _wait_for_handler(stream)
        ts = _now_utc()
        await stream.inject(
            _trade_update(
                event="partial_fill",
                order=_build_order(client_order_id="order-1", filled_qty="1"),
                timestamp=ts,
                price=189.42,
                qty=1.0,
            )
        )
        await stream.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-1", filled_qty="2"),
                timestamp=ts + timedelta(seconds=1),
                price=190.00,
                qty=1.0,
            )
        )

        # Report A was dropped (transient, degrade-don't-crash); report B landed.
        rows = await _wait_for_rows(session_factory, expected=1, timeout_seconds=15.0)
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rows) == 1
        assert float(rows[0].fill_price) == 190.00
        assert counters["commits"] == 1
