"""Options-greeks rule specs (story 04).

Three rules in this category, all gated on ``feature_flags.options_enabled=True``
via ``requires_options=True``:

* ``options_delta_pct`` — sum of options-only delta-adjusted exposure as %.
* ``portfolio_theta_pct_per_day`` — magnitude rule on options theta budget.
* ``portfolio_vega_pct_per_iv_point`` — magnitude rule on options vega budget.

Theta and vega contributions are *signed* — long options decay produces
negative theta, short options earn positive theta — and the projection engine
takes ``|projected_after|`` before classifying. This is wired through the
spec's ``magnitude=True`` flag.
"""

from __future__ import annotations

from typing import Literal

from alphamind.risk_guardrails.guardrail_evaluation.rules._helpers import (
    RuleSpec,
    existing_position,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Action,
    AssetType,
    DeltaAdjustedExposure,
    Greeks,
    LibraryConfig,
    PortfolioStateSnapshot,
    ProposedDelta,
)

_CONTRACT_MULTIPLIER = 100
# BS vega from story 02a is per 1.00 IV move (decimal). Divide by 100 to
# express the rule's contribution per 1-IV-point move.
_VEGA_PER_IV_POINT_DIVISOR = 100

_GreekName = Literal["theta", "vega"]


# ---------------------------------------------------------------------------
# options_delta_pct
# ---------------------------------------------------------------------------


def _options_delta_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.options_delta_pct


def _options_delta_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    """For options/strategies, signed contribution = signed_notional / value.

    Equity proposals contribute 0 — the rule constrains options-only delta.

    CLOSE on options/strategy mirrors the theta/vega CLOSE pattern: read the
    existing position's stored delta-adjusted exposure (negated, because the
    close removes that exposure). The proposal's DAE is zero whenever the
    strategist's close assessment omits ``option_legs`` — ALP-504.
    """
    if proposal.asset_type is AssetType.EQUITY:
        return 0.0
    if proposal.action is Action.CLOSE:
        existing = existing_position(proposal, state)
        if existing is None:
            return 0.0
        return -existing.delta_adjusted_exposure_usd / state.portfolio_value_usd * 100.0
    return dae.signed_notional_usd / state.portfolio_value_usd * 100.0


# ---------------------------------------------------------------------------
# portfolio_theta_pct_per_day
# ---------------------------------------------------------------------------


def _portfolio_theta_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.portfolio_theta_pct_per_day


def _portfolio_theta_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    return _greek_contribution(greek="theta", proposal=proposal, dae=dae, state=state, divisor=1)


# ---------------------------------------------------------------------------
# portfolio_vega_pct_per_iv_point
# ---------------------------------------------------------------------------


def _portfolio_vega_read_current(state: PortfolioStateSnapshot, config: LibraryConfig) -> float:
    return state.portfolio_vega_pct_per_iv_point


def _portfolio_vega_contribute(
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    config: LibraryConfig,
) -> float:
    return _greek_contribution(
        greek="vega",
        proposal=proposal,
        dae=dae,
        state=state,
        divisor=_VEGA_PER_IV_POINT_DIVISOR,
    )


# ---------------------------------------------------------------------------
# Shared greek-to-pct contribution
# ---------------------------------------------------------------------------


def _greek_contribution(
    *,
    greek: _GreekName,
    proposal: ProposedDelta,
    dae: DeltaAdjustedExposure,
    state: PortfolioStateSnapshot,
    divisor: int,
) -> float:
    """Signed change in greek-dollars as % of portfolio.

    For OPEN/ADD on options: contribution is the new contracts' greeks scaled
    by ``proposal.quantity`` — the existing position's greeks are *not*
    subtracted, mirroring ``gross_exposure_pct``'s ADD path which adds the new
    exposure without an existing-side term. For CLOSE: contribution is the
    negative of the existing position's greeks scaled by the *existing
    position's* quantity (not the proposal's). Equity proposals contribute
    zero.

    ``divisor`` adjusts the BS-native units to the rule's reporting unit
    (1 for theta-per-day; 100 for vega-per-1-IV-point).
    """
    if proposal.asset_type is AssetType.EQUITY:
        return 0.0
    if proposal.action is Action.CLOSE:
        contribution_dollars = -_existing_greek_dollars(greek, proposal, state)
    else:
        contribution_dollars = _proposal_greek_dollars(greek, proposal, dae)
    return contribution_dollars / divisor / state.portfolio_value_usd * 100.0


def _proposal_greek_dollars(
    greek: _GreekName, proposal: ProposedDelta, dae: DeltaAdjustedExposure
) -> float:
    if dae.net_greeks is None:
        return 0.0
    return _greek_dollars(dae.net_greeks, greek, proposal.quantity)


def _existing_greek_dollars(
    greek: _GreekName, proposal: ProposedDelta, state: PortfolioStateSnapshot
) -> float:
    existing = existing_position(proposal, state)
    if existing is None or existing.current_greeks is None:
        return 0.0
    return _greek_dollars(existing.current_greeks, greek, existing.quantity)


def _greek_dollars(greeks: Greeks, greek: _GreekName, quantity: float) -> float:
    """Per-contract greek → position-level dollar terms via quantity * contract multiplier."""
    value = greeks.theta if greek == "theta" else greeks.vega
    return value * quantity * _CONTRACT_MULTIPLIER


# ---------------------------------------------------------------------------
# Registry entry point
# ---------------------------------------------------------------------------


def options_greeks_specs() -> tuple[RuleSpec, ...]:
    return (
        RuleSpec(
            rule_id="options_delta_pct",
            unit="% of portfolio (delta-adjusted)",
            read_current=_options_delta_read_current,
            contribute=_options_delta_contribute,
            effective_limit_key="options_delta_pct",
            requires_options=True,
        ),
        RuleSpec(
            rule_id="portfolio_theta_pct_per_day",
            unit="% of portfolio per day",
            read_current=_portfolio_theta_read_current,
            contribute=_portfolio_theta_contribute,
            effective_limit_key="portfolio_theta_pct_per_day",
            requires_options=True,
            magnitude=True,
        ),
        RuleSpec(
            rule_id="portfolio_vega_pct_per_iv_point",
            unit="% of portfolio per 1-pt IV move",
            read_current=_portfolio_vega_read_current,
            contribute=_portfolio_vega_contribute,
            effective_limit_key="portfolio_vega_pct_per_iv_point",
            requires_options=True,
            magnitude=True,
        ),
    )
