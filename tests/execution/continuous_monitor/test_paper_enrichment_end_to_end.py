"""End-to-end paper-enrichment wedge integration test (ALP-534).

The seam tests cover the contract surfaces individually:

* ``fill_stream_consumer/test_task.py::TestEnrichmentCallable`` exercises
  ``persist_fill_report`` with a fake ``enrichment_callable``.
* ``test_paper_enrichment_wiring.py`` exercises ``_build_enrichment_callable``
  + production adapters with no live consumer running.

Neither plumbs a real wedge end-to-end. This module composes the production
``SqlOrderLookup`` + ``SqlAdvLookup`` + ``MapVolLookup`` via
:func:`_build_enrichment_callable`, runs the full fill-stream consumer task
against a fake stream, and asserts the persisted ``fill_records`` row carries
a non-NULL ``live_execution_estimate_json`` — catching wiring regressions that
pass each seam test individually.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import (
    FeeSchedule,
    PaperHarness,
)
from alphamind.config.models.execution import (
    OrderType as HarnessOrderType,
)
from alphamind.execution.broker_adapter import OrderSnapshot
from alphamind.execution.continuous_monitor.__main__ import (
    _build_enrichment_callable,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer import (
    run_fill_stream_consumer,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.persistence.models import AssetUniverse, Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry
from alphamind.state.tables.fill_records import FillRecordRow
from tests.state._fk_substrate import (
    stub_bracket_row,
    stub_order_row,
    stub_position_row,
    stub_thesis_row,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260518T120000Z-deadbeef",
        started_at=_now_utc(),
        mode="paper",
    )


def _harness_config() -> PaperHarness:
    return PaperHarness(
        spread_buffer_pct=10,
        impact_coefficients={
            HarnessOrderType.market: 0.5,
            HarnessOrderType.limit: 0.25,
            HarnessOrderType.stop: 0.75,
        },
        fee_schedule=FeeSchedule(
            cat_per_executed_share=0.000166,
            taf_per_share_sells=0.000145,
            sec_pct_of_notional_sells=0.0000080,
            orf_per_options_contract=0.02188,
            occ_per_options_contract=0.02,
        ),
    )


def _build_order(
    *,
    client_order_id: str = "order-aapl",
    order_id: UUID | None = None,
    qty: str = "100",
    filled_qty: str = "100",
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
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
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
    price: float = 200.0,
    qty: float = 100.0,
) -> TradeUpdate:
    return TradeUpdate(
        event=event,
        order=order or _build_order(),
        timestamp=timestamp or _now_utc(),
        price=price,
        qty=qty,
    )


class _FakeStream:
    def __init__(self) -> None:
        self.handler: Any = None

    async def subscribe_trade_updates(self, handler: Any) -> None:
        self.handler = handler

    async def inject(self, update: TradeUpdate) -> None:
        assert self.handler is not None
        await self.handler(update)

    async def _run_forever(self) -> None:
        await asyncio.Future()


class _FakeTradingClient:
    pass


class _FakeAccountStateQueries:
    def __init__(self) -> None:
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
        if False:
            yield  # pragma: no cover — empty generator


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite with full ORM schema; seeded with the full E2E substrate.

    Seeds:

    * ``asset_universe`` row for AAPL with a realistic ADV (50M shares).
    * Position / thesis / bracket / order cluster with ``order_id="order-aapl"``
      and ``instrument_spec_json`` referencing AAPL — so ``SqlOrderLookup``
      returns ``OrderAttributes(ticker_or_underlying="AAPL", ...)`` and the
      ``fill_records.order_id`` FK is satisfied.

    aiosqlite needs an on-disk file for cross-session visibility — see the
    rationale in ``fill_stream_consumer/test_task.py::session_factory``.
    """
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401 — register ORM mappings

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(
            AssetUniverse(
                asset_id="asset-aapl",
                ticker="AAPL",
                full_name="Apple Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                avg_daily_volume_shares=50_000_000,
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-05-18T00:00:00Z",
            )
        )
        # Position cluster keyed on order_id="order-aapl" with an AAPL
        # equity instrument_spec (the stub helper's default ticker is "STUB").
        position_id = PositionId("pos-aapl")
        thesis_id = ThesisId("thesis-aapl")
        bracket_id = BracketId("bracket-aapl")
        entry_order_id = OrderId("order-aapl")
        sess.add(stub_position_row(position_id, thesis_id=thesis_id, bracket_id=bracket_id))
        sess.add(stub_thesis_row(thesis_id, position_id))
        order = stub_order_row(entry_order_id, bracket_id, position_id=position_id)
        order.instrument_spec_json = json.dumps({"instrument_type": "EQUITY", "ticker": "AAPL"})
        sess.add(order)
        sess.add(stub_bracket_row(bracket_id, position_id, entry_order_id))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


async def _wait_for_handler(stream: _FakeStream, *, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if stream.handler is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream handler never registered")


async def _wait_for_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expected: int,
    timeout_seconds: float = 5.0,
) -> list[FillRecordRow]:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    rows: list[FillRecordRow] = []
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as session:
            result = await session.execute(select(FillRecordRow))
            rows = list(result.scalars().all())
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.01)
    return rows


async def test_paper_wedge_populates_live_execution_estimate_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Production-wedge plumbing: real lookups → harness → persisted row.

    Constructs the enrichment callable via ``_build_enrichment_callable`` —
    the same composition the daemon entrypoint uses — with paper mode, the
    seeded session factory, and a realized-vol map containing AAPL. Runs
    ``run_fill_stream_consumer`` end-to-end, injects one fill, and asserts
    the persisted row's ``live_execution_estimate_json`` is non-NULL and the
    payload carries the four expected fields.
    """
    realized_vol_map: dict[str, RealizedVolEntry] = {
        "AAPL": RealizedVolEntry(underlying="AAPL", trailing_30d_realized_vol=0.20),
    }
    enrichment_callable = _build_enrichment_callable(
        mode="paper",
        paper_harness=_harness_config(),
        session_factory=session_factory,
        realized_vol_map=realized_vol_map,
    )
    assert enrichment_callable is not None, "paper mode must produce a callable"

    stream = _FakeStream()
    queries = _FakeAccountStateQueries()

    task = asyncio.create_task(
        run_fill_stream_consumer(
            _session(),
            _config(),
            session_factory=session_factory,
            stream_factory=lambda _mode: stream,
            trading_client_factory=lambda _mode: _FakeTradingClient(),
            account_state_queries_factory=lambda _client: queries,
            enrichment_callable=enrichment_callable,
        )
    )

    try:
        await _wait_for_handler(stream)
        await stream.inject(
            _trade_update(
                event="fill",
                order=_build_order(client_order_id="order-aapl"),
                price=200.0,
                qty=100.0,
            )
        )

        rows = await _wait_for_rows(session_factory, expected=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(rows) == 1
    (row,) = rows
    assert row.order_id == "order-aapl"
    assert row.live_execution_estimate_json is not None, (
        "wedge plumbing must populate live_execution_estimate_json end-to-end"
    )
    payload = json.loads(row.live_execution_estimate_json)
    # Four canonical fields per LiveExecutionEstimate; non-zero on AAPL-shape
    # input (price=$200, qty=100, ADV=50M, vol=0.20) under the current
    # calibrated-not-pessimistic constants.
    for field in (
        "estimated_spread_usd",
        "estimated_impact_usd",
        "estimated_regulatory_fees_usd",
        "live_adjusted_fill_price",
    ):
        assert field in payload, f"missing field {field!r} in {payload!r}"
