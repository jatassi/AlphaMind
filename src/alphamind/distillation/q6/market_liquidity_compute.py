"""Q6 market-liquidity composite — pure compute (ALP-485, ALP-575).

The market-liquidity composite blends three macro proxies — a credit-spread
index, a financial-stress index, and a volatility index (VIX). The raw FRED
series live on incommensurate scales (VIX is order-of-magnitude larger than
the others), so the composite normalizes each component to a percentile rank
against its own trailing series before summing. Without per-component
normalization the volatility_score dominates the sum and the composite
degenerates to "VIX with extra steps" (ALP-575).

This module carries:

- :class:`MarketLiquidityResult` — the result type the session-bound loader
  populates and the assembler reads.
- :func:`normalize_market_liquidity_components` — the pure normalization
  helper consumed by the loader.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.normalization import percentile_rank

MARKET_LIQUIDITY_COMPOSITE_KIND = "market_liquidity"

# A neutral midpoint score substituted when a component's trailing series
# carries no signal — empty or zero-variance. Per-component scores live on
# the 0..100 percentile-rank scale; the summed composite_value lives on a
# 0..(N x 100) scale (300 for the current three-component blend). The
# midpoint is chosen so a bootstrap-neutral component contributes neither
# stress nor calm to the sum.
BOOTSTRAP_NEUTRAL_SCORE: float = 50.0


class CompositeMethod(StrEnum):
    """How the per-component scores feeding ``composite_value`` were produced.

    Surfaces alongside the composite in the published payload so downstream
    consumers can tell a fully-calibrated multi-component blend from a
    bootstrap-window fallback.
    """

    NORMALIZED_PERCENTILE_SUM = "normalized_percentile_sum"
    NORMALIZED_WITH_BOOTSTRAP_NEUTRAL = "normalized_with_bootstrap_neutral"
    ALL_NEUTRAL_BOOTSTRAP = "all_neutral_bootstrap"


@dataclass(frozen=True, slots=True)
class NormalizedComponents:
    """Output of :func:`normalize_market_liquidity_components`."""

    normalized: Mapping[str, float]
    composite_method: CompositeMethod


@dataclass(frozen=True, slots=True)
class MarketLiquidityResult:
    """Output of the market-liquidity composite refresh.

    ``components`` carries the raw FRED proxy values purely for payload
    transparency — no decision path reads the raw values; the structural
    consumers are ``alert_active`` and ``state``.
    ``normalized_components`` carries the per-component percentile ranks
    (0..100) that feed ``composite_value`` (sum across components,
    0..(N x 100); 300 for the current three-component blend), so a
    non-degenerate input set
    yields a composite distinct from any single raw component (ALP-575).

    ``composite_method`` tags which normalization regime produced the
    composite — :attr:`CompositeMethod.NORMALIZED_PERCENTILE_SUM` when every
    component had a usable trailing series,
    :attr:`CompositeMethod.NORMALIZED_WITH_BOOTSTRAP_NEUTRAL` when one or
    two components fell back to :data:`BOOTSTRAP_NEUTRAL_SCORE`, and
    :attr:`CompositeMethod.ALL_NEUTRAL_BOOTSTRAP` when no component had
    usable history (the composite is a tautological 150.0).

    ``percentile_60d`` is ``None`` when the trailing composite distribution
    is empty or zero-variance; the assembler preserves the ``None`` so the
    published payload reads as ``null``.
    """

    composite_value: float
    components: Mapping[str, float]
    normalized_components: Mapping[str, float]
    composite_method: CompositeMethod
    percentile_60d: float | None
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


def normalize_market_liquidity_components(
    components: Mapping[str, float],
    component_history: Mapping[str, Sequence[float]],
) -> NormalizedComponents:
    """Percentile-rank each component against its own trailing FRED series.

    A component whose trailing series is empty or zero-variance has no
    well-defined percentile rank; this helper substitutes
    :data:`BOOTSTRAP_NEUTRAL_SCORE` and tags the result via
    :class:`CompositeMethod` so the downstream consumer can distinguish a
    fully-calibrated blend from a bootstrap fallback. See the module
    docstring for the broader ALP-575 rationale.
    """
    normalized: dict[str, float] = {}
    any_neutral = False
    all_neutral = True
    for name, raw_value in components.items():
        history = list(component_history.get(name, ()))
        percentile = percentile_rank(history, raw_value)
        if percentile is None:
            normalized[name] = BOOTSTRAP_NEUTRAL_SCORE
            any_neutral = True
        else:
            normalized[name] = percentile
            all_neutral = False
    if all_neutral:
        method = CompositeMethod.ALL_NEUTRAL_BOOTSTRAP
    elif any_neutral:
        method = CompositeMethod.NORMALIZED_WITH_BOOTSTRAP_NEUTRAL
    else:
        method = CompositeMethod.NORMALIZED_PERCENTILE_SUM
    return NormalizedComponents(normalized=normalized, composite_method=method)


__all__ = [
    "BOOTSTRAP_NEUTRAL_SCORE",
    "MARKET_LIQUIDITY_COMPOSITE_KIND",
    "CompositeMethod",
    "MarketLiquidityResult",
    "NormalizedComponents",
    "normalize_market_liquidity_components",
]
