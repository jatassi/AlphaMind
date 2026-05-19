"""Q6 market-liquidity composite — pure compute (ALP-485).

The market-liquidity composite is a thin wrapper over story 07's
:func:`refresh_composite_state` — there is no pure ranking math beyond the
percentile produced by that primitive. This module carries the
:class:`MarketLiquidityResult` dataclass that the
session-bound loader populates in
:mod:`alphamind.distillation.q6._loaders` and that the assembler reads.

Holding the result type here (rather than in the loader) keeps the
compute / load split symmetric across q6 — every classifier has a
``*_compute.py`` carrying its result type, even when the per-classifier
pure computation degenerates to "wrap the persistence-row payload."
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from alphamind.distillation._calibration_core import CalibrationState

MARKET_LIQUIDITY_COMPOSITE_KIND = "market_liquidity"


@dataclass(frozen=True, slots=True)
class MarketLiquidityResult:
    """Output of the market-liquidity composite refresh.

    ``percentile_60d`` is ``None`` when the trailing composite distribution
    is empty or zero-variance (per ALP-545); the assembler preserves the
    ``None`` so the published payload reads as ``null``.
    """

    composite_value: float
    components: Mapping[str, float]
    percentile_60d: float | None
    alert_active: bool
    state: CalibrationState
    bootstrap_reason: str | None


__all__ = [
    "MARKET_LIQUIDITY_COMPOSITE_KIND",
    "MarketLiquidityResult",
]
