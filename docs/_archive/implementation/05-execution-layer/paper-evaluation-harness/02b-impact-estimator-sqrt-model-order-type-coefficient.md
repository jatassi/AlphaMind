# 02b — Impact estimator (sqrt-model + order-type coefficient)

## Goal

Produces a pure `estimate_impact(...)` function implementing the square-root market-impact model the design doc names: `impact = coefficient * estimated_spread * sqrt(fill_shares / adv_shares)`. The caller (story 03) supplies the coefficient looked up from `PaperHarness.impact_coefficients[order_type]`; this function does not load config. Returns a Money value representing the per-fill dollar impact.

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` § Spread + impact — the formula and the order-type coefficient defaults (market 0.5, limit 0.25, stop 0.75)
* `src/alphamind/config/models/execution.py` — existing `PaperHarness.impact_coefficients` shape (`dict[OrderType, float]` with model-validator requiring exact coverage)
* `src/alphamind/config/models/execution.py` § `OrderType` — the `market | limit | stop` enum
* `src/alphamind/_kernel/money.py` — `Money` boundary
* `src/alphamind/execution/paper_evaluation_harness/__init__.py` — extends `__all__`

## Depends on

* `ALP-524` (story 01) — package skeleton.

## Scope

In scope, under `src/alphamind/execution/paper_evaluation_harness/impact.py`. Tests at `tests/execution/paper_evaluation_harness/test_impact.py`.

### 1\. `estimate_impact` function

New module with:

```python
def estimate_impact(
    *,
    fill_shares: float,
    adv_shares: float,
    estimated_spread: Money,
    order_type: OrderType,
    coefficient: float,
) -> Money: ...
```

Formula: `impact = coefficient * estimated_spread * sqrt(fill_shares / adv_shares)`. Implementation handles the Decimal arithmetic for `Money` cleanly — multiply `estimated_spread` (Money / Decimal) by the float scalar `coefficient * sqrt(fill_shares / adv_shares)` via the `_kernel/money.py` helpers, with conversion at the boundary.

The function takes `order_type` as a parameter for two reasons:

* Defensive validation: if the supplied `coefficient` is incompatible with the `order_type` enum (e.g., caller mismatch), the function can log/raise. Recommended: accept the coefficient as supplied (purity) but include `order_type` in the docstring to make the call site self-documenting.
* Future-proofing if coefficient logic ever needs more than a scalar.

Caller responsibility (will be story 03): look up `coefficient = config.impact_coefficients[order_type]` and pass both through.

The function is pure: no DB / HTTP / module-level config imports.

### 2\. Edge cases

* `adv_shares <= 0` → clamp to 1.0 before the division to avoid divide-by-zero / domain error in `sqrt`. Document the clamp.
* `fill_shares <= 0` → return `money("0")` (no shares, no impact). Document.
* `estimated_spread == money("0")` → returns `money("0")` (impact is proportional to spread).
* `coefficient <= 0` → caller bug. Raise `ValueError` to fail loud — `PaperHarness.coefficients_strictly_positive` validates non-positive at config load, so a non-positive value here means programmer error in the caller.

### Out of scope

* Spread estimator (story 02a)
* Composition (story 03)
* Config loading (story 03 caller)
* Options-side adaptation (story 03 handles the substitution of options-contract data into `fill_shares`/`adv_shares`)

## Acceptance criteria

- [ ] `estimate_impact(fill_shares=10_000, adv_shares=1_000_000, estimated_spread=money("0.05"), order_type=OrderType.market, coefficient=0.5)` returns a strictly positive `Money` value.
- [ ] Doubling `fill_shares` (10_000 → 20_000) scales the impact by `sqrt(2)` (within Decimal-rounding tolerance).
- [ ] Halving `adv_shares` (1_000_000 → 500_000) scales the impact by `sqrt(2)`.
- [ ] With identical inputs except `order_type`/`coefficient`, the limit-order coefficient `0.25` produces an impact strictly less than the market-order coefficient `0.5`, which is strictly less than the stop-order coefficient `0.75`.
- [ ] `adv_shares=0` does not raise; clamps to 1.0 (effectively returns a large impact for the edge case).
- [ ] `fill_shares=0` returns `money("0")`.
- [ ] `estimated_spread=money("0")` returns `money("0")`.
- [ ] `coefficient=0` raises `ValueError`.
- [ ] `coefficient=-1.0` raises `ValueError`.
- [ ] All returned values are non-negative `Money`.
- [ ] No DB / HTTP / MainConfig imports — verified by grep.
- [ ] `paper_evaluation_harness/__init__.py` re-exports `estimate_impact`; `__all__` extends.
- [ ] `tests/execution/paper_evaluation_harness/test_impact.py` exists and passes under `uv run pytest tests/execution/paper_evaluation_harness/test_impact.py --testmon -n auto`.
- [ ] `uv run pytest --testmon -n auto` passes.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Scoped test file + lint chain. The sqrt-law monotonicity checks anchor correctness without pinning a specific coefficient value.