"""Tests for ``alphamind.execution.broker_adapter.queries`` (story ALP-379).

All tests mock out the ``TradingClient`` to avoid network calls.  The
``unittest.mock`` library is used to patch individual methods — this is
appropriate because the queries module's entire job is to call SDK methods and
reshape their return values into our typed snapshots.  We verify the snapshot
fields (shape), pagination iteration, 404→None handling, frozen model
enforcement, and the public re-export surface.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import (
    Asset,
    Calendar,
    Clock,
    Order,
    Position,
    TradeAccount,
)

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money, price, signed_money

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade_account() -> TradeAccount:
    """Minimal valid TradeAccount dict parsed into a model."""
    return TradeAccount.model_validate(
        {
            "id": "a0d8e7b5-1234-4b3a-8765-000000000001",
            "account_number": "PA000001",
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "50000.00",
            "regt_buying_power": "25000.00",
            "daytrading_buying_power": "100000.00",
            "non_marginable_buying_power": "5000.00",
            "cash": "10000.00",
            "accrued_fees": "0",
            "portfolio_value": "60000.00",
            "pattern_day_trader": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
            "created_at": "2024-01-01T00:00:00Z",
            "trade_suspended_by_user": False,
            "multiplier": "2",
            "shorting_enabled": True,
            "equity": "60000.00",
            "last_equity": "59000.00",
            "long_market_value": "50000.00",
            "short_market_value": "0.00",
            "initial_margin": "25000.00",
            "maintenance_margin": "15000.00",
            "last_maintenance_margin": "14500.00",
            "sma": "0",
            "daytrade_count": 2,
        }
    )


def _make_position(symbol: str = "AAPL") -> Position:
    return Position.model_validate(
        {
            "asset_id": "a0d8e7b5-1234-4b3a-8765-0987654321ab",
            "symbol": symbol,
            "exchange": "NASDAQ",
            "asset_class": "us_equity",
            "avg_entry_price": "150.00",
            "qty": "10",
            "side": "long",
            "market_value": "1600.00",
            "cost_basis": "1500.00",
            "unrealized_pl": "100.00",
            "unrealized_plpc": "0.0667",
            "unrealized_intraday_pl": "50.00",
            "unrealized_intraday_plpc": "0.0323",
            "current_price": "160.00",
            "lastday_price": "155.00",
            "change_today": "0.0323",
        }
    )


def _make_order(
    symbol: str = "MSFT",
    order_class: str = "simple",
    legs: list[dict[str, Any]] | None = None,
) -> Order:
    base: dict[str, Any] = {
        "id": "b1e2f3a4-5678-4c2d-9876-abcdef012345",
        "client_order_id": "client-001",
        "created_at": "2024-01-10T10:00:00Z",
        "updated_at": "2024-01-10T10:00:01Z",
        "submitted_at": "2024-01-10T10:00:00Z",
        "filled_at": None,
        "expired_at": None,
        "expires_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
        "replaced_by": None,
        "replaces": None,
        "asset_id": "c2d3e4f5-6789-4d3e-a987-bcdef0123456",
        "symbol": symbol,
        "asset_class": "us_equity",
        "notional": None,
        "qty": "100",
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": order_class,
        "order_type": "market",
        "type": "market",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "new",
        "extended_hours": False,
        "legs": legs,
        "trail_percent": None,
        "trail_price": None,
        "hwm": None,
    }
    return Order.model_validate(base)


def _make_asset(symbol: str = "NVDA") -> Asset:
    # Note: Alpaca's Asset model uses "class" as the field alias for asset_class
    return Asset.model_validate(
        {
            "id": "c2d3e4f5-6789-4d3e-a987-bcdef0123456",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "symbol": symbol,
            "name": "NVIDIA Corporation",
            "status": "active",
            "tradable": True,
            "marginable": True,
            "shortable": True,
            "easy_to_borrow": True,
            "fractionable": True,
        }
    )


def _make_calendar(
    date_str: str = "2024-01-10",
    open_time: str = "09:30",
    close_time: str = "16:00",
) -> Calendar:
    return Calendar.model_validate({"date": date_str, "open": open_time, "close": close_time})


def _make_clock(is_open: bool = True) -> Clock:
    return Clock.model_validate(
        {
            "timestamp": "2024-01-10T10:30:00-05:00",
            "is_open": is_open,
            "next_open": "2024-01-11T09:30:00-05:00",
            "next_close": "2024-01-10T16:00:00-05:00",
        }
    )


def _make_404_api_error() -> APIError:
    mock_request = httpx.Request("GET", "https://paper-api.alpaca.markets/v2/assets/Z")
    mock_response = httpx.Response(
        404,
        json={"code": 40410000, "message": "asset not found for symbol"},
        request=mock_request,
    )
    http_error = httpx.HTTPStatusError(
        "404 Not Found", request=mock_request, response=mock_response
    )
    return APIError(  # type: ignore[no-untyped-call]
        error={"code": 40410000, "message": "asset not found for symbol"},
        http_error=http_error,
    )


def _make_trade_activity_raw() -> dict[str, Any]:
    """Raw dict as returned by TradingClient.get('/account/activities', ...)."""
    return {
        "id": "20240110000000000::fill-001",
        "account_id": "a0d8e7b5-1234-4b3a-8765-0987654321ab",
        "activity_type": "FILL",
        "transaction_time": "2024-01-10T10:01:00Z",
        "type": "fill",
        "price": "150.25",
        "qty": "10",
        "side": "buy",
        "symbol": "AAPL",
        "leaves_qty": "0",
        "order_id": "a0d8e7b5-1234-4b3a-8765-000000000099",
        "cum_qty": "10",
        "order_status": "filled",
    }


def _make_non_trade_activity_raw() -> dict[str, Any]:
    """Raw dict as returned by TradingClient.get('/account/activities', ...)."""
    return {
        "id": "20240110000000000::fee-001",
        "account_id": "a0d8e7b5-1234-4b3a-8765-0987654321ab",
        "activity_type": "FEE",
        "date": "2024-01-10T00:00:00Z",
        "net_amount": "-0.01",
        "description": "Regulatory fee",
    }


def _fake_client() -> MagicMock:
    """Return a MagicMock that passes isinstance checks for TradingClient."""
    return MagicMock(spec=TradingClient)


# ---------------------------------------------------------------------------
# 1. Constructor / type-check
# ---------------------------------------------------------------------------


class TestAccountStateQueriesConstructor:
    def test_accepts_trading_client(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        qs = AccountStateQueries(client)
        assert qs is not None

    def test_rejects_non_trading_client(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        with pytest.raises(TypeError, match="TradingClient"):
            AccountStateQueries("not a client")  # type: ignore[arg-type]

    def test_rejects_none(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        with pytest.raises(TypeError):
            AccountStateQueries(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. get_account
# ---------------------------------------------------------------------------


class TestGetAccount:
    def test_returns_trade_account_snapshot_with_all_fields(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            TradeAccountSnapshot,
        )

        client = _fake_client()
        client.get_account.return_value = _make_trade_account()

        qs = AccountStateQueries(client)
        result = qs.get_account()

        assert isinstance(result, TradeAccountSnapshot)
        assert result.account_id == "a0d8e7b5-1234-4b3a-8765-000000000001"
        # ALP-462 — Decimal-exact equality; Alpaca's string-typed monetary
        # fields parse via ``money(broker_str)`` so the wrapper threads the
        # raw "10000.00" string into a ``Money`` without binary float drift.
        assert result.cash == money("10000.00")
        assert result.equity == money("60000.00")
        assert result.buying_power == money("50000.00")
        assert result.regt_buying_power == money("25000.00")
        assert result.daytrading_buying_power == money("100000.00")
        assert result.maintenance_margin == money("15000.00")
        assert result.daytrade_count == 2
        assert result.pattern_day_trader is False
        assert result.status == "ACTIVE"

    def test_snapshot_is_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
        )

        client = _fake_client()
        client.get_account.return_value = _make_trade_account()
        qs = AccountStateQueries(client)
        result = qs.get_account()

        with pytest.raises((TypeError, ValidationError)):
            result.cash = money(0.0)


# ---------------------------------------------------------------------------
# 3. get_positions
# ---------------------------------------------------------------------------


class TestGetPositions:
    def test_returns_tuple_of_position_snapshots(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            PositionSnapshot,
        )

        client = _fake_client()
        client.get_all_positions.return_value = [
            _make_position("NVDA"),
            _make_position("AAPL"),
        ]

        qs = AccountStateQueries(client)
        result = qs.get_positions()

        assert isinstance(result, tuple)
        assert all(isinstance(p, PositionSnapshot) for p in result)

    def test_positions_sorted_by_symbol(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_all_positions.return_value = [
            _make_position("TSLA"),
            _make_position("AAPL"),
            _make_position("MSFT"),
        ]

        qs = AccountStateQueries(client)
        result = qs.get_positions()

        symbols = [p.symbol for p in result]
        assert symbols == sorted(symbols)

    def test_position_snapshot_fields(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_all_positions.return_value = [_make_position("AAPL")]

        qs = AccountStateQueries(client)
        result = qs.get_positions()
        pos = result[0]

        assert pos.symbol == "AAPL"
        assert pos.asset_class == "us_equity"
        assert pos.qty == pytest.approx(10.0)
        # ALP-462 — Decimal-exact equality on the migrated price/USD fields.
        assert pos.avg_entry_price == price("150.00")
        assert pos.market_value == money("1600.00")
        assert pos.cost_basis == money("1500.00")
        assert pos.unrealized_pl == money("100.00")
        assert pos.unrealized_plpc == pytest.approx(0.0667, abs=1e-4)
        assert pos.current_price == price("160.00")
        assert pos.side == "long"

    def test_position_snapshot_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_all_positions.return_value = [_make_position("AAPL")]

        qs = AccountStateQueries(client)
        result = qs.get_positions()

        with pytest.raises((TypeError, ValidationError)):
            result[0].symbol = "X"

    def test_empty_positions_returns_empty_tuple(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_all_positions.return_value = []

        qs = AccountStateQueries(client)
        assert qs.get_positions() == ()


# ---------------------------------------------------------------------------
# 4. get_asset
# ---------------------------------------------------------------------------


class TestGetAsset:
    def test_known_symbol_returns_asset_snapshot(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            AssetSnapshot,
        )

        client = _fake_client()
        client.get_asset.return_value = _make_asset("NVDA")

        qs = AccountStateQueries(client)
        result = qs.get_asset("NVDA")

        assert isinstance(result, AssetSnapshot)
        assert result.symbol == "NVDA"
        assert result.tradable is True
        assert result.easy_to_borrow is True
        assert result.shortable is True
        assert result.fractionable is True
        assert result.marginable is True
        assert result.name == "NVIDIA Corporation"
        assert result.asset_class == "us_equity"

    def test_unknown_symbol_404_returns_none(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_asset.side_effect = _make_404_api_error()

        qs = AccountStateQueries(client)
        result = qs.get_asset("ZZNONEXISTENT")

        assert result is None

    def test_non_404_error_propagates(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        mock_request = httpx.Request("GET", "https://paper-api.alpaca.markets/v2/assets/X")
        mock_response = httpx.Response(
            500,
            json={"code": 50000000, "message": "internal server error"},
            request=mock_request,
        )
        http_error = httpx.HTTPStatusError(
            "500 Internal Server Error", request=mock_request, response=mock_response
        )
        err_500 = APIError(  # type: ignore[no-untyped-call]
            error={"code": 50000000, "message": "internal server error"},
            http_error=http_error,
        )

        client = _fake_client()
        client.get_asset.side_effect = err_500

        qs = AccountStateQueries(client)
        with pytest.raises(APIError):
            qs.get_asset("BROKEN")

    def test_asset_snapshot_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_asset.return_value = _make_asset("AAPL")

        qs = AccountStateQueries(client)
        result = qs.get_asset("AAPL")
        assert result is not None

        with pytest.raises((TypeError, ValidationError)):
            result.symbol = "X"


# ---------------------------------------------------------------------------
# 5. get_calendar
# ---------------------------------------------------------------------------


class TestGetCalendar:
    def test_returns_tuple_of_calendar_days(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            CalendarDay,
        )

        client = _fake_client()
        client.get_calendar.return_value = [
            _make_calendar("2024-01-10"),
            _make_calendar("2024-01-11"),
        ]

        qs = AccountStateQueries(client)
        start = date(2024, 1, 10)
        end = date(2024, 1, 12)
        result = qs.get_calendar(start=start, end=end)

        assert isinstance(result, tuple)
        assert all(isinstance(d, CalendarDay) for d in result)

    def test_calendar_day_fields_tz_aware(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_calendar.return_value = [_make_calendar("2024-01-10", "09:30", "16:00")]

        qs = AccountStateQueries(client)
        result = qs.get_calendar()
        day = result[0]

        assert day.date == date(2024, 1, 10)
        assert day.open_time.tzinfo is not None
        assert day.close_time.tzinfo is not None

    def test_calendar_day_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_calendar.return_value = [_make_calendar("2024-01-10")]

        qs = AccountStateQueries(client)
        result = qs.get_calendar()

        with pytest.raises((TypeError, ValidationError)):
            result[0].date = date(2024, 1, 1)

    def test_calendar_passes_start_end_to_sdk(self) -> None:
        from alpaca.trading.requests import GetCalendarRequest

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_calendar.return_value = []

        qs = AccountStateQueries(client)
        start = date(2024, 1, 10)
        end = date(2024, 1, 20)
        qs.get_calendar(start=start, end=end)

        call_args = client.get_calendar.call_args
        # Either positional or keyword
        filters_arg = call_args[0][0] if call_args[0] else call_args[1].get("filters")
        assert isinstance(filters_arg, GetCalendarRequest)
        assert filters_arg.start == start
        assert filters_arg.end == end


# ---------------------------------------------------------------------------
# 6. get_clock
# ---------------------------------------------------------------------------


class TestGetClock:
    def test_returns_market_clock(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            MarketClock,
        )

        client = _fake_client()
        client.get_clock.return_value = _make_clock(is_open=True)

        qs = AccountStateQueries(client)
        result = qs.get_clock()

        assert isinstance(result, MarketClock)
        assert result.is_open is True
        assert result.timestamp.tzinfo is not None
        assert result.next_open.tzinfo is not None
        assert result.next_close.tzinfo is not None

    def test_market_closed(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_clock.return_value = _make_clock(is_open=False)

        qs = AccountStateQueries(client)
        result = qs.get_clock()

        assert result.is_open is False

    def test_market_clock_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_clock.return_value = _make_clock()

        qs = AccountStateQueries(client)
        result = qs.get_clock()

        with pytest.raises((TypeError, ValidationError)):
            result.is_open = False


# ---------------------------------------------------------------------------
# 7. get_orders
# ---------------------------------------------------------------------------


def _run_async(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


async def _collect_orders(qs: Any, **kwargs: Any) -> list[Any]:
    results = []
    async for order in qs.get_orders(**kwargs):
        results.append(order)
    return results


async def _collect_activities(qs: Any, **kwargs: Any) -> list[Any]:
    results = []
    async for activity in qs.get_account_activities(**kwargs):
        results.append(activity)
    return results


class TestGetOrders:
    def test_yields_order_snapshots(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            OrderSnapshot,
        )

        client = _fake_client()
        client.get_orders.return_value = [_make_order("MSFT")]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs, status="all"))

        assert len(results) == 1
        assert isinstance(results[0], OrderSnapshot)

    def test_order_snapshot_fields(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = [_make_order("AAPL")]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))
        order = results[0]

        assert order.order_id == "b1e2f3a4-5678-4c2d-9876-abcdef012345"
        assert order.client_order_id == "client-001"
        assert order.symbol == "AAPL"
        assert order.qty == pytest.approx(100.0)
        assert order.filled_qty == pytest.approx(0.0)
        assert order.side == "buy"
        assert order.order_type == "market"
        assert order.time_in_force == "day"
        assert order.order_class == "simple"
        assert order.status == "new"
        assert order.filled_at is None
        assert order.replaced_by is None
        assert order.replaces is None
        assert order.legs is None

    def test_order_snapshot_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = [_make_order()]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))

        with pytest.raises((TypeError, ValidationError)):
            results[0].symbol = "X"

    def test_paginates_until_exhaustion(self) -> None:
        """get_orders continues paging while SDK returns a full page (== _PAGE_SIZE).

        We patch _PAGE_SIZE to 2 so we can use small test fixtures.  First call
        returns 2 orders (== page_size → continue); second returns 1 (< page_size
        → stop). Total = 3 orders, 2 SDK calls.
        """
        import alphamind.execution.broker_adapter.queries as q_mod
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        page1 = [_make_order("SYM0"), _make_order("SYM1")]
        page2 = [_make_order("SYM2")]

        client = _fake_client()
        client.get_orders.side_effect = [page1, page2]

        original_page_size = q_mod._PAGE_SIZE
        try:
            q_mod._PAGE_SIZE = 2
            qs = AccountStateQueries(client)
            results = asyncio.run(_collect_orders(qs))
        finally:
            q_mod._PAGE_SIZE = original_page_size

        assert len(results) == 3
        assert client.get_orders.call_count == 2

    def test_since_passed_as_after_on_request(self) -> None:
        """When since= is supplied it maps to GetOrdersRequest.after."""
        from alpaca.trading.requests import GetOrdersRequest

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        since_dt = datetime(2024, 1, 9, 12, 0, 0, tzinfo=UTC)
        client = _fake_client()
        client.get_orders.return_value = []

        qs = AccountStateQueries(client)
        asyncio.run(_collect_orders(qs, since=since_dt))

        call_args = client.get_orders.call_args
        req = call_args[0][0] if call_args[0] else call_args[1].get("filter")
        assert isinstance(req, GetOrdersRequest)
        assert req.after == since_dt

    def test_empty_orders_returns_nothing(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = []

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))
        assert results == []

    def test_status_filter_passed_to_sdk(self) -> None:
        from alpaca.trading.requests import GetOrdersRequest

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = []

        qs = AccountStateQueries(client)
        asyncio.run(_collect_orders(qs, status="open"))

        call_args = client.get_orders.call_args
        req = call_args[0][0] if call_args[0] else call_args[1].get("filter")
        assert isinstance(req, GetOrdersRequest)
        assert req.status is not None
        assert req.status.value == "open"


# ---------------------------------------------------------------------------
# 8. Mleg orders
# ---------------------------------------------------------------------------


class TestMlegOrders:
    def _make_mleg_order(self) -> Order:
        """Mleg parent with two option legs."""
        leg1: dict[str, Any] = {
            "id": "e0000000-0000-0000-0000-000000000001",
            "client_order_id": "leg-client-001",
            "created_at": "2024-01-10T10:00:00Z",
            "updated_at": "2024-01-10T10:00:01Z",
            "submitted_at": "2024-01-10T10:00:00Z",
            "filled_at": None,
            "expired_at": None,
            "expires_at": None,
            "canceled_at": None,
            "failed_at": None,
            "replaced_at": None,
            "replaced_by": None,
            "replaces": None,
            "asset_id": "c2d3e4f5-6789-4d3e-a987-bcdef0123456",
            "symbol": "AAPL240120C00150000",
            "asset_class": "us_option",
            "notional": None,
            "qty": "1",
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_class": "mleg",
            "order_type": "market",
            "type": "market",
            "side": "buy",
            "time_in_force": "day",
            "limit_price": None,
            "stop_price": None,
            "status": "new",
            "extended_hours": False,
            "legs": None,
            "trail_percent": None,
            "trail_price": None,
            "hwm": None,
            "position_intent": "buy_to_open",
            "ratio_qty": 1,
        }
        leg2 = dict(leg1)
        leg2["id"] = "e0000000-0000-0000-0000-000000000002"
        leg2["client_order_id"] = "leg-client-002"
        leg2["symbol"] = "AAPL240120P00145000"
        leg2["side"] = "sell"
        leg2["position_intent"] = "sell_to_open"

        parent: dict[str, Any] = {
            "id": "f0000000-0000-0000-0000-000000000001",
            "client_order_id": "parent-client-001",
            "created_at": "2024-01-10T10:00:00Z",
            "updated_at": "2024-01-10T10:00:01Z",
            "submitted_at": "2024-01-10T10:00:00Z",
            "filled_at": None,
            "expired_at": None,
            "expires_at": None,
            "canceled_at": None,
            "failed_at": None,
            "replaced_at": None,
            "replaced_by": None,
            "replaces": None,
            "asset_id": "d3e4f5a6-7890-4e4f-b098-cdef01234567",
            "symbol": "AAPL",
            "asset_class": "us_option",
            "notional": None,
            "qty": "1",
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_class": "mleg",
            "order_type": "market",
            "type": "market",
            "side": "buy",
            "time_in_force": "day",
            "limit_price": None,
            "stop_price": None,
            "status": "new",
            "extended_hours": False,
            "legs": [leg1, leg2],
            "trail_percent": None,
            "trail_price": None,
            "hwm": None,
        }
        return Order.model_validate(parent)

    def test_mleg_order_has_legs_populated(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            OrderLegSnapshot,
            OrderSnapshot,
        )

        client = _fake_client()
        client.get_orders.return_value = [self._make_mleg_order()]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))

        assert len(results) == 1
        order = results[0]
        assert isinstance(order, OrderSnapshot)
        assert order.legs is not None
        assert len(order.legs) == 2
        assert all(isinstance(leg, OrderLegSnapshot) for leg in order.legs)

    def test_mleg_legs_have_position_intent(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = [self._make_mleg_order()]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))
        legs = results[0].legs
        assert legs is not None

        intents = {leg.position_intent for leg in legs}
        assert "buy_to_open" in intents
        assert "sell_to_open" in intents

    def test_simple_order_has_legs_none(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_orders.return_value = [_make_order("AAPL", order_class="simple")]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_orders(qs))

        assert results[0].legs is None


# ---------------------------------------------------------------------------
# 9. get_account_activities
# ---------------------------------------------------------------------------


class TestGetAccountActivities:
    """Activities are fetched via TradingClient.get('/account/activities', ...) since
    TradingClient has no high-level get_account_activities method.  We mock
    client.get() to return raw dict pages, matching the actual API response shape."""

    def test_yields_activity_snapshots(self) -> None:
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            ActivitySnapshot,
        )

        client = _fake_client()
        # First call returns one raw activity; second call returns empty list (stop)
        client.get.side_effect = [
            [_make_trade_activity_raw()],
            [],
        ]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))

        assert len(results) == 1
        assert isinstance(results[0], ActivitySnapshot)

    def test_activity_snapshot_trade_fill_fields(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.side_effect = [
            [_make_trade_activity_raw()],
            [],
        ]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))
        act = results[0]

        assert act.id == "20240110000000000::fill-001"
        assert act.activity_type == "FILL"
        assert act.symbol == "AAPL"
        assert act.qty == pytest.approx(10.0)
        # ALP-462 — ``price`` parses Alpaca's string field via ``Decimal``.
        assert act.price == price("150.25")
        assert act.side == "buy"
        assert isinstance(act.raw, dict)
        assert "id" in act.raw

    def test_activity_snapshot_non_trade_fields(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.side_effect = [
            [_make_non_trade_activity_raw()],
            [],
        ]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))
        act = results[0]

        assert act.activity_type == "FEE"
        # ALP-462 — signed ``Money``; the wrapper parses the raw "-0.01" string
        # via ``signed_money(broker_str)`` so the negative is preserved exactly.
        assert act.net_amount == signed_money("-0.01")
        assert act.description == "Regulatory fee"
        assert act.symbol is None

    def test_paginates_multiple_pages(self) -> None:
        """Pagination stops when a page is smaller than PAGE_SIZE."""
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        page1 = [_make_trade_activity_raw(), _make_trade_activity_raw()]

        client = _fake_client()
        client.get.side_effect = [page1, []]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))

        assert len(results) == 2

    def test_activity_filter_passed_to_sdk(self) -> None:
        """activity_types and after cursor are forwarded as query params."""
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.return_value = []

        qs = AccountStateQueries(client)
        asyncio.run(_collect_activities(qs, activity_types=("FILL",), after="cursor-abc"))

        call_args = client.get.call_args
        path = call_args[0][0] if call_args[0] else call_args[1].get("path")
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("data")
        assert "/account/activities" in path
        assert "FILL" in params.get("activity_types", "")
        assert params.get("page_token") == "cursor-abc"

    def test_activity_page_size_capped_at_alpaca_max(self) -> None:
        """page_size must be 100 — Alpaca rejects /account/activities requests with
        page_size > 100 (422), unlike /v2/orders which accepts limit up to 500.
        ALP-932: the two endpoints must not share a page-size constant."""
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.return_value = []

        qs = AccountStateQueries(client)
        asyncio.run(_collect_activities(qs))

        call_args = client.get.call_args
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("data")
        assert params.get("page_size") == 100

    def test_activity_snapshot_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.side_effect = [
            [_make_trade_activity_raw()],
            [],
        ]

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))

        with pytest.raises((TypeError, ValidationError)):
            results[0].activity_type = "OTHER"

    def test_empty_activities_yields_nothing(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get.return_value = []

        qs = AccountStateQueries(client)
        results = asyncio.run(_collect_activities(qs))
        assert results == []


# ---------------------------------------------------------------------------
# 10. get_option_contracts — chain enumeration for verify-script strike picking
# ---------------------------------------------------------------------------


def _make_option_contract(
    *,
    underlying: str = "NVDA",
    expiration: date = date(2026, 6, 19),
    strike: float = 100.0,
    contract_type: str = "call",
    status: str = "active",
    symbol: str | None = None,
) -> Any:
    """Build an alpaca-py ``OptionContract`` for chain-enumeration tests."""
    from uuid import uuid4

    from alpaca.trading.models import OptionContract

    occ_strike = f"{round(strike * 1000):08d}"
    type_letter = "C" if contract_type == "call" else "P"
    yymmdd = expiration.strftime("%y%m%d")
    occ_symbol = symbol or f"{underlying:<6}{yymmdd}{type_letter}{occ_strike}"
    return OptionContract.model_validate(
        {
            "id": str(uuid4()),
            "symbol": occ_symbol,
            "name": f"{underlying} {expiration.isoformat()} {strike} {contract_type.upper()}",
            "status": status,
            "tradable": True,
            "expiration_date": expiration.isoformat(),
            "root_symbol": underlying,
            "underlying_symbol": underlying,
            "underlying_asset_id": str(uuid4()),
            "type": contract_type,
            "style": "american",
            "strike_price": strike,
            "size": "100",
        }
    )


def _make_option_contracts_response(
    contracts: list[Any], next_page_token: str | None = None
) -> Any:
    from alpaca.trading.models import OptionContractsResponse

    return OptionContractsResponse(
        option_contracts=contracts,
        next_page_token=next_page_token,
    )


class TestGetOptionContracts:
    def test_returns_option_contract_snapshots_for_underlying_and_expiration(self) -> None:
        """Wraps ``TradingClient.get_option_contracts`` for a given underlying +
        expiration date, returning a tuple of ``OptionContractSnapshot`` records
        sorted by strike price."""
        from alphamind.execution.broker_adapter.queries import (
            AccountStateQueries,
            OptionContractSnapshot,
        )

        contracts = [
            _make_option_contract(strike=120.0),
            _make_option_contract(strike=100.0),
            _make_option_contract(strike=110.0),
        ]
        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response(contracts)

        qs = AccountStateQueries(client)
        result = qs.get_option_contracts(
            underlying=Symbol("NVDA"),
            expiration=date(2026, 6, 19),
        )

        assert isinstance(result, tuple)
        assert len(result) == 3
        assert all(isinstance(c, OptionContractSnapshot) for c in result)
        # ALP-462 — strikes parse via ``price()`` so equality is Decimal-exact.
        assert [c.strike for c in result] == [price("100"), price("110"), price("120")]

    def test_passes_filters_to_sdk_request(self) -> None:
        """The call must build a ``GetOptionContractsRequest`` filtering by
        ``underlying_symbols``, ``expiration_date``, ``type=CALL``,
        ``status=ACTIVE``."""
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response([])

        qs = AccountStateQueries(client)
        qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        call_args = client.get_option_contracts.call_args
        # Either positional or keyword.
        request = call_args[0][0] if call_args[0] else call_args[1].get("request")
        assert isinstance(request, GetOptionContractsRequest)
        assert request.underlying_symbols == ["NVDA"]
        assert request.expiration_date == date(2026, 6, 19)
        assert request.type == ContractType.CALL
        assert request.status == AssetStatus.ACTIVE

    def test_returns_empty_tuple_when_chain_empty(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response([])

        qs = AccountStateQueries(client)
        result = qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        assert result == ()

    def test_returns_empty_tuple_when_response_option_contracts_is_none(self) -> None:
        """``OptionContractsResponse.option_contracts`` is ``Optional[List]``;
        the wrapper coerces ``None`` to an empty tuple so callers don't have to
        special-case it."""
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response(
            contracts=[],
        )
        client.get_option_contracts.return_value.option_contracts = None

        qs = AccountStateQueries(client)
        result = qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        assert result == ()

    def test_snapshot_carries_symbol_strike_and_type(self) -> None:
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response(
            [_make_option_contract(strike=105.0, symbol="NVDA  260619C00105000")],
        )

        qs = AccountStateQueries(client)
        (snap,) = qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        assert snap.symbol == "NVDA  260619C00105000"
        # ALP-462 — strike threaded through ``price()`` for Decimal-exact compare.
        assert snap.strike == price("105")
        assert snap.contract_type == "call"
        assert snap.expiration == date(2026, 6, 19)

    def test_snapshot_is_frozen(self) -> None:
        from pydantic import ValidationError

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response(
            [_make_option_contract(strike=100.0)],
        )

        qs = AccountStateQueries(client)
        (snap,) = qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        with pytest.raises((TypeError, ValidationError)):
            snap.strike = price(200.0)

    def test_paginates_through_next_page_token(self) -> None:
        """Heavily-listed underlyings (SPY/QQQ) routinely surface > 100
        contracts per expiration. The wrapper must follow ``next_page_token``
        until exhaustion so the caller receives the complete chain rather
        than the silently truncated first page.
        """
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        # Three pages of 100, 100, and 50 contracts; chain has 250 total
        # strikes.
        page_one = [_make_option_contract(strike=100.0 + i) for i in range(100)]
        page_two = [_make_option_contract(strike=200.0 + i) for i in range(100)]
        page_three = [_make_option_contract(strike=300.0 + i) for i in range(50)]
        responses = [
            _make_option_contracts_response(page_one, next_page_token="cursor-2"),
            _make_option_contracts_response(page_two, next_page_token="cursor-3"),
            _make_option_contracts_response(page_three, next_page_token=None),
        ]

        client = _fake_client()
        client.get_option_contracts.side_effect = responses

        qs = AccountStateQueries(client)
        result = qs.get_option_contracts(underlying=Symbol("SPY"), expiration=date(2026, 6, 19))

        assert len(result) == 250, (
            f"expected 250 contracts (3 pages of 100/100/50); "
            f"got {len(result)} — pagination did not exhaust next_page_token"
        )
        assert client.get_option_contracts.call_count == 3
        # Cursor advances on subsequent calls.
        first_request = client.get_option_contracts.call_args_list[0][0][0]
        second_request = client.get_option_contracts.call_args_list[1][0][0]
        third_request = client.get_option_contracts.call_args_list[2][0][0]
        assert first_request.page_token is None
        assert second_request.page_token == "cursor-2"
        assert third_request.page_token == "cursor-3"

    def test_pagination_terminates_on_empty_token(self) -> None:
        """Single-page responses (next_page_token None or empty) terminate
        cleanly without an extra fetch.
        """
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        client = _fake_client()
        client.get_option_contracts.return_value = _make_option_contracts_response(
            [_make_option_contract(strike=100.0)], next_page_token=None
        )

        qs = AccountStateQueries(client)
        result = qs.get_option_contracts(underlying=Symbol("NVDA"), expiration=date(2026, 6, 19))

        assert len(result) == 1
        assert client.get_option_contracts.call_count == 1


# ---------------------------------------------------------------------------
# 10b. Sync REST is offloaded off the event loop, time-bounded (ALP-850 / ALP-841)
# ---------------------------------------------------------------------------


class TestSyncRestOffloadedFromEventLoop:
    """The paginating query generators run their blocking REST call on a worker
    thread bounded by a wall-clock timeout, so a hung broker call can neither
    freeze the calling event loop (the ALP-841 wedge) nor block forever.
    """

    def test_hung_get_orders_does_not_block_event_loop(self) -> None:
        """A get_orders REST call that never returns must not freeze the loop.

        While the generator is parked on its (blocked) page fetch, a concurrent
        coroutine on the same loop must continue to make progress — proving the
        sync call was offloaded rather than run inline on the loop.
        """
        import threading

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        release = threading.Event()

        def hang(*_args: Any, **_kwargs: Any) -> list[Any]:
            # Block the worker thread until the test releases it — emulates a
            # broker socket that never responds.
            release.wait(timeout=5.0)
            return []

        client = _fake_client()
        client.get_orders.side_effect = hang

        async def scenario() -> int:
            qs = AccountStateQueries(client)
            ticks = 0

            async def consume_orders() -> None:
                async for _ in qs.get_orders():
                    pass

            async def heartbeat() -> None:
                nonlocal ticks
                # If the REST call were inline on the loop, the generator task
                # would monopolise it and these ticks would never advance.
                for _ in range(5):
                    await asyncio.sleep(0)
                    ticks += 1

            order_task = asyncio.create_task(consume_orders())
            await heartbeat()
            release.set()
            await order_task
            return ticks

        ticks = asyncio.run(scenario())
        assert ticks == 5, "concurrent coroutine starved → REST call ran inline on the loop"

    def test_hung_get_orders_times_out(self) -> None:
        """A REST call that exceeds the per-page budget raises ``TimeoutError``."""
        import threading

        import alphamind.execution.broker_adapter.queries as q_mod
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        never_released = threading.Event()

        def hang(*_args: Any, **_kwargs: Any) -> list[Any]:
            never_released.wait(timeout=5.0)
            return []

        client = _fake_client()
        client.get_orders.side_effect = hang

        original = q_mod._REST_TIMEOUT_SECONDS
        try:
            q_mod._REST_TIMEOUT_SECONDS = 0.05
            qs = AccountStateQueries(client)
            with pytest.raises(TimeoutError):
                asyncio.run(_collect_orders(qs))
        finally:
            q_mod._REST_TIMEOUT_SECONDS = original
            never_released.set()

    def test_hung_get_account_activities_times_out(self) -> None:
        """The activities generator's REST call is likewise time-bounded."""
        import threading

        import alphamind.execution.broker_adapter.queries as q_mod
        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        never_released = threading.Event()

        def hang(*_args: Any, **_kwargs: Any) -> list[Any]:
            never_released.wait(timeout=5.0)
            return []

        client = _fake_client()
        client.get.side_effect = hang

        original = q_mod._REST_TIMEOUT_SECONDS
        try:
            q_mod._REST_TIMEOUT_SECONDS = 0.05
            qs = AccountStateQueries(client)
            with pytest.raises(TimeoutError):
                asyncio.run(_collect_activities(qs))
        finally:
            q_mod._REST_TIMEOUT_SECONDS = original
            never_released.set()

    def test_get_orders_runs_off_the_calling_thread(self) -> None:
        """The blocking SDK call executes on a worker thread, not the caller's."""
        import threading

        from alphamind.execution.broker_adapter.queries import AccountStateQueries

        calling_thread = threading.get_ident()
        observed: list[int] = []

        def record_thread(*_args: Any, **_kwargs: Any) -> list[Any]:
            observed.append(threading.get_ident())
            return []

        client = _fake_client()
        client.get_orders.side_effect = record_thread

        qs = AccountStateQueries(client)
        asyncio.run(_collect_orders(qs))

        assert observed, "the SDK call never ran"
        assert observed[0] != calling_thread, "REST call ran on the event-loop thread"


# ---------------------------------------------------------------------------
# 11. Public surface re-exports
# ---------------------------------------------------------------------------


class TestPublicReExports:
    def test_all_new_symbols_in_dunder_all(self) -> None:
        import alphamind.execution.broker_adapter as pkg

        expected_new = {
            "AccountStateQueries",
            "TradeAccountSnapshot",
            "PositionSnapshot",
            "OrderSnapshot",
            "OrderLegSnapshot",
            "ActivitySnapshot",
            "AssetSnapshot",
            "CalendarDay",
            "MarketClock",
            "OptionContractSnapshot",
        }
        actual = set(pkg.__all__)
        missing = expected_new - actual
        assert not missing, f"Missing from __all__: {missing}"

    def test_symbols_importable_directly_from_package(self) -> None:
        from alphamind.execution.broker_adapter import (
            AccountStateQueries,
            ActivitySnapshot,
            AssetSnapshot,
            CalendarDay,
            MarketClock,
            OptionContractSnapshot,
            OrderLegSnapshot,
            OrderSnapshot,
            PositionSnapshot,
            TradeAccountSnapshot,
        )

        _ = (
            AccountStateQueries,
            TradeAccountSnapshot,
            PositionSnapshot,
            OrderSnapshot,
            OrderLegSnapshot,
            ActivitySnapshot,
            AssetSnapshot,
            CalendarDay,
            MarketClock,
            OptionContractSnapshot,
        )
