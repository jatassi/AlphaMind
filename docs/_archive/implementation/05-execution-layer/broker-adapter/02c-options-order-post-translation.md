# 02c — Options order POST translation

## Goal

Translate canonical OMS commands carrying single-leg options instruments (`OptionInstrument` from `oms.command_models`) into alpaca-py order requests targeting OCC option symbols, then submit via `TradingClient.submit_order(...)` wrapped by `submit_with_retry`. Enforce Alpaca's options-only constraints: `time_in_force=DAY` (the only TIF Alpaca accepts on options), `OrderClass.SIMPLE` (Alpaca does not support brackets/OCO/OTO on options — protective legs are monitor-managed), and the four options-specific rejection codes (`options_level_not_approved`, `contract_expired`, `underlying_halted`, generic asset rejections).

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Order types, § Time-in-force, § Order classes — options are day-only, simple-class only.
* `docs/design/05-execution-layer/broker-adapter.md` § Options-specific rejections — the four typed rejection codes the translator surfaces.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Options price-based stops: trigger on the underlying — confirms options brackets are monitor-managed via the underlying equity stream; the broker submission is a SIMPLE order.
* `src/alphamind/execution/oms/command_models.py` — `OptionInstrument` (asset_type=`option`, underlying, strike, expiration, contract_type, direction).
* `src/alphamind/portfolio_state/records/orders.py` — `OptionsInstrumentSpec`, `OptionContractType`. The persistence-layer types.
* `alpaca-py` SDK source for OCC symbol construction. Inspect whether the SDK exposes a helper for OCC-21-character symbols (`{root}{yymmdd}{C/P}{strike8}`) or whether the translator constructs them. Check `alpaca.trading.requests` for `MarketOrderRequest` / `LimitOrderRequest` with `asset_class=OPTIONS`.
* `src/alphamind/execution/broker_adapter/order_equity.py` (sibling story 02b) — same submission-wrapper pattern this story mirrors.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (C) — submission retry policy.

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories).

## Scope

Source under `src/alphamind/execution/broker_adapter/order_options.py` (new file). Tests at `tests/execution/broker_adapter/test_order_options.py`.

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
class OptionsSubmission:
    """Alpaca's acknowledgment record for a submitted single-leg options order."""

    alpaca_order_id: str
    client_order_id: str
    occ_symbol: str           # the resolved 21-character OCC symbol
    status: str
    order_class: str          # always "simple" for options


async def submit_options_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[OptionsSubmission]:
    ...


async def submit_options_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[OptionsSubmission]:
    ...


async def submit_options_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    occ_symbol: str,
    position_qty: float,
    position_intent: Literal["buy_to_close", "sell_to_close"],
) -> SubmissionOutcome[OptionsSubmission]:
    """Threading occ_symbol + position_intent from portfolio state because
    the canonical CloseCommand only carries position_id.
    """
    ...
```

### 2\. OCC symbol construction

OCC option symbols follow the 21-character format: `{ROOT:1-6}{YY:2}{MM:2}{DD:2}{C|P}{STRIKE:8}` where the strike is in thousandths and zero-padded to 8 digits (e.g., `NVDA  240315C00800000` for NVDA 2024-03-15 800-strike call). Build a helper:

```python
from datetime import date
from alphamind.portfolio_state.records.positions import OptionContractType


def build_occ_symbol(
    underlying: str,
    expiration: date,
    contract_type: OptionContractType,
    strike: float,
) -> str:
    """Construct the 21-character OCC symbol per the standard."""
    ...
```

If alpaca-py provides a helper for OCC construction, use it. Otherwise this helper lives in `order_options.py`.

### 3\. Order type translation

Same atomic-type mapping as story 02b:

| OMS `EntryOrder.type` / `CloseCommand.order_type` | alpaca-py request | Required fields |
| -- | -- | -- |
| `"market"` | `MarketOrderRequest(asset_class=OPTIONS, ...)` | symbol (OCC), side, qty (contracts), time_in_force=DAY |
| `"limit"` | `LimitOrderRequest(asset_class=OPTIONS, ...)` | * limit_price |
| `"stop_limit"` | `StopLimitOrderRequest(asset_class=OPTIONS, ...)` | * limit_price, stop_price |

`OrderType.STOP` (atomic stop) on options: not produced by entry orders; if a CLOSE command supplies `order_type="stop"` against an options position, fail-loud with `ValueError` (the design specifies single-leg options use `simple` class with limit/stop_limit/market — no atomic stop is in the contract for options closes either; surface as a structural error).

### 4\. Time-in-force enforcement

`TimeInForce.DAY` is the only legal TIF for options orders on Alpaca. The translator hard-codes it and asserts the command does not request anything else. If a future command schema adds an explicit TIF on options, the assertion catches the violation rather than silently overriding.

### 5\. `OrderClass.SIMPLE` enforcement

Alpaca rejects `bracket` / `oco` / `oto` on options. Per `orders-and-brackets.md § Options price-based stops: trigger on the underlying`, the OMS submits the entry as SIMPLE and the continuous monitor manages the protective leg by watching the underlying equity stream. This story sends `order_class=OrderClass.SIMPLE` regardless of the OMS command's bracket structure. The bracket-arming side (monitor + state-persistence) is downstream.

### 6\. Rejection mapping

`classify_alpaca_error` already covers the four options-specific codes from story 01. This story's translator wraps the SDK call exactly like story 02b — permanent rejections re-raise to the caller (story 03e), gateway exhaustion returns `GatewaySubmissionFailed`. The four options rejection codes are surfaced verbatim:

| Code | Trigger condition | OMS response |
| -- | -- | -- |
| `options_level_not_approved` | Account doesn't hold the required options level | Abandon command; do not retry. PM cannot retry without account-level config change |
| `contract_expired` | Option's expiration date is in the past | `403 contract_expired`. Adapter does NOT auto-refresh the chain (per design, persistent rejection signals a chain-staleness data-pipeline issue — escalate, don't paper over) |
| `underlying_halted` | Underlying equity is in a regulatory halt | Abandon command. Monitor consumes halt event from underlying's data stream; strategist re-evaluates next invocation |
| `asset_not_tradable` | OCC symbol unrecognized or not in Alpaca's options chain | Abandon. Surface OCC string in the rejection for operator triage |

### 7\. Public surface re-exports

Update `src/alphamind/execution/broker_adapter/__init__.py` to add `submit_options_open`, `submit_options_add`, `submit_options_close`, `OptionsSubmission`, `build_occ_symbol`.

### Out of scope

* Multi-leg strategy (`mleg`) translation — story 02d.
* Auto-refresh of expired options chains — design explicitly requires manual escalation; chain-refresh logic lives in the data pipeline, not the adapter.
* Underlying-stream stop monitoring — continuous monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Greeks computation at submission — guardrail-evaluation library (already shipped); the adapter never populates greeks.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/order_options.py` exists; `submit_options_open`, `submit_options_add`, `submit_options_close`, `OptionsSubmission`, `build_occ_symbol` are importable from `alphamind.execution.broker_adapter`.
- [ ] `build_occ_symbol("NVDA", date(2024,3,15), OptionContractType.CALL, 800.0)` returns a 21-character string ending with `240315C00800000` (root padded with spaces or aligned per OCC convention).
- [ ] `build_occ_symbol` for non-integer strikes (e.g., 12.50) produces correct thousandths zero-padding (`...00012500`).
- [ ] `submit_options_open` constructs an `asset_class=OPTIONS` request with `time_in_force=TimeInForce.DAY` regardless of any TIF the command might carry; constructing with a non-DAY TIF on the command raises `ValueError` before SDK call.
- [ ] `submit_options_open` constructs `OrderClass.SIMPLE` regardless of the command's invalidation-leg shape.
- [ ] `submit_options_open` constructs the OCC symbol from the command's `OptionInstrument` fields and uses it as `request.symbol`.
- [ ] `submit_options_close` uses opposite of `position_intent`: `buy_to_close` position → `side=sell_to_close`; `sell_to_close` position → `side=buy_to_close`. (Note: position-side semantics for options open positions are `buy_to_open` / `sell_to_open` and the close intents are correspondingly `sell_to_close` / `buy_to_close`.)
- [ ] On permanent rejection with code `options_level_not_approved`, the function re-raises with the typed `PermanentRejection`; caller (03e) translates to synchronous OMS rejection.
- [ ] On permanent rejection with code `contract_expired`, the function re-raises (no auto-retry, no chain-refresh); the OMS surfaces it to the strategist for next-invocation re-evaluation.
- [ ] On permanent rejection with code `underlying_halted`, the function re-raises.
- [ ] On retry-window exhaustion, returns `GatewaySubmissionFailed`.
- [ ] `client_order_id` validation matches story 02b's pattern.
- [ ] Submitting with an `OpenCommand` carrying an `EquityInstrument` (wrong asset type) raises `TypeError` — the dispatcher (story 03e) is responsible for routing equity vs. options vs. strategy; this story asserts its preconditions.
- [ ] Tests cover: OCC construction (calls, puts, integer strike, fractional strike, multi-character roots), each atomic order type (market / limit / stop_limit), each rejection code, asset-type mismatch, retry success, retry exhaustion.
- [ ] `uv run pytest tests/execution/broker_adapter/test_order_options.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_order_options.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check OCC construction by `python -c "from datetime import date; from alphamind.execution.broker_adapter import build_occ_symbol; from alphamind.portfolio_state.records.positions import OptionContractType; print(build_occ_symbol('NVDA', date(2024,3,15), OptionContractType.CALL, 800.0))"` — output should match OCC convention.
* Lint clean per CLAUDE.md.
