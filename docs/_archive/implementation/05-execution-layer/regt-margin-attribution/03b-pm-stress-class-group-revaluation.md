# 03b — PM stress class-group revaluation

## Goal

Ship the pure function `stress_class_group(class_group, market_inputs, config)` that returns the worst-case P/L magnitude (absolute value of the most-negative P/L) across a 10-point equidistant shock grid applied to one class group. At each shock point: equity revalues at the shocked underlying price; options revalue via `bs_price` with the shocked underlying and a linearly-interpolated IV multiplier paired to the shock direction. This is the per-class-group margin in the IBKR-mirror v1 reference model; story 04 sums the per-class-group results into the portfolio aggregate.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Portfolio-margin reference model — full algorithm: 10-point grid, equity + option revaluation per shock, IV adjustment paired to direction, class-group margin = abs(worst P/L).
* `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` — `bs_price` (added by story 01b) is the options revaluation primitive.
* `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py` and `types.py` — `IvProvider.lookup_iv(...)` returns an `IvLookupResult` carrying `implied_volatility`. The shock applies a multiplier to this baseline.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` § `MarketInputs` — the typed contract this function consumes (`underlying_prices`, `risk_free_rate`, `iv_provider`, `as_of`).
* `src/alphamind/execution/regt_margin_attribution/class_groups.py` (from story 02) — `ClassGroup` typed record this function consumes.
* `src/alphamind/execution/regt_margin_attribution/config.py` (from story 01a) — `RegTMarginAttributionConfig` provides the per-symbol shock percentage and the IV-shock multipliers.
* `src/alphamind/portfolio_state/records/positions.py` — `OptionsPositionDetails` and `StrategyLeg` shapes (strike, expiration, contract_type, contracts/quantity).
* `ALP-126` parent issue § Pre-resolved decisions (B), (C), (G) — IV-shock yaml-loaded coefficients with linear interpolation; per-symbol shock parameter lookup; 10-point grid as named constant.

## Depends on

* `ALP-421` (01a — Package skeleton + IBKR-mirror v1 config) — `RegTMarginAttributionConfig` defines the per-symbol shock-percentage lookup and IV-shock multipliers.
* `ALP-422` (01b — Black-Scholes price extension) — `bs_price` is the options revaluation primitive.
* `ALP-423` (02 — Class-group composition) — `ClassGroup` is the input type.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/pm_stress.py` and `tests/execution/regt_margin_attribution/test_pm_stress.py`.

### 1\. Module constants

```python
_SHOCK_GRID_POINTS = 10  # per design § Portfolio-margin reference model
```

The grid spans `[-shock_pct, +shock_pct]` with `_SHOCK_GRID_POINTS` equidistant points. Both endpoints inclusive. At `_SHOCK_GRID_POINTS = 10`, the grid is `[-1.0, -7/9, -5/9, -3/9, -1/9, +1/9, +3/9, +5/9, +7/9, +1.0]` of `shock_pct`. (Document the exact endpoint convention in a module-level docstring with a worked example so a reader can reproduce the grid by hand.)

### 2\. Shock-percentage lookup

A small helper `_shock_pct_for_underlying(symbol, config) -> float` that:

1. Looks up `config.shock_parameters.per_symbol_overrides[symbol.upper()]`; returns it if present.
2. Otherwise returns `config.shock_parameters.unmapped_default`. (Asset-class taxonomy is not v1 — `high_cap_equity` and `small_cap_equity` are exposed as separate fields on `ShockParameters` for use by future revisions, but v1 falls through directly to `unmapped_default` when there is no per-symbol entry. Document this as a positive specification in the module docstring.)

### 3\. IV-multiplier interpolation

A small helper `_iv_multiplier_at_grid_point(grid_position, iv_shock_config) -> float` that:

* Maps the grid position (range `[-1.0, +1.0]`) to a multiplier in `[worst_up_multiplier, worst_down_multiplier]` via linear interpolation.
* At grid position `-1.0` (worst-down shock) returns `worst_down_multiplier` (typically ≥ 1.0; IV rises).
* At grid position `+1.0` (worst-up shock) returns `worst_up_multiplier` (typically ≤ 1.0; IV falls).
* At grid position `0.0` returns the midpoint `(worst_down + worst_up) / 2`.

### 4\. `stress_class_group` function

```python
def stress_class_group(
    class_group: ClassGroup,
    market_inputs: MarketInputs,
    config: RegTMarginAttributionConfig,
) -> float:
    """Compute the IBKR-mirror v1 per-class-group margin.

    Pure function. Returns the absolute value of the worst-case P/L
    magnitude across a 10-point equidistant shock grid applied to all
    instruments in ``class_group``. Per
    ``regt-margin-attribution.md § Portfolio-margin reference model``.

    Steps:
      1. Look up the per-symbol shock percentage from
         ``config.shock_parameters``.
      2. Construct the 10-point grid spanning ``[-shock_pct, +shock_pct]``
         of the current underlying price.
      3. At each grid point, revalue every instrument in the class group:
         - Equity: ``shocked_underlying × signed_quantity``.
         - Options (positions or strategy legs): ``bs_price(...)`` with
           ``spot = shocked_underlying``, ``implied_volatility = baseline_iv ×
           iv_multiplier_at_this_grid_point``, all other inputs from
           ``market_inputs``. Net P/L per leg = ``(shocked_price −
           baseline_price) × signed_contracts × 100``.
      4. Sum the P/Ls across instruments to get total P/L at this grid
         point.
      5. Return ``abs(min(total_pl_at_each_grid_point))``.

    Baseline revaluation (at grid position 0.0, the unstressed midpoint)
    reuses ``bs_price`` with ``spot = current_underlying`` and
    ``iv = baseline_iv``; net P/L at that point is 0.0 by construction.

    Raises ``KeyError`` if the class group's underlying symbol is missing
    from ``market_inputs.underlying_prices``.
    Raises ``IvLookupError`` (propagated from ``market_inputs.iv_provider``)
    if no IV is available for an option leg's strike/expiration.
    """
```

The function calls `bs_price` and `IvProvider.lookup_iv` from `market_inputs.iv_provider`. Strategy positions iterate their legs (each a `StrategyLeg`) just like loose option positions.

For the IV baseline, the function calls `market_inputs.iv_provider.lookup_iv(...)` exactly once per option leg (or strategy leg) at the unshocked strike/expiration; the multiplier is then applied per grid point. Do not re-call `lookup_iv` for each grid point — that's an upstream-cost optimisation.

### 5\. Tests

`tests/execution/regt_margin_attribution/test_pm_stress.py`:

* `test_equity_only_class_group_worst_loss_is_shock_pct_times_market_value` — single long-equity position, shock 15%; assert result equals `0.15 × |quantity| × current_price`.
* `test_short_equity_only_class_group_worst_loss_on_up_shock` — single short-equity position, shock 15%; assert worst loss is on the +shock direction and equals `0.15 × |quantity| × current_price`.
* `test_long_call_only_class_group_worst_loss_on_down_shock` — single long call (positive delta), shock 20%; assert worst loss is on the −shock direction and the magnitude is strictly less than the long-call premium times multiplier (theoretical max loss for a long call is the premium; stress may not hit that). Use `bs_price` directly to compute the expected baseline and shocked prices.
* `test_long_put_only_class_group_worst_loss_on_up_shock` — single long put (negative delta), shock 20%; assert worst loss is on the +shock direction.
* `test_short_call_class_group_worst_loss_on_up_shock` — single short call; shocks above strike are worst.
* `test_hedged_equity_plus_long_call_class_group_smaller_than_unhedged_call` — long equity + long call on the same underlying. The class group's worst loss should be less than either standalone (the equity gains on up-shocks offset some of the put's loss on up-shocks, and vice versa for the call). Compares against an explicit standalone-call run.
* `test_iv_shock_paired_to_price_shock_direction` — long call; assert that the IV used at the down-shock grid endpoint is `baseline_iv × config.iv_shock.worst_down_multiplier`, and at the up-shock endpoint is `baseline_iv × config.iv_shock.worst_up_multiplier`. Use a stubbed `IvProvider` that records the multiplier applied per call.
* `test_grid_has_ten_points` — instrument the helper or use an inspect-friendly seam to assert the function evaluates exactly 10 shock points.
* `test_unknown_underlying_raises_key_error` — class group whose `underlying_symbol` is not in `market_inputs.underlying_prices` raises `KeyError`.
* `test_strategy_position_sums_per_leg_pl_at_each_grid_point` — NVDA bull call spread (one long call, one short call at a higher strike). Expected: at the +shock endpoint, the strategy's max gain is realised (defined-risk strategy); the worst loss is at the −shock endpoint. Verify the per-grid-point net P/L matches a hand-computed reference for at least one grid point.

Use a `FixtureIvProvider` (already shipped by guardrail-evaluation) with seeded `(strike, expiration) → IV` entries to make tests deterministic.

### Out of scope

* Inter-class offsets — v1 sums per-class-group margins without correlation netting (design § Aggregation).
* Per-strike volatility skew / smile dynamics within one option leg — the IV multiplier is applied uniformly to the baseline IV the provider returns; per-strike skew remains in the provider's domain.
* Maintenance margin equivalent — design specifies initial only.

## Acceptance criteria

- [ ] `stress_class_group` is defined in `src/alphamind/execution/regt_margin_attribution/pm_stress.py` with the keyword-only signature shape named in Scope §4.
- [ ] `_SHOCK_GRID_POINTS` is a module-level named constant equal to 10.
- [ ] The function evaluates exactly 10 shock points spanning `[-shock_pct, +shock_pct]` inclusive.
- [ ] Equity positions revalue at `shocked_underlying × signed_quantity` per grid point.
- [ ] Option positions and strategy legs revalue via `bs_price` with the shocked underlying and the linearly-interpolated IV multiplier.
- [ ] At the worst-down grid endpoint, IV is `baseline_iv × config.iv_shock.worst_down_multiplier`.
- [ ] At the worst-up grid endpoint, IV is `baseline_iv × config.iv_shock.worst_up_multiplier`.
- [ ] The function returns `abs(min(total_pl_at_each_grid_point))` — the worst-case loss magnitude.
- [ ] The function looks up the per-symbol shock percentage via `config.shock_parameters.per_symbol_overrides`, falling through to `unmapped_default` when absent.
- [ ] `IvProvider.lookup_iv` is called once per option leg (not 10× per leg).
- [ ] Missing underlying price raises `KeyError`; missing IV propagates `IvLookupError`.
- [ ] `stress_class_group` is a pure function (no I/O beyond the provider lookups, no clock reads, no global mutation).
- [ ] `stress_class_group` is exposed via `src/alphamind/execution/regt_margin_attribution/__init__.py`'s `__all__`.
- [ ] `tests/execution/regt_margin_attribution/test_pm_stress.py` covers the ten tests named in Scope §5 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_pm_stress.py -n auto`. Spot-check `pm_stress.py` to confirm: (a) the grid is exactly 10 equidistant points; (b) `bs_price` is called per option-leg per grid point (10 calls per leg); (c) `lookup_iv` is called once per leg; (d) the worst-case selection takes `min` (most negative) and returns its absolute value.