"""Black-Scholes closed-form greeks core (story 02a).

Pure-function math layer: ``bs_greeks`` returns delta/gamma/theta/vega for a
European call or put given spot, strike, time-to-expiration, risk-free rate,
implied volatility, and contract type. The same primitive serves the
guardrail-evaluation library's validation path and the execution layer's
post-fill greeks refresh, so live-time and paper-time greeks come from one math.

Conventions (pinned by tests):

* **Theta is per calendar day.** Closed-form theta is per year; this module
  divides by 365 once so callers consume "per day" directly. The rule layer
  reads ``portfolio_theta_pct_per_day`` against this convention.
* **Vega is per 1.00 absolute IV move** (i.e., sigma rising from 0.30 to 1.30).
  The rule layer's ``portfolio_vega_pct_per_iv_point`` consumes per-1-pp form;
  the rule-contribution layer (story 04) divides by 100. Keeping the math layer
  in absolute units avoids two conventions colliding here.

Cross-references: ``docs/design/06-risk-guardrails/guardrail-evaluation.md``
§ Delta-adjusted exposure §1; ``docs/design/05-execution-layer/architecture.md``
§ 4d Greeks refresh orchestration.
"""

from __future__ import annotations

import math

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    ContractType,
    Greeks,
)

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_DAYS_PER_YEAR = 365


def _norm_cdf(x: float) -> float:
    """``Φ(x)`` via ``math.erf`` (no scipy dependency)."""

    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _norm_pdf(x: float) -> float:
    """``φ(x)`` standard-normal density."""

    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1(
    spot: float,
    strike: float,
    time_to_expiration_years: float,
    risk_free_rate: float,
    implied_volatility: float,
    sqrt_t: float,
) -> float:
    return (
        math.log(spot / strike)
        + (risk_free_rate + 0.5 * implied_volatility * implied_volatility)
        * time_to_expiration_years
    ) / (implied_volatility * sqrt_t)


def _d2(d1: float, implied_volatility: float, sqrt_t: float) -> float:
    return d1 - implied_volatility * sqrt_t


def bs_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiration_years: float,
    risk_free_rate: float,
    implied_volatility: float,
    contract_type: ContractType,
) -> Greeks:
    """Closed-form Black-Scholes greeks for a European option.

    Returns ``Greeks(delta, gamma, theta, vega)`` with theta per calendar day
    and vega per 1.00 absolute IV move (see module docstring). The function is
    total — never returns ``nan``/``inf`` — but rejects non-positive ``spot``,
    ``strike``, or ``implied_volatility`` with ``ValueError`` because those are
    upstream contract violations, not numerical edge cases.

    At ``time_to_expiration_years <= 0`` the function returns the intrinsic
    delta (``1.0`` for ITM call, ``-1.0`` for ITM put, else ``0.0``) and zeros
    for gamma/theta/vega. ATM at expiry is treated as OTM (delta = 0) — a
    defensive code path; live proposals are evaluated before expiry.

    Negative ``risk_free_rate`` is accepted; the closed form handles it.
    """

    if spot <= 0:
        raise ValueError(f"Spot must be positive, got {spot}")
    if strike <= 0:
        raise ValueError(f"Strike must be positive, got {strike}")
    if implied_volatility <= 0:
        raise ValueError(f"Implied volatility must be positive, got {implied_volatility}")

    if time_to_expiration_years <= 0:
        if contract_type is ContractType.CALL:
            intrinsic_delta = 1.0 if spot > strike else 0.0
        else:
            intrinsic_delta = -1.0 if spot < strike else 0.0
        return Greeks(delta=intrinsic_delta, gamma=0.0, theta=0.0, vega=0.0)

    sqrt_t = math.sqrt(time_to_expiration_years)
    d1 = _d1(spot, strike, time_to_expiration_years, risk_free_rate, implied_volatility, sqrt_t)
    d2 = _d2(d1, implied_volatility, sqrt_t)

    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-risk_free_rate * time_to_expiration_years)

    gamma = pdf_d1 / (spot * implied_volatility * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t

    theta_term_common = -spot * pdf_d1 * implied_volatility / (2.0 * sqrt_t)
    if contract_type is ContractType.CALL:
        delta = _norm_cdf(d1)
        theta_per_year = theta_term_common - risk_free_rate * strike * discount * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_per_year = theta_term_common + risk_free_rate * strike * discount * _norm_cdf(-d2)

    theta = theta_per_year / _DAYS_PER_YEAR
    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


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

    if spot <= 0:
        raise ValueError(f"Spot must be positive, got {spot}")
    if strike <= 0:
        raise ValueError(f"Strike must be positive, got {strike}")
    if implied_volatility <= 0:
        raise ValueError(f"Implied volatility must be positive, got {implied_volatility}")

    if time_to_expiration_years <= 0:
        if contract_type is ContractType.CALL:
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    sqrt_t = math.sqrt(time_to_expiration_years)
    d1 = _d1(spot, strike, time_to_expiration_years, risk_free_rate, implied_volatility, sqrt_t)
    d2 = _d2(d1, implied_volatility, sqrt_t)
    discount = math.exp(-risk_free_rate * time_to_expiration_years)

    if contract_type is ContractType.CALL:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
