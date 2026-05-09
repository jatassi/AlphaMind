"""Account state GET wrappers over the Alpaca REST API (story 02a / ALP-379).

Ships a single ``AccountStateQueries`` class whose methods wrap the seven
Alpaca REST GET endpoints the OMS, continuous monitor, paper-evaluation
harness, and corporate-actions processor consume for state reconciliation.

All methods are thin translators: they call ``TradingClient`` (alpaca-py),
convert the SDK response into a frozen Pydantic record, and propagate errors
to callers without re-wrapping — callers use ``classify_alpaca_error`` from
story 01 to decide retry/abort semantics.

The two paginating methods (``get_orders``, ``get_account_activities``) expose
async generators so callers can stop early without fetching every page.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any, Literal
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ActivityType, QueryOrderStatus
from alpaca.trading.models import (
    Asset,
    Calendar,
    Clock,
    NonTradeActivity,
    Order,
    Position,
    TradeAccount,
    TradeActivity,
)
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
from pydantic import BaseModel, ConfigDict, TypeAdapter

_ET = ZoneInfo("America/New_York")

# Hoisted at module level — TypeAdapter construction is not free.
_TRADE_ACTIVITY_ADAPTER: TypeAdapter[TradeActivity] = TypeAdapter(TradeActivity)
_NON_TRADE_ACTIVITY_ADAPTER: TypeAdapter[NonTradeActivity] = TypeAdapter(NonTradeActivity)

_PAGE_SIZE = 500

# ---------------------------------------------------------------------------
# Typed response records — all frozen
# ---------------------------------------------------------------------------


class TradeAccountSnapshot(BaseModel):
    """Subset of ``TradeAccount`` fields consumed by AlphaMind."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    cash: float
    equity: float
    buying_power: float
    regt_buying_power: float
    daytrading_buying_power: float
    maintenance_margin: float
    daytrade_count: int
    pattern_day_trader: bool
    status: str


class PositionSnapshot(BaseModel):
    """Subset of Alpaca ``Position`` fields consumed by AlphaMind."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: Literal["us_equity", "us_option", "crypto"]
    qty: float
    avg_entry_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float
    current_price: float | None
    side: Literal["long", "short"]


class OrderLegSnapshot(BaseModel):
    """Per-leg child embedded in an ``OrderSnapshot.legs`` (mleg parent only)."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None = None
    side: str
    position_intent: str
    status: str


class OrderSnapshot(BaseModel):
    """Subset of Alpaca ``Order`` fields consumed by AlphaMind."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    client_order_id: str
    symbol: str
    asset_class: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None = None
    side: str
    order_type: str
    time_in_force: str
    order_class: str
    status: str
    submitted_at: dt.datetime
    filled_at: dt.datetime | None
    canceled_at: dt.datetime | None = None
    expired_at: dt.datetime | None = None
    replaced_by: str | None
    replaces: str | None
    legs: tuple[OrderLegSnapshot, ...] | None


class ActivitySnapshot(BaseModel):
    """Common projection of ``TradeActivity`` / ``NonTradeActivity``."""

    model_config = ConfigDict(frozen=True)

    id: str
    activity_type: str
    transaction_time: dt.datetime | None
    symbol: str | None
    qty: float | None
    price: float | None
    net_amount: float | None
    side: str | None
    description: str | None
    raw: dict[str, Any]


class AssetSnapshot(BaseModel):
    """Subset of Alpaca ``Asset`` fields consumed by AlphaMind."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    asset_class: str
    tradable: bool
    shortable: bool
    easy_to_borrow: bool
    fractionable: bool
    marginable: bool


class CalendarDay(BaseModel):
    """Market calendar day with tz-aware open/close times.

    ``session_open`` / ``session_close`` mirror ``open_time`` / ``close_time``
    for the standard retail Trading API (Alpaca does not surface extended-hours
    session boundaries on ``GET /v2/calendar``).
    """

    model_config = ConfigDict(frozen=True)

    date: dt.date
    open_time: dt.datetime
    close_time: dt.datetime
    session_open: dt.datetime
    session_close: dt.datetime


class MarketClock(BaseModel):
    """Current market state from ``GET /v2/clock``."""

    model_config = ConfigDict(frozen=True)

    timestamp: dt.datetime
    is_open: bool
    next_open: dt.datetime
    next_close: dt.datetime


# ---------------------------------------------------------------------------
# Private conversion helpers
# ---------------------------------------------------------------------------


def _enum_str(val: Any) -> str:
    """Return the string value of an enum or the string itself."""
    return val.value if hasattr(val, "value") else str(val)


def _to_aware(ts: dt.datetime | None) -> dt.datetime | None:
    """Return *ts* as a UTC-aware datetime; ``None`` passes through."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.UTC)
    return ts


def _localize_naive(ts: dt.datetime) -> dt.datetime:
    """Attach America/New_York timezone to a naive datetime (calendar open/close)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_ET)
    return ts


def _convert_order(order: Order) -> OrderSnapshot:
    raw_legs = order.legs
    legs: tuple[OrderLegSnapshot, ...] | None = None
    if raw_legs:
        legs = tuple(
            OrderLegSnapshot(
                order_id=str(leg.id),
                symbol=str(leg.symbol),
                qty=float(leg.qty or 0),
                filled_qty=float(leg.filled_qty or 0),
                filled_avg_price=_optional_float(leg.filled_avg_price),
                side=_enum_str(leg.side),
                position_intent=_enum_str(leg.position_intent) if leg.position_intent else "",
                status=_enum_str(leg.status),
            )
            for leg in raw_legs
        )

    order_type = order.order_type or order.type
    return OrderSnapshot(
        order_id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=str(order.symbol),
        asset_class=_enum_str(order.asset_class),
        qty=float(order.qty or 0),
        filled_qty=float(order.filled_qty or 0),
        filled_avg_price=_optional_float(order.filled_avg_price),
        side=_enum_str(order.side),
        order_type=_enum_str(order_type),
        time_in_force=_enum_str(order.time_in_force),
        order_class=_enum_str(order.order_class),
        status=_enum_str(order.status),
        submitted_at=_to_aware(order.submitted_at),  # type: ignore[arg-type]
        filled_at=_to_aware(order.filled_at),
        canceled_at=_to_aware(order.canceled_at),
        expired_at=_to_aware(order.expired_at),
        replaced_by=(str(order.replaced_by) if order.replaced_by is not None else None),
        replaces=(str(order.replaces) if order.replaces is not None else None),
        legs=legs,
    )


def _optional_float(value: str | float | None) -> float | None:
    """Coerce alpaca-py's ``str | float | None`` price field to ``float | None``."""
    if value is None:
        return None
    return float(value)


def _convert_activity(activity: TradeActivity | NonTradeActivity) -> ActivitySnapshot:
    """Project a ``TradeActivity`` or ``NonTradeActivity`` into ``ActivitySnapshot``."""
    raw = activity.model_dump()

    # TradeActivity has transaction_time; NonTradeActivity has date (not a datetime)
    transaction_time: dt.datetime | None = None
    if isinstance(activity, TradeActivity) and activity.transaction_time is not None:
        transaction_time = _to_aware(activity.transaction_time)

    qty_raw = getattr(activity, "qty", None)
    price_raw = getattr(activity, "price", None)
    net_amount_raw = getattr(activity, "net_amount", None)
    side_raw = getattr(activity, "side", None)

    return ActivitySnapshot(
        id=str(activity.id),
        activity_type=_enum_str(activity.activity_type),
        transaction_time=transaction_time,
        symbol=getattr(activity, "symbol", None),
        qty=float(qty_raw) if qty_raw is not None else None,
        price=float(price_raw) if price_raw is not None else None,
        net_amount=float(net_amount_raw) if net_amount_raw is not None else None,
        side=_enum_str(side_raw) if side_raw is not None else None,
        description=getattr(activity, "description", None),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# AccountStateQueries
# ---------------------------------------------------------------------------


class AccountStateQueries:
    """Read-only Alpaca state via the alpaca-py ``TradingClient``.

    All methods are sync (alpaca-py wraps httpx synchronously) except the two
    paginating methods (``get_orders``, ``get_account_activities``) which
    expose async generators for cursor-driven traversal so callers can stop
    early without fetching every page.

    Errors from alpaca-py propagate without translation. Callers wrap in
    their own try/except using
    :func:`~alphamind.execution.broker_adapter.errors.classify_alpaca_error`.
    The one exception: :meth:`get_asset` returns ``None`` on 404 (unknown
    symbol) rather than raising, because callers need to handle "symbol not
    in Alpaca's universe" as data, not exceptional flow.
    """

    def __init__(self, client: TradingClient) -> None:
        if not isinstance(client, TradingClient):
            msg = f"client must be a TradingClient instance, got {type(client).__name__}"
            raise TypeError(msg)
        self._client = client

    def get_account(self) -> TradeAccountSnapshot:
        """Return a frozen ``TradeAccountSnapshot`` from ``GET /v2/account``."""
        result = self._client.get_account()
        if not isinstance(result, TradeAccount):
            msg = "get_account returned unexpected raw-data response"
            raise TypeError(msg)
        return TradeAccountSnapshot(
            account_id=str(result.id),
            cash=float(result.cash or 0),
            equity=float(result.equity or 0),
            buying_power=float(result.buying_power or 0),
            regt_buying_power=float(result.regt_buying_power or 0),
            daytrading_buying_power=float(result.daytrading_buying_power or 0),
            maintenance_margin=float(result.maintenance_margin or 0),
            daytrade_count=int(result.daytrade_count or 0),
            pattern_day_trader=bool(result.pattern_day_trader),
            status=_enum_str(result.status),
        )

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        """Return a frozen tuple of ``PositionSnapshot`` sorted by symbol."""
        result = self._client.get_all_positions()
        if not isinstance(result, list):
            msg = "get_all_positions returned unexpected raw-data response"
            raise TypeError(msg)
        positions: list[Position] = result
        snapshots = sorted(
            (
                PositionSnapshot(
                    symbol=str(pos.symbol),
                    asset_class=_enum_str(pos.asset_class),  # type: ignore[arg-type]
                    qty=float(pos.qty or 0),
                    avg_entry_price=float(pos.avg_entry_price or 0),
                    market_value=float(pos.market_value or 0),
                    cost_basis=float(pos.cost_basis or 0),
                    unrealized_pl=float(pos.unrealized_pl or 0),
                    unrealized_plpc=float(pos.unrealized_plpc or 0),
                    current_price=(
                        float(pos.current_price) if pos.current_price is not None else None
                    ),
                    side=_enum_str(pos.side),  # type: ignore[arg-type]
                )
                for pos in positions
            ),
            key=lambda s: s.symbol,
        )
        return tuple(snapshots)

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncGenerator[OrderSnapshot]:
        """Yield ``OrderSnapshot`` records matching filters, paginating as needed.

        Alpaca paginates ``GET /v2/orders`` by returning up to ``limit`` orders
        per call; exhaustion is signalled by an empty list or a list shorter
        than ``limit``.  The ``until`` field advances the cursor on each page.
        When ``since`` is supplied it maps to the request's ``after`` field per
        the disconnect-recovery design (``broker-adapter.md``).
        """
        status_enum = QueryOrderStatus(status)
        until_cursor = until

        while True:
            req = GetOrdersRequest(
                status=status_enum,
                limit=_PAGE_SIZE,
                after=since,
                until=until_cursor,
                symbols=list(symbols) if symbols else None,
            )
            page_result = self._client.get_orders(filter=req)
            if not isinstance(page_result, list) or not page_result:
                break
            page: list[Order] = page_result
            for order in page:
                yield _convert_order(order)
            if len(page) < _PAGE_SIZE:
                break
            # Advance cursor past the oldest order on this page.
            until_cursor = page[-1].submitted_at

    async def get_account_activities(
        self,
        *,
        activity_types: tuple[str, ...] | None = None,
        after: str | None = None,
        until: dt.datetime | None = None,
    ) -> AsyncGenerator[ActivitySnapshot]:
        """Yield ``ActivitySnapshot`` records, cursor-paginating until exhaustion.

        ``TradingClient`` does not expose a high-level ``get_account_activities``
        method (that lives on ``BrokerClient`` which targets a different base URL).
        We drive ``GET /account/activities`` directly via ``TradingClient.get()``,
        replicating the page-token loop from
        ``BrokerClient._get_account_activities_iterator``.

        ``ActivityType.is_str_trade_activity`` is the same discriminator the SDK
        uses to route raw dicts into ``TradeActivity`` vs ``NonTradeActivity``.
        """
        params: dict[str, Any] = {"page_size": _PAGE_SIZE}
        if activity_types:
            params["activity_types"] = ",".join(activity_types)
        if after:
            params["page_token"] = after
        if until is not None:
            params["until"] = until.isoformat()

        while True:
            result = self._client.get("/account/activities", params)
            if not isinstance(result, list) or not result:
                break
            for raw in result:
                if ActivityType.is_str_trade_activity(raw.get("activity_type", "")):
                    activity: TradeActivity | NonTradeActivity = (
                        _TRADE_ACTIVITY_ADAPTER.validate_python(raw)
                    )
                else:
                    activity = _NON_TRADE_ACTIVITY_ADAPTER.validate_python(raw)
                yield _convert_activity(activity)
            last_raw = result[-1]
            if "id" not in last_raw:
                break
            params["page_token"] = last_raw["id"]
            if len(result) < _PAGE_SIZE:
                break

    def get_asset(self, symbol: str) -> AssetSnapshot | None:
        """Return ``AssetSnapshot`` for *symbol*, or ``None`` if the symbol is unknown.

        Alpaca returns 404 for symbols not in its universe.  The wrapper
        converts 404 → ``None`` so callers treat "unknown symbol" as data.
        Non-404 errors propagate unchanged.
        """
        try:
            result = self._client.get_asset(symbol)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(result, Asset):
            msg = "get_asset returned unexpected raw-data response"
            raise TypeError(msg)
        return AssetSnapshot(
            symbol=str(result.symbol),
            name=str(result.name or ""),
            asset_class=_enum_str(result.asset_class),
            tradable=bool(result.tradable),
            shortable=bool(result.shortable),
            easy_to_borrow=bool(result.easy_to_borrow),
            fractionable=bool(result.fractionable),
            marginable=bool(result.marginable),
        )

    def get_calendar(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> tuple[CalendarDay, ...]:
        """Return tz-aware ``CalendarDay`` records bounded by *start*/*end*.

        Times on the alpaca-py ``Calendar`` model are naive datetimes built from
        ``%H:%M`` strings; we attach ``America/New_York`` so callers always
        receive tz-aware values.
        """
        filters: GetCalendarRequest | None = None
        if start is not None or end is not None:
            filters = GetCalendarRequest(start=start, end=end)
        result = self._client.get_calendar(filters=filters)
        if not isinstance(result, list):
            msg = "get_calendar returned unexpected raw-data response"
            raise TypeError(msg)
        calendars: list[Calendar] = result
        return tuple(
            CalendarDay(
                date=cal.date,
                open_time=_localize_naive(cal.open),
                close_time=_localize_naive(cal.close),
                session_open=_localize_naive(cal.open),
                session_close=_localize_naive(cal.close),
            )
            for cal in calendars
        )

    def get_clock(self) -> MarketClock:
        """Return a ``MarketClock`` snapshot from ``GET /v2/clock``."""
        result = self._client.get_clock()
        if not isinstance(result, Clock):
            msg = "get_clock returned unexpected raw-data response"
            raise TypeError(msg)
        return MarketClock(
            timestamp=_to_aware(result.timestamp),  # type: ignore[arg-type]
            is_open=bool(result.is_open),
            next_open=_to_aware(result.next_open),  # type: ignore[arg-type]
            next_close=_to_aware(result.next_close),  # type: ignore[arg-type]
        )
