# 01b — Black-Scholes price extension (`bs_price`)

## Goal

Extend the guardrail-evaluation work tree's closed-form Black-Scholes math layer with a `bs_price(...)` function that returns the European call/put option price under the same input contract as the existing `bs_greeks(...)`. The PM-equivalent stress in story 03b needs to revalue options at shocked underlying prices and shocked IVs — `bs_greeks` returns delta/gamma/theta/vega but not the option price itself, so this story adds the missing primitive. Coordinated additive edit to a sibling-owned module — no rename, no signature change to `bs_greeks`.

## Reading

* `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` — existing closed-form module; reuse `_norm_cdf`, `_d1`, `_d2`, and the input-validation conventions (raise `ValueError` on non-positive `spot`/`strike`/`implied_volatility`; total at `time_to_expiration_years <= 0`).
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` § `ContractType` — `call` / `put` enum the new function dispatches on.
* `tests/risk_guardrails/guardrail_evaluation/test_black_scholes.py` (if present) — existing test conventions for the `bs_greeks` primitive; mirror them.
* `docs/design/05-execution-layer/regt-margin-attribution.md` § Portfolio-margin reference model — clarifies the revaluation call site (`bs_price` is consumed by story 03b's per-shock-point revaluation, not directly by anything in the guardrail-evaluation library).
* `ALP-126` parent issue, Coordinated edits note — confirms this is an additive edit; no renames to `bs_greeks`.

## Depends on

None within this work tree. The guardrail-evaluation work tree ([ALP-134](<https://linear.app/alphamind-jatassi/issue/ALP-134>)) is *done* and owns the module being extended.

## Scope

In scope: `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` (additive edit) and `tests/risk_guardrails/guardrail_evaluation/test_black_scholes.py` (extend with `bs_price` cases; create if missing).

### 1\. `bs_price` function

Add at the same level as `bs_greeks`, sharing all helpers:

```python
def bs_price(
    *,
    spot: float,
    strike: float,
    time_to_expiration_years: float,
    risk_free_rate: float,
    implied_volatility: float,
    contract_type: ContractType,
) -> float:
    """Closed-form Black-Scholes price for a European option.

    Returns the option premium in the same currency units as ``spot`` and
    ``strike``. Conventions match ``bs_greeks``: ``time_to_expiration_years``
    is calendar-day-anchored; non-positive ``spot``, ``strike``, or
    ``implied_volatility`` raise ``ValueError`` because those are upstream
    contract violations.

    At ``time_to_expiration_years <= 0`` the function returns intrinsic
    value: ``max(spot - strike, 0)`` for calls and ``max(strike - spot, 0)``
    for puts. ATM at expiry returns ``0.0``.

    Negative ``risk_free_rate`` is accepted; the closed form handles it.
    """
```

Implementation per the standard Black-Scholes call/put pricing formulas:

* Call: `C = spot * N(d1) - strike * exp(-r * t) * N(d2)`
* Put : `P = strike * exp(-r * t) * N(-d2) - spot * N(-d1)`

Where `N = _norm_cdf`, `d1 = _d1(...)`, `d2 = _d2(d1, sigma, sqrt_t)`. Reuse `_SQRT_2`, `_norm_cdf`, `_d1`, `_d2` already in the module; do not duplicate.

### 2\. Module `__all__`

Add `"bs_price"` to the module's exported names if `__all__` is defined; otherwise no action.

### 3\. Tests

Extend (or create) `tests/risk_guardrails/guardrail_evaluation/test_black_scholes.py` with:

* `test_bs_price_call_atm` — at-the-money call (spot==strike) at non-trivial `t`, IV, r — assert price is positive and approximately matches a hand-computed reference value (use `pytest.approx` with a tight tolerance).
* `test_bs_price_put_atm` — same for puts.
* `test_bs_price_call_deep_itm_approaches_intrinsic_minus_pv_strike` — `spot >> strike`, short `t` — assert price approaches `spot − strike * exp(-r*t)` within a small tolerance.
* `test_bs_price_put_deep_itm_approaches_intrinsic_minus_pv_strike` — same for puts (`strike >> spot`).
* `test_bs_price_call_deep_otm_approaches_zero` — `strike >> spot`, modest IV — assert price is near zero (`< 0.01 * spot`).
* `test_bs_price_at_expiry_returns_intrinsic` — `time_to_expiration_years = 0.0`: call ITM returns `spot − strike`, put OTM returns `0.0`.
* `test_bs_price_rejects_non_positive_spot` — `spot=0.0` raises `ValueError` mentioning "Spot must be positive".
* `test_bs_price_rejects_non_positive_strike` — `strike=-1.0` raises `ValueError`.
* `test_bs_price_rejects_non_positive_iv` — `implied_volatility=0.0` raises `ValueError`.
* `test_bs_price_put_call_parity` — at consistent inputs, assert `bs_price(call) − bs_price(put) ≈ spot − strike * exp(-r*t)` within a small tolerance.
* `test_bs_price_negative_risk_free_rate` — `risk_free_rate=-0.02` produces a positive, finite price for a near-ATM call.

### Out of scope

* Modifications to `bs_greeks` signature or behavior.
* Implied-volatility solving (inverse of `bs_price`) — not needed by the regt-margin-attribution work tree.
* Per-strike volatility skew / smile dynamics — `bs_price` consumes one IV per call (skew application happens upstream in 03b's IV-shock interpolation).

## Acceptance criteria

- [ ] `bs_price` is defined in `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` with the keyword-only signature and docstring named in Scope §1.
- [ ] `bs_price` returns the correct value for a hand-computed at-the-money call test case (within `1e-6` tolerance).
- [ ] Put-call parity holds within `1e-6` tolerance across at least one non-trivial input set.
- [ ] At `time_to_expiration_years = 0.0`, `bs_price` returns intrinsic value (`max(spot − strike, 0)` for calls; `max(strike − spot, 0)` for puts).
- [ ] Non-positive `spot`, `strike`, or `implied_volatility` raise `ValueError` with a message naming the offending parameter.
- [ ] Negative `risk_free_rate` is accepted and produces a finite positive price for a near-ATM call.
- [ ] `bs_greeks`'s existing tests still pass (no behavior change to that function).
- [ ] All new tests pass under `uv run pytest tests/risk_guardrails/guardrail_evaluation/test_black_scholes.py -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/risk_guardrails/guardrail_evaluation/ -n auto` and confirm both `bs_greeks` and `bs_price` tests pass. Inspect `black_scholes.py` to confirm `bs_price` reuses the existing `_norm_cdf`, `_d1`, `_d2` helpers rather than duplicating math.