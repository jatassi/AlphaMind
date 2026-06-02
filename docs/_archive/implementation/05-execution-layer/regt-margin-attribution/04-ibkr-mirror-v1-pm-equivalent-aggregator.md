# 04 — IBKR-mirror v1 PM-equivalent aggregator

## Goal

Ship the thin pure function `compute_pm_equivalent_margin(positions, market_inputs, config)` that composes class-group composition + per-class-group stress into the portfolio-aggregate IBKR-mirror v1 PM-equivalent margin. It calls `compose_class_groups(positions)` from story 02 and `stress_class_group(...)` from story 03b for each group, then sums the per-class-group results. The orchestrator in story 05 calls this for both `pm_equivalent_before` and `pm_equivalent_after`.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Portfolio-margin reference model § Aggregation — "Sum the per-class-group margins. Inter-class offsets ... are v2 scope."
* `src/alphamind/execution/regt_margin_attribution/class_groups.py` (from story 02) — `compose_class_groups` is the partition primitive.
* `src/alphamind/execution/regt_margin_attribution/pm_stress.py` (from story 03b) — `stress_class_group` is the per-group margin primitive.
* `src/alphamind/execution/regt_margin_attribution/config.py` (from story 01a) — `RegTMarginAttributionConfig` passed through unchanged.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` § `MarketInputs` — passed through unchanged.
* `ALP-126` parent issue § Pre-resolved decisions — confirms no inter-class offsets in v1; v1 understates PM (overstates regt_excess in the conservative direction).

## Depends on

* `ALP-425` (03b — PM stress class-group revaluation) — `stress_class_group` is the per-group primitive this function aggregates.

  Transitively this story also relies on `ALP-421` (config), `ALP-422` (bs_price), `ALP-423` (class_groups). Those are direct dependencies of 03b; the orchestrator sequences them automatically.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/pm_equivalent.py` and `tests/execution/regt_margin_attribution/test_pm_equivalent.py`.

### 1\. `compute_pm_equivalent_margin` function

```python
def compute_pm_equivalent_margin(
    positions: tuple[PositionRecord, ...],
    market_inputs: MarketInputs,
    config: RegTMarginAttributionConfig,
) -> float:
    """Sum IBKR-mirror v1 per-class-group margins into the portfolio aggregate.

    Pure function. Composition of ``compose_class_groups`` and
    ``stress_class_group``. Returns the portfolio-aggregate PM-equivalent
    initial-margin requirement in USD.

    Per ``regt-margin-attribution.md § Aggregation``: no inter-class
    offsets in v1. The sum biases the result upward relative to IBKR's
    actual product-group methodology output (and therefore biases
    ``regt_excess_over_pm`` downward — the conservative direction).

    Pending / closed positions are excluded via the open-status filter
    applied before class-group composition.
    """
```

The function is intentionally thin: filter positions to `PositionStatus.OPEN`, call `compose_class_groups(open_positions)`, iterate the resulting tuple, sum the `stress_class_group(...)` results. Return `0.0` when the open-position set is empty.

### 2\. Tests

`tests/execution/regt_margin_attribution/test_pm_equivalent.py`:

* `test_empty_positions_returns_zero` — `compute_pm_equivalent_margin((), market_inputs, config)` returns `0.0`.
* `test_single_class_group_equals_stress_class_group_result` — one equity-only position; assert result equals `stress_class_group(...)` for that single class group.
* `test_two_class_groups_sum_correctly` — long AAPL + long NVDA stock (two separate class groups); assert result equals the sum of the two `stress_class_group` results.
* `test_hedged_book_no_correlation_offset_applied` — long AAPL + short NVDA stock (different underlyings; both class groups stress to their own worst-case); assert result equals the unhedged sum (no offset applied; v1 has no inter-class correlation netting).
* `test_pending_positions_excluded` — one open NVDA + one pending AAPL; assert the result equals only the NVDA class group's stress (pending excluded).

Use the same `FixtureIvProvider` pattern as story 03b's tests.

### Out of scope

* Inter-class offsets — v2 scope.
* The shock parameter table, IV multipliers, or per-leg formulas — all owned upstream.
* Any caching across `*_before`/`*_after` calls — the orchestrator (story 05) calls this function twice per fill and relies on the function being cheap rather than cached.

## Acceptance criteria

- [ ] `compute_pm_equivalent_margin` is defined in `src/alphamind/execution/regt_margin_attribution/pm_equivalent.py` with the keyword-only signature named in Scope §1.
- [ ] The function filters positions to `PositionStatus.OPEN` before class-group composition.
- [ ] The function returns `0.0` when no open positions exist.
- [ ] The function returns the sum of `stress_class_group(...)` across all class groups composed from the open positions.
- [ ] No inter-class offset is applied (the sum is straight summation).
- [ ] `compute_pm_equivalent_margin` is a pure function.
- [ ] `compute_pm_equivalent_margin` is exposed via `src/alphamind/execution/regt_margin_attribution/__init__.py`'s `__all__`.
- [ ] `tests/execution/regt_margin_attribution/test_pm_equivalent.py` covers the five tests named in Scope §2 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_pm_equivalent.py -n auto`. Confirm the implementation is a thin composer — body should be five-to-fifteen lines, with no algorithmic logic beyond filtering and summation.