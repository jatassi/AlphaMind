"""Tests for the Black-Scholes greeks core (story 02a).

Pins reference values, edge-case contract assertions, numerical-stability
boundaries, and the per-day theta / per-1.00-absolute-IV vega conventions
documented in the story.

Reference values come from textbook tables and a hand-recomputed cross-check;
each reference-scenario test cites its source inline.
"""

from __future__ import annotations

import math

import pytest

from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    Greeks,
    bs_greeks,
)


def test_hull_example_17_6_call_reference() -> None:
    """Hull, *Options, Futures, and Other Derivatives* (10th ed.), Example 17.6:
    S=49, K=50, r=0.05, sigma=0.20, T=20/52 (weeks-to-years). Hull lists
    delta=0.522 and theta-per-year=-4.31; published reference values.
    """
    result = bs_greeks(
        spot=49.0,
        strike=50.0,
        time_to_expiration_years=20 / 52,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert result.delta == pytest.approx(0.5216046610663964, abs=1e-4)
    assert result.gamma == pytest.approx(0.06554403934784439, abs=1e-4)
    # Theta per day = theta-per-year / 365; Hull's per-year is -4.31.
    assert result.theta == pytest.approx(-4.305331822932573 / 365, abs=1e-2)
    # Vega per 1.00 absolute IV move.
    assert result.vega == pytest.approx(12.105479882628801, abs=1e-2)
    assert isinstance(result, Greeks)


def test_atm_call_near_expiry_reference() -> None:
    """ATM call near-expiry: S=K=100, T=7/365, r=0.05, sigma=0.20.

    Reference values cross-checked against an independent Black-Scholes
    implementation (the textbook closed form computed in a fresh
    ``math.erf``-based recomputation, then pinned). Near-expiry ATM has
    delta near 0.5 and elevated gamma — a sanity-anchoring scenario.
    """
    result = bs_greeks(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=7 / 365,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert result.delta == pytest.approx(0.519329057387885, abs=1e-4)
    assert result.gamma == pytest.approx(0.14386903649200675, abs=1e-4)
    assert result.theta == pytest.approx(-0.08578850444840519, abs=1e-2)
    assert result.vega == pytest.approx(5.518264413392039, abs=1e-2)


def test_otm_put_far_expiry_reference() -> None:
    """OTM put far-expiry: S=100, K=80, T=1.0, r=0.05, sigma=0.30.

    Reference values cross-checked against an independent Black-Scholes
    implementation. Far-OTM put has small negative delta and elevated vega —
    the shape that drives ``portfolio_vega_pct_per_iv_point`` exposure.
    """
    result = bs_greeks(
        spot=100.0,
        strike=80.0,
        time_to_expiration_years=1.0,
        risk_free_rate=0.05,
        implied_volatility=0.30,
        contract_type=ContractType.PUT,
    )

    assert result.delta == pytest.approx(-0.14446348205173187, abs=1e-4)
    assert result.gamma == pytest.approx(0.007578475325194228, abs=1e-4)
    assert result.theta == pytest.approx(-0.007013628774225471, abs=1e-2)
    assert result.vega == pytest.approx(22.735425975582682, abs=1e-2)


def test_deep_itm_call_mid_expiry_reference() -> None:
    """Deep-ITM call mid-expiry: S=120, K=100, T=0.5, r=0.05, sigma=0.25.

    Reference values cross-checked against an independent Black-Scholes
    implementation. Deep-ITM call has delta approaching 1.0; useful for
    catching off-by-one branch errors between call and put paths.
    """
    result = bs_greeks(
        spot=120.0,
        strike=100.0,
        time_to_expiration_years=0.5,
        risk_free_rate=0.05,
        implied_volatility=0.25,
        contract_type=ContractType.CALL,
    )

    assert result.delta == pytest.approx(0.8963773101586902, abs=1e-4)
    assert result.gamma == pytest.approx(0.00849018066140937, abs=1e-4)
    assert result.theta == pytest.approx(-0.021969404855298004, abs=1e-2)
    assert result.vega == pytest.approx(15.282325190536866, abs=1e-2)


def test_atm_straddle_legs_reference() -> None:
    """ATM straddle (call + put) leg-by-leg: S=K=100, T=0.25, r=0.05, sigma=0.20.

    Reference values cross-checked against an independent Black-Scholes
    implementation. Per put-call parity, delta_call - delta_put = 1, and
    gamma/vega match across the legs — the structural test
    ``test_put_call_parity_sanity`` asserts this on top of the literal values.
    """
    call = bs_greeks(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=0.25,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )
    put = bs_greeks(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=0.25,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.PUT,
    )

    assert call.delta == pytest.approx(0.5694601832076737, abs=1e-4)
    assert put.delta == pytest.approx(-0.43053981679232634, abs=1e-4)
    assert call.gamma == pytest.approx(0.03928800094473793, abs=1e-4)
    assert put.gamma == pytest.approx(0.03928800094473793, abs=1e-4)
    assert call.theta == pytest.approx(-0.028696304790426883, abs=1e-2)
    assert put.theta == pytest.approx(-0.015167841769962751, abs=1e-2)
    assert call.vega == pytest.approx(19.644000472368965, abs=1e-2)
    assert put.vega == pytest.approx(19.644000472368965, abs=1e-2)


def test_put_call_parity_sanity() -> None:
    """For a matched (S, K, T, r, sigma) call/put pair:
    ``delta_call - delta_put ≈ 1``, ``gamma_call ≈ gamma_put``,
    ``vega_call ≈ vega_put``. The put-call parity relations on greeks must
    hold for any inputs where the call and put share sub-expressions; the
    test pins the call/put branches against silent divergence.
    """
    common = {
        "spot": 75.0,
        "strike": 80.0,
        "time_to_expiration_years": 0.4,
        "risk_free_rate": 0.04,
        "implied_volatility": 0.28,
    }
    call = bs_greeks(**common, contract_type=ContractType.CALL)
    put = bs_greeks(**common, contract_type=ContractType.PUT)

    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-9)
    assert call.gamma == pytest.approx(put.gamma, abs=1e-9)
    assert call.vega == pytest.approx(put.vega, abs=1e-9)


def test_at_expiry_itm_call_returns_intrinsic_delta_one() -> None:
    """``time_to_expiration_years=0`` for an ITM call (spot > strike) returns
    ``Greeks(1, 0, 0, 0)``. Defensive code path — proposals are normally
    evaluated before expiry, but the function must not divide by zero.
    """
    result = bs_greeks(
        spot=110.0,
        strike=100.0,
        time_to_expiration_years=0.0,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert result == Greeks(delta=1.0, gamma=0.0, theta=0.0, vega=0.0)


def test_at_expiry_otm_put_returns_all_zero_greeks() -> None:
    """``time_to_expiration_years=0`` for an OTM put (spot > strike) returns
    ``Greeks(0, 0, 0, 0)`` — the put has no intrinsic value, no greeks.
    """
    result = bs_greeks(
        spot=110.0,
        strike=100.0,
        time_to_expiration_years=0.0,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.PUT,
    )

    assert result == Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)


@pytest.mark.parametrize("bad_iv", [0.0, -0.1])
def test_non_positive_iv_raises_value_error(bad_iv: float) -> None:
    """``implied_volatility <= 0`` is a contract violation upstream; the
    Black-Scholes core rejects it explicitly so the failure surfaces at the
    boundary instead of as a silent ``nan``/``inf``.
    """
    with pytest.raises(ValueError, match="Implied volatility must be positive"):
        bs_greeks(
            spot=100.0,
            strike=100.0,
            time_to_expiration_years=0.25,
            risk_free_rate=0.05,
            implied_volatility=bad_iv,
            contract_type=ContractType.CALL,
        )


def test_non_positive_spot_raises_value_error() -> None:
    """Negative or zero spot is a contract violation; the core rejects it."""
    with pytest.raises(ValueError, match="Spot must be positive"):
        bs_greeks(
            spot=-1.0,
            strike=100.0,
            time_to_expiration_years=0.25,
            risk_free_rate=0.05,
            implied_volatility=0.20,
            contract_type=ContractType.CALL,
        )


def test_non_positive_strike_raises_value_error() -> None:
    """Negative or zero strike is a contract violation; the core rejects it."""
    with pytest.raises(ValueError, match="Strike must be positive"):
        bs_greeks(
            spot=100.0,
            strike=-1.0,
            time_to_expiration_years=0.25,
            risk_free_rate=0.05,
            implied_volatility=0.20,
            contract_type=ContractType.CALL,
        )


def test_deep_itm_call_returns_finite_delta_near_one() -> None:
    """``spot=1000, strike=1`` — the call is so deep ITM the delta saturates.
    All greeks must be finite (no overflow in ``math.exp``/``erf``).
    """
    result = bs_greeks(
        spot=1000.0,
        strike=1.0,
        time_to_expiration_years=0.25,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert result.delta == pytest.approx(1.0, abs=1e-6)
    assert math.isfinite(result.gamma)
    assert math.isfinite(result.theta)
    assert math.isfinite(result.vega)


def test_deep_otm_call_returns_finite_delta_near_zero() -> None:
    """``spot=1, strike=1000`` — the call is so deep OTM the delta vanishes.
    All greeks must be finite (no underflow producing ``nan``).
    """
    result = bs_greeks(
        spot=1.0,
        strike=1000.0,
        time_to_expiration_years=0.25,
        risk_free_rate=0.05,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert result.delta == pytest.approx(0.0, abs=1e-6)
    assert math.isfinite(result.gamma)
    assert math.isfinite(result.theta)
    assert math.isfinite(result.vega)


def test_large_time_to_expiration_returns_finite_greeks() -> None:
    """``T=5`` (five years) returns finite greeks; vega is large but finite.
    LEAPS-style inputs must not produce overflow.
    """
    result = bs_greeks(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=5.0,
        risk_free_rate=0.05,
        implied_volatility=0.30,
        contract_type=ContractType.CALL,
    )

    assert math.isfinite(result.delta)
    assert math.isfinite(result.gamma)
    assert math.isfinite(result.theta)
    assert math.isfinite(result.vega)
    assert result.vega > 0  # long-dated options have large vega


def test_negative_risk_free_rate_accepted() -> None:
    """The 2020-2022 negative-rate era is in living memory; the closed form
    handles negative rates correctly. Result must be finite.
    """
    result = bs_greeks(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=0.5,
        risk_free_rate=-0.005,
        implied_volatility=0.20,
        contract_type=ContractType.CALL,
    )

    assert math.isfinite(result.delta)
    assert math.isfinite(result.gamma)
    assert math.isfinite(result.theta)
    assert math.isfinite(result.vega)
    # ATM-ish call still has delta around 0.5 with mildly negative rate
    assert 0.4 < result.delta < 0.6


def test_theta_returned_per_calendar_day() -> None:
    """Pin the per-day convention. The closed-form theta is per year; this
    function divides by 365 once. We verify by computing the per-year
    reference inline and checking ``returned_theta * 365 ≈ per_year_theta``.
    The rule layer's ``portfolio_theta_pct_per_day`` consumes the per-day form.
    """
    spot, strike, t, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
    result = bs_greeks(
        spot=spot,
        strike=strike,
        time_to_expiration_years=t,
        risk_free_rate=r,
        implied_volatility=sigma,
        contract_type=ContractType.CALL,
    )

    # Recompute the per-year theta closed form here (test independence from
    # the implementation's helpers; if the implementation drifts off the
    # per-year basis, this assertion fails).
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    cdf_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    expected_per_year = (
        -spot * pdf_d1 * sigma / (2.0 * sqrt_t) - r * strike * math.exp(-r * t) * cdf_d2
    )

    assert result.theta * 365 == pytest.approx(expected_per_year, abs=1e-6)


def test_vega_returned_per_one_absolute_iv_move() -> None:
    """Pin the per-1.00-absolute-IV convention. The rule layer's
    ``portfolio_vega_pct_per_iv_point`` consumes per-1-pp form; the
    rule-contribution layer (story 04) divides by 100. We verify by computing
    the closed-form ``S * φ(d1) * √T`` inline and checking it matches the
    returned vega — i.e., **not** divided by 100 here.
    """
    spot, strike, t, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
    result = bs_greeks(
        spot=spot,
        strike=strike,
        time_to_expiration_years=t,
        risk_free_rate=r,
        implied_volatility=sigma,
        contract_type=ContractType.CALL,
    )

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    expected_per_one_absolute = spot * pdf_d1 * sqrt_t

    assert result.vega == pytest.approx(expected_per_one_absolute, abs=1e-9)
    # Per-1-pp form would be ~100x smaller; pin that the function does NOT
    # apply that scaling.
    assert result.vega > expected_per_one_absolute / 2
