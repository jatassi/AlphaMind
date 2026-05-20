"""Delta-adjusted exposure with conservative buffer and strategy aggregation
(story 03).

Single call site that combines Black-Scholes greeks (story 02a), IV sourcing
(story 02b), and the conservative buffer into a per-proposal
``DeltaAdjustedExposure``. Downstream rule-contribution math (story 04)
consumes ``signed_notional_usd`` and ``net_greeks`` without reaching back into
this layer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_greeks
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Direction,
    Greeks,
    IvSource,
    LibraryConfig,
    MarketInputs,
    OptionLeg,
    ProposedDelta,
)

_CONTRACT_MULTIPLIER = 100
_DAYS_PER_YEAR = 365

# Per-regime multiplier applied to ``LibraryConfig.conservative_buffer_pct``.
# Keys match ``Regime.value`` (underscore form) since ``LibraryConfig.active_regime``
# carries the enum's ``.value`` string. Out-of-band labels fall through to ``1.0``;
# structural input errors are caught by ``from_resolved_config`` upstream so the
# math layer stays total.
_REGIME_BUFFER_MULTIPLIERS = {
    "low_vol": 0.8,
    "normal": 1.0,
    "elevated": 1.5,
    "crisis": 2.0,
}


def compute_delta_adjusted_exposure(
    *,
    proposal: ProposedDelta,
    market: MarketInputs,
    config: LibraryConfig,
    delta_buffer_factor: float = 1.0,
) -> DeltaAdjustedExposure:
    """Combine Black-Scholes, IV sourcing, and the conservative buffer.

    ``delta_buffer_factor`` scales the effective conservative buffer at the
    call site. Default ``1.0`` reuses the regime-resolved buffer unchanged;
    cascade re-evaluations (breach-behavior story 05b) pass a tighter factor
    when calibration demands extra conservatism.
    """
    if proposal.action in (Action.ADJUST, Action.CANCEL):
        return _exposure_neutral(proposal)

    # CLOSE on options/strategy may omit ``option_legs`` — a close assessment
    # references an existing position whose legs are already on the book
    # (ALP-504). With no legs to re-price, the library does not synthesize a
    # proposal DAE; the rule contributions read from
    # ``existing.delta_adjusted_exposure_usd`` instead.
    if (
        proposal.action is Action.CLOSE
        and proposal.asset_type is not AssetType.EQUITY
        and proposal.option_legs is None
    ):
        return _exposure_neutral(proposal)

    if proposal.asset_type is AssetType.EQUITY:
        return DeltaAdjustedExposure(
            proposal_id=proposal.id,
            signed_notional_usd=_directional_sign(proposal) * float(proposal.notional_usd),
            net_greeks=None,
            iv_used=None,
            iv_source=None,
            unbuffered_delta=None,
        )

    legs = proposal.option_legs or ()
    spot = market.underlying_prices[proposal.underlying]
    leg_results = [
        _resolve_leg(leg=leg, proposal=proposal, market=market, spot=spot) for leg in legs
    ]

    net_greeks = _sum_leg_greeks(leg_results)
    unbuffered_abs_delta = abs(net_greeks.delta)
    buffered_abs_delta = unbuffered_abs_delta * (
        1 + _effective_buffer_fraction(config, factor=delta_buffer_factor)
    )
    # A single-leg OPTION carries its directional sign on the position-level
    # ``direction``; the per-leg ``abs()`` above keeps a short-call long-direction
    # leg from double-counting that sign. A multi-leg STRATEGY has no
    # position-level ``direction`` (``direction is None`` — ALP-603); its true
    # directional sign already lives in the net-signed leg deltas, so the sign
    # tracks ``net_greeks.delta``. ``CLOSE`` inverts either sign (the signed
    # notional carries the direction of change).
    if proposal.asset_type is AssetType.STRATEGY:
        delta_sign = math.copysign(1.0, net_greeks.delta) if net_greeks.delta != 0.0 else 1.0
        exposure_sign = -delta_sign if proposal.action is Action.CLOSE else delta_sign
    else:
        exposure_sign = _directional_sign(proposal)
    signed_notional = (
        exposure_sign * buffered_abs_delta * spot * _CONTRACT_MULTIPLIER * proposal.quantity
    )

    return DeltaAdjustedExposure(
        proposal_id=proposal.id,
        signed_notional_usd=signed_notional,
        net_greeks=net_greeks,
        iv_used=_mean_iv(leg_results),
        iv_source=_aggregate_iv_source(leg_results),
        unbuffered_delta=unbuffered_abs_delta,
    )


# ---------------------------------------------------------------------------
# Per-leg resolution and aggregation helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LegResult:
    """Per-leg greeks already weighted by signed ``leg.quantity``, plus the
    IV used for the leg."""

    weighted_greeks: Greeks
    iv: float
    iv_source: IvSource


def _directional_sign(proposal: ProposedDelta) -> float:
    """Signed exposure direction for an EQUITY or single-leg OPTION proposal.

    A multi-leg STRATEGY carries ``direction=None`` (ALP-603) and never reaches
    this helper — its sign is derived from ``net_greeks.delta`` instead. For
    the EQUITY / single-leg OPTION callers the entry point's cross-field check
    guarantees a non-``None`` ``direction``: ``LONG`` maps to ``+1`` and
    ``SHORT`` to ``-1``. ``CLOSE`` inverts the sign — a close reduces exposure,
    so the signed notional carries the *direction of change* rather than the
    position's own direction.
    """
    direction_sign = 1.0 if proposal.direction is Direction.LONG else -1.0
    return -direction_sign if proposal.action is Action.CLOSE else direction_sign


def _exposure_neutral(proposal: ProposedDelta) -> DeltaAdjustedExposure:
    """Result for actions where the library does not synthesize a proposal DAE.

    Used for ``ADJUST``/``CANCEL`` (exposure-neutral by definition) and for
    ``CLOSE`` on options/strategy without ``option_legs`` (no leg breakdown to
    re-price; the rule contributions read the existing position's stored
    DAE).

    Greeks are zero for options (so the rule-contribution math sees zero
    impact on theta/vega budgets) and ``None`` for equity (the dataclass
    invariant for ``net_greeks``).
    """
    zero_greeks = (
        Greeks(0.0, 0.0, 0.0, 0.0) if proposal.asset_type is not AssetType.EQUITY else None
    )
    return DeltaAdjustedExposure(
        proposal_id=proposal.id,
        signed_notional_usd=0.0,
        net_greeks=zero_greeks,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def _resolve_leg(
    *, leg: OptionLeg, proposal: ProposedDelta, market: MarketInputs, spot: float
) -> _LegResult:
    iv_result = market.iv_provider.lookup_iv(
        underlying=proposal.underlying,
        strike=leg.strike,
        expiration=leg.expiration,
        contract_type=leg.contract_type,
        as_of=market.as_of,
    )
    time_to_expiration_years = (leg.expiration - market.as_of.date()).days / _DAYS_PER_YEAR
    leg_greeks = bs_greeks(
        spot=spot,
        strike=leg.strike,
        time_to_expiration_years=time_to_expiration_years,
        risk_free_rate=market.risk_free_rate,
        implied_volatility=iv_result.implied_volatility,
        contract_type=leg.contract_type,
    )
    weight = float(leg.quantity)
    return _LegResult(
        weighted_greeks=Greeks(
            delta=weight * leg_greeks.delta,
            gamma=weight * leg_greeks.gamma,
            theta=weight * leg_greeks.theta,
            vega=weight * leg_greeks.vega,
        ),
        iv=iv_result.implied_volatility,
        iv_source=iv_result.source,
    )


def _sum_leg_greeks(leg_results: Sequence[_LegResult]) -> Greeks:
    return Greeks(
        delta=math.fsum(r.weighted_greeks.delta for r in leg_results),
        gamma=math.fsum(r.weighted_greeks.gamma for r in leg_results),
        theta=math.fsum(r.weighted_greeks.theta for r in leg_results),
        vega=math.fsum(r.weighted_greeks.vega for r in leg_results),
    )


def _effective_buffer_fraction(config: LibraryConfig, *, factor: float = 1.0) -> float:
    multiplier = _REGIME_BUFFER_MULTIPLIERS.get(config.active_regime, 1.0)
    return (config.conservative_buffer_pct * multiplier * factor) / 100.0


def _mean_iv(leg_results: Sequence[_LegResult]) -> float:
    return math.fsum(r.iv for r in leg_results) / len(leg_results)


def _aggregate_iv_source(leg_results: Sequence[_LegResult]) -> IvSource:
    """``REALIZED_VOL_FALLBACK`` if any leg fell back; ``SURFACE`` only when
    every leg was surfaced."""
    if any(r.iv_source is IvSource.REALIZED_VOL_FALLBACK for r in leg_results):
        return IvSource.REALIZED_VOL_FALLBACK
    return IvSource.SURFACE
