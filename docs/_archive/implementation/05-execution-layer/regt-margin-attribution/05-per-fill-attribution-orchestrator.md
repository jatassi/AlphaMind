# 05 — Per-fill attribution orchestrator

## Goal

Ship the pure function `compute_attribution(pre_fill_positions, post_fill_positions, market_inputs, config)` that returns a fully-populated `RegTMarginAttribution` record. It calls `compute_regt_margin` and `compute_pm_equivalent_margin` twice each (once for pre-fill, once for post-fill positions), computes the four marginal deltas and the headline `regt_excess_over_pm`, and packages everything with `pm_model_version` into the canonical 8-field record. Story 06a's Phase 1 wedge calls this exactly once per integrated fill.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Outputs — the canonical 8-field structure: `regt_margin_before/after/marginal_consumption`, `pm_equivalent_before/after/marginal_consumption`, `regt_excess_over_pm`, `pm_model_version`.
* `docs/design/05-execution-layer/regt-margin-attribution.md` § Computation flow — confirms the `*_before` → integrate → `*_after` → marginal deltas sequence; clarifies that batched fills are processed in `fill_timestamp` order so each fill's `*_before` reflects all prior fills' post-fill state.
* `src/alphamind/execution/state_persistence/write_paths/records.py:52` — the `RegTMarginAttribution` Pydantic model (frozen, all 8 fields). This story constructs instances of it.
* `src/alphamind/execution/regt_margin_attribution/regt_margin.py` (from story 03a) — `compute_regt_margin` is called twice.
* `src/alphamind/execution/regt_margin_attribution/pm_equivalent.py` (from story 04) — `compute_pm_equivalent_margin` is called twice.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` § `MarketInputs` — input contract.
* `ALP-126` parent issue § Pre-resolved decision (F) — `MarketInputs` is shared across the batch (one read at Phase 1 start), so this orchestrator does not re-read market data per fill.

## Depends on

* `ALP-424` (03a — Reg T per-leg margin formula) — `compute_regt_margin` is called for the Reg T snapshots.
* `ALP-426` (04 — IBKR-mirror v1 PM-equivalent aggregator) — `compute_pm_equivalent_margin` is called for the PM-equivalent snapshots.

  Transitively this story relies on [ALP-421](https://linear.app/alphamind-jatassi/issue/ALP-421/01a-package-skeleton-ibkr-mirror-v1-config) / 422 / 423 / 425 via 04's dependency chain.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/orchestrator.py` and `tests/execution/regt_margin_attribution/test_orchestrator.py`.

### 1\. `compute_attribution` function

Keyword-only signature: `pre_fill_positions: tuple[PositionRecord, ...]`, `post_fill_positions: tuple[PositionRecord, ...]`, `market_inputs: MarketInputs`, `config: RegTMarginAttributionConfig`. Returns `RegTMarginAttribution`.

Steps (straight-line, no branching):

1. `regt_margin_before = compute_regt_margin(pre_fill_positions, market_inputs.underlying_prices)`
2. `pm_equivalent_before = compute_pm_equivalent_margin(pre_fill_positions, market_inputs, config)`
3. `regt_margin_after = compute_regt_margin(post_fill_positions, market_inputs.underlying_prices)`
4. `pm_equivalent_after = compute_pm_equivalent_margin(post_fill_positions, market_inputs, config)`
5. `regt_marginal_consumption = regt_margin_after - regt_margin_before`
6. `pm_marginal_consumption = pm_equivalent_after - pm_equivalent_before`
7. `regt_excess_over_pm = regt_marginal_consumption - pm_marginal_consumption`
8. `pm_model_version = config.pm_model_version`

No special-casing for zero-delta fills (the algebra produces zeros correctly); no caching; no internal state.

Propagates `KeyError` from `compute_regt_margin` if a position's underlying is missing from `market_inputs.underlying_prices`; propagates `IvLookupError` from `compute_pm_equivalent_margin` if an option leg's IV is unavailable.

### 2\. Tests

`tests/execution/regt_margin_attribution/test_orchestrator.py`:

* `test_no_change_fill_produces_zero_marginal_consumption` — pre and post position sets identical (e.g., a fully-canceled order with no fill) → all four marginal fields equal `0.0` and `regt_excess_over_pm == 0.0`.
* `test_empty_pre_and_post_positions_produces_zero_record` — both inputs empty → all dollar fields `0.0`, `pm_model_version` equals `config.pm_model_version`.
* `test_buy_long_equity_opens_position_increases_both_requirements` — pre empty; post: long 100 NVDA at $500 → `regt_margin_after > 0`, `pm_equivalent_after > 0`, both marginal consumptions positive.
* `test_close_long_equity_releases_both_requirements` — pre: long 100 NVDA; post empty → both marginal consumptions negative; `regt_excess_over_pm` is the difference between the two.
* `test_regt_excess_over_pm_equals_regt_minus_pm_marginal` — algebraic invariant on a non-trivial fill.
* `test_pm_model_version_propagates_from_config` — `result.pm_model_version == config.pm_model_version`.
* `test_record_is_frozen` — field assignment on the returned record raises (Pydantic frozen contract).
* `test_finite_fields_when_inputs_are_finite` — all 7 dollar fields are finite (not nan/inf) when inputs are finite.
* `test_unknown_underlying_propagates_key_error` — `market_inputs.underlying_prices` missing a symbol from one of the position sets propagates `KeyError`.
* `test_batched_fills_pre_state_threading_documented` — synthetic two-fill batch: at fill 1, `pre_fill_positions` is the empty book; at fill 2, `pre_fill_positions` equals the post-fill state of fill 1. Assert the two records produced are coherent (sum of marginal consumptions equals the post-batch minus pre-batch values). Documents the caller's threading contract.

### Out of scope

* Phase 1 wiring — story 06a.
* Reading positions from the database / computing positions from fills — caller's responsibility.
* Caching across calls — orchestrator is stateless.
* Persisting the result — story 06a uses the existing fill-records codec.

## Acceptance criteria

- [ ] `compute_attribution` is defined in `src/alphamind/execution/regt_margin_attribution/orchestrator.py` with the keyword-only signature shape named in Scope §1.
- [ ] The function returns a `RegTMarginAttribution` (imported from `src/alphamind/execution/state_persistence/write_paths/records.py`).
- [ ] All 8 fields are populated per the algebraic steps in Scope §1.
- [ ] `regt_excess_over_pm == regt_marginal_consumption − pm_marginal_consumption` as an invariant on every returned record.
- [ ] `pm_model_version` is populated from `config.pm_model_version`.
- [ ] No-change fills (identical pre and post) produce a record with all dollar fields equal to `0.0`.
- [ ] `compute_attribution` is a pure function (no I/O beyond the inputs' provider lookups, no clock reads, no global mutation).
- [ ] `compute_attribution` is exposed via `src/alphamind/execution/regt_margin_attribution/__init__.py`'s `__all__`.
- [ ] `tests/execution/regt_margin_attribution/test_orchestrator.py` covers the ten tests named in Scope §2 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_orchestrator.py -n auto`. Spot-check `orchestrator.py` to confirm: (a) the body is the eight straight-line steps in Scope §1 with no branching; (b) `RegTMarginAttribution` is imported from the canonical location (not re-defined locally); (c) the function returns the frozen Pydantic record directly.