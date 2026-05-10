# 01e — Drop `position_weight_pct` upper bound; audit range constraints

## Goal

Remove the `[0, 100]` upper bound on `PositionRecord.position_weight_pct` and audit other range constraints on the position record for the same anti-pattern. Position weight = market value / total portfolio value; for short positions market value is negative (short equity owes capital to cover) and for leveraged longs weight can exceed 100%. Both are legitimate states the design supports. The current constraint will fire `ValueError` against any short-position fixture the moment it's constructed — the only reason it hasn't bitten is that every existing test fixture is long-only equity. Replace the bounded constraint with `Annotated[float, Field(allow_inf_nan=False)]` (finite-only). Audit and document `notional_exposure_usd` and `delta_adjusted_exposure_usd` sign conventions while the file is open.

## Reading

* `src/alphamind/portfolio_state/records/positions.py:229-238` — current `_check_range_constraints` validator with the `[0, 100]` bound and `>= 0` constraints.
* `src/alphamind/portfolio_state/records/positions.py:137,139,140` — current types of `position_weight_pct`, `notional_exposure_usd`, `delta_adjusted_exposure_usd`.
* `src/alphamind/portfolio_state/computations/positions.py` — see how the assembler computes `position_weight_pct` (sign behavior under shorts and leverage).
* `src/alphamind/portfolio_state/assembler.py:553` — call site populating `position_weight_pct` from `compute_position_weight_pct(market_value, total_portfolio_value)`.
* `docs/design/05-execution-layer/position-model.md` § Base position — defines position weight as "market value as a percentage of total portfolio value (positions + cash)" — short MV is negative.
* `docs/design/05-execution-layer/position-model.md` § Equity position — short equity adds borrow rate, locate status, margin held; explicitly supported instrument.
* `docs/design/05-execution-layer/position-model.md` § Exposure calculations — "delta-adjusted" exposure for shorts is signed (negative for short equities, signed-by-delta for options); notional exposure is described as the magnitude.
* `tests/portfolio_state/records/test_positions.py::TestRangeConstraints` — existing tests on the `[0, 100]` bound; will need updates.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Architectural invariants — confirms additive-changes-don't-break rule; this story is additive in semantics (the upper bound was wrong, so removing it doesn't break anything that was correct, but it does relax tests asserting the bound).

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it relaxes one validator and documents two field semantics in `records/positions.py`.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` (validator change, docstring additions). Tests at `tests/portfolio_state/records/test_positions.py` (extend `TestRangeConstraints`).

### 1\. Drop the `position_weight_pct` upper bound

In `_check_range_constraints` (positions.py:229-238), remove the `position_weight_pct in [0, 100]` check. Keep `position_age_hours >= 0` and `notional_exposure_usd >= 0` (see §3 below for the latter's semantics).

Replace the field's type annotation with `Annotated[float, Field(allow_inf_nan=False)]` (finite-only). Negative weights (shorts) and >100% weights (leveraged) are now accepted.

### 2\. Document `position_weight_pct` sign convention

Add a class-level docstring section on `PositionRecord` explaining:

* `position_weight_pct` is signed: positive for long positions, negative for short positions, can exceed 100% absolute value when leveraged.
* Computed as `current_market_value_usd / total_portfolio_value` × 100; the sign follows the market value sign.

### 3\. Document and validate `notional_exposure_usd` vs `delta_adjusted_exposure_usd` semantics

Per design (`docs/design/05-execution-layer/position-model.md` § Exposure calculations):

* `notional_exposure_usd` is a magnitude — positive only. Keep the existing `>= 0` validator.
* `delta_adjusted_exposure_usd` is signed — positive for net-long delta, negative for net-short delta. No constraint added (any finite float).

Document both in the class docstring. Add a `model_validator` for `delta_adjusted_exposure_usd` requiring it be finite (`allow_inf_nan=False`).

### 4\. Update existing tests

Update `tests/portfolio_state/records/test_positions.py::TestRangeConstraints`:

* Remove the test asserting `position_weight_pct > 100` raises.
* Remove the test asserting `position_weight_pct < 0` raises.
* Add new tests: short-position fixture with `position_weight_pct = -3.5` constructs successfully; leveraged-long fixture with `position_weight_pct = 145.0` constructs successfully.
* Keep tests for: `position_age_hours < 0` raises, `notional_exposure_usd < 0` raises.
* Add test: `delta_adjusted_exposure_usd = -50000.0` constructs successfully (signed); `delta_adjusted_exposure_usd = float('inf')` raises.

### Out of scope

* Changing how `position_weight_pct` is computed in `computations/positions.py` — the computation already produces signed values; this story relaxes the record-level validator to match.
* Auditing range constraints on other records (e.g., [capital.py](http://capital.py)) — different file, different scope.
* Renaming any fields.

## Acceptance criteria

- [ ] `PositionRecord.position_weight_pct` no longer has the `[0, 100]` constraint; the `_check_range_constraints` validator only enforces `position_age_hours >= 0` and `notional_exposure_usd >= 0` (and the new `delta_adjusted_exposure_usd` finite check).
- [ ] `position_weight_pct` is typed as `Annotated[float, Field(allow_inf_nan=False)]` — finite values of any sign accepted.
- [ ] Constructing `PositionRecord(..., position_weight_pct=-3.5)` (short position fixture) succeeds.
- [ ] Constructing `PositionRecord(..., position_weight_pct=145.0)` (leveraged long fixture) succeeds.
- [ ] Constructing `PositionRecord(..., position_weight_pct=float('inf'))` raises `ValidationError`.
- [ ] Constructing `PositionRecord(..., delta_adjusted_exposure_usd=-50000.0)` succeeds (signed).
- [ ] Constructing `PositionRecord(..., delta_adjusted_exposure_usd=float('inf'))` raises `ValidationError`.
- [ ] Constructing `PositionRecord(..., notional_exposure_usd=-100.0)` continues to raise `ValidationError` (notional is magnitude-only).
- [ ] Constructing `PositionRecord(..., position_age_hours=-1.0)` continues to raise `ValidationError`.
- [ ] `PositionRecord` class docstring or field-adjacent comments document the sign conventions for `position_weight_pct`, `notional_exposure_usd`, `delta_adjusted_exposure_usd`.
- [ ] All existing `uv run pytest -n auto` tests pass after `TestRangeConstraints` is updated; no other tests rely on the dropped `[0, 100]` bound.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus the new short-position and leveraged-long fixtures.
* Run `uv run pytest tests/portfolio_state/records/test_positions.py -n auto -v` — confirm `TestRangeConstraints` passes with the updated assertions.
* Spot-check by `python -c "from alphamind.portfolio_state.records.positions import PositionRecord; print(PositionRecord.model_fields['position_weight_pct'])"` should show no upper-bound metadata.
* Lint clean per CLAUDE.md.