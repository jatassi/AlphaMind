"""Tests for ``alphamind.execution.broker_adapter.fill_stream`` (story ALP-384).

Story 02f — `trade_updates` fill-stream subscriber + per-event translator that
maps alpaca-py ``TradeUpdate`` events into the OMS-facing ``FillReport`` shape
documented in ``architecture.md § Fill report contract``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderType,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.models import Order, TradeUpdate
from pydantic import ValidationError

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
)
from alphamind.execution.broker_adapter import (
    FillReport,
    OrderStatus,
    subscribe_trade_updates,
    translate_trade_update,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic alpaca-py payloads
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _build_order(
    *,
    client_order_id: str = "oms-order-1",
    order_id: UUID | None = None,
    symbol: str | None = "AAPL",
    asset_class: AssetClass | None = AssetClass.US_EQUITY,
    order_class: OrderClass = OrderClass.SIMPLE,
    order_type: OrderType | None = OrderType.LIMIT,
    side: OrderSide | None = OrderSide.BUY,
    status: AlpacaOrderStatus = AlpacaOrderStatus.NEW,
    filled_qty: str | None = "0",
    qty: str | None = "100",
    legs: list[Order] | None = None,
    position_intent: PositionIntent | None = None,
    ratio_qty: str | None = None,
) -> Order:
    """Build an alpaca-py ``Order`` with sensible defaults for tests."""
    return Order(
        id=order_id or uuid4(),
        client_order_id=client_order_id,
        created_at=_now_utc(),
        updated_at=_now_utc(),
        submitted_at=_now_utc(),
        symbol=symbol,
        asset_class=asset_class,
        order_class=order_class,
        order_type=order_type,
        type=order_type,
        side=side,
        time_in_force=TimeInForce.DAY,
        status=status,
        extended_hours=False,
        qty=qty,
        filled_qty=filled_qty,
        legs=legs,
        position_intent=position_intent,
        ratio_qty=ratio_qty,
    )


def _build_trade_update(
    *,
    event: str,
    order: Order,
    timestamp: datetime | None = None,
    price: float | None = None,
    qty: float | None = None,
    position_qty: float | None = None,
) -> TradeUpdate:
    """Build an alpaca-py ``TradeUpdate`` payload."""
    return TradeUpdate(
        event=event,
        order=order,
        timestamp=timestamp or _now_utc(),
        price=price,
        qty=qty,
        position_qty=position_qty,
    )


# ---------------------------------------------------------------------------
# FillReport schema
# ---------------------------------------------------------------------------


class TestFillReportSchema:
    def test_is_frozen(self) -> None:
        report = FillReport(
            client_order_id=ClientOrderId("oms-1"),
            alpaca_order_id=AlpacaOrderId("apc-1"),
            parent_client_order_id=None,
            parent_alpaca_order_id=None,
            event_type="filled",
            fill_timestamp=_now_utc(),
            fill_price=100.0,
            fill_quantity=10.0,
            cumulative_filled_quantity=10.0,
            remaining_quantity=0.0,
            execution_venue="NASDAQ",
            occ_symbol=None,
            position_intent=None,
            raw_event_payload={},
        )
        # Bypass mypy's literal narrowing on a deliberately-invalid mutation;
        # frozen models reject any field assignment regardless of the new value.
        with pytest.raises(ValidationError):
            cast(Any, report).event_type = "something_else"

    def test_event_type_rejects_unknown_status(self) -> None:
        # Cast to Any to bypass mypy's literal narrowing — the test intent is
        # to verify Pydantic rejects unknown OrderStatus values at runtime.
        bad_event = cast(Any, "totally_made_up")
        with pytest.raises(ValidationError):
            FillReport(
                client_order_id=ClientOrderId("oms-1"),
                alpaca_order_id=AlpacaOrderId("apc-1"),
                parent_client_order_id=None,
                parent_alpaca_order_id=None,
                event_type=bad_event,
                fill_timestamp=_now_utc(),
                fill_price=None,
                fill_quantity=None,
                cumulative_filled_quantity=0.0,
                remaining_quantity=0.0,
                execution_venue=None,
                occ_symbol=None,
                position_intent=None,
                raw_event_payload={},
            )

    def test_order_status_literal_covers_documented_values(self) -> None:
        """The OrderStatus literal must include every event type the translator emits."""
        from typing import get_args

        documented = {
            "new",
            "filled",
            "partially_filled",
            "canceled",
            "expired",
            "replaced",
            "replace_rejected",
            "stopped",
            "rejected",
            "done_for_day",
        }
        assert set(get_args(OrderStatus)) == documented


# ---------------------------------------------------------------------------
# translate_trade_update — single events
# ---------------------------------------------------------------------------


class TestTranslateEquityEvents:
    def test_equity_fill_returns_single_report_with_fill_fields(self) -> None:
        order = _build_order(symbol="AAPL", filled_qty="100", qty="100")
        ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
        update = _build_trade_update(
            event="fill", order=order, timestamp=ts, price=189.42, qty=100.0
        )

        reports = translate_trade_update(update)

        assert len(reports) == 1
        (report,) = reports
        assert report.client_order_id == order.client_order_id
        assert report.alpaca_order_id == str(order.id)
        assert report.event_type == "filled"
        assert report.fill_price == pytest.approx(189.42)
        assert report.fill_quantity == pytest.approx(100.0)
        assert report.cumulative_filled_quantity == pytest.approx(100.0)
        assert report.remaining_quantity == pytest.approx(0.0)
        assert report.parent_client_order_id is None
        assert report.parent_alpaca_order_id is None
        assert report.occ_symbol is None
        assert report.position_intent is None
        assert report.fill_timestamp == ts

    def test_canceled_event_has_null_fill_fields(self) -> None:
        order = _build_order(filled_qty="0", status=AlpacaOrderStatus.CANCELED)
        update = _build_trade_update(event="canceled", order=order)

        (report,) = translate_trade_update(update)

        assert report.event_type == "canceled"
        assert report.fill_price is None
        assert report.fill_quantity is None

    def test_replaced_event_carries_new_alpaca_order_id(self) -> None:
        new_id = uuid4()
        order = _build_order(order_id=new_id, status=AlpacaOrderStatus.REPLACED)
        update = _build_trade_update(event="replaced", order=order)

        (report,) = translate_trade_update(update)

        assert report.event_type == "replaced"
        assert report.alpaca_order_id == str(new_id)

    def test_partial_fill_uses_partially_filled_status(self) -> None:
        order = _build_order(filled_qty="40", qty="100")
        update = _build_trade_update(
            event="partial_fill", order=order, price=100.0, qty=40.0, position_qty=40.0
        )

        (report,) = translate_trade_update(update)

        assert report.event_type == "partially_filled"
        assert report.fill_quantity == pytest.approx(40.0)
        assert report.remaining_quantity == pytest.approx(60.0)

    def test_filtered_event_returns_empty_tuple(self) -> None:
        order = _build_order(status=AlpacaOrderStatus.PENDING_NEW)
        update = _build_trade_update(event="pending_new", order=order)

        assert translate_trade_update(update) == ()


# ---------------------------------------------------------------------------
# translate_trade_update — single-leg options
# ---------------------------------------------------------------------------


class TestTranslateOptionsEvents:
    def test_single_leg_options_partial_fill_populates_occ_symbol(self) -> None:
        occ_symbol = "AAPL250620C00200000"
        order = _build_order(
            symbol=occ_symbol,
            asset_class=AssetClass.US_OPTION,
            filled_qty="2",
            qty="5",
        )
        update = _build_trade_update(event="partial_fill", order=order, price=3.45, qty=2.0)

        (report,) = translate_trade_update(update)

        assert report.event_type == "partially_filled"
        assert report.occ_symbol == occ_symbol


# ---------------------------------------------------------------------------
# translate_trade_update — mleg parent + leg children
# ---------------------------------------------------------------------------


class TestTranslateMlegEvents:
    def _build_mleg_legs(self) -> list[Order]:
        return [
            _build_order(
                client_order_id="leg-1",
                symbol="AAPL250620C00200000",
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.SIMPLE,
                position_intent=PositionIntent.BUY_TO_OPEN,
                ratio_qty="1",
                qty="1",
                filled_qty="1",
                side=OrderSide.BUY,
                status=AlpacaOrderStatus.FILLED,
            ),
            _build_order(
                client_order_id="leg-2",
                symbol="AAPL250620C00210000",
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.SIMPLE,
                position_intent=PositionIntent.SELL_TO_OPEN,
                ratio_qty="1",
                qty="1",
                filled_qty="1",
                side=OrderSide.SELL,
                status=AlpacaOrderStatus.FILLED,
            ),
            _build_order(
                client_order_id="leg-3",
                symbol="AAPL250620P00190000",
                asset_class=AssetClass.US_OPTION,
                order_class=OrderClass.SIMPLE,
                position_intent=PositionIntent.SELL_TO_OPEN,
                ratio_qty="1",
                qty="1",
                filled_qty="1",
                side=OrderSide.SELL,
                status=AlpacaOrderStatus.FILLED,
            ),
        ]

    def test_mleg_fill_returns_parent_plus_per_leg_children(self) -> None:
        legs = self._build_mleg_legs()
        parent_id = uuid4()
        parent_order = _build_order(
            client_order_id="strategy-1",
            order_id=parent_id,
            symbol=None,
            asset_class=None,
            order_class=OrderClass.MLEG,
            order_type=None,
            side=None,
            qty="1",
            filled_qty="1",
            status=AlpacaOrderStatus.FILLED,
            legs=legs,
        )
        update = _build_trade_update(event="fill", order=parent_order, price=2.10, qty=1.0)

        reports = translate_trade_update(update)

        # 1 parent + 3 leg children = 4 reports
        assert len(reports) == 4
        parent, leg1, leg2, leg3 = reports

        assert parent.client_order_id == "strategy-1"
        assert parent.alpaca_order_id == str(parent_id)
        assert parent.parent_client_order_id is None
        assert parent.parent_alpaca_order_id is None
        assert parent.occ_symbol is None
        assert parent.position_intent is None
        assert parent.event_type == "filled"

        for child in (leg1, leg2, leg3):
            assert child.parent_client_order_id == "strategy-1"
            assert child.parent_alpaca_order_id == str(parent_id)
            assert child.occ_symbol is not None
            assert child.position_intent is not None
            assert child.event_type == "filled"

        assert leg1.occ_symbol == "AAPL250620C00200000"
        assert leg1.position_intent == "buy_to_open"
        assert leg2.position_intent == "sell_to_open"
        assert leg3.position_intent == "sell_to_open"


# ---------------------------------------------------------------------------
# Aliases / extra coverage
# ---------------------------------------------------------------------------


class TestEventTypeMapping:
    @pytest.mark.parametrize(
        ("raw_event", "expected_status"),
        [
            ("new", "new"),
            ("expired", "expired"),
            ("rejected", "rejected"),
            ("stopped", "stopped"),
            ("done_for_day", "done_for_day"),
            ("replace_rejected", "replace_rejected"),
            ("order_replace_rejected", "replace_rejected"),
        ],
    )
    def test_other_documented_events_map_through(
        self, raw_event: str, expected_status: OrderStatus
    ) -> None:
        order = _build_order()
        update = _build_trade_update(event=raw_event, order=order)

        (report,) = translate_trade_update(update)

        assert report.event_type == expected_status

    @pytest.mark.parametrize(
        "filtered",
        ["pending_new", "pending_cancel", "pending_replace", "calculated", "accepted"],
    )
    def test_other_filtered_events_return_empty_tuple(self, filtered: str) -> None:
        order = _build_order()
        update = _build_trade_update(event=filtered, order=order)

        assert translate_trade_update(update) == ()


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------


class TestTimestampInvariants:
    def test_fill_timestamp_is_tz_aware_utc_when_naive_input(self) -> None:
        order = _build_order()
        # Intentionally naive: the test exercises the model's UTC-coercion path
        # for upstream payloads that don't carry tzinfo. Built by stripping
        # tzinfo off an aware datetime so the DTZ-aware lint stays happy.
        naive_ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC).replace(tzinfo=None)
        update = _build_trade_update(event="fill", order=order, timestamp=naive_ts)

        (report,) = translate_trade_update(update)

        assert report.fill_timestamp.tzinfo is not None
        assert report.fill_timestamp.utcoffset() == UTC.utcoffset(report.fill_timestamp)

    def test_fill_timestamp_preserves_aware_input(self) -> None:
        order = _build_order()
        ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
        update = _build_trade_update(event="fill", order=order, timestamp=ts)

        (report,) = translate_trade_update(update)

        assert report.fill_timestamp == ts


# ---------------------------------------------------------------------------
# subscribe_trade_updates — generator behavior
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal alpaca-py-shaped stub.

    Captures the registered handler, exposes ``inject(update)`` for tests to
    push synthetic ``TradeUpdate`` payloads without any real network, and
    provides an awaitable ``_run_forever`` that mirrors the protocol the
    generator depends on — it parks until cancelled, then records the
    cancellation so tests can assert clean shutdown.
    """

    def __init__(self) -> None:
        self.handler: Any = None
        self.run_cancelled = False
        self._park = asyncio.Event()

    def subscribe_trade_updates(self, handler: Any) -> None:
        self.handler = handler

    async def inject(self, update: TradeUpdate) -> None:
        assert self.handler is not None, "handler must be registered first"
        await self.handler(update)

    async def _run_forever(self) -> None:
        try:
            await self._park.wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise


class TestSubscribeTradeUpdates:
    async def test_registers_handler_and_yields_reports(self) -> None:
        stream = _FakeStream()
        order = _build_order()

        gen = subscribe_trade_updates(stream)
        anext_task = asyncio.create_task(gen.__anext__())

        # Wait for handler registration
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stream.handler is not None

        update = _build_trade_update(event="fill", order=order, price=10.0, qty=5.0)
        await stream.inject(update)

        report = await anext_task
        assert isinstance(report, FillReport)
        assert report.event_type == "filled"

        await gen.aclose()

    async def test_mleg_event_yields_all_reports_individually_in_order(self) -> None:
        stream = _FakeStream()
        legs = TestTranslateMlegEvents()._build_mleg_legs()
        parent = _build_order(
            client_order_id="strat-X",
            symbol=None,
            asset_class=None,
            order_class=OrderClass.MLEG,
            order_type=None,
            side=None,
            legs=legs,
            qty="1",
            filled_qty="1",
            status=AlpacaOrderStatus.FILLED,
        )

        gen = subscribe_trade_updates(stream)
        anext_task_1 = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stream.handler is not None

        update = _build_trade_update(event="fill", order=parent, price=2.0, qty=1.0)
        await stream.inject(update)

        first = await anext_task_1
        assert first.parent_client_order_id is None
        assert first.client_order_id == "strat-X"

        second = await gen.__anext__()
        third = await gen.__anext__()
        fourth = await gen.__anext__()
        for child in (second, third, fourth):
            assert child.parent_client_order_id == "strat-X"

        await gen.aclose()

    async def test_filtered_events_are_not_yielded(self) -> None:
        stream = _FakeStream()
        order = _build_order()

        gen = subscribe_trade_updates(stream)
        anext_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Inject a filtered event first; the generator should ignore it
        await stream.inject(_build_trade_update(event="pending_new", order=order))
        # Then a real event
        await stream.inject(_build_trade_update(event="fill", order=order, price=1.0, qty=1.0))

        report = await anext_task
        assert report.event_type == "filled"

        await gen.aclose()

    async def test_cancellation_exits_cleanly_and_cancels_background_task(self) -> None:
        stream = _FakeStream()

        gen = subscribe_trade_updates(stream)
        anext_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stream.handler is not None

        anext_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await anext_task

        await gen.aclose()
        # The generator's background run task should have been cancelled when the
        # generator closed.
        await asyncio.sleep(0)
        assert stream.run_cancelled

    async def test_translation_exception_propagates_to_consumer(self) -> None:
        """If translate_trade_update raises, the consumer sees the exception."""
        stream = _FakeStream()

        gen = subscribe_trade_updates(stream)
        anext_task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stream.handler is not None

        # Inject a malformed update (not a TradeUpdate at all). The translator
        # accesses ``update.event`` first, which raises ``AttributeError`` on
        # the bare object. The exception must propagate to the consumer so the
        # monitor's run-loop can catch it and trigger reconnect.
        class _Broken:
            pass

        # Bypass mypy's TradeUpdate parameter check — the test deliberately
        # exercises malformed-payload propagation behavior.
        await stream.inject(_Broken())  # type: ignore[arg-type]

        with pytest.raises(AttributeError):
            await anext_task

        await gen.aclose()
