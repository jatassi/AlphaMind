---
status: in_progress
completed_date:
commit_id:
---

# 05a — Position-level computations

## Goal

Implement pure-function computations the snapshot assembler uses to populate the consumer-facing computed fields on `PositionRecord` (story 03a). One module of stateless, deterministic helpers for market value, position weight, position age, distance-to-target/stop, risk/reward at current price, notional exposure, and delta-adjusted exposure. The assembler (story 06) calls these in a fixed order; this story owns the math, not the orchestration.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 1a (current market value, position weight, position age, entry execution summary), § 2a (unrealized P/L, distance to target, distance to stop, risk/reward at current price)
- `../../../design/05-execution-layer/position-model.md` § Exposure calculations: delta-adjusted — formula for delta-adjusted exposure across equity, options, and strategy positions
- `../../../design/05-execution-layer/orders-and-brackets.md` § Three invalidation types — bracket legs distinguish target (TAKE_PROFIT) from stop (PRICE_STOP); distance computations consult these
- `02-package-skeleton-and-config.md` — package layout (`computations/positions.py` is this story's target)
- `03a-position-records.md` — `PositionRecord`, `EquityPositionDetails`, `OptionsPositionDetails`, `StrategyPositionDetails`, `Direction`, `InstrumentType`
- `03c-order-and-bracket-records.md` — `BracketRecord`, `BracketLeg`, `BracketLegType`, `PriceParameters` — invalidation/target leg trigger conditions feed distance calculations
- `03g-current-price-provider-protocol.md` — `PriceQuote` carries `price_usd` and `is_stale`
- `04a-master-snapshot.md` — describes the computed fields the snapshot delivers; this story produces the values that populate them

## Depends on

- 02
- 03a (consumes `PositionRecord` and instrument-detail types)
- 03c (consumes `BracketRecord` and `BracketLeg`)
- 03g (consumes `PriceQuote`)

## Scope

In scope, all under `src/alphamind/portfolio_state/computations/positions.py` — pure functions with no I/O, no clocks, no shared state. Tests at `tests/portfolio_state/computations/test_positions.py`.

### 1. Market value

`compute_market_value_usd(position: PositionRecord, price: PriceQuote) -> float`

- For `InstrumentType.EQUITY`: `position.equity_details.share_count * price.price_usd` for `Direction.LONG`; `-position.equity_details.share_count * price.price_usd` for `Direction.SHORT` (short positions report negative market value).
- For `InstrumentType.OPTIONS`: `position.options_details.contract_count * position.options_details.contract_multiplier * price.price_usd` (the `price` here is the option's own price; the assembler is responsible for fetching the option's price, not the underlying's).
- For `InstrumentType.STRATEGY`: sum across `position.strategy_details.legs`; each leg's contribution is `leg.contract_count * leg.contract_multiplier * leg_price` — but strategy market value cannot be computed from a single `price` argument since each leg has its own price. **Strategy positions are out of scope for this function** — the assembler uses `compute_strategy_market_value_usd(position, leg_prices: dict[str, PriceQuote])` (defined below).
- Validates: `position.instrument_type` is EQUITY or OPTIONS; STRATEGY raises `ValueError("use compute_strategy_market_value_usd")`.

`compute_strategy_market_value_usd(position: PositionRecord, leg_prices: dict[str, PriceQuote]) -> float`

- Sum across `position.strategy_details.legs` of `leg.contract_count * leg.contract_multiplier * leg_prices[leg.leg_id].price_usd`.
- Raises `KeyError` (or a typed `MissingLegPriceError`, see below) if any `leg.leg_id` is absent from `leg_prices`.

### 2. Position weight

`compute_position_weight_pct(position_market_value_usd: float, total_portfolio_value_usd: float) -> float`

- Returns `(abs(position_market_value_usd) / total_portfolio_value_usd) * 100.0`. Uses `abs()` so a short position's weight is its size relative to the portfolio, not a negative number per `portfolio-state.md` § 1a "position weight: market value as a percentage of total portfolio value (positions + cash)" — the design treats weight as a magnitude.
- Returns `0.0` when `total_portfolio_value_usd == 0` (degenerate but valid: empty portfolio, no positions). The function does not raise on zero division.
- Validates: `total_portfolio_value_usd >= 0`. Negative total raises `ValueError`.

### 3. Position age

`compute_position_age_hours(entry_timestamp: datetime, now: datetime) -> float`

- Returns `(now - entry_timestamp).total_seconds() / 3600.0`.
- Both arguments must be tz-aware; mixing tz-naive and tz-aware raises `ValueError`.
- Returns `0.0` when `entry_timestamp == now`; non-negative for `now >= entry_timestamp`. Returns a negative value when `now < entry_timestamp` (the function does not clamp; the caller is responsible for asserting the assembler's `now` is monotonic).

### 4. Unrealized P/L

`compute_unrealized_pnl_usd(market_value_usd: float, cost_basis_usd: float, direction: Direction) -> float`

- `Direction.LONG`: `market_value_usd - cost_basis_usd`.
- `Direction.SHORT`: `cost_basis_usd - abs(market_value_usd)` — short positions profit when market value (price × quantity) drops below cost basis.

`compute_unrealized_pnl_pct(unrealized_pnl_usd: float, cost_basis_usd: float) -> float`

- Returns `(unrealized_pnl_usd / cost_basis_usd) * 100.0`.
- Returns `0.0` when `cost_basis_usd == 0` (degenerate: zero-cost position; the function does not raise).

### 5. Distance to target / stop

The bracket holds the target leg (`BracketLegType.TAKE_PROFIT`) and one or more invalidation legs (`PRICE_STOP`, `TIME_EXPIRATION`, `EVENT_INVALIDATION`). Distance computations operate on price-based legs only.

`compute_distance_to_target_usd(current_price_usd: float, bracket: BracketRecord) -> float | None`

- Iterates `bracket.protective_legs` to find the leg with `leg_type == BracketLegType.TAKE_PROFIT`. There is at most one (validated at the bracket-record level, but this function returns `None` if absent, never raises).
- Parses the leg's `trigger_condition` as a price level. Per 03c's `BracketLeg.trigger_condition: str`, the trigger is rendered as a stable string. This story owns a small parser: a price level is the string form of a positive float (e.g., `"880.00"`), parsed via `float(trigger_condition)`. Non-numeric trigger conditions (event-based, time-based) make this leg ineligible for distance computation; the function returns `None` in that case.
- Returns `target_price - current_price_usd` for `Direction.LONG` positions (positive when the target is above the current price — the more remaining upside, the larger the value).
- Returns `current_price_usd - target_price` for `Direction.SHORT` positions (positive when the target is below the current price).
- The function takes `direction: Direction` as a third argument so the sign convention is correct.

`compute_distance_to_stop_usd(current_price_usd: float, bracket: BracketRecord, direction: Direction) -> float | None`

- Same shape; iterates for the first `BracketLegType.PRICE_STOP` leg. Sign convention: positive when the stop is below the current price for LONG (cushion remaining), positive when the stop is above the current price for SHORT.
- Returns `None` when no PRICE_STOP leg exists.

### 6. Risk/reward at current price

`compute_risk_reward_at_current(distance_to_target_usd: float | None, distance_to_stop_usd: float | None) -> float | None`

- Returns `distance_to_target_usd / abs(distance_to_stop_usd)` when both are non-`None` and `distance_to_stop_usd != 0`.
- Returns `None` when either argument is `None`.
- Returns `None` when `distance_to_stop_usd == 0` (the position is at the stop; risk/reward is undefined). The function does not raise on zero division.
- Sign convention: a positive ratio indicates favorable R:R remaining; a negative ratio indicates the price is past the target or past the stop (the design's "a 3:1 ratio at entry might be 1:1 halfway to target" — the function does not constrain this to positive).

### 7. Notional exposure

`compute_notional_exposure_usd(position: PositionRecord, price: PriceQuote) -> float`

- Equity: `share_count * price.price_usd` (always non-negative — exposure is a magnitude, not signed). Direction is captured separately via `delta_adjusted_exposure_usd`.
- Options: `contract_count * contract_multiplier * underlying_price` — the option's *underlying* price; not the option's premium price. The assembler must supply the underlying price.
- Strategy: sum of leg notional exposures via the per-leg underlying prices: `compute_strategy_notional_exposure_usd(position, leg_underlying_prices)`.

`compute_strategy_notional_exposure_usd(position: PositionRecord, leg_underlying_prices: dict[str, PriceQuote]) -> float`

- Sum across `position.strategy_details.legs` of `leg.contract_count * leg.contract_multiplier * leg_underlying_prices[leg.leg_id].price_usd`.

### 8. Delta-adjusted exposure

Per `position-model.md` § Exposure calculations:

`compute_delta_adjusted_exposure_usd(position: PositionRecord, price: PriceQuote) -> float`

- `InstrumentType.EQUITY`, `Direction.LONG`: `share_count * price.price_usd` (delta is 1 for long equity).
- `InstrumentType.EQUITY`, `Direction.SHORT`: `-share_count * price.price_usd` (delta is -1 for short equity).
- `InstrumentType.OPTIONS`: `contract_count * contract_multiplier * options_details.greeks.delta * underlying_price`. `price` here must be the underlying's price; the assembler is responsible for resolving option vs underlying.
- `InstrumentType.STRATEGY`: handled by `compute_strategy_delta_adjusted_exposure_usd(position, leg_underlying_prices)` — delegated for the same reason as market value.

`compute_strategy_delta_adjusted_exposure_usd(position: PositionRecord, leg_underlying_prices: dict[str, PriceQuote]) -> float`

- Returns `position.strategy_details.strategy_greeks.delta * sum(leg notionals)` — the strategy's net delta is in `strategy_details.strategy_greeks` per 03a, applied to the summed underlying notional. Equivalent algebra to summing per-leg deltas; this form keeps the public contract aligned with `strategy_greeks` as the canonical strategy-level delta.

### 9. Typed errors

- `class MissingLegPriceError(KeyError)` — raised by the strategy variants when `leg_prices` or `leg_underlying_prices` is missing a `leg_id` present in the position's `legs`. Inherits from `KeyError` so existing dict-access idioms behave naturally; the typed subclass lets the assembler distinguish missing-leg-price from generic dict misses.

### 10. Tests

Per function, pure-input → pure-output tests. Cover:
- Equity LONG: positive market value, positive notional, positive delta-adjusted.
- Equity SHORT: negative market value, positive notional (magnitude), negative delta-adjusted.
- Options LONG call: market value uses option price; delta-adjusted uses underlying × delta.
- Options LONG put: delta-adjusted is negative even though direction is LONG (puts have negative delta).
- Strategy: market value sums per-leg via `leg_prices` dict.
- `MissingLegPriceError` raised when a leg_id is absent.
- `compute_position_weight_pct(0, 0)` returns `0.0` (no zero-division exception).
- `compute_position_weight_pct(100, -50)` raises `ValueError` (negative total portfolio value).
- `compute_position_age_hours(now, now)` returns `0.0`; `compute_position_age_hours(future, now)` returns negative.
- Mixed tz-aware / tz-naive datetimes raise `ValueError`.
- `compute_unrealized_pnl_usd` correctly applies the LONG vs SHORT sign convention on a known fixture.
- `compute_unrealized_pnl_pct` returns `0.0` on zero cost basis.
- `compute_distance_to_target_usd` parses a numeric `trigger_condition`; non-numeric returns `None`.
- `compute_distance_to_target_usd` returns the right sign for LONG vs SHORT.
- `compute_distance_to_stop_usd` parses similarly; returns `None` when no PRICE_STOP leg present.
- `compute_risk_reward_at_current` returns `None` on either-`None` input or zero-divisor input; returns the documented signed ratio otherwise.

Out of scope:
- Portfolio-level P/L rollup (story 05b).
- Sector / directional / gross exposure aggregation across positions (story 05c).
- Risk budget consumption against active limits (story 05d).
- Fetching prices (the assembler injects `PriceQuote` instances; story 03g declares the provider).
- Resolving "option vs underlying" price selection for options/strategy positions — the assembler decides which price to pass; this story's functions take the price argument as given.
- VWAP / slippage from `PositionFill` records — entry execution summary is a separate concern; this story focuses on the per-invocation snapshot fields.
- Mark-to-market reconciliation against Alpaca's account state (broker-adapter concern).

## Notes

The functions are deliberately thin and focused; complex logic (option-vs-underlying price selection, leg-price assembly for strategies) is the assembler's job. Per `feedback_simplify_before_building.md`, this module is a function library, not a class hierarchy.

Per `feedback_avoid_numeric_anchors.md`, no function imposes thresholds. Distance and R:R values are returned as raw numbers; downstream consumers (strategist, PM) interpret them.

The `trigger_condition: str` parsing is minimal: a numeric string parses to a float; anything else returns `None` for the relevant distance computation. This matches `03c`'s contract that `trigger_condition` is "a stable string" rendering price, timestamp, or event description. The parser does not handle timestamp- or event-form triggers because distance-to-stop and distance-to-target are price-based by construction (per `orders-and-brackets.md`'s "price-based invalidation" and the design's "Distance to target: current price vs. thesis target").

The strategy variants (`compute_strategy_market_value_usd`, `compute_strategy_notional_exposure_usd`, `compute_strategy_delta_adjusted_exposure_usd`) take `leg_prices` / `leg_underlying_prices` dicts because each leg has its own underlying or own option price. The single-position-single-price form does not generalize; splitting the API surface keeps each function's contract clean.

The `MissingLegPriceError` subclass of `KeyError` lets the assembler catch missing-leg-price specifically while still allowing the standard `KeyError` interceptor in the test suite. This is a deliberate seam for the assembler's "what to do when a leg's underlying price is unavailable" policy decision (deferred to the assembler / freshness module — story 06 / story 08).

Per `feedback_no_inventing_component_names.md`, every function name describes the scalar it computes, sourced from the field names on `PositionRecord` (story 03a). No new vocabulary is introduced.

Short equity market value is signed (negative). The portfolio-level rollups (story 05b) sum signed market values to compute net exposure; the `compute_position_weight_pct` function takes `abs()` to give a non-negative weight, matching the design's framing of weight as a magnitude.

## Acceptance criteria

- [ ] `compute_market_value_usd(position, price)` returns the documented value for EQUITY (signed by direction) and OPTIONS; raises `ValueError` for STRATEGY.
- [ ] `compute_strategy_market_value_usd(position, leg_prices)` returns the leg-summed value; missing leg_id raises `MissingLegPriceError`.
- [ ] `compute_position_weight_pct` returns the absolute-value-weighted percentage; returns `0.0` on zero portfolio value; raises `ValueError` on negative portfolio value.
- [ ] `compute_position_age_hours` returns the correct hour-difference; mixed tz-aware/tz-naive raises `ValueError`.
- [ ] `compute_unrealized_pnl_usd` returns the documented value for LONG and SHORT.
- [ ] `compute_unrealized_pnl_pct` returns `0.0` on zero cost basis; otherwise the documented percentage.
- [ ] `compute_distance_to_target_usd` parses a numeric `trigger_condition`; returns `None` for non-numeric or absent TAKE_PROFIT leg; sign convention matches direction.
- [ ] `compute_distance_to_stop_usd` parses similarly; returns `None` when no PRICE_STOP leg; sign convention matches direction.
- [ ] `compute_risk_reward_at_current` returns `None` on either-`None` input or zero-divisor input; otherwise the signed ratio.
- [ ] `compute_notional_exposure_usd` and `compute_delta_adjusted_exposure_usd` return the documented values for EQUITY and OPTIONS.
- [ ] `compute_strategy_notional_exposure_usd` and `compute_strategy_delta_adjusted_exposure_usd` return the leg-summed and strategy-greek values respectively.
- [ ] `MissingLegPriceError` is a `KeyError` subclass; raised from each strategy-variant function on missing leg_id.
- [ ] All functions are pure: identical inputs produce identical outputs across repeated calls (verified by parametrized property test).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
