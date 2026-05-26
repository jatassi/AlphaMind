# 01b — Strategy net-premium & net-greeks aggregation primitives

# 01b — Strategy net-premium & net-greeks aggregation primitives

## Goal

Add two pure aggregation functions to `src/alphamind/execution/position_model/strategy_payoff.py`: `compute_strategy_net_premium_usd` and `compute_strategy_greeks`. They produce the strategy-level `net_premium_usd` and `strategy_greeks` values carried on `StrategyPositionDetails`. The module already ships the credit-aware `compute_strategy_max_profit_usd` / `compute_strategy_max_loss_usd` / `compute_strategy_breakeven_levels` functions but no net-premium or net-greeks aggregator — these two complete the set. Story 02 (Phase 1 payoff recompute) and story 04 (debug-e2e seed) consume them.

## Reading

* `src/alphamind/execution/position_model/strategy_payoff.py` — the existing module: `_validate_legs`, `_leg_sign` (`+1` LONG / `-1` SHORT), `_leg_payoff`, `compute_strategy_max_profit_usd`. Reuse `_validate_legs` and `_leg_sign`.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyLeg` (`leg_id`, `options: OptionsPositionDetails`, `direction: Direction | None`), `OptionsPositionDetails` (`contract_count`, `contract_multiplier`, `premium_paid_per_contract`, `greeks`), `OptionGreeks` (`delta`/`gamma`/`theta`/`vega`, `as_of_timestamp`, `iv_used`, `refresh_failed`; `__post_init__` rejects `iv_used <= 0` only when non-None).
* `src/alphamind/portfolio_state/computations/positions.py` — `compute_strategy_delta_adjusted_exposure_usd` does `strategy_greeks.delta * summed_notional`; the greek aggregation must make that product correct.
* `docs/design/05-execution-layer/position-model.md` § Strategy position (as updated by story 01a) — net-premium sign convention; strategy greeks "summed from component legs".
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (C) — `strategy_greeks` is net-signed.

## Depends on

Nothing. Wave 1.

## Scope

In scope: `src/alphamind/execution/position_model/strategy_payoff.py` and the package `__init__.py`. Tests extend the existing strategy-payoff test module under `tests/execution/position_model/`.

### 1\. `compute_strategy_net_premium_usd`

`compute_strategy_net_premium_usd(legs: tuple[StrategyLeg, ...]) -> float`. Returns the signed net premium:

`sum over legs of  leg_sign * contract_count * contract_multiplier * premium_paid_per_contract`

where `leg_sign` is `+1` for a LONG leg (premium paid) and `-1` for a SHORT leg (premium received). A positive result is a net debit; a negative result is a net credit. Calls `_validate_legs` first (reuse the existing helper). The result is exactly the `net_premium_usd` argument the existing `compute_strategy_max_profit_usd` / `compute_strategy_max_loss_usd` functions already expect.

### 2\. `compute_strategy_greeks`

`compute_strategy_greeks(legs: tuple[StrategyLeg, ...]) -> OptionGreeks`. Returns the strategy's net-signed aggregate greeks. For each of delta / gamma / theta / vega:

`strategy_greek = ( sum over legs of leg_sign * contract_count * contract_multiplier * leg.greeks.<g> ) / ( sum over legs of contract_count * contract_multiplier )`

with `leg_sign` `+1` LONG / `-1` SHORT. This contract-weighted signed average makes `strategy_greeks.delta * summed_notional` equal the strategy's true delta-adjusted exposure (the contract that `compute_strategy_delta_adjusted_exposure_usd` consumes). A net-short-delta strategy yields a negative `strategy_greeks.delta`; a short-heavy strategy can yield negative net gamma / vega — `OptionGreeks` has no positivity validator, so signed values are valid. The result carries `as_of_timestamp=None`, `iv_used=None` (no single strategy IV), and `refresh_failed` = the OR of the legs' flags. Calls `_validate_legs` first.

### Out of scope

Wiring these into the OPEN write path, Phase 1, or the assembler — story 02 recomputes via them, story 04 seeds via them. The existing `compute_strategy_max_*` / `compute_strategy_breakeven_levels` functions are unchanged.

## Acceptance criteria

- [ ] `compute_strategy_net_premium_usd` returns a positive value for a net-debit strategy and a negative value for a net-credit strategy (a SHORT leg subtracts its premium contribution).
- [ ] `compute_strategy_net_premium_usd` raises `ValueError` on empty legs, a leg missing `direction`, or a non-positive `contract_count` (via `_validate_legs`).
- [ ] `compute_strategy_greeks` returns an `OptionGreeks` whose `delta` is negative for a net-short-delta structure and positive for a net-long-delta structure.
- [ ] `compute_strategy_greeks` applies `leg_sign` (`+1` LONG / `-1` SHORT) to every greek before the contract-weighted aggregation — a SHORT leg flips the sign of its delta/gamma/theta/vega contribution.
- [ ] `compute_strategy_greeks` sets `iv_used=None` and `as_of_timestamp=None` on the result and ORs the legs' `refresh_failed`.
- [ ] Both functions are importable from `alphamind.execution.position_model` (re-exported via `__init__.py`).
- [ ] The strategy-payoff test module covers both functions for a debit vertical spread, a credit vertical spread, and a net-short structure (e.g. short strangle), and passes under `uv run pytest tests/execution/position_model/ --testmon -n auto`.

## Verification

Run `uv run pytest tests/execution/position_model/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. Spot-check: a bear call spread (sell the lower strike, buy the higher) returns a negative `compute_strategy_net_premium_usd`.
