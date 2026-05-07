"""Distillation->guardrail regime mapping (story 03).

The distillation layer's four-tier regime classification labels and the
guardrail-side ``Regime`` enum are not 1:1 — ``VOL_EXPANSION`` covers both the
``normal`` and ``elevated`` VIX bands, and ``VOL_NORMALIZATION`` is a
transitioning-down state that resolves to whichever band the current VIX
implies. This module is the pure function that bridges them, plus the small
adapter that pulls the three VIX boundaries out of the distillation Pydantic
config into the canonical :class:`VixBoundaryThresholds` dataclass.

Reading:
``docs/implementation/06-risk-guardrails/regime-adaptation/03-distillation-to-guardrail-regime-mapping.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind.config.models.distillation import RegimeClassification
from alphamind.config.models.regimes import Regime
from alphamind.risk_guardrails.regime_adaptation.types import VixBoundaryThresholds

if TYPE_CHECKING:
    # Eager import would trigger ``distillation.baselines``, which holds a
    # latent cycle with ``persistence.models`` (which now re-exports
    # ``RegimeTransitionState`` from this package). ``DistillationRegimeLabel``
    # and ``VixBand`` are deferred to call-time inside the function below.
    from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
    from alphamind.distillation.regime import VixBand


def map_distillation_to_guardrail_regime(
    *,
    distillation_label: DistillationRegimeLabel,
    vix_level: float,
    vix_thresholds: VixBoundaryThresholds,
) -> Regime:
    """Map a distillation regime label + VIX level to the guardrail ``Regime``.

    ``LOW_VOL_COMPRESSION`` and ``CRISIS_SPIKE`` are definitionally resolved
    by the distillation layer; the mapper trusts the label and returns the
    matching guardrail regime regardless of ``vix_level``.

    ``VOL_EXPANSION`` and ``VOL_NORMALIZATION`` are resolved by classifying
    ``vix_level`` into the underlying VIX band via
    :func:`alphamind.distillation.regime.classify_vix_band` so the boundary
    semantics live in one place.

    Raises ``ValueError`` when ``vix_level`` is negative — surfaces upstream
    data corruption rather than silently classifying it as ``low_vol``.
    """
    # Lazy imports — see TYPE_CHECKING block at top of module for the cycle rationale.
    from alphamind.distillation.regime import RegimeLabel as DistillationRegimeLabel
    from alphamind.distillation.regime import VixBand, classify_vix_band

    # The four VIX bands map onto the four guardrail regimes one-for-one. The
    # distillation layer's ``classify_vix_band`` is the single source of truth for
    # the boundary semantics (``<=`` at the upper end of each band); this table
    # translates its output into the guardrail vocabulary.
    vix_band_to_regime: dict[VixBand, Regime] = {
        VixBand.LOW_VOL: Regime.low_vol,
        VixBand.NORMAL: Regime.normal,
        VixBand.ELEVATED: Regime.elevated,
        VixBand.CRISIS: Regime.crisis,
    }

    if vix_level < 0:
        msg = f"vix_level must be non-negative; got vix_level={vix_level}"
        raise ValueError(msg)
    if distillation_label is DistillationRegimeLabel.LOW_VOL_COMPRESSION:
        return Regime.low_vol
    if distillation_label is DistillationRegimeLabel.CRISIS_SPIKE:
        return Regime.crisis
    band = classify_vix_band(
        vix_level=vix_level,
        low_vol_vix_max=vix_thresholds.low_vol_vix_max,
        normal_vix_max=vix_thresholds.normal_vix_max,
        elevated_vix_max=vix_thresholds.elevated_vix_max,
    )
    return vix_band_to_regime[band]


def from_regime_classification(rc: RegimeClassification) -> VixBoundaryThresholds:
    """Copy the three VIX thresholds verbatim from the distillation config."""
    return VixBoundaryThresholds(
        low_vol_vix_max=rc.regime_low_vol_vix_max,
        normal_vix_max=rc.regime_normal_vix_max,
        elevated_vix_max=rc.regime_elevated_vix_max,
    )
