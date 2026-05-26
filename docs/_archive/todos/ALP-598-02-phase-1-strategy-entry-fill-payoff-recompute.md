# 02 — Phase 1 strategy entry-fill payoff recompute

# 02 — Phase 1 strategy entry-fill payoff recompute

## Goal

Make Phase 1's strategy fill integration recompute the parent `StrategyPositionDetails` payoff metrics — `net_premium_usd`, `max_profit_usd`, `max_loss_usd`, `breakeven_levels` — from the filled legs once leg counts and premiums are known. Today `phase1.py`'s strategy-fill helpers update per-leg `contract_count` / `premium_paid_per_contract` but never touch the parent payoff fields, so a strategy created via the OPEN write path (story 01c) keeps its zeroed skeleton metrics forever. This wires the existing `strategy_payoff.py` primitives in — satisfying the parent AC that `max_profit_usd` / `max_loss_usd` are populated for live strategy positions, not stubbed `0.0`.

## Reading

* `src/alphamind/execution/write_paths/phase1.py` — `_apply_strategy_fill_to_position`, `_apply_strategy_entry_or_continuation` (updates legs via `_set_leg_entry`, fires the atomic PENDING→OPEN at the last leg), `_apply_strategy_open_fill` / `_add_to_leg` (ADD fills on an OPEN strategy), `_apply_strategy_close_fill`.
* `src/alphamind/execution/position_model/strategy_payoff.py` — `compute_strategy_max_profit_usd`, `compute_strategy_max_loss_usd`, `compute_strategy_breakeven_levels` (existing; each takes `(legs, net_premium_usd)` and raises `ValueError` on a leg with `contract_count <= 0`), plus `compute_strategy_net_premium_usd` (shipped by story 01b).
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails`, `StrategyLeg`, `OptionsPositionDetails`.
* `docs/design/05-execution-layer/position-model.md` § Strategy position (as updated by story 01a).
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (A).

## Depends on

* 01b ([ALP-593](https://linear.app/alphamind-jatassi/issue/ALP-593/01b-strategy-net-premium-and-net-greeks-aggregation-primitives)) — imports `compute_strategy_net_premium_usd` (alongside the existing `compute_strategy_max_*` / `compute_strategy_breakeven_levels`).

## Scope

In scope: the strategy-fill helpers in `src/alphamind/execution/write_paths/phase1.py`. Tests under `tests/execution/write_paths/`.

### 1\. Recompute parent payoff metrics from filled legs

Add a helper that, given the updated legs of a `StrategyPositionDetails`, returns a `StrategyPositionDetails` with:

* `net_premium_usd` = `compute_strategy_net_premium_usd(legs)`
* `max_profit_usd` = `compute_strategy_max_profit_usd(legs, net_premium_usd)`
* `max_loss_usd` = `compute_strategy_max_loss_usd(legs, net_premium_usd)`
* `breakeven_levels` = `compute_strategy_breakeven_levels(legs, net_premium_usd)`

Apply it wherever a strategy fill changes a leg's `contract_count` / `premium_paid_per_contract` — the entry-fill path (`_apply_strategy_entry_or_continuation`) and the ADD path (`_apply_strategy_open_fill` / `_add_to_leg`) — reading the post-update legs so the parent metrics always agree with the legs.

Recompute only when every leg has a positive `contract_count` (the `strategy_payoff` functions raise on a zero-count leg). While any leg is still a zero-count skeleton, leave the parent metrics at their skeleton zeros. For the entry path this condition is first met at the atomic PENDING→OPEN transition (a strategy enters all legs simultaneously); for an ADD on an already-OPEN strategy every leg is already positive.

### 2\. Leave `strategy_greeks` untouched

`strategy_greeks` is not recomputed here — a fill carries no greeks and per-leg greeks are still zero/stale at fill time; greek refresh is the continuous monitor's job. Leave `strategy_greeks` as the fill found it.

### Out of scope

`strategy_greeks` recomputation. Partial-close rescaling of the parent payoff metrics — the `_apply_strategy_close_fill` path is unchanged. The OPEN skeleton build (story 01c). The assembler / P/L / margin consumers (stories 03a / 03b).

## Acceptance criteria

- [ ] After a strategy's atomic PENDING→OPEN entry fill, `StrategyPositionDetails.net_premium_usd` equals `compute_strategy_net_premium_usd` of the filled legs — positive for a debit strategy, negative for a credit strategy.
- [ ] After the entry fill, `max_profit_usd`, `max_loss_usd`, and `breakeven_levels` equal the `strategy_payoff.py` computations over the filled legs and the recomputed net premium.
- [ ] While a strategy entry is partially filled (any leg still at `contract_count = 0`), the parent payoff metrics stay at their skeleton zeros and no `ValueError` is raised.
- [ ] An ADD fill that increases a strategy's leg counts triggers a parent-metric recompute consistent with the new legs.
- [ ] `strategy_greeks` is unchanged by the fill path.
- [ ] A test seeds a PENDING net-credit strategy, applies entry fills leg-by-leg, and asserts the parent metrics are zero until the final leg fills and credit-correct (negative `net_premium_usd`, finite `max_loss_usd`) afterward; passes under `uv run pytest tests/execution/write_paths/ --testmon -n auto`.

## Verification

`uv run pytest tests/execution/write_paths/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. Spot-check a credit iron condor: `net_premium_usd < 0`, `max_loss_usd` finite and negative.
