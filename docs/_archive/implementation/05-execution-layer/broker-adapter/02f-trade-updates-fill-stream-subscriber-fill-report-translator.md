# 02f — trade_updates fill-stream subscriber + fill-report translator

## Goal

Ship the `subscribe_trade_updates(...)` async-generator primitive that subscribes to Alpaca's `trade_updates` websocket via alpaca-py's `TradingStream`, authenticates, and yields fill-report-shaped events for every order-lifecycle event (new / fill / partial_fill / canceled / expired / replaced / replace_rejected / stopped / rejected / done_for_day). Build the per-event translator that maps alpaca-py's `TradeUpdate` into the OMS-facing `FillReport` shape per `architecture.md § Fill report contract`. Handle mleg parent + per-leg children correlation: a parent strategy event yields one report; per-leg children events yield reports tagged with the parent's `client_order_id`.

The subscriber primitive is consumed by the continuous-monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>) which wraps it with the run-forever lifecycle, fill-buffer write integration, and disconnect-recovery orchestration. This story ships only the primitive — story 03d covers the disconnect-recovery routine the monitor invokes on reconnect.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Fill stream — `trade_updates` channel, binary-framed JSON/MessagePack, per-event-type table, mleg fill events, atomicity contract, re-subscription on disconnect.
* `docs/design/05-execution-layer/architecture.md` § Fill report contract — the OMS-facing fill report shape (Order ID, Alpaca order ID, fill timestamp, fill price, fill quantity, remaining quantity, order status, execution venue, `live_execution_estimate` paper-mode metadata).
* `docs/design/05-execution-layer/architecture.md` § 4a Alpaca fill-stream consumption — the monitor subscribes via the broker adapter; binary-framed events; disconnect recovery via GET /v2/orders since-recovery.
* `docs/design/05-execution-layer/state-persistence.md` § Tier 2 Lifecycle entities — Fill records — fields the report populates.
* `src/alphamind/portfolio_state/records/positions.py` — `LiveExecutionEstimate`, `PositionFill` (per-position fill log entries; the OMS maps `FillReport` onto these).
* `alpaca-py` SDK source for `TradingStream`: `subscribe_trade_updates`, the `TradeUpdate` model with `event` / `order` / `timestamp` / `position_qty` / `price` / `qty` / `execution_id` fields.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (D) — websocket primitive in adapter; lifecycle in monitor.

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories) — provides `AlpacaClientFactory.build_trading_stream()`.

## Scope

Source under `src/alphamind/execution/broker_adapter/fill_stream.py` (new file). Tests at `tests/execution/broker_adapter/test_fill_stream.py`.

### 1. `FillReport` Pydantic record

Match `architecture.md § Fill report contract` field-for-field. Frozen Pydantic.

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


OrderStatus = Literal[
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
]


class FillReport(BaseModel):
    """OMS-facing projection of an Alpaca trade_updates event.

    Every alpaca-py TradeUpdate translates to one FillReport. mleg parent
    events produce one report; per-leg children produce reports each with
    parent_client_order_id populated.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    alpaca_order_id: str
    parent_client_order_id: str | None  # set on mleg per-leg children
    parent_alpaca_order_id: str | None  # set on mleg per-leg children
    event_type: OrderStatus
    fill_timestamp: datetime  # tz-aware; Alpaca's event timestamp, not collection time
    fill_price: float | None  # None for non-fill events
    fill_quantity: float | None  # None for non-fill events
    cumulative_filled_quantity: float
    remaining_quantity: float
    execution_venue: str | None  # exchange code in live mode; "paper" / None in paper
    occ_symbol: str | None  # populated for options/mleg-leg events; None for equity
    position_intent: Literal[
        "buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close",
    ] | None  # populated on mleg leg children; None elsewhere
    raw_event_payload: dict[str, Any]  # alpaca-py TradeUpdate.model_dump() for forward compat
```

The `live_execution_estimate` field is NOT populated by this story — it's the paper-evaluation harness's responsibility (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>) to attach metadata. The monitor's run-loop interposes the harness between this primitive and the fill-buffer write (per architecture).

### 2. `subscribe_trade_updates` async generator

```python
from collections.abc import AsyncIterator

from alpaca.trading.stream import TradingStream


async def subscribe_trade_updates(
    stream: TradingStream,
) -> AsyncIterator[FillReport]:
    """Subscribe to trade_updates and yield FillReport per event.

    Connects, authenticates, subscribes to the trade_updates channel, and
    yields one FillReport per alpaca-py TradeUpdate received. Each
    TradeUpdate flows through translate_trade_update; mleg parent events
    yield one report, and each leg child of a parent yields its own report
    referencing the parent.

    The monitor (ALP-123) wraps this generator with:
    * connect/disconnect lifecycle
    * fill-buffer durable write per yielded FillReport
    * reconnect orchestration on websocket failure
    * GET /v2/orders since-recovery on reconnect (story 03d)

    The primitive itself yields events as they arrive; on
    asyncio.CancelledError or stream close, the generator exits cleanly.
    Any exception during translation raises out — the caller's run-loop
    catches and triggers reconnect.
    """
    ...
```

Implementation notes:

* alpaca-py's `TradingStream.subscribe_trade_updates(handler)` registers a coroutine handler; this story uses an `asyncio.Queue` bridge so the registered handler enqueues `TradeUpdate` payloads and the generator drains the queue.
* The generator handles connect timing: register the handler, then call `stream.run()` in a background task, then begin draining the queue. On exit, cancel the background task.

### 3. `translate_trade_update` translator function

The pure-translation core. Takes an alpaca-py `TradeUpdate` and returns 1+ `FillReport` (one for parent equity/options events, multiple for mleg parent + per-leg).

```python
def translate_trade_update(update: TradeUpdate) -> tuple[FillReport, ...]:
    """Map an alpaca-py TradeUpdate into one or more FillReport records.

    For equity / single-leg options: returns a single-element tuple.
    For mleg parent events: returns the parent FillReport followed by
    per-leg FillReport children, each with parent_client_order_id and
    parent_alpaca_order_id populated.
    """
    ...
```

### 4. Per-event-type mapping

Per `broker-adapter.md § Fill stream § Event types consumed`:

| alpaca-py event | FillReport.event_type | Notes |
| -- | -- | -- |
| `new` | `"new"` | Order accepted by venue. fill_price / fill_quantity = None. |
| `fill` | `"filled"` | Full fill. Populate fill_price + fill_quantity from event. |
| `partial_fill` | `"partially_filled"` | Populate fill_price + fill_quantity from event. |
| `canceled` | `"canceled"` | fill_price / fill_quantity = None. |
| `expired` | `"expired"` | TIF expired. |
| `replaced` | `"replaced"` | PATCH replacement took effect. New Alpaca order ID is in the event payload. |
| `replace_rejected` | `"replace_rejected"` | PATCH race lost; original order remains active. |
| `stopped` | `"stopped"` | Treated as a fill variant. fill_price / fill_quantity from event. |
| `rejected` | `"rejected"` | Post-acceptance rejection (rare). |
| `done_for_day` | `"done_for_day"` | Day order with remaining qty at session close. |
| `order_replace_rejected` | `"replace_rejected"` | Alias for `replace_rejected`. |

Other alpaca-py event types not in this table (e.g., `pending_new`, `pending_cancel`, `calculated`) are filtered — no FillReport produced. The translator returns an empty tuple for filtered events. The generator yields only non-empty tuples' contents.

### 5. mleg parent + per-leg correlation

When the alpaca-py `TradeUpdate` arrives for an mleg parent strategy:

* `update.order.order_class == "mleg"` and `update.order.legs` is populated.
* Produce one `FillReport` for the parent (no `parent_client_order_id`, `occ_symbol=None`, `position_intent=None`).
* For each leg in `update.order.legs`, produce a child `FillReport` with `parent_client_order_id = parent.client_order_id`, `parent_alpaca_order_id = parent.id`, `occ_symbol = leg.symbol`, `position_intent = leg.position_intent`.

The atomicity contract from `broker-adapter.md § Multi-leg fill events § Atomicity` applies at the OMS level (waiting for "all legs filled" before activating the strategy-level bracket); this story produces individual reports, the OMS aggregates downstream.

### 6. Public surface re-exports

Update `broker_adapter/__init__.py` to add `subscribe_trade_updates`, `translate_trade_update`, `FillReport`, `OrderStatus`.

### Out of scope

* Run-forever loop — continuous-monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Fill-buffer durable write — already shipped under state-persistence (<issue id="beaf98a0-a9fc-44a8-ac46-32e50f604345">ALP-119</issue>); the monitor wraps the generator with `persist_fill(report)`.
* Disconnect recovery (`GET /v2/orders since-recovery`) — story 03d.
* `live_execution_estimate` annotation — paper-evaluation-harness work tree (<issue id="aa192802-32fd-4562-b48a-a8452298e176">ALP-130</issue>).
* Per-leg fill-correlation aggregation (\"all legs filled → activate strategy\") — that's an OMS concern (extends Phase 1 fill integration in story 04b).

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/fill_stream.py` exists; `subscribe_trade_updates`, `translate_trade_update`, `FillReport`, `OrderStatus` are importable from `alphamind.execution.broker_adapter`.
- [ ] `FillReport` is a frozen Pydantic model with all the documented fields; the `event_type` field accepts only the documented `OrderStatus` literals.
- [ ] `translate_trade_update` for an equity `fill` event returns a single-element tuple with `event_type="filled"`, `fill_price` and `fill_quantity` populated from the event, `parent_client_order_id=None`, `occ_symbol=None`.
- [ ] `translate_trade_update` for a single-leg-options `partial_fill` returns one report with `occ_symbol` populated from the order's symbol field, `event_type="partially_filled"`.
- [ ] `translate_trade_update` for an mleg parent `fill` returns N+1 reports: one parent (no leg fields), N children (one per leg) each with `parent_client_order_id` populated, `occ_symbol` and `position_intent` set.
- [ ] `translate_trade_update` for `canceled` returns one report with `event_type="canceled"` and `fill_price`/`fill_quantity` both `None`.
- [ ] `translate_trade_update` for `replaced` populates `alpaca_order_id` with the NEW (post-replacement) Alpaca order ID, the OMS uses this to extend its `alpaca_order_id_chain`.
- [ ] `translate_trade_update` for filtered event types (e.g., `pending_new`) returns an empty tuple.
- [ ] `subscribe_trade_updates(stream)` registers a handler with `stream.subscribe_trade_updates(handler)` and starts `stream.run()` in a background task.
- [ ] `subscribe_trade_updates` yields `FillReport` records as events arrive; multiple reports from a single mleg `TradeUpdate` are yielded individually in order (parent first, then per-leg children).
- [ ] `subscribe_trade_updates` exits cleanly on `asyncio.CancelledError` from the consumer; the background `stream.run()` task is cancelled.
- [ ] `subscribe_trade_updates` re-raises any exception encountered during translation (so the monitor's run-loop can catch + reconnect).
- [ ] All `FillReport.fill_timestamp` values are tz-aware UTC.
- [ ] Tests use a fake `TradingStream` (or alpaca-py's test harness) that injects synthetic `TradeUpdate` payloads; cover: each event type, mleg parent + per-leg children, filtered events, generator cancellation, exception propagation.
- [ ] `uv run pytest tests/execution/broker_adapter/test_fill_stream.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_fill_stream.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by constructing a synthetic mleg `TradeUpdate` with 3 legs and asserting `translate_trade_update` returns a 4-tuple with the parent first.
* Lint clean per CLAUDE.md.
