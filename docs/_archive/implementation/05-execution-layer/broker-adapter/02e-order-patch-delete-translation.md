# 02e — Order PATCH + DELETE translation

## Goal

Translate canonical OMS commands that mutate or withdraw an existing order — `AdjustCommand` and `CancelCommand` — into Alpaca's `replace_order_by_id(...)` (cancel-and-replace) and `cancel_order_by_id(...)` REST calls, wrapped by `submit_with_retry`. Implement Alpaca's PATCH-as-cancel-and-replace semantics: a PATCH returns a NEW Alpaca order ID, the OMS's `client_order_id` migrates to the replacement, and the OMS's `alpaca_order_id_chain` extends. Surface the modifiable-fields constraint per asset class (equities widest, options narrower, mleg `limit_price` + `qty` only) and enforce status restrictions (`accepted` / `pending_*` blocks PATCH).

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Order modification — cancel-and-replace contract, race window semantics, status restrictions, unreplaceable orders, modifiable fields per asset class, options modification surface, mleg modification surface.
* `docs/design/05-execution-layer/broker-adapter.md` § Order cancellation — fire-and-forget contract; terminal state arrives via trade_updates.
* `docs/design/05-execution-layer/oms-commands.md` § ADJUST, § CANCEL — the canonical command shapes this story translates from.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Bracket modification — every modification logs as a deviation; first-class operation.
* `src/alphamind/execution/oms/command_models.py` — `AdjustCommand`, `CancelCommand`, `NewStopLevel`, `NewTargetLevel`, `NewEventInvalidation`, `BracketAdjustment`.
* `src/alphamind/portfolio_state/records/orders.py` — `OrderRecord.alpaca_order_id`, `alpaca_order_id_chain`, `modification_count`, `last_update_timestamp`. The persistence-layer fields PATCH updates flow into.
* `alpaca-py` SDK: `TradingClient.replace_order_by_id(order_id, ReplaceOrderRequest(...))` and `TradingClient.cancel_order_by_id(order_id)`. Inspect `ReplaceOrderRequest` for the modifiable-field surface.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (C).

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories).

## Scope

Source under `src/alphamind/execution/broker_adapter/order_modify.py` (new file). Tests at `tests/execution/broker_adapter/test_order_modify.py`.

### 1. Public submission API

```python
from alpaca.trading.client import TradingClient

from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    Submitted,
    SubmissionOutcome,
)
from alphamind.execution.oms.command_models import AdjustCommand, CancelCommand


@dataclass(frozen=True)
class ReplacementAck:
    """Alpaca's acknowledgment of a successful replace_order_by_id call.

    The replacement carries a NEW Alpaca order ID. The OMS appends it to
    the order's alpaca_order_id_chain and increments modification_count.
    The replaced original transitions to a 'replaced' terminal state via
    trade_updates (events handled by story 02f).
    """

    new_alpaca_order_id: str
    replaced_alpaca_order_id: str
    client_order_id: str
    status: str  # Alpaca's reported replacement status


@dataclass(frozen=True)
class CancellationAck:
    """Alpaca's acknowledgment of a fire-and-forget cancel.

    The terminal state (canceled, or race-condition filled) arrives via
    trade_updates separately.
    """

    alpaca_order_id: str
    accepted: bool


async def submit_replace(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: str,
    target_asset_class: Literal["us_equity", "us_option", "us_option_strategy"],
    target_order_class: Literal["simple", "bracket", "oco", "oto", "mleg"],
    fields: ReplaceFields,
) -> SubmissionOutcome[ReplacementAck]:
    """Validate fields against the per-asset-class modifiable surface,
    then call client.replace_order_by_id. Raises ValueError before any
    SDK call if a field is not modifiable for this asset class /
    order class.
    """
    ...


async def submit_cancel(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: str,
) -> SubmissionOutcome[CancellationAck]:
    """Fire-and-forget cancel."""
    ...
```

### 2. `ReplaceFields` discriminator

`AdjustCommand` carries a discriminated change-type field (one of `new_stop_level`, `new_target_level`, `new_time_expiration`, `new_event_invalidation`, `thesis_component_updates`); only the first three plus `BracketAdjustment` translate into broker PATCHes (event invalidation and thesis updates are OMS-side only).

```python
@dataclass(frozen=True)
class ReplaceFields:
    """The intersection of OMS-modifiable fields and Alpaca-PATCHable fields."""

    limit_price: float | None = None
    stop_price: float | None = None
    qty: float | None = None
    trail_price: float | None = None       # equity-only
    trail_percent: float | None = None     # equity-only
    time_in_force: Literal["day", "gtc", "gtd"] | None = None
```

### 3. Per-asset-class modifiable-field gates

The translator validates that the requested fields are PATCHable for the target's asset class + order class:

| Asset class / order class | PATCHable fields |
| -- | -- |
| us_equity / simple | limit_price, stop_price, qty, trail_price, trail_percent, time_in_force |
| us_equity / bracket child (take_profit or stop_loss leg) | limit_price, stop_price |
| us_equity / oco child | limit_price, stop_price |
| us_equity / oto child | limit_price, stop_price, qty, time_in_force |
| us_option / simple (single-leg) | limit_price, stop_price, qty, time_in_force |
| us_option_strategy / mleg | limit_price (net), qty (proportional) |

Out-of-surface field requests raise `ValueError` before any SDK call. The translator does NOT silently drop fields — it surfaces the mismatch so the caller (ADJUST handler in OMS) reports back to the PM that the requested change is not supported.

`trail_price` / `trail_percent` are equity-only AND atomic-only — they're modifiable on a SIMPLE trailing-stop entry but not on bracket/OCO/OTO children. Per design: "no `trail_price`/`trail_percent` since trailing stop is unsupported on options".

### 4. Status restrictions

Alpaca rejects PATCH on orders in `accepted`, `pending_new`, `pending_cancel`, or `pending_replace` status. The translator does NOT pre-check status (status is in OMS state, not adapter state) — instead, the SDK call surfaces Alpaca's `422 unprocessable_entity` rejection through the standard error mapping. Add a new code to `classify_alpaca_error` if needed: `replace_status_invalid` (for the four blocked statuses).

### 5. mleg-specific PATCH constraints

Per design: PATCH on mleg accepts `limit_price` and `qty` only. `qty` applies proportionally per leg ratios (alpaca-py handles the math). Any change to `legs[]` (add/remove/substitute, change strike/expiration/side/ratio) requires explicit cancel + resubmit at the OMS level — Alpaca rejects with `422 unprocessable_entity` and the strategist must surface as fresh OPEN of new strategy + CLOSE of old. The translator surfaces this rejection cleanly via the standard error mapping; the actual PM-level "convert to fresh OPEN+CLOSE" workflow lives in the strategist's prompt contract, not here.

### 6. Cancel-and-replace ID-chain semantics

When `replace_order_by_id` succeeds, Alpaca returns a new Order with a new `id`. The translator returns this as `new_alpaca_order_id`; the original target ID is echoed as `replaced_alpaca_order_id`. The OMS Phase 2 write path appends to `alpaca_order_id_chain` and increments `modification_count`; that wiring lives in story 03e (engine-stub coordinated swap). The translator only produces the typed acknowledgment.

### 7. Race window: `replace_rejected`

If the original order fills between PATCH arrival and replacement taking effect, Alpaca's response on the immediate REST call may still be `200 OK`, but the actual replacement is rejected. The rejection surfaces via the `trade_updates` websocket as `replace_rejected` (story 02f handles the event mapping). For this story's scope: the REST-level `200 OK` is reported as `Submitted[ReplacementAck]`; the OMS reconciles the race when the websocket event arrives.

### 8. Cancellation

`submit_cancel` is the simplest case — call `client.cancel_order_by_id(target_id)`, get back a status code, return `CancellationAck(target_alpaca_order_id, accepted=True)` on success. Alpaca's response is fire-and-forget; the actual canceled-or-raced terminal state arrives via `trade_updates`.

### 9. Public surface re-exports

Update `broker_adapter/__init__.py` to add `submit_replace`, `submit_cancel`, `ReplacementAck`, `CancellationAck`, `ReplaceFields`.

### Out of scope

* `client_order_id` migration — that's an OMS Phase 2 concern (already shipped under <issue id="beaf98a0-a9fc-44a8-ac46-32e50f604345">ALP-119</issue>); the translator just returns the new Alpaca order ID.
* Bracket-record modification-history append — state-persistence layer (already shipped); the translator surfaces the typed ack and the caller persists.
* Trade-update event consumption (`replaced` / `replace_rejected`) — story 02f.
* Notional / OTO replace rejection — design says these are unreplaceable at the OMS level (use cancel + new submission instead). The translator surfaces Alpaca's rejection if it arrives, but the OMS pre-empts via its own ADJUST handler.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/order_modify.py` exists; `submit_replace`, `submit_cancel`, `ReplacementAck`, `CancellationAck`, `ReplaceFields` importable from `alphamind.execution.broker_adapter`.
- [ ] `submit_replace` validates field-surface against the asset-class × order-class table; out-of-surface field raises `ValueError` before any SDK call.
- [ ] `submit_replace` for a us_equity simple order accepts all six fields (limit_price, stop_price, qty, trail_price, trail_percent, time_in_force).
- [ ] `submit_replace` for a us_option simple order rejects `trail_price` / `trail_percent` (raises `ValueError`).
- [ ] `submit_replace` for an mleg order rejects all fields except `limit_price` and `qty` (raises `ValueError` on stop_price / trail\\_\\* / time_in_force / etc.).
- [ ] `submit_replace` for an mleg with only `limit_price` constructs an alpaca-py `ReplaceOrderRequest` with just that field set.
- [ ] On successful Alpaca acknowledgment, returns `Submitted[ReplacementAck]` with `new_alpaca_order_id` from Alpaca's response, `replaced_alpaca_order_id` echoing the input target ID.
- [ ] On retry-window exhaustion, returns `GatewaySubmissionFailed`.
- [ ] On a permanent rejection (e.g., status-invalid PATCH on a `pending_new` order), the function re-raises with `PermanentRejection`; caller (03e) translates to synchronous OMS rejection.
- [ ] `submit_cancel` calls `client.cancel_order_by_id` with the target ID and returns `Submitted[CancellationAck(alpaca_order_id, accepted=True)]` on success.
- [ ] `submit_cancel` returns `GatewaySubmissionFailed` on retry exhaustion (network failures); raises `PermanentRejection` on `404 not_found` (already-canceled or unknown order).
- [ ] Cancellation does not require a fresh `client_order_id` — it operates on the target Alpaca order ID directly.
- [ ] Tests cover: each asset-class × order-class field-surface gate (positive + negative cases), retry-success, retry-exhaustion, mleg leg-mutation rejection (Alpaca's `422`), status-invalid PATCH rejection, simple cancel success, cancel of unknown order, cancel of already-filled order.
- [ ] `uv run pytest tests/execution/broker_adapter/test_order_modify.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_order_modify.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by constructing a `ReplaceFields(limit_price=850.0)` against an mleg target and asserting the resulting alpaca-py `ReplaceOrderRequest` has only `limit_price` set.
* Lint clean per CLAUDE.md.
