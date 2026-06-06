# 02a — Spread estimator (price/ADV/volatility fallback)

## Goal

Produces a pure `estimate_spread(...)` function that estimates the bid-ask spread an equity (or options) order would have crossed at fill time, using a price/ADV/volatility model. The estimated spread feeds the impact estimator (story 02b) and the composition (story 03). Output is a Money value representing the per-share (or per-contract for options) dollar spread, scaled by the configured `spread_buffer_pct` from `PaperHarness`. This story does NOT load config or perform I/O — the caller supplies the buffer scalar and the price/ADV/volatility inputs.

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` § Spread + impact — the formula structure and the +10% buffer framing as "calibrator, not safety factor"
* `src/alphamind/config/models/execution.py` — existing `PaperHarness.spread_buffer_pct` shape (`float, ge=0`)
* `src/alphamind/distillation/_repository.py` § `TickerADVRow` — the ADV source the wedge will pass through; the field is `avg_daily_volume_shares: float | None`
* `src/alphamind/_kernel/money.py` — `Money`, `Price`, `money()` boundary
* `src/alphamind/execution/paper_evaluation_harness/__init__.py` — extends the `__all__` list set by story 01

## Depends on

* `ALP-524` (story 01) — the package skeleton + config-loading scaffolding the test scaffolding shares; allows tests under the same package conftest.

## Scope

In scope, under `src/alphamind/execution/paper_evaluation_harness/spread.py`. Tests at `tests/execution/paper_evaluation_harness/test_spread.py`.

### 1\. `estimate_spread` function

New module `src/alphamind/execution/paper_evaluation_harness/spread.py` with:

```python
def estimate_spread(
    *,
    fill_price: Price,
    adv_shares: float,
    realized_volatility: float,
    buffer_pct: float,
) -> Money: ...
```

Returns the estimated bid-ask spread in dollars (per share for equities, per contract for options — the caller is responsible for using the right units in `adv_shares` for the instrument; the estimator treats the inputs symbolically).

**Model form.** Use a fallback heuristic of the shape:

```
spread_dollars = max(
    minimum_spread_floor_dollars,
    k * fill_price * realized_volatility / sqrt(adv_dollars_scaled),
)
spread_with_buffer = spread_dollars * (1 + buffer_pct / 100)
```

where `adv_dollars_scaled` is a documented scaling of `adv_shares * fill_price` (e.g., dollars per million), and `minimum_spread_floor_dollars` represents the penny-tick floor for actively-traded equities. The subagent picks the model constants (`k`, `minimum_spread_floor_dollars`, the ADV scaling) and documents them inline with a citation or rationale referenced to microstructure literature (Amihud illiquidity, Corwin-Schultz, or similar). Constants are NOT yaml-tunable — they are model parameters, not operator knobs (per parent decision E and the calibrated-not-pessimistic principle).

The function is pure: no DB / HTTP / module-level config imports. All inputs flow through parameters.

### 2\. Edge cases

* `adv_shares == 0` → clamp to a small positive epsilon (e.g., 1.0) before the sqrt to avoid divide-by-zero. Document this in the docstring as a defensive guard; in practice `adv_shares=0` means data unavailable and the wedge should catch this earlier — but the function should not crash.
* `realized_volatility == 0` → the model still produces `minimum_spread_floor_dollars` because of the `max(...)` clamp. No special-case.
* `fill_price == 0` → same clamping behavior.

### Out of scope

* Observed-quote source (deferred per parent decision E)
* Impact estimator (story 02b)
* Composition into `LiveExecutionEstimate` (story 03)
* Config loading — caller supplies `buffer_pct` directly
* Options-side adaptation — the function takes generic units; the caller (story 03) substitutes options-contract data

## Acceptance criteria

- [ ] `estimate_spread(fill_price=price("100"), adv_shares=10_000_000, realized_volatility=0.20, buffer_pct=10)` returns a strictly positive `Money` value.
- [ ] For an AAPL-like input (`fill_price=price("200")`, `adv_shares=50_000_000`, `realized_volatility=0.20`, `buffer_pct=10`), the returned spread is in the range `[$0.005, $0.50]` — i.e., a few bps to a few tens of bps. Test asserts an order-of-magnitude bound, not a point value.
- [ ] For an illiquid input (`fill_price=price("50")`, `adv_shares=100_000`, `realized_volatility=0.30`, `buffer_pct=10`), the returned spread is strictly greater than the AAPL-like spread above.
- [ ] Halving `adv_shares` widens the spread (monotone-decreasing in ADV).
- [ ] Doubling `realized_volatility` widens the spread (monotone-increasing in vol) until the floor saturates.
- [ ] Setting `buffer_pct=0` produces a spread strictly less than `buffer_pct=10` (linear in `(1 + buffer/100)`).
- [ ] `adv_shares=0` does not raise; returns at least `minimum_spread_floor_dollars` × buffer.
- [ ] `realized_volatility=0` returns `minimum_spread_floor_dollars` × buffer (floor saturates).
- [ ] The module has no DB / HTTP / `MainConfig` imports — verified by `grep` over `src/alphamind/execution/paper_evaluation_harness/spread.py`.
- [ ] `paper_evaluation_harness/__init__.py` re-exports `estimate_spread`; `__all__` extends to include it.
- [ ] `tests/execution/paper_evaluation_harness/test_spread.py` exists and passes under `uv run pytest tests/execution/paper_evaluation_harness/test_spread.py --testmon -n auto`.
- [ ] `uv run pytest --testmon -n auto` passes.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Run the scoped test file plus the lint chain. The sanity-bound tests for AAPL-like and illiquid inputs anchor the model output to plausible microstructure values without over-constraining the exact constants.