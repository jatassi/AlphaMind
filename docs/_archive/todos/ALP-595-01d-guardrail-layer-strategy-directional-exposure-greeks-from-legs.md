# 01d — Guardrail layer: strategy directional exposure & greeks from legs

# 01d — Guardrail layer: strategy directional exposure & greeks from legs

## Goal

Make the guardrail layer reflect a multi-leg strategy's true directional contribution. Two read sites currently lean on the position-level `direction` (always `LONG` for a strategy) instead of the strategy's net-signed greeks / per-leg directions: `_accumulate_portfolio_greeks` in `risk_guardrails/library_snapshot.py` (the delivered portfolio greek view) and the `guardrail_evaluation` exposure projection feeding `net_long_pct`. After this story, a net-short-delta credit strategy contributes negative delta to the portfolio greek view and its true signed exposure to `net_long_pct`.

## Reading

* `src/alphamind/risk_guardrails/library_snapshot.py` — `_accumulate_portfolio_greeks` (signs the contribution by position-level `direction` — always `+1` for a strategy — and scales by `details.legs[0].options.contract_count`, an arbitrary first-leg count) and `_build_greeks`.
* `src/alphamind/risk_guardrails/guardrail_evaluation/` — `delta_adjusted.py`, `rules/exposure.py`, `risk_budget.py`, `types.py` — where strategy exposure is projected and `net_long_pct` is computed.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `_build_option_legs` projects `ValidationStrategyLeg` tuples (each carrying per-leg direction) into the library's option-leg shape; trace whether per-leg directions are honored downstream.
* `src/alphamind/portfolio_state/computations/positions.py` — `compute_strategy_delta_adjusted_exposure_usd` does `strategy_greeks.delta * summed_notional`; this is the net-signed `strategy_greeks` contract.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails`, `StrategyLeg.direction`, `OptionGreeks`.
* [ALP-588](https://linear.app/alphamind-jatassi/issue/ALP-588/support-short-net-credit-multi-leg-options-strategies-end-to-end) (parent) § Pre-resolved decision (C); § Notes for the orchestrator (no strategy consumer branches on position-level direction); § Surfacing conditions (the `net_long_pct` false-alarm clause).

## Depends on

Nothing. Wave 1. This story only *consumes* the net-signed `strategy_greeks` contract — its tests build fixtures directly — so it has no hard dependency on story 01b, which produces the aggregation primitive.

## Scope

In scope: `src/alphamind/risk_guardrails/library_snapshot.py` and the `guardrail_evaluation` exposure-projection module(s). Tests under `tests/risk_guardrails/`.

### 1\. Portfolio greek accumulation for strategies

In `_accumulate_portfolio_greeks`, the strategy branch computes `scale = (sum over legs of contract_count * contract_multiplier) / portfolio_value_usd * 100` and returns `strategy_greeks.delta * scale`, `strategy_greeks.theta * scale`, `strategy_greeks.vega * scale`. Two changes from the current code: no `direction`-derived sign (the sign already lives in the net-signed `strategy_greeks` — parent decision C), and the leg-summed multiplier units replace the current first-leg `legs[0].options.contract_count` scaling. The invariant the tests assert: a net-short-delta strategy contributes a negative delta.

### 2\. Strategy directional exposure in the validation library

Audit `guardrail_evaluation`'s strategy exposure projection (`delta_adjusted.py` / `rules/exposure.py`). The PM-envelope path hands the library a strategy with position-level `direction=LONG` plus per-leg `ValidationStrategyLeg` directions. A strategy's directional / `net_long_pct` contribution must come from the per-leg signed deltas, not the synthetic top-level `LONG`. If the audit finds the library already honors per-leg directions, the `net_long_pct` overstatement is a false alarm — record an inspection note and surface it per the parent's Surfacing conditions; otherwise fix the projection so a net-short strategy does not inflate `net_long_pct`.

### Out of scope

`process.py`'s hard-coded `Direction.LONG` on the strategy `ValidationInstrument` — that placeholder stays (the documented convention; per-leg directions ride on the leg tuples). Populating `strategy_greeks` net-signed on live positions — story 02 / story 04. The portfolio-greek refresh cadence — unchanged.

## Acceptance criteria

- [ ] A net-short-delta strategy contributes a negative delta to the portfolio greek view produced by `library_snapshot.py`.
- [ ] `_accumulate_portfolio_greeks` does not apply a sign derived from a strategy's position-level `direction` — the sign comes from `strategy_greeks`.
- [ ] `_accumulate_portfolio_greeks` no longer scales a strategy's contribution by a single leg's `contract_count`; it uses the leg-summed multiplier units.
- [ ] The guardrail validation `net_long_pct` for a net-short / net-credit strategy reflects its true signed directional exposure (a net-short strategy does not inflate `net_long_pct`) — or an inspection note records that the library already honors per-leg directions and that finding is surfaced.
- [ ] No `risk_guardrails` code path branches on the position-level `direction` of a `StrategyPositionDetails` position.
- [ ] Tests under `tests/risk_guardrails/` cover a net-short-delta strategy's portfolio-greek and exposure contribution and pass under `uv run pytest tests/risk_guardrails/ --testmon -n auto`.

## Verification

`uv run pytest tests/risk_guardrails/ --testmon -n auto`; `uv run ruff check .` and `uv run mypy` clean. If the validation-library audit concludes the projection is already correct, the orchestrator surfaces that inspection note to the operator.
