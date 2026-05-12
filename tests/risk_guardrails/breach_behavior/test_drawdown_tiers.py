"""Tests for cumulative drawdown tier classifier and progressive override applier."""

from __future__ import annotations

import pytest

from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    apply_progressive_tier_overrides,
    classify_cumulative_drawdown_tier,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _shipped_tiers() -> tuple[ProgressiveTier, ...]:
    """The three-tier configuration shipped in config/guardrails.yaml."""
    return (
        ProgressiveTier(trigger_pct=8.0, max_position_size_pct=3.0, max_gross_pct=80.0),
        ProgressiveTier(trigger_pct=10.0, max_position_size_pct=2.0, max_gross_pct=60.0),
        ProgressiveTier(trigger_pct=12.0, full_halt=True),
    )


def _two_tier_no_middle() -> tuple[ProgressiveTier, ...]:
    """Two-tier configuration: tier 1 + full-halt only, no middle tier."""
    return (
        ProgressiveTier(trigger_pct=8.0, max_position_size_pct=3.0, max_gross_pct=80.0),
        ProgressiveTier(trigger_pct=12.0, full_halt=True),
    )


def _build_parameter_set(
    *,
    position_max_size_value: float = 5.0,
    gross_exposure_value: float = 120.0,
    regime_multiplier_applied: float = 1.0,
    base_position_max_size: float = 5.0,
    base_gross_exposure: float = 120.0,
    extra_entries: tuple[ActiveRiskParameterEntry, ...] = (),
    active_overlays: tuple[str, ...] = (),
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    transition_state: RegimeTransitionState = RegimeTransitionState.STABLE,
    transition_invocations_remaining: int = 0,
    parameter_change_flag: bool = False,
) -> ActiveRiskParameterSet:
    """Build an ActiveRiskParameterSet for tests."""
    base_entries = (
        ActiveRiskParameterEntry(
            rule_id="position_max_size_pct",
            rule_label="Max position size (% of portfolio)",
            value=position_max_size_value,
            unit="pct",
            regime_multiplier_applied=regime_multiplier_applied,
            base_value=base_position_max_size,
        ),
        ActiveRiskParameterEntry(
            rule_id="gross_exposure_pct",
            rule_label="Max gross exposure (% of portfolio)",
            value=gross_exposure_value,
            unit="pct",
            regime_multiplier_applied=regime_multiplier_applied,
            base_value=base_gross_exposure,
        ),
    )
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        parameter_change_flag=parameter_change_flag,
        entries=base_entries + extra_entries,
        active_overlays=active_overlays,
    )


# ---------------------------------------------------------------------------
# Tier classification — boundaries
# ---------------------------------------------------------------------------


def test_below_tier_one_returns_none() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=7.99,
            progressive_tiers=_shipped_tiers(),
        )
        is None
    )


def test_tier_one_boundary_is_inclusive() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=8.0,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.CONSTRAINED
    )


def test_tier_one_mid_range() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=9.5,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.CONSTRAINED
    )


def test_tier_two_boundary() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=10.0,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.HEAVILY_CONSTRAINED
    )


def test_tier_two_mid_range() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=11.5,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.HEAVILY_CONSTRAINED
    )


def test_tier_three_boundary() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=12.0,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.FULL_HALT
    )


def test_tier_three_well_past() -> None:
    assert (
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=20.0,
            progressive_tiers=_shipped_tiers(),
        )
        == DrawdownTier.FULL_HALT
    )


def test_recovery_sequence_no_hysteresis() -> None:
    tiers = _shipped_tiers()
    sequence = [13.0, 11.5, 9.5, 7.5]
    results = [
        classify_cumulative_drawdown_tier(current_drawdown_pct=value, progressive_tiers=tiers)
        for value in sequence
    ]
    assert results == [
        DrawdownTier.FULL_HALT,
        DrawdownTier.HEAVILY_CONSTRAINED,
        DrawdownTier.CONSTRAINED,
        None,
    ]


def test_negative_drawdown_rejected() -> None:
    with pytest.raises(ValueError, match=r"-1\.0"):
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=-1.0,
            progressive_tiers=_shipped_tiers(),
        )


def test_empty_tiers_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        classify_cumulative_drawdown_tier(
            current_drawdown_pct=8.0,
            progressive_tiers=(),
        )


def test_two_tier_no_middle_skips_heavily_constrained() -> None:
    tiers = _two_tier_no_middle()
    assert (
        classify_cumulative_drawdown_tier(current_drawdown_pct=9.0, progressive_tiers=tiers)
        == DrawdownTier.CONSTRAINED
    )
    assert (
        classify_cumulative_drawdown_tier(current_drawdown_pct=12.5, progressive_tiers=tiers)
        == DrawdownTier.FULL_HALT
    )


# ---------------------------------------------------------------------------
# Override application
# ---------------------------------------------------------------------------


def test_override_tier_none_returns_input_unchanged() -> None:
    input_set = _build_parameter_set(
        active_overlays=("pre_event", "stress"),
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=None,
        progressive_tiers=_shipped_tiers(),
    )
    assert output == input_set


def _entry_by_id(parameter_set: ActiveRiskParameterSet, rule_id: str) -> ActiveRiskParameterEntry:
    for entry in parameter_set.entries:
        if entry.rule_id == rule_id:
            return entry
    raise AssertionError(f"missing entry {rule_id!r}")


def test_override_constrained_applies_tier_one() -> None:
    extra = (
        ActiveRiskParameterEntry(
            rule_id="sector_concentration_pct",
            rule_label="Max sector concentration",
            value=25.0,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=25.0,
        ),
    )
    input_set = _build_parameter_set(
        position_max_size_value=5.0,
        gross_exposure_value=120.0,
        regime_multiplier_applied=1.0,
        base_position_max_size=5.0,
        base_gross_exposure=120.0,
        extra_entries=extra,
    )

    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )

    pos = _entry_by_id(output, "position_max_size_pct")
    gross = _entry_by_id(output, "gross_exposure_pct")
    sector = _entry_by_id(output, "sector_concentration_pct")

    assert pos.value == 3.0
    assert pos.regime_multiplier_applied == 1.0
    assert pos.base_value == 5.0
    assert gross.value == 80.0
    assert gross.regime_multiplier_applied == 1.0
    assert gross.base_value == 120.0
    # Other entries flow through unchanged.
    assert sector.value == 25.0
    assert sector.base_value == 25.0
    assert "cumulative_drawdown_tier_1" in output.active_overlays
    # Metadata fields preserved.
    assert output.regime_label == input_set.regime_label
    assert output.transition_state == input_set.transition_state
    assert output.transition_invocations_remaining == 0
    assert output.parameter_change_flag is False


def test_override_heavily_constrained_applies_tier_two() -> None:
    input_set = _build_parameter_set(
        position_max_size_value=5.0,
        gross_exposure_value=120.0,
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.HEAVILY_CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    assert _entry_by_id(output, "position_max_size_pct").value == 2.0
    assert _entry_by_id(output, "gross_exposure_pct").value == 60.0
    assert "cumulative_drawdown_tier_2" in output.active_overlays


def test_override_full_halt_does_not_change_rule_values() -> None:
    input_set = _build_parameter_set(
        position_max_size_value=5.0,
        gross_exposure_value=120.0,
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.FULL_HALT,
        progressive_tiers=_shipped_tiers(),
    )
    assert _entry_by_id(output, "position_max_size_pct").value == 5.0
    assert _entry_by_id(output, "gross_exposure_pct").value == 120.0
    assert "cumulative_drawdown_tier_3" in output.active_overlays


def test_override_never_loosens_when_regime_tighter() -> None:
    # Crisis-regime-resolved values already tighter than tier 1's 3.0 / 80.0.
    input_set = _build_parameter_set(
        position_max_size_value=2.5,
        gross_exposure_value=70.0,
        regime_multiplier_applied=0.5,
        base_position_max_size=5.0,
        base_gross_exposure=120.0,
        regime_label=RegimeLabel.CRISIS,
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    assert _entry_by_id(output, "position_max_size_pct").value == 2.5
    assert _entry_by_id(output, "gross_exposure_pct").value == 70.0


def test_override_missing_position_max_size_raises_keyerror() -> None:
    incomplete = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="gross_exposure_pct",
                rule_label="Max gross exposure",
                value=120.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=120.0,
            ),
        ),
        active_overlays=(),
    )
    with pytest.raises(KeyError, match="position_max_size_pct"):
        apply_progressive_tier_overrides(
            active_risk_parameters=incomplete,
            tier=DrawdownTier.CONSTRAINED,
            progressive_tiers=_shipped_tiers(),
        )


def test_override_insufficient_tier_coverage_raises_valueerror() -> None:
    # Only one non-halt tier; HEAVILY_CONSTRAINED requires two.
    only_tier_one = (
        ProgressiveTier(trigger_pct=8.0, max_position_size_pct=3.0, max_gross_pct=80.0),
    )
    input_set = _build_parameter_set()
    with pytest.raises(ValueError, match="HEAVILY_CONSTRAINED"):
        apply_progressive_tier_overrides(
            active_risk_parameters=input_set,
            tier=DrawdownTier.HEAVILY_CONSTRAINED,
            progressive_tiers=only_tier_one,
        )


def test_override_idempotent_under_repeated_application() -> None:
    input_set = _build_parameter_set(
        active_overlays=("pre_event",),
    )
    once = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    twice = apply_progressive_tier_overrides(
        active_risk_parameters=once,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    assert once == twice
    # Tag must not duplicate.
    assert once.active_overlays.count("cumulative_drawdown_tier_1") == 1


def test_override_active_overlays_alphabetically_sorted() -> None:
    input_set = _build_parameter_set(
        active_overlays=("pre_event", "stress"),
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    assert output.active_overlays == (
        "cumulative_drawdown_tier_1",
        "pre_event",
        "stress",
    )


def test_override_composes_with_regime_resolved_input() -> None:
    # Elevated regime resolved value (3.5) is between base (5.0) and tier 1 (3.0).
    input_set = _build_parameter_set(
        position_max_size_value=3.5,
        gross_exposure_value=100.0,
        regime_multiplier_applied=0.7,
        base_position_max_size=5.0,
        base_gross_exposure=120.0,
        regime_label=RegimeLabel.ELEVATED,
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=2,
    )
    output = apply_progressive_tier_overrides(
        active_risk_parameters=input_set,
        tier=DrawdownTier.CONSTRAINED,
        progressive_tiers=_shipped_tiers(),
    )
    pos = _entry_by_id(output, "position_max_size_pct")
    assert pos.value == 3.0  # Tier override wins.
    assert pos.regime_multiplier_applied == 1.0
    assert pos.base_value == 5.0  # Base value untouched.
    # Regime label and transition state preserved.
    assert output.regime_label == RegimeLabel.ELEVATED
    assert output.transition_state == RegimeTransitionState.TIGHTENING
    assert output.transition_invocations_remaining == 2


def test_classifier_and_override_are_deterministic() -> None:
    tiers = _shipped_tiers()
    input_set = _build_parameter_set(active_overlays=("stress",))

    classifier_results = {
        classify_cumulative_drawdown_tier(current_drawdown_pct=11.0, progressive_tiers=tiers)
        for _ in range(100)
    }
    override_results = {
        apply_progressive_tier_overrides(
            active_risk_parameters=input_set,
            tier=DrawdownTier.HEAVILY_CONSTRAINED,
            progressive_tiers=tiers,
        )
        for _ in range(100)
    }
    assert len(classifier_results) == 1
    assert len(override_results) == 1
