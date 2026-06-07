"""Q6 dollar-move attribution classifier — pure compute (ALP-485).

Extracted from the legacy ``q6_macro.py`` so the per-category indicator
compute step can run q6 in parallel under ``asyncio.TaskGroup`` +
``asyncio.to_thread``.

The corresponding session-bound FRED reads live in
:mod:`alphamind.distillation.q6._loaders`. The pure-compute
:func:`classify_dollar_attribution` is the function the orchestrator's
thread-bound dispatch calls; it imports no ORM types so the
``distillation-compute-no-sqlalchemy`` import-linter contract pins it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class DollarAttributionLabel(StrEnum):
    """The three dollar-move attribution labels per ``external.md`` § quant 6f."""

    RATE_DIFFERENTIAL_DRIVEN = "rate_differential_driven"
    RISK_SENTIMENT_DRIVEN = "risk_sentiment_driven"
    TRADE_FLOW_DRIVEN = "trade_flow_driven"


# Definitional cutoff for dollar-move attribution. The single-factor
# discriminant compares |rate correlation| vs |SPY correlation| over the
# trailing 20-day window; whichever is dominant by at least this absolute
# margin wins. Residual (neither dominant) → trade_flow_driven.

# regime classification cutoff per external.md § 2 quant 6f — minimum
# |correlation-coefficient| margin for a single factor to claim the label.
# Below this, neither factor explains the move and the residual
# ``trade_flow_driven`` label fires.
_DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN: float = 0.20


@dataclass(frozen=True, slots=True)
class DollarAttributionResult:
    """Output of :func:`classify_dollar_attribution`."""

    label: DollarAttributionLabel
    rate_correlation: float
    risk_correlation: float


def pearson_correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation of two equal-length series.

    Returns ``0.0`` for empty input or zero-variance series. The dollar
    attribution heuristic treats ``0.0`` as "no signal" → caller falls
    through to the residual label.
    """
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    dot = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b, strict=True):
        da = x - mean_a
        db = y - mean_b
        dot += da * db
        var_a += da * da
        var_b += db * db
    if var_a == 0.0 or var_b == 0.0:
        return 0.0
    return float(dot / (var_a * var_b) ** 0.5)


def classify_dollar_attribution(
    *,
    dxy_returns: Sequence[float],
    rate_diff_returns: Sequence[float],
    spy_returns: Sequence[float],
) -> DollarAttributionResult:
    """Classify a DXY move's likely driver into one of three labels.

    The heuristic is a deliberate simplification of full multi-factor
    attribution: compare |corr(DXY, rate_diff)| against |corr(DXY, SPY)| over
    the trailing 20-day window the caller passes.

    - When the larger |correlation| exceeds the smaller by at least
      ``_DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN``, the dominant factor wins:
      ``rate_differential_driven`` if the rate factor; ``risk_sentiment_driven``
      if the SPY factor.
    - Otherwise the residual ``trade_flow_driven`` label fires.

    The simplification is documented per the story Notes: "the simplest
    version that produces a non-arbitrary label."
    """
    rate_corr = pearson_correlation(dxy_returns, rate_diff_returns)
    risk_corr = pearson_correlation(dxy_returns, spy_returns)

    abs_rate = abs(rate_corr)
    abs_risk = abs(risk_corr)
    margin = abs(abs_rate - abs_risk)

    if margin < _DOLLAR_ATTRIBUTION_DOMINANCE_MARGIN:
        label = DollarAttributionLabel.TRADE_FLOW_DRIVEN
    elif abs_rate > abs_risk:
        label = DollarAttributionLabel.RATE_DIFFERENTIAL_DRIVEN
    else:
        label = DollarAttributionLabel.RISK_SENTIMENT_DRIVEN

    return DollarAttributionResult(
        label=label,
        rate_correlation=rate_corr,
        risk_correlation=risk_corr,
    )


__all__ = [
    "DollarAttributionLabel",
    "DollarAttributionResult",
    "classify_dollar_attribution",
    "pearson_correlation",
]
