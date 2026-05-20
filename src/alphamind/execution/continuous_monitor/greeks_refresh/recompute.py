"""Pure recomputation of ``OptionGreeks`` via guardrail-evaluation's Black-Scholes (story 03a).

Single entry point :func:`recompute_greeks` for the refresh task. Wraps
``bs_greeks`` (the shared closed-form primitive used at OPEN/ADD validation
time) and assembles the result into an :class:`OptionGreeks` record with
freshness-metadata fields populated.

For strategy positions, :func:`recompute_strategy_greeks` refreshes each
leg's greeks and then delegates aggregation to
``position_model.strategy_payoff.compute_strategy_greeks`` — the
parent-ALP-588-decision-(C)-canonical aggregator — so the typed
``strategy_greeks`` field on ``StrategyPositionDetails`` carries the same
contract-weighted signed-average convention every downstream consumer
assumes (ALP-612).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from alphamind.execution.position_model.strategy_payoff import compute_strategy_greeks
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    StrategyLeg,
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

    Aggregation delegates to
    :func:`alphamind.execution.position_model.strategy_payoff.compute_strategy_greeks`
    — the parent-ALP-588-decision-(C)-canonical aggregator — so the refreshed
    ``strategy_greeks`` carries the *contract-weighted signed average*
    convention every downstream consumer assumes (ALP-612)::

        sum(leg_sign * contract_count * contract_multiplier * leg.greeks.<g>)
        / sum(contract_count * contract_multiplier)

    A leg whose ``direction`` is unset defaults to ``LONG`` — the field's
    optional convention for single-leg legs that inherit direction from the
    parent ``PositionRecord``.

    The aggregated ``strategy_greeks`` re-stamps the two freshness fields the
    canonical aggregator clears — ``as_of_timestamp = as_of`` and
    ``iv_used = mean of leg IVs`` (a single scalar surfaces the sourced IV at
    the strategy level for operator-facing audits). ``refresh_failed`` carries
    the canonical OR of the legs' flags — always ``False`` here, since
    :func:`recompute_greeks` raises rather than emitting a failed-refresh leg.
    """
    per_leg_greeks: dict[str, OptionGreeks] = {}
    refreshed_legs: list[StrategyLeg] = []
    for leg in strategy.legs:
        leg_greeks = recompute_greeks(
            position=leg.options,
            iv=leg_ivs[leg.leg_id],
            spot=spot,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
        )
        per_leg_greeks[leg.leg_id] = leg_greeks
        refreshed_legs.append(
            replace(
                leg,
                options=replace(leg.options, greeks=leg_greeks),
                direction=leg.direction if leg.direction is not None else Direction.LONG,
            )
        )

    # `compute_strategy_greeks` validates the legs (non-empty, single
    # expiration / underlying); calling it first means an empty-legs strategy
    # raises a clear ValueError before the IV-mean divide-by-zero below.
    canonical = compute_strategy_greeks(tuple(refreshed_legs))
    aggregated_iv = math.fsum(leg_ivs[leg.leg_id] for leg in strategy.legs) / len(strategy.legs)
    return per_leg_greeks, replace(canonical, as_of_timestamp=as_of, iv_used=aggregated_iv)
