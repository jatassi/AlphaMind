"""Tests for the transition state machine (story 04b).

The state machine resolves the next ``NextTransitionDecision`` from the
previously-persisted ``RegimeAdaptationState`` and the freshly-classified
``Regime`` for the current invocation. Tightening is a one-step transition;
loosening interpolates over ``LOOSENING_INVOCATIONS`` invocations; tightening
always overrides loosening.
"""

from __future__ import annotations

import dataclasses

import pytest

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.regimes import Regime
from alphamind.risk_guardrails.regime_adaptation import (
    LOOSENING_INVOCATIONS,
    NextTransitionDecision,
    RegimeAdaptationState,
)
from alphamind.risk_guardrails.regime_adaptation.transition_machine import (
    _REGIME_VOLATILITY_LADDER,
    compute_next_transition,
    regime_ladder_index,
)

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _state(
    *,
    active_regime: Regime,
    transition_state: RegimeTransitionState,
    transition_invocations_remaining: int = 0,
    transition_started_invocation_id: str | None = None,
    transition_origin_regime: Regime | None = None,
    prior_regime: Regime | None = None,
    invocation_id: str = "INV-PRIOR",
) -> RegimeAdaptationState:
    """Build a ``RegimeAdaptationState`` honoring its ``__post_init__`` invariants."""
    return RegimeAdaptationState(
        as_of="2026-04-29T12:00:00+00:00",
        invocation_id=invocation_id,
        active_regime=active_regime,
        prior_regime=prior_regime,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        transition_started_invocation_id=transition_started_invocation_id,
        transition_origin_regime=transition_origin_regime,
        active_overlays=(),
        distillation_regime_label="vol_expansion",
        distillation_vix_level=18.5,
        regime_skip_emergency=False,
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_returns_stable_with_zero_remaining_and_optionals_none() -> None:
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-1",
        prior_state=None,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


# ---------------------------------------------------------------------------
# No-change transitions
# ---------------------------------------------------------------------------


def test_no_change_stable_stays_stable() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.STABLE,
    )
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def test_no_change_tightening_collapses_to_stable() -> None:
    prior = _state(
        active_regime=Regime.elevated,
        transition_state=RegimeTransitionState.TIGHTENING,
    )
    decision = compute_next_transition(
        new_regime=Regime.elevated,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.elevated,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def test_no_change_loosening_decrements_remaining_by_one() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=2,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )


def test_no_change_loosening_countdown_to_one_carries_optionals_forward() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=2,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-3",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=1,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )


def test_no_change_loosening_completes_to_stable_when_remaining_reaches_zero() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=1,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-4",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


# ---------------------------------------------------------------------------
# Tightening transitions
# ---------------------------------------------------------------------------


def test_tightening_from_stable() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.STABLE,
    )
    decision = compute_next_transition(
        new_regime=Regime.elevated,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def test_tightening_skipping_a_band() -> None:
    prior = _state(
        active_regime=Regime.low_vol,
        transition_state=RegimeTransitionState.STABLE,
    )
    decision = compute_next_transition(
        new_regime=Regime.crisis,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.crisis,
        prior_regime=Regime.low_vol,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def test_tightening_overrides_loosening_dropping_residue() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=2,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    decision = compute_next_transition(
        new_regime=Regime.elevated,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def test_tightening_from_tightening_uses_immediate_predecessor_as_prior() -> None:
    prior = _state(
        active_regime=Regime.elevated,
        transition_state=RegimeTransitionState.TIGHTENING,
    )
    decision = compute_next_transition(
        new_regime=Regime.crisis,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.crisis,
        prior_regime=Regime.elevated,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


# ---------------------------------------------------------------------------
# Loosening transitions
# ---------------------------------------------------------------------------


def test_loosening_from_stable_seeds_remaining_origin_and_started() -> None:
    prior = _state(
        active_regime=Regime.elevated,
        transition_state=RegimeTransitionState.STABLE,
    )
    decision = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.elevated,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=LOOSENING_INVOCATIONS,
        transition_started_invocation_id="INV-2",
        transition_origin_regime=Regime.elevated,
    )


def test_loosening_skipping_a_band() -> None:
    prior = _state(
        active_regime=Regime.crisis,
        transition_state=RegimeTransitionState.STABLE,
    )
    decision = compute_next_transition(
        new_regime=Regime.low_vol,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.low_vol,
        prior_regime=Regime.crisis,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=LOOSENING_INVOCATIONS,
        transition_started_invocation_id="INV-2",
        transition_origin_regime=Regime.crisis,
    )


def test_loosening_during_loosening_resets_interpolation_with_prior_active_as_origin() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=2,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    decision = compute_next_transition(
        new_regime=Regime.low_vol,
        invocation_id="INV-3",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.low_vol,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=LOOSENING_INVOCATIONS,
        transition_started_invocation_id="INV-3",
        transition_origin_regime=Regime.normal,
    )


def test_loosening_from_tightening_uses_prior_active_regime_as_origin() -> None:
    prior = _state(
        active_regime=Regime.crisis,
        transition_state=RegimeTransitionState.TIGHTENING,
    )
    decision = compute_next_transition(
        new_regime=Regime.elevated,
        invocation_id="INV-4",
        prior_state=prior,
    )
    assert decision == NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.crisis,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=LOOSENING_INVOCATIONS,
        transition_started_invocation_id="INV-4",
        transition_origin_regime=Regime.crisis,
    )


# ---------------------------------------------------------------------------
# Regime ordering helper
# ---------------------------------------------------------------------------


def test_regime_ladder_index_orders_by_increasing_volatility() -> None:
    assert (
        regime_ladder_index(Regime.low_vol)
        < regime_ladder_index(Regime.normal)
        < regime_ladder_index(Regime.elevated)
        < regime_ladder_index(Regime.crisis)
    )


def test_regime_ladder_index_covers_every_regime_member() -> None:
    """Iterating the enum confirms no member is missing from the ladder."""
    for regime in Regime:
        # Should not raise.
        index = regime_ladder_index(regime)
        assert 0 <= index < len(_REGIME_VOLATILITY_LADDER)


def test_regime_volatility_ladder_length_matches_regime_enum() -> None:
    assert len(_REGIME_VOLATILITY_LADDER) == len(list(Regime))


# ---------------------------------------------------------------------------
# Determinism + immutability
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_identical_outputs() -> None:
    prior = _state(
        active_regime=Regime.elevated,
        transition_state=RegimeTransitionState.STABLE,
    )
    first = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    second = compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert first == second


def test_prior_state_argument_is_not_mutated() -> None:
    prior = _state(
        active_regime=Regime.normal,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        transition_started_invocation_id="INV-1",
        transition_origin_regime=Regime.elevated,
    )
    snapshot = dataclasses.replace(prior)
    compute_next_transition(
        new_regime=Regime.normal,
        invocation_id="INV-2",
        prior_state=prior,
    )
    assert prior == snapshot


# ---------------------------------------------------------------------------
# Property test: invariants hold across every scenario
# ---------------------------------------------------------------------------


def _every_scenario() -> tuple[NextTransitionDecision, ...]:
    """Run every behavioral scenario in this file and collect every decision.

    A property-style harness: any decision the state machine can emit is
    captured and checked against the structural invariants.
    """
    decisions: list[NextTransitionDecision] = []

    # Bootstrap.
    decisions.append(
        compute_next_transition(
            new_regime=Regime.normal,
            invocation_id="INV-1",
            prior_state=None,
        )
    )

    # Every (prior_state, prior_regime, new_regime) combination spanning the
    # decision-rule branches.
    prior_states = (
        RegimeTransitionState.STABLE,
        RegimeTransitionState.TIGHTENING,
        RegimeTransitionState.LOOSENING,
    )
    regimes = list(Regime)
    for prior_transition_state in prior_states:
        for prior_active in regimes:
            for new in regimes:
                if prior_transition_state == RegimeTransitionState.LOOSENING:
                    # Loosening priors require both Optionals populated. The
                    # origin must be a regime strictly higher than the active
                    # one so the prior was a real loosening; if the active is
                    # already at the top of the ladder, that combination is
                    # not a valid loosening prior and we skip it.
                    higher = [
                        r
                        for r in regimes
                        if regime_ladder_index(r) > regime_ladder_index(prior_active)
                    ]
                    if not higher:
                        continue
                    prior = _state(
                        active_regime=prior_active,
                        transition_state=prior_transition_state,
                        transition_invocations_remaining=2,
                        transition_started_invocation_id="INV-PRIOR",
                        transition_origin_regime=higher[-1],
                    )
                else:
                    prior = _state(
                        active_regime=prior_active,
                        transition_state=prior_transition_state,
                    )
                decisions.append(
                    compute_next_transition(
                        new_regime=new,
                        invocation_id="INV-NOW",
                        prior_state=prior,
                    )
                )

    return tuple(decisions)


@pytest.mark.parametrize("decision", _every_scenario())
def test_decision_invariants(decision: NextTransitionDecision) -> None:
    if decision.transition_state == RegimeTransitionState.STABLE:
        assert decision.transition_invocations_remaining == 0
        assert decision.transition_started_invocation_id is None
        assert decision.transition_origin_regime is None
    elif decision.transition_state == RegimeTransitionState.LOOSENING:
        assert decision.transition_started_invocation_id is not None
        assert decision.transition_origin_regime is not None
        assert decision.transition_invocations_remaining >= 1
    elif decision.transition_state == RegimeTransitionState.TIGHTENING:
        assert decision.transition_invocations_remaining == 0
        assert decision.transition_started_invocation_id is None
        assert decision.transition_origin_regime is None
