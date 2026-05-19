# 02a — Account state GET wrappers

## Goal

Ship thin, typed wrappers over the seven Alpaca REST GET endpoints the OMS, continuous monitor, paper-evaluation harness, and corporate-actions processor consume for state reconciliation. Each wrapper returns a Pydantic model bound to the alpaca-py SDK's response shape, with pagination on the two endpoints that need it (`/v2/orders`, `/v2/account/activities`). The wrappers contain no business logic — they convert SDK responses into typed records and propagate Alpaca errors up to callers via story 01's `classify_alpaca_error` taxonomy.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Account state queries — the seven endpoints' purpose and consumers.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path — Phase 1 calls `GET /v2/positions` and `GET /v2/account` for reconciliation; the disconnect-recovery routine (story 03d) calls `GET /v2/orders?since=...`.
* `docs/design/05-execution-layer/corporate-actions.md` § Source of truth: Alpaca — corporate actions consume `GET /v2/account/activities?after=<cursor>` with cursor pagination.
* `alpaca-py` SDK source for `TradingClient` methods: `get_account()`, `get_all_positions()`, `get_orders()`, `get_account_activities()`, `get_asset()`, `get_calendar()`, `get_clock()`. Inspect the request-parameter classes (e.g., `GetOrdersRequest`, `GetAccountActivitiesRequest`) and response models (`TradeAccount`, `Position`, `Order`, `BaseActivity` subclasses, `Asset`, `Calendar`, `Clock`).
* `src/alphamind/execution/broker_adapter/client_factory.py` (story 01) — `AlpacaClientFactory.build_trading_client()` is what this story uses to obtain a `TradingClient`.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (B) — pagination policy.

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories) — provides `AlpacaClientFactory.build_trading_client()` and `classify_alpaca_error`.

## Scope

Source under `src/alphamind/execution/broker_adapter/queries.py` (new file). Tests at `tests/execution/broker_adapter/test_queries.py`. The seven wrappers below all sit on a single `AccountStateQueries` class so they share a `TradingClient` instance per adapter lifetime.

### 1\. `AccountStateQueries`

```python
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Literal

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetAccountActivitiesRequest,
    GetAssetsRequest,
    GetCalendarRequest,
    GetOrdersRequest,
)
from pydantic import BaseModel, ConfigDict


class AccountStateQueries:
    """Read-only Alpaca state via the alpaca-py TradingClient.

    All methods are sync (alpaca-py wraps httpx synchronously). The two
    paginating methods (orders, activities) expose async iterators for
    cursor-driven traversal so callers can stop early without fetching
    every page.
    """

    def __init__(self, client: TradingClient) -> None:
        self._client = client

    def get_account(self) -> TradeAccountSnapshot:
        ...

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        ...

    async def get_orders(
        self,
        *,
        status: Literal["open", "closed", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> AsyncIterator[OrderSnapshot]:
        """Yield orders matching filters, paginating cursors as needed."""
        ...

    async def get_account_activities(
        self,
        *,
        activity_types: tuple[str, ...] | None = None,
        after: str | None = None,  # opaque cursor from a prior page
        until: datetime | None = None,
    ) -> AsyncIterator[ActivitySnapshot]:
        """Yield account activities, cursor-paginating until exhaustion."""
        ...

    def get_asset(self, symbol: str) -> AssetSnapshot:
        ...

    def get_calendar(
        self, *, start: date | None = None, end: date | None = None
    ) -> tuple[CalendarDay, ...]:
        ...

    def get_clock(self) -> MarketClock:
        ...
```

### 2\. Typed response records

Each wrapper returns a Pydantic record with `model_config = ConfigDict(frozen=True)`. The fields are a strict subset of alpaca-py's response models, naming only what AlphaMind consumes.

* `TradeAccountSnapshot`: `account_id: str`, `cash: float`, `equity: float`, `buying_power: float`, `regt_buying_power: float`, `daytrading_buying_power: float`, `maintenance_margin: float`, `daytrade_count: int`, `pattern_day_trader: bool`, `status: str`.
* `PositionSnapshot`: `symbol: str`, `asset_class: Literal["us_equity", "us_option", "crypto"]`, `qty: float`, `avg_entry_price: float`, `market_value: float`, `cost_basis: float`, `unrealized_pl: float`, `unrealized_plpc: float`, `current_price: float | None`, `side: Literal["long", "short"]`. Multi-leg strategy positions are NOT populated by Alpaca's `/v2/positions` (it returns per-leg or per-instrument; strategy positions are reconstructed by the OMS from the order's `client_order_id` correlation, not this endpoint).
* `OrderSnapshot`: `order_id: str` (Alpaca's UUID), `client_order_id: str`, `symbol: str`, `asset_class: str`, `qty: float`, `filled_qty: float`, `side: str`, `order_type: str`, `time_in_force: str`, `order_class: str`, `status: str`, `submitted_at: datetime`, `filled_at: datetime | None`, `replaced_by: str | None`, `replaces: str | None`, `legs: tuple[OrderLegSnapshot, ...] | None` (mleg parents only).
* `OrderLegSnapshot` (for mleg children embedded in an `OrderSnapshot.legs`): `order_id: str`, `symbol: str`, `qty: float`, `filled_qty: float`, `side: str`, `position_intent: str`, `status: str`.
* `ActivitySnapshot`: `id: str` (cursor-stable), `activity_type: str`, `transaction_time: datetime`, `symbol: str | None`, `qty: float | None`, `price: float | None`, `net_amount: float | None`, `side: str | None`, `description: str | None`, `raw: dict[str, Any]` (the alpaca-py model's `model_dump()` for forward compat with subtypes corporate-actions / paper-harness consume).
* `AssetSnapshot`: `symbol: str`, `name: str`, `asset_class: str`, `tradable: bool`, `shortable: bool`, `easy_to_borrow: bool`, `fractionable: bool`, `marginable: bool`.
* `CalendarDay`: `date: date`, `open_time: datetime`, `close_time: datetime`, `session_open: datetime`, `session_close: datetime`. Times are tz-aware (America/New_York → UTC normalized).
* `MarketClock`: `timestamp: datetime` (now), `is_open: bool`, `next_open: datetime`, `next_close: datetime`.

### 3\. Pagination iteration

`get_orders` and `get_account_activities` MUST page until exhaustion. Implementation: alpaca-py's pagination — check the SDK's request-parameter shape (`page_token` / `cursor` / `until`-driven iteration) and build an async generator that loops `while True: page = client.get_*(request); yield from page; if len(page) < page_size: break`.

For `get_orders`: when `since` is supplied, pass it as `after` on the request; combine with `status="all"` for disconnect recovery's case-by-case fetch.

For `get_account_activities`: support both `activity_types` filtering and the opaque `after` cursor (Alpaca returns activities with stable `id` values that callers checkpoint).

### 4\. Error propagation

Errors from alpaca-py raise out of the wrappers without translation. Callers wrap in their own try/except using `classify_alpaca_error` from story 01. The wrappers' job is shape, not error semantics.

The one exception: `get_asset(symbol)` for an unknown symbol returns `404`; the wrapper returns `None` rather than raising. Per design, callers (e.g., guardrail layer's short-sell eligibility check) need to handle "asset doesn't exist in Alpaca's universe" without exception flow.

### 5\. Public surface re-exports

Update `src/alphamind/execution/broker_adapter/__init__.py` to re-export `AccountStateQueries` and the seven snapshot record types.

### Out of scope

* Order POST translation (stories 02b/c/d).
* Order PATCH / DELETE (story 02e).
* Trade-update websocket (story 02f).
* Settlement-date computation (story 04a) and account-derived venue state (story 03b) — those compose on top of these wrappers.
* Corporate-action activity-type taxonomy beyond the catch-all `raw: dict` field — that lives in the corporate-actions work tree.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/queries.py` exists and `AccountStateQueries` is importable from `alphamind.execution.broker_adapter`.
- [ ] `AccountStateQueries(client)` wraps an `alpaca.trading.client.TradingClient` instance; constructing with a non-`TradingClient` raises `TypeError`.
- [ ] `get_account()` returns a `TradeAccountSnapshot` with every documented field populated.
- [ ] `get_positions()` returns a tuple of `PositionSnapshot` records sorted by symbol.
- [ ] `get_asset("NVDA")` against Alpaca paper returns an `AssetSnapshot` with `tradable=True` and `easy_to_borrow=True`; `get_asset("ZZNONEXISTENT")` returns `None`.
- [ ] `get_calendar(start=..., end=...)` returns tz-aware `CalendarDay` records bounded by the supplied date range.
- [ ] `get_clock()` returns a `MarketClock` whose `is_open`, `next_open`, `next_close` are consistent with the trading calendar.
- [ ] `async for order in queries.get_orders(status="all")` yields `OrderSnapshot` records and pages cursor-by-cursor until Alpaca returns no more.
- [ ] `get_orders(since=<datetime>)` filters to orders submitted on or after `since`.
- [ ] `async for activity in queries.get_account_activities(activity_types=("FILL",), after=<cursor>)` yields `ActivitySnapshot` records and pages until exhaustion.
- [ ] Mleg parent orders surface `OrderSnapshot.legs` populated with `OrderLegSnapshot` children carrying `position_intent` strings (`"buy_to_open"` / `"sell_to_open"` / `"buy_to_close"` / `"sell_to_close"`); equity / single-leg-options orders have `legs=None`.
- [ ] All snapshot Pydantic models are frozen (`ConfigDict(frozen=True)`).
- [ ] Tests exercise each wrapper against a `responses`-mocked alpaca-py client: shape assertions on every snapshot field, pagination iteration exhaustion, `get_asset` 404 → None, error propagation through to caller.
- [ ] Tests under `tests/execution/broker_adapter/test_queries.py` pass under `uv run pytest tests/execution/broker_adapter/test_queries.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_queries.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by `python -c "from alphamind.execution.broker_adapter import AccountStateQueries, TradeAccountSnapshot, OrderSnapshot, ActivitySnapshot; print('ok')\"`.