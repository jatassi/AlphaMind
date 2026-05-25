## Symptom

The continuous monitor's breach-loop cascade dispatcher ships with three placeholder values in its per-tick `BreachDispatchContext` provider. Per the substrate's own comments, **"the dispatcher will not fire without that map populated"** — so the dispatcher is currently a no-op in production. Even if it did fire, the cascade arithmetic uses constants for liquidity and risk/reward, and option DAE re-projection would hit an empty IV provider.

## Root cause

`src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:541-661` — the `make_dispatch_context_provider` factory:

```python
# Constant defaults instead of live per-position data
liquidity = tuple(
    PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=10.0)
    for p in open_positions
)
risk_reward = tuple(
    PositionRiskReward(position_id=p.position_id, risk_reward_ratio=2.0)
    for p in open_positions
)

# Empty IV provider for the cascade context (the breach-loop task itself
# uses a real one separately)
_iv_provider = _EMPTY_IV_PROVIDER  # line 618; defined at line 661
_EMPTY_IV_PROVIDER = FixtureIvProvider(surface={}, realized_vol={})
```

And `src/alphamind/execution/continuous_monitor/__main__.py:373-379` calls the factory **without** passing `open_positions_reader`:

```python
dispatch_context_provider = make_dispatch_context_provider(
    snapshot_provider=snapshot_provider,
    regime_provider=regime_provider,
    library_config_factory=library_config_factory,
    underlying_cache=underlying_cache_typed,
    progressive_tiers=progressive_tiers,
    # open_positions_reader= not passed — defaults to None → open_positions=()
)
```

despite `open_positions_reader = SqlOpenPositionsReader(db_session_factory)` already being constructed at `__main__.py:189` and threaded into other registrations (lines 199, 207, 235).

The substrate's own docstring acknowledges all three: "Liquidity defaults to a constant ADV ratio (`10.0`) and risk/reward defaults to `2.0` per position — the live ADV / R/R signals do not yet flow into the monitor process. Replacing these with live signals is a follow-up once the monitor's market-data path exposes them." And: "The daemon's wiring currently leaves this empty because the per-rule kwargs providers are out of scope for [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers) — the dispatcher will not fire without that map populated."

## Why it surfaced now

Discovered in the post-[ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae) audit for similar incomplete-implementation patterns. The cascade dispatcher is not exercised in `verify_debug_e2e.py` (which runs the decision pipeline, not the continuous monitor). Production runs of the monitor either silently no-op the dispatcher (if breaches don't trip the dispatcher path) or — when invoked — produce decisions based on constants.

## Scope — three wiring tasks, optionally bundled

**(1) Pass** `open_positions_reader`**.** From `__main__.py:373` into `make_dispatch_context_provider`. Confirm with the cascade spec author whether per-rule kwargs providers (also documented as deferred) need to be wired in the same PR for the dispatcher to actually fire.

**(2) Replace** `_EMPTY_IV_PROVIDER` with the real IV provider used by the breach-loop task itself. May require threading the IV provider through `make_dispatch_context_provider`'s signature.

**(3) Wire live ADV / R/R signals** — or, if not yet available from the monitor's market-data path, make the constant defaults explicit via config (operator-visible) with a tracked follow-up issue, rather than silent inline constants.

## Acceptance criteria

* Cascade dispatcher's `BreachDispatchContext.open_positions` is non-empty when positions exist in the snapshot.
* IV lookups in the cascade re-projection use the same IV provider the breach-loop task uses.
* Either live ADV / R/R signals are wired, OR the constant defaults are made explicit via config + tracked follow-up.
* New integration test in `tests/execution/continuous_monitor/` constructs a breach-loop with a synthetic snapshot containing positions, triggers a breach, and asserts the dispatcher receives populated context.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Likely related to [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers) (per substrate's docstring: "out of scope for [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers)").
* Sibling incomplete-implementation pattern to [ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae).
* Blocked by: [ALP-505](https://linear.app/alphamind-jatassi/issue/ALP-505/scheduler-builds-libraryconfig-with-empty-escalation-zones-and-zero) — the cascade context's `library_config` runs through `make_library_config_factory` which uses `from_resolved_config` correctly today, but a fully-exercised cascade test needs the decision-pipeline path to work end-to-end too.

## Reading

* `src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:541-661` — `make_dispatch_context_provider` factory and `_EMPTY_IV_PROVIDER`.
* `src/alphamind/execution/continuous_monitor/__main__.py:189,373-379` — `open_positions_reader` constructed but not passed.
* `src/alphamind/risk_guardrails/breach_behavior/secondary_breach.py` — cascade arithmetic that consumes the placeholder ADV / R/R values.