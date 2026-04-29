"""Tests for the distillation->guardrail regime mapping (story 03).

Story 03 lands the pure function ``map_distillation_to_guardrail_regime`` and
its config-side adapter ``from_regime_classification``. Every test here exercises
the public interface only — no I/O, no Session, no filesystem fixtures.
"""

from __future__ import annotations

import dataclasses

import pytest

from alphamind.config.models.distillation import RegimeClassification
from alphamind.config.models.regimes import Regime
from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
from alphamind.risk_guardrails.regime_adaptation import VixBoundaryThresholds
from alphamind.risk_guardrails.regime_adaptation.regime_mapping import (
    from_regime_classification,
    map_distillation_to_guardrail_regime,
)


def _baseline_thresholds() -> VixBoundaryThresholds:
    """The canonical four-tier boundaries from threshold-calibration.md."""
    return VixBoundaryThresholds(
        low_vol_vix_max=14.0,
        normal_vix_max=22.0,
        elevated_vix_max=35.0,
    )


@pytest.mark.parametrize("vix_level", [5.0, 14.0, 50.0])
def test_low_vol_compression_always_maps_to_low_vol(vix_level: float) -> None:
    """``LOW_VOL_COMPRESSION`` is definitionally low-vol regardless of VIX."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.LOW_VOL_COMPRESSION,
        vix_level=vix_level,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.low_vol


@pytest.mark.parametrize("vix_level", [10.0, 35.0, 80.0])
def test_crisis_spike_always_maps_to_crisis(vix_level: float) -> None:
    """``CRISIS_SPIKE`` is definitionally crisis regardless of VIX."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.CRISIS_SPIKE,
        vix_level=vix_level,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.crisis


def test_vol_expansion_mid_normal_band_maps_to_normal() -> None:
    """``VOL_EXPANSION`` at VIX=18 (mid-normal-band) resolves to ``normal``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=18.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.normal


def test_vol_expansion_mid_elevated_band_maps_to_elevated() -> None:
    """``VOL_EXPANSION`` at VIX=28 (mid-elevated-band) resolves to ``elevated``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=28.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.elevated


def test_vol_expansion_below_low_vol_max_maps_to_low_vol() -> None:
    """``VOL_EXPANSION`` at VIX=8 falls back to ``low_vol`` via the band table.

    Distillation rarely emits ``VOL_EXPANSION`` below the normal band, but the
    mapper is total over its input domain — every (label, VIX) pair resolves
    to exactly one guardrail regime.
    """
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=8.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.low_vol


def test_vol_expansion_above_elevated_max_maps_to_crisis() -> None:
    """``VOL_EXPANSION`` at VIX=50 falls back to ``crisis`` via the band table."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=50.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.crisis


def test_vol_normalization_mid_normal_band_maps_to_normal() -> None:
    """``VOL_NORMALIZATION`` at VIX=18 (mid-normal-band) resolves to ``normal``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_NORMALIZATION,
        vix_level=18.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.normal


def test_vol_normalization_mid_elevated_band_maps_to_elevated() -> None:
    """``VOL_NORMALIZATION`` at VIX=28 (mid-elevated-band) resolves to ``elevated``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_NORMALIZATION,
        vix_level=28.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.elevated


# ---------------------------------------------------------------------------
# Boundary semantics — `<=` at the upper end of each band, matching
# ``classify_vix_band``. Locked here so a future refactor that changes either
# function reveals the coupling.
# ---------------------------------------------------------------------------


def test_vix_at_low_vol_max_boundary_maps_to_low_vol() -> None:
    """Boundary: VIX=14.0 (== ``low_vol_vix_max``) -> ``low_vol``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=14.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.low_vol


def test_vix_at_normal_max_boundary_maps_to_normal() -> None:
    """Boundary: VIX=22.0 (== ``normal_vix_max``) -> ``normal``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=22.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.normal


def test_vix_at_elevated_max_boundary_maps_to_elevated() -> None:
    """Boundary: VIX=35.0 (== ``elevated_vix_max``) -> ``elevated``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=35.0,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.elevated


def test_vix_one_tick_above_normal_max_maps_to_elevated() -> None:
    """Strict-inequality semantics: VIX=22.001 -> ``elevated``."""
    result = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=22.001,
        vix_thresholds=_baseline_thresholds(),
    )
    assert result is Regime.elevated


# ---------------------------------------------------------------------------
# Negative-VIX guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(DistillationRegimeLabel))
def test_negative_vix_level_raises_regardless_of_label(
    label: DistillationRegimeLabel,
) -> None:
    """A negative ``vix_level`` is upstream data corruption; raise rather than
    silently classify it as ``low_vol``. The error names the field so the
    operator can find the upstream cause."""
    with pytest.raises(ValueError, match="vix_level"):
        map_distillation_to_guardrail_regime(
            distillation_label=label,
            vix_level=-1.0,
            vix_thresholds=_baseline_thresholds(),
        )


# ---------------------------------------------------------------------------
# from_regime_classification adapter
# ---------------------------------------------------------------------------


def _baseline_regime_classification() -> RegimeClassification:
    """A canonical ``RegimeClassification`` mirroring threshold-calibration.md."""
    return RegimeClassification(
        regime_low_vol_vix_max=14.0,
        regime_normal_vix_min=14.0,
        regime_normal_vix_max=22.0,
        regime_elevated_vix_min=22.0,
        regime_elevated_vix_max=35.0,
        regime_crisis_vix_min=35.0,
        regime_term_structure_backwardation_threshold=0.0,
        regime_vvix_high_percentile=80,
        regime_vvix_low_percentile=20,
    )


def test_from_regime_classification_mirrors_three_thresholds_verbatim() -> None:
    """The adapter copies the three VIX boundaries verbatim from
    ``RegimeClassification``; no re-derivation, no rounding."""
    rc = _baseline_regime_classification()
    thresholds = from_regime_classification(rc)
    assert thresholds.low_vol_vix_max == rc.regime_low_vol_vix_max
    assert thresholds.normal_vix_max == rc.regime_normal_vix_max
    assert thresholds.elevated_vix_max == rc.regime_elevated_vix_max


# ---------------------------------------------------------------------------
# Determinism / purity
# ---------------------------------------------------------------------------


def test_mapper_is_deterministic_and_does_not_mutate_thresholds() -> None:
    """Identical inputs produce identical outputs; calling the mapper does not
    mutate ``vix_thresholds`` (the dataclass is frozen, but lock the contract
    so a future refactor cannot quietly relax it)."""
    thresholds = _baseline_thresholds()
    snapshot = dataclasses.replace(thresholds)
    first = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=18.0,
        vix_thresholds=thresholds,
    )
    second = map_distillation_to_guardrail_regime(
        distillation_label=DistillationRegimeLabel.VOL_EXPANSION,
        vix_level=18.0,
        vix_thresholds=thresholds,
    )
    assert first is second
    assert thresholds == snapshot
