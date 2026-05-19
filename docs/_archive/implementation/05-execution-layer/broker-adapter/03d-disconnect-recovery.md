# 03d — Disconnect recovery

## Goal

Ship `recover_missed_fills_since(...)` — the GET-based recovery routine the continuous monitor invokes after a `trade_updates` websocket disconnect to surface fill events that arrived while the websocket was down. Calls `AccountStateQueries.get_orders(status="all", since=<last_seen_ts>)` to enumerate orders touched in the disconnect window, transforms each into one or more `FillReport` records (using order-state inspection rather than event-log fetching, since Alpaca's REST surface lacks an event-log endpoint), and yields them in fill-timestamp order so the OMS Phase 1 path can integrate them like normal fills.

The reconnect lifecycle (re-establishing the websocket) lives in the continuous-monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>); this story ships only the recovery primitive.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Fill stream § Re-subscription on disconnect — Alpaca orders are authoritative; on disconnect, monitor reconnects and re-queries `GET /v2/orders` with a `since` parameter to recover missed events.
* `docs/design/05-execution-layer/architecture.md` § 4a Alpaca fill-stream consumption — disconnect recovery semantics.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path — Fill collection summary records reconciliation deltas; this story produces fills, the Phase 1 path consumes them.
* `src/alphamind/execution/broker_adapter/queries.py` (story 02a) — `AccountStateQueries.get_orders(status, since, ...)` async iterator.
* `src/alphamind/execution/broker_adapter/fill_stream.py` (story 02f) — `FillReport` shape this story produces.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (D) — primitive in adapter; lifecycle in monitor.

## Depends on

* <issue id="4029f936-92d1-4f06-a99a-6f7fd8b2841d">ALP-379</issue> (02a — Account state GET wrappers).
* <issue id="7690cddb-d84d-4a0e-bc52-e7942f0480c4">ALP-384</issue> (02f — trade_updates fill-stream subscriber + fill-report translator).

## Scope

Source under `src/alphamind/execution/broker_adapter/recovery.py` (new file). Tests at `tests/execution/broker_adapter/test_recovery.py`.

### 1. Recovery routine

```python
from collections.abc import AsyncIterator
from datetime import datetime

from alphamind.execution.broker_adapter import (
    AccountStateQueries,
    FillReport,
    OrderSnapshot,
)


async def recover_missed_fills_since(
    queries: AccountStateQueries,
    *,
    since: datetime,
    until: datetime | None = None,
) -> AsyncIterator[FillReport]:
    """Yield FillReport for orders touched between `since` and `until` (or now).

    Calls AccountStateQueries.get_orders(status="all", since=since, until=until)
    and for each order, derives FillReport records from its current state:

    * If filled / partially_filled: yield a synthetic 'filled' or
      'partially_filled' FillReport with the order's reported fill price
      and quantity. Note: Alpaca's order endpoint reports only the most
      recent state — we cannot reconstruct intermediate partial fills.
      The OMS treats this as the canonical truth for the order's state
      and reconciles via Phase 1's processed-fill ledger.
    * If canceled / expired / rejected / replaced: yield a synthetic
      report of the corresponding event_type with no fill price/qty.
    * If still open (new / accepted / pending_*): yield a 'new' report
      so the OMS can confirm the order's existence; idempotent if the
      OMS already saw the original 'new' event.

    Mleg parents: yield parent + per-leg children identically to story
    02f's translate_trade_update.

    Yields in fill_timestamp order (Alpaca's filled_at / canceled_at /
    expired_at, falling back to submitted_at when no terminal timestamp
    exists).
    """
    ...
```

### 2. Order-snapshot → FillReport translator

Reuses or duplicates the per-event-type mapping from story 02f's `translate_trade_update`, but adapts to `OrderSnapshot` (REST shape) rather than `TradeUpdate` (websocket shape):

| Order status | FillReport.event_type | Notes |
| -- | -- | -- |
| `new` / `accepted` / `pending_new` | `"new"` | Re-confirmation; OMS dedup by client_order_id. |
| `filled` | `"filled"` | fill_price, fill_quantity from order's filled_avg_price + filled_qty. |
| `partially_filled` | `"partially_filled"` | Partial state; OMS reconciles via existing fill_records ledger. |
| `canceled` | `"canceled"` |  |
| `expired` | `"expired"` |  |
| `rejected` | `"rejected"` |  |
| `replaced` | `"replaced"` | order's `replaced_by` field carries the new Alpaca order ID. |

Filtered statuses (`pending_cancel`, `pending_replace`, `pending_review`, `held`, `accepted_for_bidding`, `done_for_day` — the last is rare and mostly informational) translate to `new` or omitted; document the choice per status.

### 3. Idempotency

The OMS Phase 1 path is idempotent on `client_order_id` + `event_type` — a duplicate FillReport for an already-processed event is recognized via the fill_records ledger and skipped. So this story's recovery routine does NOT need to dedup against the OMS's prior state; just emit everything in the window.

### 4. Mleg parent + per-leg correlation

When `OrderSnapshot.order_class == "mleg"` and `OrderSnapshot.legs` is populated:

* Yield one parent `FillReport` (no leg fields).
* Yield one child `FillReport` per leg with `parent_client_order_id`, `occ_symbol`, `position_intent` populated.

Each leg's status comes from the `OrderLegSnapshot` child returned by `get_orders` (story 02a); not all legs may be in the same status (e.g., 3 of 4 filled, 1 still open under thin liquidity).

### 5. Public surface

Update `broker_adapter/__init__.py` to add `recover_missed_fills_since` and an `OrderSnapshotToFillReport` translator (the latter useful for testing).

### Out of scope

* Reconnect lifecycle — continuous monitor (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Initial subscription on monitor start — that's `subscribe_trade_updates` (story 02f).
* Reconciliation log integration — Phase 1 already records reconciliation deltas; this story just produces fills, not the delta record.
* `since` parameter checkpointing — the monitor decides when to checkpoint and what timestamp to use as `since`; this story takes whatever it's given.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/recovery.py` exists; `recover_missed_fills_since` is importable from `alphamind.execution.broker_adapter`.
- [ ] `recover_missed_fills_since(queries, since=ts)` calls `queries.get_orders(status="all", since=ts)` and translates each yielded `OrderSnapshot` into FillReport(s).
- [ ] For a `filled` OrderSnapshot, yields one `FillReport` with `event_type="filled"`, `fill_price` populated from `filled_avg_price`, `fill_quantity` from `filled_qty`, `cumulative_filled_quantity` matching `filled_qty`, `remaining_quantity=0`.
- [ ] For a `canceled` OrderSnapshot, yields one `FillReport` with `event_type="canceled"`, `fill_price=None`, `fill_quantity=None`.
- [ ] For an `expired` OrderSnapshot with partial fill: yields ONE `FillReport` with `event_type="expired"` and the partial fill quantity reflected.
- [ ] For a `replaced` OrderSnapshot: yields one `FillReport` with `event_type="replaced"` and `alpaca_order_id` populated from the order's `replaced_by` field.
- [ ] For an mleg parent OrderSnapshot with N legs filled: yields N+1 reports (parent + per leg) in stable order.
- [ ] For mleg with mixed leg statuses (some filled, some still open): each leg's report carries that leg's individual status.
- [ ] Reports are yielded in fill_timestamp ascending order; ties broken by Alpaca order ID.
- [ ] When `until` is supplied: orders with submitted_at > until are excluded.
- [ ] When `until` is None: defaults to `datetime.now(UTC)`.
- [ ] `since` is required (no default); calling without it raises `TypeError`.
- [ ] Tests use a fake `AccountStateQueries` returning synthetic `OrderSnapshot` records; cover: each terminal status, mleg parent + per-leg, multi-order ordering, since/until filtering, idempotency-friendly emission of all events.
- [ ] `uv run pytest tests/execution/broker_adapter/test_recovery.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_recovery.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check via story 05's verify script — trigger a synthetic disconnect (test fixture), submit a paper order, simulate reconnect, call `recover_missed_fills_since(since=<pre-disconnect-ts>)` and assert the fill is recovered.
* Lint clean per CLAUDE.md.
