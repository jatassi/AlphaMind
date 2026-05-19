# 02d — Multi-leg mleg order POST translation

## Goal

Translate canonical OMS commands carrying strategy instruments (`StrategyInstrument` from `oms.command_models`) into alpaca-py `OrderClass.MLEG` requests with up to 4 legs, per-leg `position_intent`, and ratio_qty in simplified form (GCD = 1). Support all five named strategy types (`vertical_spread`, `calendar_spread`, `straddle`, `strangle`, `iron_condor`, `custom`) and submit via `TradingClient.submit_order(...)` wrapped by `submit_with_retry`. Produce an `MLEGSubmission` carrying the parent strategy ID and per-leg children for the OMS to correlate downstream fill events.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Supported instruments — multi-leg constraints (Level 3 approval, up to 4 legs, no equity+options mix, mleg cannot include brackets/OCO/OTO).
* `docs/design/05-execution-layer/broker-adapter.md` § Order submission — mleg required parameters: `order_class: mleg` + `legs: [{ symbol, side, ratio_qty, position_intent }]`.
* `docs/design/05-execution-layer/broker-adapter.md` § Multi-leg (`mleg`) fill events — per-leg children inherit parent's correlation; atomicity contract.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Multi-leg strategies — vertical / calendar / straddle / strangle / iron condor / custom; up to 4 legs; strategy-level bracket operates on underlying not legs.
* `src/alphamind/execution/oms/command_models.py` — `StrategyInstrument` (asset_type=`strategy`, strategy_type, underlying, legs\[\]), `StrategyLeg` (strike, expiration, contract_type, direction, quantity_ratio).
* `src/alphamind/portfolio_state/records/orders.py` — `StrategyInstrumentSpec`, the typed-records analog. `OrderClass.MLEG` enum + `_check_mleg_requires_strategy` cross-validator.
* `alpaca-py` SDK source for mleg request construction. Inspect whether mleg is built via `MarketOrderRequest(order_class=MLEG, legs=[...])` or a separate `MLegOrderRequest`. Identify the per-leg shape (symbol / side / ratio_qty / position_intent).
* `src/alphamind/execution/broker_adapter/order_options.py` (sibling story 02c) — OCC symbol construction is reused for each leg.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (C).

## Depends on

* <issue id="ddc9b5a4-a59d-4d3e-93cd-2fd41b81d717">ALP-378</issue> (01 — Package skeleton + Alpaca client factories).

## Scope

Source under `src/alphamind/execution/broker_adapter/order_mleg.py` (new file). Tests at `tests/execution/broker_adapter/test_order_mleg.py`.

### 1. Public submission API

The translator exposes three submission functions — one per OMS command type that touches a strategy position. (CLOSE inverts each leg's `position_intent`; ADD scales ratios by the additional unit count while preserving `position_intent`; OPEN is the green-field case.)

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
class MLEGLegAck:
    """Per-leg child of an mleg parent acknowledgment."""

    occ_symbol: str
    side: Literal["buy", "sell"]
    ratio_qty: int
    position_intent: Literal[
        "buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"
    ]


@dataclass(frozen=True)
class MLEGSubmission:
    """Alpaca's acknowledgment record for a submitted mleg order."""

    alpaca_order_id: str       # parent strategy ID
    client_order_id: str
    status: str
    legs: tuple[MLEGLegAck, ...]
    strategy_type: Literal[
        "vertical_spread", "calendar_spread", "straddle",
        "strangle", "iron_condor", "custom",
    ]


async def submit_mleg_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[MLEGSubmission]:
    ...


async def submit_mleg_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    open_legs: tuple[MLEGLegAck, ...],
) -> SubmissionOutcome[MLEGSubmission]:
    """Adds to a strategy position by submitting an mleg with the same
    legs scaled by command.additional_quantity. position_intent on each
    leg matches the original open intent (buy_to_open / sell_to_open) —
    no inversion. Per oms-commands.md, ADD on a strategy adds a uniform
    multiple of the existing strategy units; the canonical AddCommand
    carries the position_id and additional_quantity, so the translator
    multiplies each open leg's ratio_qty by the additional unit count
    and re-simplifies before submission.
    """
    ...


async def submit_mleg_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    open_legs: tuple[MLEGLegAck, ...],  # threaded from portfolio state
) -> SubmissionOutcome[MLEGSubmission]:
    """Closes the strategy by submitting an mleg counter-order with each
    leg's position_intent inverted (buy_to_open → sell_to_close, etc.).
    """
    ...
```

### 2. Per-leg ratio_qty simplification

Alpaca requires `ratio_qty` values in simplified form — gcd of all leg ratios = 1. The translator computes `gcd` over the legs' `quantity_ratio` field from the canonical `StrategyLeg.quantity_ratio: int >= 1` and divides each by it. Example: legs with ratios `(2, 4, 2, 4)` simplify to `(1, 2, 1, 2)`.

### 3. Strategy-type validation

Each named strategy type has structural constraints (e.g., vertical spread = 2 legs same expiration same contract_type; iron condor = 4 legs with specific strike ordering). The translator does NOT enforce these — that's the analyst/strategist's job upstream. The translator only validates Alpaca-level constraints:

* `legs[]` length is 1 to 4 inclusive (Alpaca rejects > 4).
* All legs must be options (no equity legs in an mleg per `broker-adapter.md`).
* All legs share the same underlying.
* `ratio_qty` values after simplification are positive integers.
* `position_intent` values are valid Alpaca enum members.

Structural strategy-type validation (e.g., "iron_condor must have 4 legs") lives upstream in canonical command validation (already covered by `OrderRecord._check_mleg_requires_strategy` plus the strategist's prompt contract).

### 4. Submission

Same submission-with-retry pattern as 02b/c. Construct `MarketOrderRequest` (or `LimitOrderRequest` if the strategy carries a net debit/credit limit) with `order_class=OrderClass.MLEG, legs=[...]`. Set `time_in_force=TimeInForce.DAY` (mleg follows options TIF rule).

The `legs[]` payload to alpaca-py uses each leg's OCC symbol (built via `build_occ_symbol` from story 02c — import from sibling), `side`, `ratio_qty`, `position_intent`.

### 5. Mleg-specific rejection: `invalid_legs`

`classify_alpaca_error`'s `invalid_legs` code already covers Alpaca's `422 invalid_legs` response. The error includes the specific `legs[]` index Alpaca flagged; the translator surfaces this as a typed payload:

```python
@dataclass(frozen=True)
class InvalidLegsRejection(PermanentRejection):
    leg_index: int  # which leg Alpaca rejected
    leg_reason: str  # Alpaca's per-leg reason string
```

Or extend `PermanentRejection` to optionally carry leg-specific detail. Implementer's choice; whichever round-trips cleanly through to the strategist's surfaced rejection so they can substitute a corrected leg.

### 6. Public surface re-exports

Update `__init__.py` to add the three submit functions plus `MLEGSubmission`, `MLEGLegAck`.

### Out of scope

* Equity / single-leg options translation (stories 02b, 02c).
* Mleg modification — design says any change to `legs[]` requires explicit cancel + new submission; PATCH on mleg is limited to `limit_price` (the strategy's net debit/credit) and `qty` proportionally (see story 02e).
* Strategy-type structural validation — upstream (analyst, strategist, canonical command validators).
* Per-leg fill correlation logic — that's in story 02f (trade_updates → fill report).

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/order_mleg.py` exists; `submit_mleg_open`, `submit_mleg_add`, `submit_mleg_close`, `MLEGSubmission`, `MLEGLegAck` importable from `alphamind.execution.broker_adapter`.
- [ ] `submit_mleg_open` for a 2-leg vertical-spread `OpenCommand` constructs an `OrderClass.MLEG` request with `legs[]` containing 2 entries, each carrying OCC symbol + side + ratio_qty + position_intent.
- [ ] `submit_mleg_open` for a 4-leg iron-condor produces `legs[]` of length 4.
- [ ] `submit_mleg_open` for a strategy with > 4 legs raises `ValueError` before SDK call.
- [ ] `submit_mleg_open` for a strategy whose legs span multiple underlyings raises `ValueError`.
- [ ] Ratio simplification: legs with `quantity_ratio = (2, 4, 2, 4)` produce `ratio_qty = (1, 2, 1, 2)` in the request.
- [ ] Ratio simplification: legs with `quantity_ratio = (1, 1)` produce `ratio_qty = (1, 1)` (no-op).
- [ ] Each leg's `position_intent` is set per leg `direction` and command type: OPEN with long leg → `buy_to_open`, OPEN with short leg → `sell_to_open`; CLOSE inverts those to `sell_to_close` / `buy_to_close`.
- [ ] All mleg submissions use `time_in_force=TimeInForce.DAY` (no GTC on mleg).
- [ ] On successful submission, returns `Submitted[MLEGSubmission]` with `payload.alpaca_order_id` (parent), `payload.legs` (per-leg ack tuple), `payload.strategy_type` echoed from the command.
- [ ] On `invalid_legs` rejection, raises with payload identifying the rejected leg index and Alpaca's per-leg reason; strategist can then propose an alternative.
- [ ] `submit_mleg_close` inverts each leg's `position_intent` (`buy_to_open` → `sell_to_close`, `sell_to_open` → `buy_to_close`).
- [ ] `submit_mleg_add` scales each leg's ratio by `command.additional_quantity` while keeping the original `position_intent` (no inversion); the resulting ratios are re-simplified.
- [ ] Submitting with an `OpenCommand` carrying an equity or single-leg-option instrument raises `TypeError` (dispatcher routes to 02b / 02c instead).
- [ ] `client_order_id` validation matches story 02b's pattern.
- [ ] Tests cover: ratio simplification edge cases, all 5 named strategy types' construction, > 4 legs rejection, multi-underlying rejection, position_intent inversion, retry-success, retry-exhaustion, `invalid_legs` rejection round-trip.
- [ ] `uv run pytest tests/execution/broker_adapter/test_order_mleg.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter/test_order_mleg.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check by constructing a synthetic vertical-spread OPEN and asserting the resulting alpaca-py request has `order_class=MLEG` with 2 legs of correctly simplified ratios.
* Lint clean per CLAUDE.md.
