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
from typing import Literal

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.normalization import percentile_rank

MARKET_LIQUIDITY_COMPOSITE_KIND = "market_liquidity"

# A neutral midpoint (50th-percentile) score substituted when a component's
# trailing series carries no signal — empty or zero-variance. Chosen as the
# midpoint of the 0..100 percentile-rank scale so a bootstrap-neutral
# component contributes neither stress nor calm to the sum.
BOOTSTRAP_NEUTRAL_SCORE: float = 50.0

CompositeMethod = Literal[
    "normalized_percentile_sum",
    "normalized_with_bootstrap_neutral",
    "all_neutral_bootstrap",
]


@dataclass(frozen=True, slots=True)
class NormalizedComponents:
    """Output of :func:`normalize_market_liquidity_components`."""

    normalized: Mapping[str, float]
    composite_method: CompositeMethod


@dataclass(frozen=True, slots=True)
class MarketLiquidityResult:
    """Output of the market-liquidity composite refresh.

    ``components`` carries the raw FRED proxy values; ``normalized_components``
    carries the per-component percentile ranks (0..100) that feed the
    composite sum. ``composite_value`` is the sum of the normalized scores,
    so a non-degenerate input set yields a composite distinct from any
    single raw component (ALP-575).

    ``composite_method`` tags which normalization regime produced the
    composite — ``"normalized_percentile_sum"`` when every component had a
    usable trailing series, ``"normalized_with_bootstrap_neutral"`` when one
    or two components fell back to :data:`BOOTSTRAP_NEUTRAL_SCORE`, and
    ``"all_neutral_bootstrap"`` when no component had usable history (the
    composite is a tautological 150.0).

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

    Each raw FRED value is converted to its percentile rank against the
    component's own trailing series. Without this normalization the
    volatility_score (VIX, order ~10-50) dominates by magnitude over
    credit_spread_score (~0-10) and stress_index_score (~-2..+2), so the
    composite degenerates to "VIX with extra steps" (ALP-575).

    When a component's trailing series is empty or zero-variance the
    percentile rank is undefined; this helper substitutes
    :data:`BOOTSTRAP_NEUTRAL_SCORE` (50.0, the percentile midpoint) for that
    component and tags the result with
    ``composite_method = "normalized_with_bootstrap_neutral"`` (or
    ``"all_neutral_bootstrap"`` when no component had usable history) so
    downstream consumers know the composite isn't a fully-calibrated blend.
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
    method: CompositeMethod
    if all_neutral:
        method = "all_neutral_bootstrap"
    elif any_neutral:
        method = "normalized_with_bootstrap_neutral"
    else:
        method = "normalized_percentile_sum"
    return NormalizedComponents(normalized=normalized, composite_method=method)


__all__ = [
    "BOOTSTRAP_NEUTRAL_SCORE",
    "MARKET_LIQUIDITY_COMPOSITE_KIND",
    "CompositeMethod",
    "MarketLiquidityResult",
    "NormalizedComponents",
    "normalize_market_liquidity_components",
]
