"""Shared statistical primitives — a leaf kernel module (ALP-589).

This module imports nothing first-party and no ORM types, so it sits beneath
every layer in the import graph and any consumer may depend on it.

:func:`percentile_from_normal` is the single Normal-CDF percentile formula.
Before ALP-589 it lived as two near-identical private copies — one in the
analysis-layer qualitative loader
(``analysis.qualitative_research.loaders``), one in the distillation
sentiment-percentile compute
(``distillation.qualitative.sentiment_percentile_compute``) — differing only
in output scale and degenerate-``stdev`` handling. Collecting the math here
keeps the formula from drifting; scale and degenerate-distribution policy
stay with each caller (see those modules' docstrings for why the two
percentile surfaces are kept separate).
"""

from __future__ import annotations

import math

# sqrt(2): the scale factor in the standard-Normal CDF
# Phi(z) = 0.5 * (1 + erf(z / sqrt(2))).
_SQRT_TWO: float = math.sqrt(2.0)


def percentile_from_normal(*, value: float, mean: float, stdev: float) -> float:
    """Return the Normal-CDF percentile of ``value`` against ``(mean, stdev)``.

    The result is ``Phi((value - mean) / stdev)`` — the probability a draw
    from ``Normal(mean, stdev)`` lands at or below ``value`` — clamped to the
    unit interval ``[0.0, 1.0]``. ``0.5`` sits at the mean.

    ``stdev`` must be strictly positive. A non-positive ``stdev`` is a
    degenerate distribution with no meaningful percentile, and callers differ
    on how to represent that — the analysis loader emits ``None``, the
    distillation compute substitutes a median placeholder — so this function
    raises :class:`ValueError` and leaves the policy to the caller rather
    than baking one choice into the shared math.
    """
    if stdev <= 0:
        raise ValueError(f"stdev must be positive, got {stdev}")
    z = (value - mean) / stdev
    cdf = 0.5 * (1.0 + math.erf(z / _SQRT_TWO))
    return max(0.0, min(1.0, cdf))


__all__ = ["percentile_from_normal"]
