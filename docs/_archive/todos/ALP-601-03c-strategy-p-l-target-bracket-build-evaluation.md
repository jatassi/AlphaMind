# 03c — Strategy P/L-target bracket: build + evaluation

# 03c — Strategy P/L-target bracket: build + evaluation

## Goal

Make strategy positions get a working P/L-target bracket. Two layers block it today: the OPEN write path builds a strategy take-profit leg with the hard-coded-LONG `_target_to_bracket_leg`, and `bracket_stops` disables P/L evaluation for strategies at two sites (`task.py` short-circuits `StrategyPositionDetails` to `return False`; `triggers.py`'s `_options_details_for_pl` raises `NotImplementedError` for a strategy). This story owns the full strategy-bracket vertical — the OPEN-path bracket build and the `bracket_stops` evaluation — so a strategy's take-profit fires on the strategy's net P/L. This is the full-scope option the operator chose for the parent's bracket question.

## Reading

* `src/alphamind/execution/write_paths/phase2/open.py` — `_build_pending_bracket`, `_target_to_bracket_leg` (builds a `PriceTrigger` TAKE_PROFIT leg, hard-coded `direction=Direction.LONG`), `_writeback_open`.
* `src/alphamind/portfolio_state/records/orders.py` — `BracketLeg`, `PLAnchorSpec` (`spec_type`, `pct`, `actual_entry_price`), `BracketLeg._validate_pl_anchor_compatibility` (a `pl_anchor` is valid only on TAKE_PROFIT / PRICE_STOP legs; a `pl_anchor` leg still carries a `PriceTrigger`), `PriceTrigger`.
* `src/alphamind/execution/continuous_monitor/bracket_stops/task.py` — `_leg_should_fire` (the `if leg.pl_anchor is not None: if isinstance(position.details, StrategyPositionDetails): return False` short-circuit).
* `src/alphamind/execution/continuous_monitor/bracket_stops/triggers.py` — `evaluate_pl_target_trigger`, `_options_details_for_pl` (raises `NotImplementedError` for a strategy), `_crosses_threshold`, `_bs_option_price` (Black-Scholes per-leg pricing primitive), `evaluate_price_based_trigger`.
* `src/alphamind/commands/command_models.py` — `Target` (`target_type: absolute_price | pl_percentage | pl_dollar`).
* `src/alphamind/portfolio_state/computations/positions.py` — `compute_strategy_market_value_usd` (signed leg-value sum).
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails` (`net_premium_usd`, `max_profit_usd`, `legs`).
* `docs/design/05-execution-layer/orders-and-brackets.md` § P/L-based bracket legs; the strategy bracket "references the strategy's net P/L".
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (F); § Resolved design decisions (full bracket scope).

## Depends on

* 01c ([ALP-594](https://linear.app/alphamind-jatassi/issue/ALP-594/01c-open-write-path-create-strategy-positions)) — both edit `open.py`; 03c refines the strategy bracket that 01c's `_writeback_open` builds.
* 02 ([ALP-598](https://linear.app/alphamind-jatassi/issue/ALP-598/02-phase-1-strategy-entry-fill-payoff-recompute)) — the strategy P/L evaluator reads the recomputed `net_premium_usd` / `max_profit_usd`.

## Scope

In scope: `src/alphamind/execution/write_paths/phase2/open.py` (strategy bracket build), `src/alphamind/execution/continuous_monitor/bracket_stops/task.py`, `src/alphamind/execution/continuous_monitor/bracket_stops/triggers.py`. Tests under `tests/execution/write_paths/` and `tests/execution/continuous_monitor/bracket_stops/`.

### 1\. Strategy P/L-target evaluation (`triggers.py`)

Replace the strategy `NotImplementedError` in `_options_details_for_pl` with a strategy P/L evaluation path (a new `evaluate_strategy_pl_target_trigger`, or a strategy branch — leave single-option `evaluate_pl_target_trigger` behavior unchanged). It computes the strategy's current net P/L: for each leg derive the leg's current option price via the existing `_bs_option_price` primitive (spot + the leg's strike / contract type / expiration / IV), sum the signed per-leg market values (LONG `+`, SHORT `-`, scaled by `contract_count × contract_multiplier`), then subtract the record's `net_premium_usd` to get the strategy net P/L. Fire the TAKE_PROFIT when that net P/L crosses the leg's target. The strategy evaluator reads `net_premium_usd` straight off `StrategyPositionDetails` (the strategy's cost basis), so it needs no `PLAnchorSpec.actual_entry_price` anchor the single-option path relies on.

### 2\. Un-disable strategy P/L brackets (`task.py`)

Remove the `isinstance(position.details, StrategyPositionDetails): return False` short-circuit in `_leg_should_fire` so a strategy's P/L-target leg routes into the strategy P/L evaluator.

### 3\. Strategy take-profit bracket build (`open.py`)

`_build_pending_bracket` for a strategy builds a TAKE_PROFIT leg carrying the strategy's P/L target from `command.target`, in the representation the deliverable-1 evaluator consumes. A strategy take-profit references the strategy's net P/L (parent decision F), not a single-sided underlying-price threshold — `_target_to_bracket_leg`'s hard-coded-LONG `PriceTrigger` direction is not applied to a strategy take-profit.

### Out of scope

Single-option / equity bracket build and evaluation — unchanged. Strategy PRICE_STOP / invalidation legs beyond what `command.invalidation_legs` already produces. Partial-close bracket adjustment.

### Surfacing condition

If the existing `PLAnchorSpec` (designed for single-option premium anchoring) does not cleanly carry a strategy's net-P/L target, surface to the operator before inventing a parallel leg representation — do not improvise a second P/L-anchor type.

## Acceptance criteria

- [ ] `_leg_should_fire` no longer short-circuits a `StrategyPositionDetails` position's P/L-target leg to `False`.
- [ ] The strategy P/L evaluator computes the strategy's current net P/L from per-leg Black-Scholes-derived prices and the record's `net_premium_usd`.
- [ ] A credit strategy's take-profit fires when the strategy's net P/L reaches its profit target and does not fire while net P/L is below target.
- [ ] `evaluate_pl_target_trigger` / `_options_details_for_pl` no longer raises `NotImplementedError` for a `StrategyPositionDetails` position.
- [ ] `_build_pending_bracket` builds a strategy TAKE_PROFIT leg the strategy P/L evaluator consumes; the hard-coded-LONG `PriceTrigger` direction from `_target_to_bracket_leg` is not applied to a strategy take-profit.
- [ ] Single-leg option and equity bracket build + evaluation behavior is unchanged (regression-covered).
- [ ] Tests under `tests/execution/write_paths/` and `tests/execution/continuous_monitor/bracket_stops/` cover a credit strategy's take-profit build and firing; pass under `uv run pytest tests/execution/write_paths/ tests/execution/continuous_monitor/ --testmon -n auto`.

## Verification

`uv run pytest tests/execution/write_paths/ tests/execution/continuous_monitor/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. The orchestrator confirms a constructed credit strategy whose net P/L crosses its target fires the take-profit leg, and a below-target one does not.
