"""Q6 yield-curve regime classifier — pure compute (ALP-485).

Extracted from the legacy ``q6_macro.py`` so the orchestrator's Phase 2 can
run q6 in parallel under ``asyncio.TaskGroup`` + ``asyncio.to_thread``.

The corresponding session-bound FRED reads live in
:mod:`alphamind.distillation.q6._loaders`. The pure-compute
:func:`classify_yield_curve_regime` is the function the orchestrator's
thread-bound dispatch calls; it imports no ORM types so the
``distillation-compute-no-sqlalchemy`` import-linter contract pins it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class YieldCurveRegimeLabel(StrEnum):
    """The five yield-curve regime labels per ``external.md`` § quant 6a."""

    NORMAL_UPWARD_SLOPING = "normal_upward_sloping"
    FLAT = "flat"
    INVERTED = "inverted"
    STEEPENING = "steepening"
    FLATTENING = "flattening"


# Definitional cutoffs for the yield-curve classifier. Per the story Notes:
# these are documented inline as ``# regime classification cutoff per
# external.md § 2`` — not exposed as Class A because tightening risks
# producing labels the analysts and PM aren't trained to read.

# regime classification cutoff per external.md § 2 quant 6a — 5-day 2s10s
# change at or above this magnitude triggers a steepening / flattening
# transition label.
_YIELD_CURVE_TRANSITION_BPS_CUTOFF: float = 0.25
# regime classification cutoff per external.md § 2 quant 6a — |2s10s spread|
# at or below this is treated as flat.
_YIELD_CURVE_FLAT_BPS_CUTOFF: float = 0.10


@dataclass(frozen=True, slots=True)
class YieldCurveRegimeResult:
    """Output of :func:`classify_yield_curve_regime`."""

    label: YieldCurveRegimeLabel
    spread_2s10s: float
    spread_3m10y: float
    spread_5s30s: float
    regime_transition: bool


def classify_yield_curve_regime(
    *,
    dgs3mo: float,
    dgs2: float,
    dgs5: float,
    dgs10: float,
    dgs30: float,
    spread_2s10s_5d_ago: float,
    prior_label: YieldCurveRegimeLabel | None,
) -> YieldCurveRegimeResult:
    """Classify the yield curve into one of five regime labels.

    Spreads are computed as long minus short (in percentage points, since the
    FRED ``DGS*`` series are quoted that way):

    - ``2s10s = DGS10 - DGS2``
    - ``3m10y = DGS10 - DGS3MO``
    - ``5s30s = DGS30 - DGS5``

    Classification:

    - ``steepening`` / ``flattening`` (transition labels) fire when the
      trailing 5-day change in the 2s10s spread exceeds
      ``_YIELD_CURVE_TRANSITION_BPS_CUTOFF`` percentage points (25 bps).
    - ``inverted`` when 2s10s < 0 outside the flat band.
    - ``flat`` when |2s10s| <= ``_YIELD_CURVE_FLAT_BPS_CUTOFF``.
    - ``normal_upward_sloping`` otherwise.

    ``regime_transition`` fires when the resolved label differs from
    ``prior_label`` (None on the first invocation never triggers a transition).
    """
    spread_2s10s = dgs10 - dgs2
    spread_3m10y = dgs10 - dgs3mo
    spread_5s30s = dgs30 - dgs5

    five_day_change = spread_2s10s - spread_2s10s_5d_ago
    if abs(five_day_change) >= _YIELD_CURVE_TRANSITION_BPS_CUTOFF:
        label = (
            YieldCurveRegimeLabel.STEEPENING
            if five_day_change > 0
            else YieldCurveRegimeLabel.FLATTENING
        )
    elif abs(spread_2s10s) <= _YIELD_CURVE_FLAT_BPS_CUTOFF:
        label = YieldCurveRegimeLabel.FLAT
    elif spread_2s10s < 0:
        label = YieldCurveRegimeLabel.INVERTED
    else:
        label = YieldCurveRegimeLabel.NORMAL_UPWARD_SLOPING

    transition = prior_label is not None and prior_label is not label
    return YieldCurveRegimeResult(
        label=label,
        spread_2s10s=spread_2s10s,
        spread_3m10y=spread_3m10y,
        spread_5s30s=spread_5s30s,
        regime_transition=transition,
    )


__all__ = [
    "YieldCurveRegimeLabel",
    "YieldCurveRegimeResult",
    "classify_yield_curve_regime",
]
