## Symptom

The continuous monitor's cascade dispatcher consumes per-position liquidity (ADV ratio) and risk/reward signals via two operator-visible placeholder values in `config/continuous_monitor.yaml`:

* `cascade_dispatch_placeholder_adv_to_position_size_ratio: 10.0`
* `cascade_dispatch_placeholder_risk_reward_ratio: 2.0`

Every open position receives the same placeholder ratio regardless of its actual liquidity profile or distance-to-target / distance-to-stop. Cascade arithmetic (forced-reduction sizing, margin-call selection by worst R/R) therefore makes uniform decisions in production runs.

## Root cause

The monitor's market-data path does not yet expose:

* **Per-symbol ADV.** No 20-day rolling volume aggregation lives on the `UnderlyingPriceCache` or any companion cache.
* **Per-position risk/reward.** The `PositionView.risk_reward_at_current` field is populated by the snapshot assembler, but the assembler reads `distance_to_target_usd` / `distance_to_stop_usd` from inputs the monitor does not currently thread through.

The substrate's `make_dispatch_context_provider` (`src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py`) accepts both ratios as required keyword parameters and applies them uniformly to every open position in the per-tick `BreachDispatchContext`.

## Scope

**(1) ADV path.** Extend the data-layer collector / monitor's underlying-stream cache to surface per-symbol average daily volume (20-day window). Thread that map through the substrate so the `PositionLiquidity` tuple reflects per-position ADV-to-notional ratios.

**(2) R/R path.** Either (a) read `PositionView.risk_reward_at_current` from the same `make_open_positions_view_provider` output the dispatcher already consumes, or (b) extend `make_open_positions_view_provider` to return a parallel R/R map. Replace the uniform `placeholder_risk_reward_ratio` with the per-position values.

**(3) Config cleanup.** Once both signals flow live, retire both `cascade_dispatch_placeholder_*` knobs from `config/continuous_monitor.yaml` and from `ContinuousMonitorConfig`. Tighten `make_dispatch_context_provider`'s signature to drop the placeholder parameters.

## Acceptance criteria

* `BreachDispatchContext.liquidity` and `BreachDispatchContext.risk_reward_metric` carry per-position values driven by live data, not config-supplied uniform defaults.
* The two `cascade_dispatch_placeholder_*` knobs are removed from `config/continuous_monitor.yaml` and from the Pydantic model.
* Cascade-orchestrator tests cover the per-position-ADV and per-position-R/R selector branches against synthetic live signals.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Surfaced by: [ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider) (PR landing the operator-visible placeholders).
* Reading: `src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py` — `make_dispatch_context_provider` consumes the placeholders.
* Reading: `src/alphamind/risk_guardrails/breach_behavior/position_selection.py` — `PositionLiquidity` / `PositionRiskReward` documentation describes the live shapes.
* Reading: `src/alphamind/portfolio_state/views/positions.py` — `risk_reward_at_current` field the snapshot assembler computes.