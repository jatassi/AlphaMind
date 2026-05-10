# 01c — Strategy payoff utilities

## Goal

Add `direction: Direction | None` to `StrategyLeg` and ship three pure functions in a new module `src/alphamind/execution/position_model/strategy_payoff.py`: `compute_strategy_max_profit_usd`, `compute_strategy_max_loss_usd`, `compute_strategy_breakeven_levels`. Each takes the typed `tuple[StrategyLeg, ...]` plus the strategy's `net_premium_usd` and returns the at-expiration payoff metrics the design's `StrategyPositionDetails` schema names. Closed-form derivation via piecewise-linear payoff function over strike kinks. Natural caller is State persistence (<issue id="beaf98a0-a9fc44a8-ac46-32e50f604345">ALP-119</issue>) Phase 2 OPEN handler when constructing `StrategyPositionDetails` from a multi-leg OPEN command; this story ships the functions standalone with unit-level fixtures covering vertical spreads, iron condors, straddles, and strangles.

## Reading

* `docs/design/05-execution-layer/position-model.md` § Strategy position — defines the fields a strategy position holds (max_profit, max_loss, breakeven_levels), the "max loss is what the guardrail layer uses for margin and concentration" guarantee, and the supported structures (vertical spread, iron condor, straddle, calendar spread, custom).
* `docs/design/05-execution-layer/orders-and-brackets.md` § Multi-leg strategies — defines the order-side leg structure (Alpaca `order_class: mleg`, up to 4 legs).
* `src/alphamind/portfolio_state/records/positions.py` — existing `StrategyPositionDetails`, `StrategyLeg`, `OptionsPositionDetails`, `OptionContractType` (CALL/PUT), `Direction` (LONG/SHORT) — the typed inputs the new functions consume.
* `src/alphamind/portfolio_state/records/orders.py` — `InstrumentSpec` STRATEGY shape uses `legs: tuple[InstrumentSpec, ...]`; this story does NOT touch the order-side spec, only the position-side `StrategyLeg`.
* `tests/portfolio_state/records/test_positions.py` — test patterns for StrategyPositionDetails / StrategyLeg fixture construction the new test module should mirror.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B), (E) — additive-field rule for `StrategyLeg.direction` and the strategy-payoff math constraints (Python `float` returns; ValueError on missing direction or zero contract_count; kink-set evaluation for breakeven enumeration).

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it edits the existing `StrategyLeg` record additively and ships a new pure-function module.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` (one field add to `StrategyLeg`) and `src/alphamind/execution/position_model/{__init__.py, strategy_payoff.py}` (new module). Tests at `tests/execution/position_model/test_strategy_payoff.py`.

### 1\. Add `direction` to `StrategyLeg`

In `src/alphamind/portfolio_state/records/positions.py`, add to the `StrategyLeg` model:

```python
direction: Direction | None = None
```

Place adjacent to the existing `leg_id` and `options` fields. The default `None` preserves backward compatibility with all existing fixture builders and the tests they construct against `StrategyLeg`. Frozen-model semantics (`ConfigDict(frozen=True)`) and field ordering are unchanged. No `model_validator` rule added on this field — its absence is legitimate when the leg's payoff isn't being evaluated.

### 2\. Implement the three payoff functions

Create `src/alphamind/execution/position_model/strategy_payoff.py` exporting:

```python
from alphamind.portfolio_state.records.positions import StrategyLeg


def compute_strategy_max_profit_usd(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> float:
    """Maximum P/L (in USD) the strategy can realize at expiration.

    Computed by evaluating the piecewise-linear at-expiration payoff function
    at each strike kink and at -inf / +inf, then taking the maximum across all
    sample points. The function is bounded above only when the strategy has
    defined-risk on the upside (e.g., bear vertical, iron condor); for
    naked-long unbounded structures (long call, long straddle), the function
    returns float('inf').

    Sign convention: positive = profit, negative = loss. net_premium_usd is
    the strategy's net debit (positive) or net credit (negative) at entry —
    it shifts the payoff function vertically.

    Per-leg sign: a long leg (direction=LONG) contributes positively to
    payoff; a short leg (direction=SHORT) contributes negatively. The
    options.contract_count is treated as magnitude (always > 0); leg
    direction supplies the sign.

    Raises ValueError if any leg's direction is None, any leg's
    contract_count <= 0, or legs is empty.
    """


def compute_strategy_max_loss_usd(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> float:
    """Minimum P/L (in USD; negative = loss) the strategy can realize at expiration.

    Same evaluation procedure as compute_strategy_max_profit_usd, taking the
    minimum across sample points. Returns float('-inf') for unbounded-loss
    structures (e.g., naked short call). The design names "max loss" as the
    margin/concentration anchor — callers depending on a finite return must
    handle -inf for unbounded-risk strategies.

    Same parameter validation as compute_strategy_max_profit_usd.
    """


def compute_strategy_breakeven_levels(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> tuple[float, ...]:
    """Underlying-price levels at which the strategy's at-expiration P/L is zero.

    Computed by enumerating zero-crossings of the piecewise-linear payoff
    function. At each adjacent pair of kink points (sorted strikes plus
    -inf/+inf sentinels), check whether the payoff sign flips; if it does,
    interpolate linearly to find the zero crossing. The result is the sorted
    tuple of all crossings. Empty tuple when the payoff has no zero crossing
    (the strategy is always profitable or always lossy across all underlying
    prices).

    Same parameter validation as compute_strategy_max_profit_usd.
    """
```

### 3\. Payoff-function semantics (testable specifics)

* **Per-leg payoff at underlying U:** for an option with strike K, contract_type CALL or PUT, contract_count C, contract_multiplier M, direction LONG or SHORT:
  * intrinsic_call(U, K) = max(U - K, 0)
  * intrinsic_put(U, K) = max(K - U, 0)
  * leg_payoff(U) = sign × intrinsic × C × M, where sign = +1 for LONG, -1 for SHORT
* **Strategy payoff at U:** sum of leg payoffs minus net_premium_usd. (`net_premium_usd > 0` means a net debit — entry cost reduces P/L; `net_premium_usd < 0` means net credit — entry receipt adds to P/L.)
* **Kink set:** the unique set of strike prices across all legs, sorted ascending. Unbounded sample points conceptually represent U → 0 and U → +∞.
* **Max/min sampling:** evaluate strategy_payoff(U) at each kink; for the bounds, evaluate the limit slope (sum of long-call deltas − sum of short-call deltas at +∞; sum of long-put deltas − sum of short-put deltas at U=0). If any limit slope is positive at +∞, max profit is +∞; if negative at +∞ or positive at 0, max loss is -∞.
* **Breakeven interpolation:** between adjacent kink points U₁ < U₂ where payoff(U₁) and payoff(U₂) have opposite signs (or one is exactly zero), the breakeven is U₁ + |payoff(U₁)| / (|payoff(U₁)| + |payoff(U₂)|) × (U₂ - U₁). Exact-zero kink points are themselves breakevens. Sort and dedupe the result.

### 4\. Module exports

`src/alphamind/execution/position_model/__init__.py` exports the three functions.

### Out of scope

* Calendar spreads with different per-leg expirations — at-expiration evaluation assumes a single common expiration. If the input legs have differing `options.expiration_date`, raise `ValueError` (this is one of the design's named structures but the at-expiration model doesn't support it; the surfacing condition in the parent body covers a follow-up story).
* Margin computation against `max_loss_usd` — that lives with regt-margin-attribution (<issue id="8a620c5d-8435-4858-879e-f0d8e876ad2d">ALP-126</issue>).
* Greek aggregation across legs (`strategy_greeks`) — already populated as data on `StrategyPositionDetails`; not derived by this story.
* Wiring the functions into any production code path — natural callers don't exist yet; this story ships standalone.

## Acceptance criteria

- [ ] `StrategyLeg.direction: Direction | None = None` field exists and is importable from `alphamind.portfolio_state.records.positions`.
- [ ] All existing `tests/portfolio_state/records/test_positions.py` tests pass unchanged after the field add (additive verification).
- [ ] All existing `uv run pytest -n auto` tests (full suite) pass unchanged after the field add — no pre-existing test fails.
- [ ] `compute_strategy_max_profit_usd`, `compute_strategy_max_loss_usd`, `compute_strategy_breakeven_levels` are all importable from `alphamind.execution.position_model`.
- [ ] **Long bull call vertical spread** (long 1 call @ K=100, short 1 call @ K=110, multiplier=100, net_premium=$300): `max_profit = 700`, `max_loss = -300`, `breakevens = (103.0,)`.
- [ ] **Long bear put vertical spread** (long 1 put @ K=100, short 1 put @ K=90, multiplier=100, net_premium=$300): `max_profit = 700`, `max_loss = -300`, `breakevens = (97.0,)`.
- [ ] **Iron condor** (long 1 put @ K=90, short 1 put @ K=95, short 1 call @ K=105, long 1 call @ K=110, multiplier=100, net_premium=-$200 (credit)): `max_profit = 200`, `max_loss = -300`, `breakevens = (93.0, 107.0)`.
- [ ] **Long straddle** (long 1 call @ K=100, long 1 put @ K=100, multiplier=100, net_premium=$500): `max_profit = inf`, `max_loss = -500`, `breakevens = (95.0, 105.0)`.
- [ ] **Long strangle** (long 1 call @ K=105, long 1 put @ K=95, multiplier=100, net_premium=$300): `max_profit = inf`, `max_loss = -300`, `breakevens = (92.0, 108.0)`.
- [ ] Empty `legs` tuple → all three functions raise `ValueError`.
- [ ] Any leg with `direction=None` → all three functions raise `ValueError` whose message names the offending leg_id.
- [ ] Any leg with `options.contract_count <= 0` → all three functions raise `ValueError` whose message names the offending leg_id.
- [ ] Legs with mixed `options.expiration_date` values → all three functions raise `ValueError` (calendar spreads out of scope per parent body surfacing condition).
- [ ] `compute_strategy_breakeven_levels` returns a tuple sorted ascending with no duplicate values (within float tolerance).
- [ ] `compute_strategy_breakeven_levels` returns an empty tuple when the strategy has no zero-crossing (e.g., a deep-credit iron condor where max_loss > 0).
- [ ] `tests/execution/position_model/test_strategy_payoff.py` exists with parametrized test cases covering at minimum the five named strategy structures above plus the four error-raising paths (empty legs, missing direction, non-positive contract_count, mixed expiration).
- [ ] All new tests pass under `uv run pytest tests/execution/position_model/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/execution/position_model/test_strategy_payoff.py -n auto -v` — confirm all parametrized cases pass.
* Spot-check: import the three functions and call them on a vertical-spread fixture; assert the closed-form max_profit / max_loss / breakeven match the criterion-1 case.
* Spot-check the additive field by `python -c "from alphamind.portfolio_state.records.positions import StrategyLeg; print('direction' in StrategyLeg.model_fields)"` should print `True`.
* Lint clean per CLAUDE.md.