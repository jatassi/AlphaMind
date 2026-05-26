# 01c — OPEN write path: create strategy positions

# 01c — OPEN write path: create strategy positions

## Goal

Make `_build_pending_position` in `execution/write_paths/phase2/open.py` build a `StrategyPositionDetails` skeleton for a `StrategyInstrument` instead of raising `NotImplementedError`. After this story a strategy `OpenCommand` produces a PENDING strategy `PositionRecord` in production — today only the debug-e2e seed creates strategy positions. The skeleton's payoff metrics are zeroed; story 02 recomputes them from the filled legs at the Phase 1 entry-fill (parent decision A). The strategy bracket is not this story's concern (parent decision F — story 03c).

## Reading

* `src/alphamind/execution/write_paths/phase2/open.py` — `_writeback_open`, `_build_pending_position` (its `else` branch raises `NotImplementedError` for `StrategyInstrument`), `_direction_from_instrument`; study how the `OptionInstrument` branch builds `OptionsPositionDetails` at `contract_count=0.0`.
* `src/alphamind/commands/command_models.py` — `StrategyInstrument` (`asset_type`, `strategy_type: StrategyType`, `underlying`, `legs: tuple[StrategyLeg, ...]`, min 2) and the wire `StrategyLeg` (`strike: Price`, `expiration: str`, `contract_type`, `direction`, `quantity_ratio: int`).
* `src/alphamind/portfolio_state/records/positions.py` — record-form `StrategyPositionDetails`, `StrategyLeg` (`leg_id`, `options`, `direction`), `OptionsPositionDetails`, `OptionGreeks`, `PositionRecord`, `PositionStatus`, `Direction`, `OptionContractType`. `PositionRecord._check_status_rules` already permits a PENDING strategy with `execution_history`.
* `src/alphamind/execution/constants.py` — `LISTED_OPTION_CONTRACT_MULTIPLIER`.
* `docs/design/05-execution-layer/position-model.md` § Strategy position (as updated by story 01a).
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decisions (A) and (F), and § Surfacing conditions (validation-metadata greeks for a strategy are a per-leg average, not a net).

## Depends on

Nothing. Wave 1.

## Scope

In scope: `src/alphamind/execution/write_paths/phase2/open.py`. Tests under `tests/execution/write_paths/`.

### 1\. `_build_pending_position` strategy branch

Replace the `else` branch that raises `NotImplementedError` for a `StrategyInstrument` with a branch that builds a `StrategyPositionDetails` skeleton:

* One record-form `StrategyLeg` per wire `StrategyLeg`, each with a deterministic `leg_id` (e.g. `{position_id}-leg-{idx}`), `direction` carried straight through from the wire leg, and `options` an `OptionsPositionDetails` skeleton — `underlying_ticker` = the strategy's `underlying`; `strike_price` / `expiration_date` / `contract_type` from the wire leg; `contract_count=0.0`; `contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER`; `premium_paid_per_contract=0.0`; `greeks` a zeroed `OptionGreeks`.
* `strategy_type_label` = the wire `strategy_type` value.
* `net_premium_usd=0.0`, `max_profit_usd=0.0`, `max_loss_usd=0.0`, `breakeven_levels=()`, `strategy_greeks` a zeroed `OptionGreeks` — all skeleton zeros; story 02 recomputes them from the filled legs.

The skeleton mirrors the single-option branch (`contract_count=0.0`, `premium_paid_per_contract=0.0`): the record reflects state, not intent. The strategy branch does **not** read `validation_greeks` / `validation_iv` and does not require them to be non-None — the validation metadata's strategy greeks are a per-leg average, not a net, so they must not seed `strategy_greeks` (see parent Surfacing conditions). Per-leg greeks are zeroed and refreshed later by the continuous monitor, exactly as for single-leg options.

### 2\. `_direction_from_instrument`

Keep the existing `Direction.LONG` return for a `StrategyInstrument`, and rewrite its docstring to state — per the parent's resolved decision — that this is an inert placeholder, not a meaningful direction; strategy consumers derive directional sign from the legs (cross-reference `position-model.md`). No behavior change.

### Out of scope

The strategy bracket — `_build_pending_bracket` / `_target_to_bracket_leg` strategy handling is story 03c (parent decision F); `_writeback_open` still calls the existing bracket builder for a strategy and this story does not change it. Payoff-metric population — story 02. Phase 1 strategy fill integration already exists in `phase1.py`.

## Acceptance criteria

- [ ] `_build_pending_position` returns a `PositionRecord` carrying `StrategyPositionDetails` for a `StrategyInstrument` — no `NotImplementedError`.
- [ ] The built `StrategyPositionDetails` has one `StrategyLeg` per wire leg, each with a deterministic `leg_id`, the wire leg's `direction`, and an `OptionsPositionDetails` skeleton at `contract_count=0.0` and `premium_paid_per_contract=0.0`.
- [ ] `net_premium_usd`, `max_profit_usd`, `max_loss_usd` are `0.0` and `breakeven_levels` is empty on the freshly-built skeleton.
- [ ] The strategy branch does not read `validation_greeks` / `validation_iv`; `strategy_greeks` and every leg's `greeks` are zeroed `OptionGreeks`.
- [ ] `_direction_from_instrument` returns `Direction.LONG` for a `StrategyInstrument` and its docstring states this is an inert placeholder.
- [ ] The built strategy `PositionRecord` is `PositionStatus.PENDING` and passes `PositionRecord.__post_init__`.
- [ ] A test drives `_writeback_open` (or `_build_pending_position` directly) for a strategy `OpenCommand` and asserts the persisted position; passes under `uv run pytest tests/execution/write_paths/ --testmon -n auto`.

## Verification

`uv run pytest tests/execution/write_paths/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. The orchestrator confirms a strategy OPEN command produces a PENDING strategy position rather than raising.
