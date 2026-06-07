"""Q6 inflation regime classifier — pure compute (ALP-485).

Extracted from the legacy ``q6_macro.py`` so the per-category indicator
compute step can run q6 in parallel under ``asyncio.TaskGroup`` +
``asyncio.to_thread``.

The corresponding session-bound FRED reads live in
:mod:`alphamind.distillation.q6._loaders`. The pure-compute
:func:`classify_inflation_regime` is the function the orchestrator's
thread-bound dispatch calls; it imports no ORM types so the
``distillation-compute-no-sqlalchemy`` import-linter contract pins it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class InflationRegimeLabel(StrEnum):
    """The four inflation regime labels per ``external.md`` § quant 6c."""

    HOT = "hot"
    COOLING = "cooling"
    STABLE = "stable"
    DEFLATION_RISK = "deflation_risk"


# Definitional cutoffs for the inflation classifier. Per the story Notes:
# documented inline as ``# regime classification cutoff per external.md
# § 2 quant 6c`` — not exposed as Class A.

# regime classification cutoff per external.md § 2 quant 6c — trailing
# 3-month breakeven trend magnitude (in percentage points) for the
# hot/cooling labels. ``trend_bps`` is in pp so 30 bps = 0.30.
_INFLATION_BREAKEVEN_TREND_PP_CUTOFF: float = 0.30
# regime classification cutoff per external.md § 2 quant 6c — 10y breakeven
# below this level, sustained over 30 days, triggers the deflation_risk label.
DEFLATION_RISK_BREAKEVEN_PP_CUTOFF: float = 1.5


@dataclass(frozen=True, slots=True)
class InflationRegimeResult:
    """Output of :func:`classify_inflation_regime`."""

    label: InflationRegimeLabel
    breakeven_trend_bps: float
    cpi_surprise_signs_positive: int
    cpi_surprise_signs_negative: int
    regime_transition: bool


def classify_inflation_regime(
    *,
    breakeven_trend_bps: float,
    recent_cpi_surprise_signs: Sequence[int],
    breakeven_below_threshold_30d: bool,
    prior_label: InflationRegimeLabel | None,
) -> InflationRegimeResult:
    """Classify the inflation regime into one of four labels.

    Inputs:

    - ``breakeven_trend_bps`` — trailing 3-month change in the 10y breakeven
      (``T10YIE``), in percentage points (positive means breakevens rising).
    - ``recent_cpi_surprise_signs`` — list of integer signs (``+1``, ``-1``,
      ``0``) for the last 3 CPI releases. Sourced from the macro-release
      surprise component (``actual - consensus``); the caller computes the
      sign and passes it through.
    - ``breakeven_below_threshold_30d`` — whether the 10y breakeven has stayed
      below ``DEFLATION_RISK_BREAKEVEN_PP_CUTOFF`` (1.5%) over the trailing
      30 days. Computed by the caller against ``macro_observations``.

    Classification (rule table per ``external.md`` § quant 6c):

    - ``hot`` when breakeven trend ≥ +``_INFLATION_BREAKEVEN_TREND_PP_CUTOFF``
      AND surprises are positive in the last 3 releases (no negatives).
    - ``cooling`` when breakeven trend ≤ -``_INFLATION_BREAKEVEN_TREND_PP_CUTOFF``
      AND surprises are negative in the last 3 releases (no positives).
    - ``deflation_risk`` when ``breakeven_below_threshold_30d`` is True.
    - ``stable`` otherwise.

    The ``deflation_risk`` rule is checked first per
    ``external.md`` § quant 6c — sustained low breakevens are the dominant
    signal regardless of the recent trend.
    """
    positives = sum(1 for sign in recent_cpi_surprise_signs if sign > 0)
    negatives = sum(1 for sign in recent_cpi_surprise_signs if sign < 0)

    if breakeven_below_threshold_30d:
        label = InflationRegimeLabel.DEFLATION_RISK
    elif (
        breakeven_trend_bps >= _INFLATION_BREAKEVEN_TREND_PP_CUTOFF
        and negatives == 0
        and positives > 0
    ):
        label = InflationRegimeLabel.HOT
    elif (
        breakeven_trend_bps <= -_INFLATION_BREAKEVEN_TREND_PP_CUTOFF
        and positives == 0
        and negatives > 0
    ):
        label = InflationRegimeLabel.COOLING
    else:
        label = InflationRegimeLabel.STABLE

    transition = prior_label is not None and prior_label is not label
    return InflationRegimeResult(
        label=label,
        breakeven_trend_bps=breakeven_trend_bps,
        cpi_surprise_signs_positive=positives,
        cpi_surprise_signs_negative=negatives,
        regime_transition=transition,
    )


__all__ = [
    "DEFLATION_RISK_BREAKEVEN_PP_CUTOFF",
    "InflationRegimeLabel",
    "InflationRegimeResult",
    "classify_inflation_regime",
]
