"""Transition state machine for the regime-adaptation feature (story 04b).

Resolves the next ``NextTransitionDecision`` from the previously-persisted
``RegimeAdaptationState`` and the freshly-classified ``Regime`` for the current
invocation. Encodes the asymmetric mechanics from
``regime-adaptation.md`` § Transition mechanics: tightening is a one-step
transition, loosening interpolates over ``LOOSENING_INVOCATIONS`` invocations,
and tightening always overrides loosening.

Reading: the story file at
``docs/implementation/06-risk-guardrails/regime-adaptation/04b-transition-state-machine.md``.
"""

from __future__ import annotations

from alphamind.config.models.regimes import Regime
from alphamind.portfolio_state.records.capital import RegimeTransitionState
from alphamind.risk_guardrails.regime_adaptation.types import (
    LOOSENING_INVOCATIONS,
    NextTransitionDecision,
    RegimeAdaptationState,
)

_REGIME_VOLATILITY_LADDER: tuple[Regime, ...] = (
    Regime.low_vol,
    Regime.normal,
    Regime.elevated,
    Regime.crisis,
)
"""Regimes ordered by increasing volatility. Higher index = higher vol;
tightening moves to a higher index, loosening to a lower index."""


def regime_ladder_index(regime: Regime) -> int:
    """Return the regime's position in the increasing-volatility ladder."""
    return _REGIME_VOLATILITY_LADDER.index(regime)


def compute_next_transition(
    *,
    new_regime: Regime,
    invocation_id: str,
    prior_state: RegimeAdaptationState | None,
) -> NextTransitionDecision:
    """Resolve the next transition state from prior persisted state and new regime.

    The function is pure: equal inputs produce equal outputs; ``prior_state``
    is not mutated. Bootstrap (no prior state) yields ``STABLE``. Same-regime
    invocations either decrement an in-flight loosening countdown or collapse
    a one-step tightening to ``STABLE``. Regime changes produce ``TIGHTENING``
    (volatility up) or ``LOOSENING`` (volatility down), with tightening always
    overriding any in-flight loosening.
    """
    if prior_state is None:
        return _stable_decision(active_regime=new_regime, prior_regime=None)

    prior_index = regime_ladder_index(prior_state.active_regime)
    new_index = regime_ladder_index(new_regime)

    if new_index == prior_index:
        return _no_change_decision(prior_state=prior_state, new_regime=new_regime)
    if new_index > prior_index:
        return _tightening_decision(
            new_regime=new_regime,
            prior_regime=prior_state.active_regime,
        )
    return _loosening_decision(
        new_regime=new_regime,
        prior_regime=prior_state.active_regime,
        invocation_id=invocation_id,
    )


def _no_change_decision(
    *,
    prior_state: RegimeAdaptationState,
    new_regime: Regime,
) -> NextTransitionDecision:
    """Resolve the next decision when the regime did not change."""
    if prior_state.transition_state == RegimeTransitionState.LOOSENING:
        next_remaining = prior_state.transition_invocations_remaining - 1
        if next_remaining == 0:
            return _stable_decision(active_regime=new_regime, prior_regime=new_regime)
        return NextTransitionDecision(
            active_regime=new_regime,
            prior_regime=new_regime,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=next_remaining,
            transition_started_invocation_id=prior_state.transition_started_invocation_id,
            transition_origin_regime=prior_state.transition_origin_regime,
        )
    # STABLE → STABLE; TIGHTENING (one-step) → STABLE.
    return _stable_decision(active_regime=new_regime, prior_regime=new_regime)


def _tightening_decision(
    *,
    new_regime: Regime,
    prior_regime: Regime,
) -> NextTransitionDecision:
    """Resolve the next decision for a tightening transition.

    Any in-flight loosening residue is discarded; a second tightening on top
    of an existing tightening simply moves to the new (higher) regime. The
    Optional fields stay ``None`` because tightening is one-step.
    """
    return NextTransitionDecision(
        active_regime=new_regime,
        prior_regime=prior_regime,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def _loosening_decision(
    *,
    new_regime: Regime,
    prior_regime: Regime,
    invocation_id: str,
) -> NextTransitionDecision:
    """Resolve the next decision for a loosening transition.

    The interpolation seed is identical regardless of whether the prior state
    was ``STABLE``, ``TIGHTENING``, or ``LOOSENING`` — a loosening during
    loosening *resets* the interpolation, with the prior state's
    ``active_regime`` (not its prior ``transition_origin_regime``) as the
    new origin.
    """
    return NextTransitionDecision(
        active_regime=new_regime,
        prior_regime=prior_regime,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=LOOSENING_INVOCATIONS,
        transition_started_invocation_id=invocation_id,
        transition_origin_regime=prior_regime,
    )


def _stable_decision(
    *,
    active_regime: Regime,
    prior_regime: Regime | None,
) -> NextTransitionDecision:
    """Build a STABLE decision with the canonical ``remaining=0`` and Optionals ``None``."""
    return NextTransitionDecision(
        active_regime=active_regime,
        prior_regime=prior_regime,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )
