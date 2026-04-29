---
status: done
completed_date: 2026-04-28
commit_id: 43739ba
---

# 02a — Black-Scholes greeks core

## Goal

Implement the pure-function Black-Scholes greeks primitive the library uses to compute delta, gamma, theta, and vega for European calls and puts. This is the same model the [continuous monitor's greeks refresh](../../../design/05-execution-layer/architecture.md#4d-greeks-refresh-orchestration) calls — single source of truth so validation-time greeks and post-fill refresh greeks come from the same math. The function takes spot, strike, time-to-expiration, risk-free rate, IV, and contract type; returns a `Greeks` dataclass.

The conservative buffer, IV sourcing, and strategy-leg aggregation are *not* this story's concern — they live in 02b (IV) and 03 (delta-adjusted exposure). This story owns the closed-form math and its numerical-stability properties.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Delta-adjusted exposure §1 — names the four greeks and the inputs (underlying price, IV, risk-free rate, time to expiration)
- `docs/design/05-execution-layer/architecture.md` § 4d Greeks refresh orchestration — second caller of the same model; refresh writes `delta`, `gamma`, `theta`, `vega` to the position record
- `docs/design/01-data-layer/collector/storage.md` § `options_contract_snapshots` — column shape (`delta`, `gamma`, `theta`, `vega` REAL) confirming the four-greek surface; Polygon supplies these for cross-checking the math during testing
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `Greeks` and `ContractType` defined in 01

## Depends on

- 01 (canonical types)

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` defining `bs_greeks(...)` as a pure function:
  ```python
  def bs_greeks(
      *,
      spot: float,
      strike: float,
      time_to_expiration_years: float,
      risk_free_rate: float,
      implied_volatility: float,
      contract_type: ContractType,
  ) -> Greeks:
  ```
  - Closed-form Black-Scholes formulas for European options, no dividends.
  - Delta: `Φ(d1)` for calls, `Φ(d1) - 1` for puts.
  - Gamma: `φ(d1) / (S × σ × √T)`.
  - Theta (per calendar day): `[ -S × φ(d1) × σ / (2√T) - r × K × e^(-rT) × Φ(d2) ]/365` for calls, with the put adjustment.
  - Vega (per 1.00 absolute IV move, i.e., one full unit; callers scale to per-1-IV-point by dividing by 100): `S × φ(d1) × √T`. Vega is returned per **1.00 absolute IV move**; the per-1-IV-point convention used in `portfolio_vega_pct_per_iv_point` is applied at the rule-contribution layer (story 04), not here.
  - Theta is returned per **calendar day**; the rule layer reads it as percentage of portfolio per day.
- Use `math.erf` for `Φ(x) = 0.5 × (1 + erf(x / √2))` (no scipy dependency); `φ(x) = e^(-x²/2) / √(2π)` is one line.
- Edge-case handling. The function is total — never returns `nan`/`inf` — but the boundaries below are treated explicitly:
  - `time_to_expiration_years <= 0`: return `Greeks(delta=intrinsic_delta, gamma=0, theta=0, vega=0)`. `intrinsic_delta` is `1.0` if call ITM (spot > strike), `0.0` if call OTM, `-1.0` if put ITM (spot < strike), `0.0` if put OTM. Calls and puts ATM (`spot == strike`) at expiry are treated as OTM (`0.0`) for delta — borderline, but the proposal would have already executed or expired by validation time; this is a defensive code path, not a hot path.
  - `implied_volatility <= 0`: raise `ValueError(f"Implied volatility must be positive, got {implied_volatility}")`. The IV-sourcing layer (02b) is responsible for never passing a non-positive value; this is a defensive contract assertion.
  - `spot <= 0` or `strike <= 0`: raise `ValueError`.
  - `risk_free_rate < 0`: allowed (negative rates are rare but legitimate); the formula handles it.
- Helper functions `_d1(...)` and `_d2(...)` are private; only `bs_greeks` is public.
- Re-export `bs_greeks` from `guardrail_evaluation/__init__.py` (extend the existing public API list from 01).
- Unit tests:
  - **Reference values.** Hand-pick at least four scenarios — ATM call near-expiry, OTM put far-expiry, deep-ITM call mid-expiry, ATM straddle leg-by-leg — and assert returned greeks match published Black-Scholes values to a tolerance (`1e-4` on delta, `1e-4` on gamma, `1e-2` on theta and vega; theta and vega have larger absolute magnitudes). Reference values come from a textbook table (e.g., Hull, *Options, Futures, and Other Derivatives*, the Black-Scholes table) or a recomputation cross-checked against an independent implementation; the test file cites the source per scenario.
  - **Put-call parity sanity.** For a call and put with identical (S, K, T, r, σ): `delta_call - delta_put ≈ 1`, `gamma_call ≈ gamma_put`, `vega_call ≈ vega_put`. Asserts the call/put branches share the right sub-expressions.
  - **Edge case at expiry.** `bs_greeks(time_to_expiration_years=0, ...)` for an ITM call returns `Greeks(delta=1, gamma=0, theta=0, vega=0)`; OTM returns all zeros.
  - **Negative IV rejection.** Calling with `implied_volatility=0.0` or `-0.1` raises `ValueError`.
  - **Negative spot/strike rejection.** Each raises `ValueError`.
  - **Numerical stability at deep ITM/OTM.** `bs_greeks` with `spot=1000, strike=1, ...` returns `delta≈1.0` for the call without overflow; `bs_greeks` with `spot=1, strike=1000, ...` returns `delta≈0.0` without underflow. Both must produce finite (non-`inf`/`nan`) values.
  - **Large time-to-expiration.** `T=5` (five years) returns finite values; vega is large but finite.

Out of scope:

- IV sourcing — story 02b owns the `lookup_iv` interface and the realized-vol fallback.
- Conservative buffer application — story 03 applies the buffer to absolute net delta after aggregation.
- Strategy-leg aggregation — story 03.
- Dividend-adjusted Black-Scholes (Black model variant). The asset universe excludes deep-dividend names where the no-dividend model materially mis-prices; for borderline cases, the conservative buffer absorbs the error.
- American-option early-exercise adjustment. AlphaMind trades exchange-listed equity options, which are American in style, but the early-exercise premium is small for ATM/OTM options far from dividend dates; the conservative buffer absorbs the residual error. This is a documented modelling choice — the design doc anchors the model as Black-Scholes (European), no Black model.
- Greeks not in scope: rho (sensitivity to interest rates) is out — the rule layer does not consume rho.
- Caching of `Φ`/`φ` evaluations — the function runs at validation time on a small number of proposals; raw `math.erf` is fast enough.

## Notes

**Why no scipy dependency.** Pulling scipy in for `scipy.stats.norm.cdf` adds a heavyweight dependency for a one-line `math.erf` formula. The standard library covers everything needed.

**Theta convention is per-calendar-day.** Per `portfolio_theta_pct_per_day` in [`rules-and-limits.md`](../../../design/06-risk-guardrails/rules-and-limits.md), the rule consumes theta in percent-of-portfolio per day; theta-per-year would force every caller to divide by 365. The Black-Scholes closed form is per year; this function divides by 365 once.

**Vega convention is per-1.00-absolute-IV-move.** Industry convention is split: Bloomberg and most risk systems report "per 1 percentage point IV change" (i.e., σ goes from 0.30 to 0.31), which is `S × φ(d1) × √T / 100`. AlphaMind's rule [`portfolio_vega_pct_per_iv_point`](../../../design/06-risk-guardrails/rules-and-limits.md) consumes the per-1-percentage-point form. To avoid two scaling conventions in the math layer, this function returns the raw per-1.00-absolute form; the rule-contribution layer (story 04) divides by 100. Tests pin both behaviors so the convention does not silently shift.

**Negative rates allowed.** The 2020–2022 negative-rate era is in living memory and may recur. The Black-Scholes formula handles negative rates correctly; the validator does not need to guard against it.

**No second IV interpretation.** IV is the volatility input to Black-Scholes — annualized, decimal (e.g., `0.30` for 30% IV). The IV-sourcing layer (02b) is responsible for delivering a value in this convention.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/black_scholes.py` exists and defines `bs_greeks(...)` with the documented signature.
- [ ] `bs_greeks` is re-exported from `guardrail_evaluation/__init__.py`.
- [ ] A unit test asserts at least four hand-picked reference scenarios (with documented sources) match published values within tolerance.
- [ ] A unit test asserts put-call parity on delta, gamma, and vega for a matched call/put pair.
- [ ] A unit test asserts `bs_greeks(time_to_expiration_years=0, ...)` for an ITM call returns `delta=1, gamma=0, theta=0, vega=0`.
- [ ] A unit test asserts `bs_greeks(time_to_expiration_years=0, ...)` for an OTM put returns `Greeks(0, 0, 0, 0)`.
- [ ] A unit test asserts `bs_greeks(implied_volatility=0)` and `bs_greeks(implied_volatility=-0.1)` each raise `ValueError`.
- [ ] A unit test asserts `bs_greeks(spot=-1, ...)` and `bs_greeks(strike=-1, ...)` each raise `ValueError`.
- [ ] A unit test asserts deep-ITM (`spot=1000, strike=1`) and deep-OTM (`spot=1, strike=1000`) cases return finite (non-`inf`/`nan`) greeks.
- [ ] A unit test asserts negative `risk_free_rate` is accepted and produces finite greeks.
- [ ] A unit test asserts theta is returned per calendar day (verifiable by comparing against a per-year reference and dividing).
- [ ] A unit test asserts vega is returned per-1.00-absolute-IV-move (verifiable against a reference computation).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
