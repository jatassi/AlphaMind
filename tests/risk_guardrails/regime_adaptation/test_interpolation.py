"""Tests for the loosening-interpolation primitive (story 04c)."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from alphamind.portfolio_state.records.capital import RegimeTransitionState
from alphamind.risk_guardrails.regime_adaptation.interpolation import (
    interpolate_loosening_multipliers,
    resolve_active_multipliers,
)


def test_interpolation_at_remaining_three_is_one_third() -> None:
    """At remaining=3, loosening=3, fraction=1/3 of the way from origin to destination."""
    result = interpolate_loosening_multipliers(
        origin_multipliers={"position_max_size_pct": 0.40},
        destination_multipliers={"position_max_size_pct": 1.00},
        transition_invocations_remaining=3,
    )
    assert result["position_max_size_pct"] == pytest.approx(0.60, abs=1e-9)


def test_interpolation_at_remaining_two_is_two_thirds() -> None:
    """At remaining=2, loosening=3, fraction=2/3."""
    result = interpolate_loosening_multipliers(
        origin_multipliers={"position_max_size_pct": 0.40},
        destination_multipliers={"position_max_size_pct": 1.00},
        transition_invocations_remaining=2,
    )
    assert result["position_max_size_pct"] == pytest.approx(0.80, abs=1e-9)


def test_interpolation_at_remaining_one_is_full_destination() -> None:
    """At remaining=1, loosening=3, fraction=1 — last interpolation step is full destination."""
    result = interpolate_loosening_multipliers(
        origin_multipliers={"position_max_size_pct": 0.40},
        destination_multipliers={"position_max_size_pct": 1.00},
        transition_invocations_remaining=1,
    )
    assert result["position_max_size_pct"] == pytest.approx(1.00, abs=1e-9)


def test_multiple_rules_interpolate_independently() -> None:
    """Each rule's multiplier interpolates against its own (origin, destination) pair.

    Unchanged multipliers (origin == destination) interpolate to the same value
    (no-op), preserving the "drawdown limits don't loosen in low-vol" invariant
    without special-casing.
    """
    result = interpolate_loosening_multipliers(
        origin_multipliers={
            "position_max_size_pct": 0.70,
            "sector_concentration_pct": 0.80,
            "daily_drawdown_pct": 1.00,
        },
        destination_multipliers={
            "position_max_size_pct": 1.00,
            "sector_concentration_pct": 1.00,
            "daily_drawdown_pct": 1.00,
        },
        transition_invocations_remaining=2,
    )
    assert result["position_max_size_pct"] == pytest.approx(0.90, abs=1e-9)
    assert result["sector_concentration_pct"] == pytest.approx(
        0.80 + (1.00 - 0.80) * (2 / 3), abs=1e-9
    )
    assert result["daily_drawdown_pct"] == pytest.approx(1.00, abs=1e-9)


def test_interpolation_is_direction_agnostic() -> None:
    """The formula works for both loosening (origin > destination) and tightening directions.

    The orchestrator only invokes this primitive for ``LOOSENING``, but the
    primitive is total over both directions for testability.
    """
    result = interpolate_loosening_multipliers(
        origin_multipliers={"x": 1.20},
        destination_multipliers={"x": 0.40},
        transition_invocations_remaining=3,
    )
    assert result["x"] == pytest.approx(1.20 + (0.40 - 1.20) * (1 / 3), abs=1e-9)


def test_mismatched_key_sets_raise_value_error_naming_keys() -> None:
    """The function does not silently drop or invent rules; mismatch surfaces as an error."""
    with pytest.raises(ValueError, match="symmetric difference") as exc_info:
        interpolate_loosening_multipliers(
            origin_multipliers={"a": 0.5, "b": 0.6},
            destination_multipliers={"a": 1.0, "c": 0.8},
            transition_invocations_remaining=3,
        )
    msg = str(exc_info.value)
    # The symmetric difference is {"b", "c"} — both keys must be named.
    assert "b" in msg
    assert "c" in msg


def test_remaining_zero_raises_value_error() -> None:
    """remaining=0 means the prior state already transitioned to STABLE; primitive not called."""
    with pytest.raises(ValueError, match="transition_invocations_remaining"):
        interpolate_loosening_multipliers(
            origin_multipliers={"a": 0.5},
            destination_multipliers={"a": 1.0},
            transition_invocations_remaining=0,
        )


def test_remaining_above_loosening_invocations_raises_value_error() -> None:
    """remaining greater than loosening_invocations is outside the design's window."""
    with pytest.raises(ValueError, match="transition_invocations_remaining"):
        interpolate_loosening_multipliers(
            origin_multipliers={"a": 0.5},
            destination_multipliers={"a": 1.0},
            transition_invocations_remaining=4,
        )


def test_loosening_invocations_zero_raises_value_error() -> None:
    """loosening_invocations < 1 is invalid."""
    with pytest.raises(ValueError, match="loosening_invocations"):
        interpolate_loosening_multipliers(
            origin_multipliers={"a": 0.5},
            destination_multipliers={"a": 1.0},
            transition_invocations_remaining=1,
            loosening_invocations=0,
        )


def test_negative_multiplier_in_origin_raises_value_error() -> None:
    """Multiplier values must be strictly positive (mirrors RegimeConfig validator)."""
    with pytest.raises(ValueError, match="multiplier"):
        interpolate_loosening_multipliers(
            origin_multipliers={"a": -0.5},
            destination_multipliers={"a": 1.0},
            transition_invocations_remaining=3,
        )


def test_zero_multiplier_in_destination_raises_value_error() -> None:
    """Zero multiplier values are invalid for a guardrail multiplier."""
    with pytest.raises(ValueError, match="multiplier"):
        interpolate_loosening_multipliers(
            origin_multipliers={"a": 0.5},
            destination_multipliers={"a": 0.0},
            transition_invocations_remaining=3,
        )


def test_custom_loosening_invocations_two_step_window() -> None:
    """The formula generalizes to a custom loosening_invocations argument."""
    # remaining=2, loosening=2 → fraction = (2-2+1)/2 = 1/2
    halfway = interpolate_loosening_multipliers(
        origin_multipliers={"x": 0.40},
        destination_multipliers={"x": 1.00},
        transition_invocations_remaining=2,
        loosening_invocations=2,
    )
    assert halfway["x"] == pytest.approx(0.70, abs=1e-9)
    # remaining=1, loosening=2 → fraction = 1
    last = interpolate_loosening_multipliers(
        origin_multipliers={"x": 0.40},
        destination_multipliers={"x": 1.00},
        transition_invocations_remaining=1,
        loosening_invocations=2,
    )
    assert last["x"] == pytest.approx(1.00, abs=1e-9)


def test_determinism_identical_inputs_produce_identical_outputs() -> None:
    """Equal inputs produce equal outputs."""
    origin = {"a": 0.40, "b": 0.60}
    destination = {"a": 1.00, "b": 0.90}
    first = interpolate_loosening_multipliers(
        origin_multipliers=origin,
        destination_multipliers=destination,
        transition_invocations_remaining=2,
    )
    second = interpolate_loosening_multipliers(
        origin_multipliers=origin,
        destination_multipliers=destination,
        transition_invocations_remaining=2,
    )
    assert dict(first) == dict(second)


def test_input_mappings_are_not_mutated() -> None:
    """The function does not mutate its input mappings."""
    origin = {"a": 0.40, "b": 0.60}
    destination = {"a": 1.00, "b": 0.90}
    interpolate_loosening_multipliers(
        origin_multipliers=origin,
        destination_multipliers=destination,
        transition_invocations_remaining=2,
    )
    assert origin == {"a": 0.40, "b": 0.60}
    assert destination == {"a": 1.00, "b": 0.90}


def test_output_is_read_only_mapping_proxy() -> None:
    """The returned mapping is a MappingProxyType view; mutations not visible to caller."""
    result = interpolate_loosening_multipliers(
        origin_multipliers={"a": 0.40},
        destination_multipliers={"a": 1.00},
        transition_invocations_remaining=3,
    )
    assert isinstance(result, MappingProxyType)


def test_resolve_active_multipliers_stable_returns_active_map_verbatim() -> None:
    """STABLE state skips interpolation; the active map is returned verbatim."""
    active = {"position_max_size_pct": 1.00, "sector_concentration_pct": 1.00}
    result = resolve_active_multipliers(
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        active_regime_multipliers=active,
        transition_origin_multipliers=None,
    )
    assert dict(result) == active


def test_resolve_active_multipliers_tightening_returns_active_map_verbatim() -> None:
    """TIGHTENING applies destination multipliers immediately — no interpolation."""
    active = {"position_max_size_pct": 0.40}
    result = resolve_active_multipliers(
        transition_state=RegimeTransitionState.TIGHTENING,
        transition_invocations_remaining=0,
        active_regime_multipliers=active,
        transition_origin_multipliers=None,
    )
    assert dict(result) == active


def test_resolve_active_multipliers_loosening_with_none_origin_raises() -> None:
    """LOOSENING with origin=None violates the story-02 invariant; surfaces as ValueError."""
    with pytest.raises(ValueError, match="transition_origin_multipliers"):
        resolve_active_multipliers(
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=3,
            active_regime_multipliers={"a": 1.0},
            transition_origin_multipliers=None,
        )


def test_resolve_active_multipliers_loosening_returns_interpolated_map() -> None:
    """LOOSENING dispatches to the interpolation primitive."""
    result = resolve_active_multipliers(
        transition_state=RegimeTransitionState.LOOSENING,
        transition_invocations_remaining=3,
        active_regime_multipliers={"position_max_size_pct": 1.00},
        transition_origin_multipliers={"position_max_size_pct": 0.40},
    )
    assert result["position_max_size_pct"] == pytest.approx(0.60, abs=1e-9)


def test_top_level_package_reexports_interpolation_symbols() -> None:
    """Both interpolation primitives are reachable from the top-level package."""
    import alphamind.risk_guardrails.regime_adaptation as regime_adaptation

    assert hasattr(regime_adaptation, "interpolate_loosening_multipliers")
    assert hasattr(regime_adaptation, "resolve_active_multipliers")
