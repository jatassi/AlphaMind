"""Tests for the active risk parameter set assembler (story 08)."""

from __future__ import annotations

import pytest

import alphamind.risk_guardrails.regime_adaptation as regime_adaptation
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.risk_guardrails.regime_adaptation import RuleMetadata
from alphamind.risk_guardrails.regime_adaptation.parameter_set import (
    assemble_active_risk_parameter_set,
)
from alphamind.risk_guardrails.regime_adaptation.types import NextTransitionDecision


def _decision_stable_normal() -> NextTransitionDecision:
    return NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )


def _rule_metadata() -> dict[str, RuleMetadata]:
    return {
        "position_max_size_pct": RuleMetadata(
            rule_id="position_max_size_pct",
            label="Position max size",
            unit="pct",
        ),
        "sector_concentration_pct": RuleMetadata(
            rule_id="sector_concentration_pct",
            label="Sector concentration",
            unit="pct",
        ),
    }


def _assemble_normal_no_overlays(
    *,
    prior_parameter_set: ActiveRiskParameterSet | None = None,
) -> ActiveRiskParameterSet:
    """Reusable assembly call for the normal-regime, no-overlay base case."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    return assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=prior_parameter_set,
    )


def test_bootstrap_returns_parameter_change_flag_false() -> None:
    """Bootstrap (prior_parameter_set=None) yields parameter_change_flag=False."""
    result = _assemble_normal_no_overlays()

    assert isinstance(result, ActiveRiskParameterSet)
    assert result.regime_label == RegimeLabel.NORMAL
    assert result.transition_state == RegimeTransitionState.STABLE
    assert result.transition_invocations_remaining == 0
    assert result.parameter_change_flag is False
    assert result.active_overlays == ()


def test_same_as_prior_returns_parameter_change_flag_false() -> None:
    """Identical inputs and prior set yield parameter_change_flag=False."""
    prior = _assemble_normal_no_overlays()
    current = _assemble_normal_no_overlays(prior_parameter_set=prior)

    assert current.parameter_change_flag is False


def test_regime_change_since_prior_returns_parameter_change_flag_true() -> None:
    """Prior in NORMAL, current in ELEVATED → parameter_change_flag=True."""
    prior = _assemble_normal_no_overlays()
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    elevated_multipliers = {
        "position_max_size_pct": 0.70,
        "sector_concentration_pct": 0.80,
    }
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    decision = NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    current = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=elevated_multipliers,
        interpolated_multipliers=elevated_multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=prior,
    )

    assert current.regime_label == RegimeLabel.ELEVATED
    assert current.transition_state == RegimeTransitionState.TIGHTENING
    assert current.parameter_change_flag is True


def test_overlay_activated_since_prior_returns_parameter_change_flag_true() -> None:
    """Prior with no overlays, current with stress overlay → parameter_change_flag=True."""
    prior = _assemble_normal_no_overlays()
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 0.85}

    current = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=prior,
    )

    assert current.active_overlays == ("stress",)
    assert current.parameter_change_flag is True


def test_overlay_multiplier_composition_is_multiplicative() -> None:
    """Entry's value equals base * interpolated_multiplier * overlay_multiplier."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    elevated_multipliers = {
        "position_max_size_pct": 0.70,
        "sector_concentration_pct": 0.80,
    }
    overlay_composed = {
        "position_max_size_pct": 1.0,
        "sector_concentration_pct": 0.85,
    }
    decision = NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    result = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=elevated_multipliers,
        interpolated_multipliers=elevated_multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    sector_entry = next(e for e in result.entries if e.rule_id == "sector_concentration_pct")
    assert sector_entry.base_value == pytest.approx(25.0, abs=1e-9)
    assert sector_entry.value == pytest.approx(25.0 * 0.80 * 0.85, abs=1e-9)
    assert sector_entry.regime_multiplier_applied == pytest.approx(0.80 * 0.85, abs=1e-9)


def test_multiple_overlays_compose_multiplicatively() -> None:
    """Two overlays touching the same rule compose by product of multipliers."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    # pre_event=0.80 and stress=0.90 both touching position_max_size_pct → product 0.72.
    overlay_composed = {
        "position_max_size_pct": 0.80 * 0.90,
        "sector_concentration_pct": 1.0,
    }

    result = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.pre_event, Overlay.stress),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    position_entry = next(e for e in result.entries if e.rule_id == "position_max_size_pct")
    assert position_entry.value == pytest.approx(5.0 * 0.72, abs=1e-9)
    assert position_entry.regime_multiplier_applied == pytest.approx(0.72, abs=1e-9)


def test_loosening_interpolation_is_reflected_in_entry_value() -> None:
    """During LOOSENING, the entry uses the interpolated (not active) multiplier."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    # Active regime is normal (1.00), but interpolation is one-third of the way
    # from crisis-origin (0.40) to normal-destination (1.00) for position_max_size_pct.
    active_multipliers = {"position_max_size_pct": 1.00, "sector_concentration_pct": 1.00}
    interpolated_multipliers = {
        "position_max_size_pct": 0.60,
        "sector_concentration_pct": 1.00,
    }
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    decision = NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=Regime.crisis,
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        transition_started_invocation_id="inv-001",
        transition_origin_regime=Regime.crisis,
    )

    result = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=active_multipliers,
        interpolated_multipliers=interpolated_multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    position_entry = next(e for e in result.entries if e.rule_id == "position_max_size_pct")
    assert position_entry.regime_multiplier_applied == pytest.approx(0.60, abs=1e-9)
    assert position_entry.value == pytest.approx(5.0 * 0.60, abs=1e-9)
    assert result.transition_state == RegimeTransitionState.LOOSENING
    assert result.transition_invocations_remaining == 3


@pytest.mark.parametrize(
    ("regime", "expected_label"),
    [
        (Regime.low_vol, RegimeLabel.LOW_VOL),
        (Regime.normal, RegimeLabel.NORMAL),
        (Regime.elevated, RegimeLabel.ELEVATED),
        (Regime.crisis, RegimeLabel.CRISIS),
    ],
)
def test_regime_label_mapping_is_correct_for_every_regime(
    regime: Regime, expected_label: RegimeLabel
) -> None:
    """Every Regime member maps to the corresponding portfolio-state RegimeLabel."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    decision = NextTransitionDecision(
        active_regime=regime,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    result = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    assert result.regime_label == expected_label


@pytest.mark.parametrize(
    ("transition_state", "remaining", "started_id", "origin_regime"),
    [
        (RegimeTransitionState.STABLE, 0, None, None),
        (RegimeTransitionState.TIGHTENING, 0, None, None),
        (RegimeTransitionState.LOOSENING, 1, "inv-001", Regime.crisis),
        (RegimeTransitionState.LOOSENING, 2, "inv-001", Regime.crisis),
        (RegimeTransitionState.LOOSENING, 3, "inv-001", Regime.crisis),
    ],
)
def test_transition_state_and_invocations_remaining_passthrough(
    transition_state: RegimeTransitionState,
    remaining: int,
    started_id: str | None,
    origin_regime: Regime | None,
) -> None:
    """``transition_state`` and ``transition_invocations_remaining`` pass through verbatim."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    decision = NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=transition_state,
        transition_invocations_remaining=remaining,
        transition_started_invocation_id=started_id,
        transition_origin_regime=origin_regime,
    )

    result = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    assert result.transition_state == transition_state
    assert result.transition_invocations_remaining == remaining


def test_entries_are_sorted_alphabetically_by_rule_id() -> None:
    """Entries appear in alphabetical order regardless of input map ordering."""
    base = {"sector_concentration_pct": 25.0, "position_max_size_pct": 5.0}
    multipliers = {"sector_concentration_pct": 1.0, "position_max_size_pct": 1.0}
    overlay_composed = {"sector_concentration_pct": 1.0, "position_max_size_pct": 1.0}

    result = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    rule_ids = [entry.rule_id for entry in result.entries]
    assert rule_ids == sorted(rule_ids)
    assert rule_ids == ["position_max_size_pct", "sector_concentration_pct"]


def test_active_overlays_are_sorted_alphabetically_as_strings() -> None:
    """Input ``(Overlay.stress, Overlay.pre_event)`` → output ``("pre_event", "stress")``."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}

    result = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress, Overlay.pre_event),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    assert result.active_overlays == ("pre_event", "stress")


def test_mismatched_interpolated_and_base_keys_raise_value_error() -> None:
    """interpolated_multipliers and base_profile_rule_values must align."""
    base = {"position_max_size_pct": 5.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}

    with pytest.raises(ValueError, match="identical key sets"):
        assemble_active_risk_parameter_set(
            next_transition=_decision_stable_normal(),
            base_profile_rule_values=base,
            active_regime_multipliers=multipliers,
            interpolated_multipliers=multipliers,
            overlay_multipliers_composed=overlay_composed,
            active_overlays=(),
            rule_metadata=_rule_metadata(),
            prior_parameter_set=None,
        )


def test_extra_overlay_key_raises_value_error() -> None:
    """overlay_multipliers_composed must not contain rules absent from interpolated_multipliers."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {
        "position_max_size_pct": 1.0,
        "sector_concentration_pct": 1.0,
        "phantom_rule": 0.5,
    }

    with pytest.raises(ValueError, match="phantom_rule"):
        assemble_active_risk_parameter_set(
            next_transition=_decision_stable_normal(),
            base_profile_rule_values=base,
            active_regime_multipliers=multipliers,
            interpolated_multipliers=multipliers,
            overlay_multipliers_composed=overlay_composed,
            active_overlays=(),
            rule_metadata=_rule_metadata(),
            prior_parameter_set=None,
        )


def test_missing_rule_metadata_raises_value_error() -> None:
    """Every rule_id in interpolated_multipliers must appear in rule_metadata."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    metadata = {
        "position_max_size_pct": RuleMetadata(
            rule_id="position_max_size_pct",
            label="Position max size",
            unit="pct",
        ),
    }

    with pytest.raises(ValueError, match="sector_concentration_pct"):
        assemble_active_risk_parameter_set(
            next_transition=_decision_stable_normal(),
            base_profile_rule_values=base,
            active_regime_multipliers=multipliers,
            interpolated_multipliers=multipliers,
            overlay_multipliers_composed=overlay_composed,
            active_overlays=(),
            rule_metadata=metadata,
            prior_parameter_set=None,
        )


def test_overlay_missing_for_a_rule_defaults_to_one() -> None:
    """A rule absent from overlay_multipliers_composed defaults to multiplier 1.0."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    # Only sector_concentration_pct present in the overlay map.
    overlay_composed = {"sector_concentration_pct": 0.85}

    result = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    position_entry = next(e for e in result.entries if e.rule_id == "position_max_size_pct")
    assert position_entry.regime_multiplier_applied == pytest.approx(1.0, abs=1e-9)
    assert position_entry.value == pytest.approx(5.0, abs=1e-9)
    sector_entry = next(e for e in result.entries if e.rule_id == "sector_concentration_pct")
    assert sector_entry.regime_multiplier_applied == pytest.approx(0.85, abs=1e-9)


def test_inputs_are_not_mutated() -> None:
    """The function does not mutate any input mapping."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 0.70, "sector_concentration_pct": 0.80}
    overlay_composed = {"sector_concentration_pct": 0.85}
    metadata = _rule_metadata()
    decision = NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    base_snapshot = dict(base)
    multipliers_snapshot = dict(multipliers)
    overlay_snapshot = dict(overlay_composed)
    metadata_snapshot = dict(metadata)

    assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=metadata,
        prior_parameter_set=None,
    )

    assert base == base_snapshot
    assert multipliers == multipliers_snapshot
    assert overlay_composed == overlay_snapshot
    assert metadata == metadata_snapshot


def test_determinism_identical_inputs_produce_identical_outputs() -> None:
    """The function is pure: same inputs yield equal results."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 0.70, "sector_concentration_pct": 0.80}
    overlay_composed = {"sector_concentration_pct": 0.85}
    decision = NextTransitionDecision(
        active_regime=Regime.elevated,
        prior_regime=Regime.normal,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    first = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )
    second = assemble_active_risk_parameter_set(
        next_transition=decision,
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(Overlay.stress,),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    assert first == second


def test_entry_metadata_is_pulled_from_rule_metadata() -> None:
    """Each entry's ``rule_label`` and ``unit`` come from the supplied rule_metadata."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}

    result = assemble_active_risk_parameter_set(
        next_transition=_decision_stable_normal(),
        base_profile_rule_values=base,
        active_regime_multipliers=multipliers,
        interpolated_multipliers=multipliers,
        overlay_multipliers_composed=overlay_composed,
        active_overlays=(),
        rule_metadata=_rule_metadata(),
        prior_parameter_set=None,
    )

    by_id = {entry.rule_id: entry for entry in result.entries}
    assert by_id["position_max_size_pct"].rule_label == "Position max size"
    assert by_id["position_max_size_pct"].unit == "pct"
    assert by_id["sector_concentration_pct"].rule_label == "Sector concentration"
    assert by_id["sector_concentration_pct"].unit == "pct"


def test_stable_with_nonzero_remaining_bubbles_pydantic_validator_error() -> None:
    """The upstream ActiveRiskParameterSet validator rejects STABLE + non-zero remaining."""
    base = {"position_max_size_pct": 5.0, "sector_concentration_pct": 25.0}
    multipliers = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    overlay_composed = {"position_max_size_pct": 1.0, "sector_concentration_pct": 1.0}
    decision = NextTransitionDecision(
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=1,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
    )

    with pytest.raises(ValueError, match="STABLE"):
        assemble_active_risk_parameter_set(
            next_transition=decision,
            base_profile_rule_values=base,
            active_regime_multipliers=multipliers,
            interpolated_multipliers=multipliers,
            overlay_multipliers_composed=overlay_composed,
            active_overlays=(),
            rule_metadata=_rule_metadata(),
            prior_parameter_set=None,
        )


def test_active_risk_parameter_set_rejects_duplicate_rule_ids() -> None:
    """The upstream ActiveRiskParameterSet validator still rejects duplicate rule_ids.

    The assembler itself cannot emit duplicates (its inputs are dicts), but the
    invariant remains protected at the model layer.
    """
    duplicate_entries = (
        ActiveRiskParameterEntry(
            rule_id="position_max_size_pct",
            rule_label="Position max size",
            value=5.0,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=5.0,
        ),
        ActiveRiskParameterEntry(
            rule_id="position_max_size_pct",
            rule_label="Duplicate",
            value=4.0,
            unit="pct",
            regime_multiplier_applied=0.8,
            base_value=5.0,
        ),
    )
    with pytest.raises(ValueError, match="unique rule_id"):
        ActiveRiskParameterSet(
            regime_label=RegimeLabel.NORMAL,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=duplicate_entries,
            active_overlays=(),
        )


def test_assemble_active_risk_parameter_set_is_reexported_from_package() -> None:
    """The assembler is reachable from the top-level package."""
    assert regime_adaptation.assemble_active_risk_parameter_set is (
        assemble_active_risk_parameter_set
    )
