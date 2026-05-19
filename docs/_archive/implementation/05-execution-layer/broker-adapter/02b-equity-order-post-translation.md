# 02b — Equity order POST translation

## Goal

Translate canonical OMS commands carrying equity instruments — `EquityInstrument` from `oms.command_models` and `EquityInstrumentSpec` from `portfolio_state.records.orders` — into alpaca-py order request objects, then submit them via `TradingClient.submit_order(...)` wrapped by `submit_with_retry`. Cover all four atomic order types (`market`, `limit`, `stop`, `stop_limit`) and three Tier-2 contingent classes (`bracket`, `oco`, `oto`) per `orders-and-brackets.md` and `broker-adapter.md`. Return either an Alpaca acknowledgment carrying the broker's order ID or a typed gateway-failure result the OMS Phase 2 path translates to `command_abandoned`.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Order types, § Order classes, § Order submission — atomic vs contingent class semantics; required parameters; rejection-reason mapping.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Tier 2 — Contingent orders — the BRACKET / OCO / OTO contracts the OMS expresses.
* `docs/design/05-execution-layer/orders-and-brackets.md` § P/L-based bracket legs — at submission time, the absolute price computed from the planned entry is what gets submitted; recalculation at fill time is downstream (state-persistence).
* `src/alphamind/execution/oms/command_models.py` — `OpenCommand`, `AddCommand`, `CloseCommand`, `EquityInstrument`, `EntryOrder`, `Target`, `InvalidationLeg`, `PriceCondition`, `TimeCondition`, `EventCondition`. The OMS-facing canonical command shape this story translates from.
* `src/alphamind/portfolio_state/records/orders.py` — `OrderRecord`, `OrderType`, `OrderClass`, `OrderDirection`, `OrderDuration`, `BracketRecord`, `BracketLeg`, `PriceTrigger`, `PLAnchorSpec`. The persistence-layer types story 03e maps the broker outcome onto.
* `alpaca-py` SDK `alpaca.trading.requests`: `MarketOrderRequest`, `LimitOrderRequest`, `StopOrderRequest`, `StopLimitOrderRequest`, `TakeProfitRequest`, `StopLossRequest`, `OrderClass.BRACKET / .OCO / .OTO / .SIMPLE`, `TimeInForce.DAY / .GTC / .GTD / .OPG / .CLS`. Inspect kwargs and validators.
* `src/alphamind/execution/broker_adapter/retry.py` — `submit_with_retry`, `Submitted`, `GatewaySubmissionFailed`.
* `src/alphamind/execution/broker_adapter/errors.py` — `classify_alpaca_error`, `PermanentRejection`.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (C) — submission retry policy.

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories) — provides `submit_with_retry` and `classify_alpaca_error`.

## Scope

Source under `src/alphamind/execution/broker_adapter/order_equity.py` (new file). Tests at `tests/execution/broker_adapter/test_order_equity.py`. The translator is a pure function module — no class state — that takes a canonical `OpenCommand` / `AddCommand` / `CloseCommand` plus an `ExecutionConfig` and `TradingClient`, and returns a typed outcome.

### 1\. Public submission API

```python
from alpaca.trading.client import TradingClient

from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    Submitted,
    SubmissionOutcome,
)
from alphamind.execution.oms.command_models import (
    AddCommand,
    CloseCommand,
    OpenCommand,
)


@dataclass(frozen=True)
class EquitySubmission:
    """Alpaca's acknowledgment record for a submitted equity order."""

    alpaca_order_id: str
    client_order_id: str
    status: str  # Alpaca's reported status: accepted | new | pending_new | ...
    order_class: str  # simple | bracket | oco | oto


async def submit_equity_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[EquitySubmission]:
    """Translate an OPEN command targeting an equity instrument and submit.

    The command_class is determined from the bracket shape:
    * One target leg + one price-stop invalidation leg → BRACKET.
    * One target leg only (no price-stop) → OTO with take-profit child.
    * One price-stop only → OTO with stop-loss child.
    * No mechanical bracket structure (event-only invalidation) → SIMPLE.
    OCO is not produced by OPEN — it is produced by ADJUST when the PM
    converts a position from bracket to OCO post-entry; that path lives
    in story 02e (PATCH).
    """
    ...


async def submit_equity_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[EquitySubmission]:
    """Translate an ADD command and submit. ADD is always SIMPLE — it is a
    fresh order on an existing position; bracket adjustments arrive
    separately via PATCH (story 02e).
    """
    ...


async def submit_equity_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    position_qty: float,
    position_side: Literal["long", "short"],
) -> SubmissionOutcome[EquitySubmission]:
    """Translate a CLOSE command and submit. Always SIMPLE; uses opposite
    side of the position. ``position_qty`` and ``position_side`` are
    threaded from portfolio state because the canonical CloseCommand
    references the position by ID, not by ticker/direction.
    """
    ...
```

### 2\. Atomic order type translation

Map `EntryOrder.type` and `OrderType` enum values to alpaca-py request classes:

| OMS `EntryOrder.type` | alpaca-py request | Required fields |
| -- | -- | -- |
| `"market"` | `MarketOrderRequest` | symbol, side, qty, time_in_force |
| `"limit"` | `LimitOrderRequest` | * limit_price |
| `"stop_limit"` | `StopLimitOrderRequest` | * limit_price, stop_price |

Stop-only orders (`OrderType.STOP`) are not produced by OPEN/ADD entry orders per `oms-command-schema.md`'s `entry_order` enum (`market | limit | stop_limit`); they appear only as a CLOSE's `order_type` or as a bracket protective leg's order type.

### 3\. Time-in-force defaults

For equity entry / add: default `TimeInForce.DAY` unless the command explicitly carries a different duration. For closes: default `TimeInForce.DAY` for market, `TimeInForce.DAY` for limit unless conviction-driven hold favors `GTC` (the canonical CLOSE command does not carry a TIF; default DAY is correct per design — invalidation is bounded by the next invocation).

### 4\. Contingent class translation

When the canonical OPEN command's `invalidation_legs` contain at least one mechanical price-stop leg AND a target is supplied:

* If both target + price-stop present → submit `OrderClass.BRACKET` with `take_profit=TakeProfitRequest(limit_price=<target.price>)` and `stop_loss=StopLossRequest(stop_price=<price-stop.trigger_price>, limit_price=<price-stop.limit_price | None>)`.
* If only target present → submit `OrderClass.OTO` with a take-profit child.
* If only price-stop present → submit `OrderClass.OTO` with a stop-loss child.
* If neither present (event-only invalidation) → submit `OrderClass.SIMPLE`. The PM has accepted that no mechanical backstop applies; the bracket-record's hard-backstop validator (which DOES require a mechanical leg per `BracketRecord._check_hard_backstop`) will already have raised before this story is reached, so this branch is defensive — but defensive in the form of "select SIMPLE", not "raise".

P/L-anchored target/stop submission: when the OMS command carries a `target_type` / `stop_type` of `pl_percentage` or `pl_dollar`, the schema's `price` field already carries the absolute-price equivalent computed from the planned entry. Submit that absolute price as-is. Recalculation at fill time happens in state-persistence's bracket-activation step (already shipped under <issue id="beaf98a0-a9fc-44a8-ac46-32e50f604345">ALP-119</issue>); this story does not implement P/L recalc.

### 5\. Submission with retry

Wrap the SDK call:

```python
async def _submit() -> Order:
    return await asyncio.to_thread(client.submit_order, request)


outcome = await submit_with_retry(
    _submit,
    window_seconds=execution.submission_retry_window_seconds,
)
match outcome:
    case Submitted(payload=order, attempt_count=n):
        return Submitted(
            EquitySubmission(
                alpaca_order_id=str(order.id),
                client_order_id=order.client_order_id,
                status=order.status.value,
                order_class=order.order_class.value,
            ),
            attempt_count=n,
        )
    case GatewaySubmissionFailed():
        return outcome
```

The `client_order_id` parameter on the request is set from the caller-supplied `client_order_id` argument (the OMS's stable command ID), enabling end-to-end correlation per `broker-adapter.md § Order submission`.

### 6\. `client_order_id` derivation

The story does NOT derive command IDs. The caller (engine-stub coordinated swap, story 03e) supplies `client_order_id`. The translator threads it onto the alpaca-py request's `client_order_id` field. Validation: assert it's non-empty and matches `^inv-` or `^MON\.` per `oms-command-ids.md`.

### Out of scope

* Options and mleg orders (stories 02c, 02d).
* Order modification or cancellation (story 02e).
* Engine-stub swap (story 03e) — that wires this story's submission into the OMS write path.
* Bracket-record / order-record persistence — already covered by state-persistence Phase 2; this story's outputs feed into that.
* P/L-anchor recalculation at fill time — already in `BracketLeg._validate_pl_anchor_compatibility` and Phase 1 bracket-activation step.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/order_equity.py` exists; `submit_equity_open`, `submit_equity_add`, `submit_equity_close`, and `EquitySubmission` are importable from `alphamind.execution.broker_adapter`.
- [ ] `submit_equity_open` for an `OpenCommand` with `entry_order.type == "market"` constructs a `MarketOrderRequest` with `qty=command.position_size.quantity`, `side="buy" | "sell"`, `time_in_force=DAY`, `client_order_id=<supplied>`.
- [ ] `submit_equity_open` for `entry_order.type == "limit"` constructs a `LimitOrderRequest` with `limit_price=command.entry_order.limit_price`.
- [ ] `submit_equity_open` for `entry_order.type == "stop_limit"` constructs a `StopLimitOrderRequest` with both `limit_price` and `stop_price`.
- [ ] An OPEN with target + price-stop invalidation produces `OrderClass.BRACKET` with populated `take_profit` and `stop_loss` children.
- [ ] An OPEN with target only (no price-stop, only event/time invalidation) produces `OrderClass.OTO` with take-profit child.
- [ ] An OPEN with price-stop only produces `OrderClass.OTO` with stop-loss child.
- [ ] An OPEN with event-only invalidation produces `OrderClass.SIMPLE`.
- [ ] `submit_equity_add` produces `OrderClass.SIMPLE` regardless of any bracket adjustment in the command (bracket adjustment is handled separately via PATCH).
- [ ] `submit_equity_close` derives `side` as opposite of `position_side` (long position → sell, short position → buy_to_cover). Quantity is `command.quantity` if numeric, else `position_qty` if `command.quantity == "all"`.
- [ ] On successful submission, returns `Submitted[EquitySubmission]` with `payload.alpaca_order_id` populated from Alpaca's response, `payload.client_order_id` echoing the supplied ID, `payload.status` reflecting Alpaca's reported status, `payload.order_class` matching the submitted class.
- [ ] On retry-window exhaustion, returns `GatewaySubmissionFailed(reason, attempt_count, last_error_class)` without raising.
- [ ] On a permanent rejection (validation_failed / insufficient_buying_power / insufficient_shares / asset_not_tradable per `classify_alpaca_error`), the function re-raises a typed exception carrying the `PermanentRejection`. Caller (story 03e) maps to synchronous OMS rejection.
- [ ] `client_order_id` is asserted non-empty and matches `^(inv-|MON\.)` patterns per `oms-command-ids.md`; unmatched IDs raise `ValueError` before any SDK call.
- [ ] Tests cover: each atomic order type × each contingent class combination (where applicable), retry-success, retry-exhaustion, permanent-rejection re-raise, `client_order_id` validation, side derivation for close.
- [ ] `uv run pytest tests/execution/broker_adapter/test_order_equity.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_order_equity.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by constructing a synthetic `OpenCommand` with an equity instrument + bracket parameters and asserting the resulting alpaca-py request payload via `request.model_dump()` matches the expected shape.
* Lint clean per CLAUDE.md.
