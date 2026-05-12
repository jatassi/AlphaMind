"""Pure recomputation of ``OptionGreeks`` via guardrail-evaluation's Black-Scholes (story 03a).

Single entry point :func:`recompute_greeks` for the refresh task. Wraps
``bs_greeks`` (the shared closed-form primitive used at OPEN/ADD validation
time) and assembles the result into an :class:`OptionGreeks` record with
freshness-metadata fields populated.

For strategy positions, :func:`recompute_strategy_greeks` aggregates the
per-leg call so the typed ``strategy_greeks`` field on
``StrategyPositionDetails`` stays consistent with the per-leg greeks that
the guardrail-evaluation library produces at OPEN/ADD time. Aggregation
mirrors ``delta_adjusted._sum_leg_greeks``: signed-weighted greeks summed
across legs, with the leg's direction sign-flipping shorts.
"""

from __future__ import annotations

import math
from datetime import datetime

from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_greeks
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

_DAYS_PER_YEAR = 365


def _to_library_contract_type(value: OptionContractType) -> ContractType:
    """Translate the portfolio-state enum into the guardrail-evaluation enum.

    The two enums carry the same vocabulary (``CALL`` / ``PUT``) but live
    in separate modules — the guardrail-evaluation library predates the
    portfolio-state ones. This adapter is the only place the translation
    happens for refresh-time greeks.
    """
    if value is OptionContractType.CALL:
        return ContractType.CALL
    return ContractType.PUT


def recompute_greeks(
    *,
    position: OptionsPositionDetails,
    iv: float,
    spot: float,
    as_of: datetime,
    risk_free_rate: float,
) -> OptionGreeks:
    """Recompute ``OptionGreeks`` for a single-leg options position.

    Calls ``bs_greeks`` with the position's strike, expiration, and
    contract-type geometry plus the supplied market inputs. The returned
    record carries the long-equivalent greeks (per ``OptionGreeks`` sign
    convention — short-side sign-flips happen at the position-level
    direction), with freshness metadata stamped from the function inputs:

    * ``as_of_timestamp = as_of``
    * ``iv_used = iv``
    * ``refresh_failed = False``

    The function is pure: no I/O, no clock reads. Time-to-expiration is
    derived deterministically from ``(position.expiration_date - as_of.date())``
    in days/365, matching the convention ``bs_greeks`` documents.
    """
    time_to_expiration_years = (position.expiration_date - as_of.date()).days / _DAYS_PER_YEAR
    greeks = bs_greeks(
        spot=spot,
        strike=position.strike_price,
        time_to_expiration_years=time_to_expiration_years,
        risk_free_rate=risk_free_rate,
        implied_volatility=iv,
        contract_type=_to_library_contract_type(position.contract_type),
    )
    return OptionGreeks(
        delta=greeks.delta,
        gamma=greeks.gamma,
        theta=greeks.theta,
        vega=greeks.vega,
        as_of_timestamp=as_of,
        iv_used=iv,
        refresh_failed=False,
    )


def recompute_strategy_greeks(
    *,
    strategy: StrategyPositionDetails,
    leg_ivs: dict[str, float],
    spot: float,
    as_of: datetime,
    risk_free_rate: float,
) -> tuple[dict[str, OptionGreeks], OptionGreeks]:
    """Refresh per-leg greeks + the aggregated ``strategy_greeks``.

    *leg_ivs* maps each ``StrategyLeg.leg_id`` to its freshly-fetched IV.
    Returns ``(per_leg_greeks, aggregated_strategy_greeks)``; the caller is
    responsible for stitching the per-leg ``OptionGreeks`` back onto each
    ``StrategyLeg.options.greeks`` field.

    Aggregation:

    * Each leg's greeks are signed by its :class:`Direction` — ``LONG``
      keeps the long-equivalent sign, ``SHORT`` flips. (When a strategy
      record carries ``direction=None`` on a leg, we default to LONG; this
      matches the field's optional convention used by single-leg legs that
      inherit direction from the parent ``PositionRecord``.)
    * The four greeks are summed via ``math.fsum`` for numerical stability
      under cancellation between long and short legs.

    The aggregated ``strategy_greeks`` re-uses the same freshness metadata
    as the per-leg refresh: ``as_of_timestamp = as_of``,
    ``iv_used = mean of leg IVs`` (a single scalar surfaces the sourced IV
    at the strategy level for operator-facing audits),
    ``refresh_failed = False``.
    """
    per_leg_greeks: dict[str, OptionGreeks] = {}
    signed_legs: list[tuple[float, OptionGreeks]] = []
    iv_values: list[float] = []
    for leg in strategy.legs:
        leg_iv = leg_ivs[leg.leg_id]
        leg_greeks = recompute_greeks(
            position=leg.options,
            iv=leg_iv,
            spot=spot,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
        )
        per_leg_greeks[leg.leg_id] = leg_greeks
        sign = -1.0 if leg.direction is Direction.SHORT else 1.0
        signed_legs.append((sign * leg.options.contract_count, leg_greeks))
        iv_values.append(leg_iv)

    delta = math.fsum(weight * g.delta for weight, g in signed_legs)
    gamma = math.fsum(weight * g.gamma for weight, g in signed_legs)
    theta = math.fsum(weight * g.theta for weight, g in signed_legs)
    vega = math.fsum(weight * g.vega for weight, g in signed_legs)
    aggregated_iv = math.fsum(iv_values) / len(iv_values) if iv_values else 0.0

    aggregated = OptionGreeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        as_of_timestamp=as_of,
        iv_used=aggregated_iv,
        refresh_failed=False,
    )
    return per_leg_greeks, aggregated
