"""Pure-compute core for q1 gap analysis (ALP-467).

This module is the compute half of the load/compute split for ``q1/gap``.
It carries the pure classification logic for gap events plus the
probability-resolution helper that operates on hand-loaded event counts.
No SQLAlchemy imports: every DB read sits in ``q1/gap_loaders.py``.

The legacy ``q1/gap.py`` re-exports the pure surface so existing call sites
keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphamind.distillation._calibration_core import (
    CalibratedValue,
    tag_with_fallback,
)
from alphamind.distillation.normalization import atr_normalize

# ---------------------------------------------------------------------------
# Categorical labels for gap classification
# ---------------------------------------------------------------------------

GAP_KIND_FULL: str = "full"
"""Gap classification: open lies entirely outside the prior bar's range."""

GAP_KIND_PARTIAL: str = "partial"
"""Gap classification: open lies inside the prior bar's range."""

GAP_TREND_WITH: str = "with_trend"
"""Gap direction matches the prior trend."""

GAP_TREND_COUNTER: str = "counter_trend"
"""Gap direction opposes the prior trend."""


GapDirection = Literal["up", "down", "flat"]
TrendDirection = Literal["up", "down", "flat"]


# ---------------------------------------------------------------------------
# Pure analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapAnalysisResult:
    """Gap classification plus magnitude in absolute and ATR-relative terms."""

    gap_absolute: float
    gap_atr_ratio: float
    direction: GapDirection
    kind: str
    trend_classification: str


def analyze_gap(
    *,
    today_open: float,
    prior_close: float,
    prior_high: float,
    prior_low: float,
    atr_14d: float,
    trend_direction: TrendDirection,
) -> GapAnalysisResult:
    """Classify a gap given the open vs the prior bar.

    See module docstring; this function is pure and operates entirely on
    its arguments.
    """
    gap_absolute = today_open - prior_close
    gap_atr_ratio = atr_normalize(gap_absolute, atr_14d)
    direction: GapDirection
    if gap_absolute > 0.0:
        direction = "up"
        kind = GAP_KIND_FULL if today_open > prior_high else GAP_KIND_PARTIAL
        trend_match = trend_direction == "up"
    elif gap_absolute < 0.0:
        direction = "down"
        kind = GAP_KIND_FULL if today_open < prior_low else GAP_KIND_PARTIAL
        trend_match = trend_direction == "down"
    else:
        # Halt-resume opens or illiquid names can produce an open exactly
        # equal to the prior close — there is no gap to direction-label.
        direction = "flat"
        kind = GAP_KIND_PARTIAL
        trend_match = trend_direction == "flat"
    trend_classification = GAP_TREND_WITH if trend_match else GAP_TREND_COUNTER

    return GapAnalysisResult(
        gap_absolute=gap_absolute,
        gap_atr_ratio=gap_atr_ratio,
        direction=direction,
        kind=kind,
        trend_classification=trend_classification,
    )


# ---------------------------------------------------------------------------
# Detection — gate on the threshold and surface a DetectedGap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectedGap:
    """A gap that cleared the detection threshold and should produce an event row."""

    ticker: str
    event_ts: str
    direction: GapDirection
    magnitude_atr_multiple: float


def detect_session_gap(
    *,
    ticker: str,
    today_open: float,
    prior_close: float,
    atr_14d: float,
    event_ts: str,
    min_atr_multiple: float,
) -> DetectedGap | None:
    """Return a :class:`DetectedGap` if the gap clears ``min_atr_multiple``."""
    gap_absolute = today_open - prior_close
    if atr_14d <= 0:
        return None
    magnitude = abs(gap_absolute) / atr_14d
    if magnitude < min_atr_multiple:
        return None
    direction: GapDirection = "up" if gap_absolute > 0.0 else "down"
    return DetectedGap(
        ticker=ticker,
        event_ts=event_ts,
        direction=direction,
        magnitude_atr_multiple=magnitude,
    )


# ---------------------------------------------------------------------------
# Gap-fill probability — pure compute over loaded event counts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapFillEventHistory:
    """Frozen inputs for :func:`compute_gap_fill_probability`.

    Carries the per-ticker resolved/filled counts and the sector-pool
    resolved/filled counts the repository pre-loads in one pass. The
    compute step combines these into the three calibration branches with
    no further DB access.
    """

    ticker_resolved: int
    ticker_filled: int
    sector_resolved: int
    sector_filled: int


def compute_gap_fill_probability(
    *,
    history: GapFillEventHistory,
    min_events: int,
) -> CalibratedValue:
    """Resolve the gap-fill probability using pre-loaded event counts.

    Mirrors the three-branch contract previously implemented in
    :func:`alphamind.distillation.q1.gap.resolve_gap_fill_probability` but
    consumes pre-loaded counts instead of issuing SQL itself.
    """
    if history.ticker_resolved > 0:
        per_ticker_rate: float | None = float(history.ticker_filled) / float(
            history.ticker_resolved
        )
    else:
        per_ticker_rate = None

    def _sector_fallback() -> float | None:
        if history.sector_resolved == 0:
            return None
        return float(history.sector_filled) / float(history.sector_resolved)

    return tag_with_fallback(
        observed_n=history.ticker_resolved,
        required_n=min_events,
        input_name="gap_fill_min_events",
        computed_value=per_ticker_rate,
        fallback=_sector_fallback,
    )


__all__ = [
    "GAP_KIND_FULL",
    "GAP_KIND_PARTIAL",
    "GAP_TREND_COUNTER",
    "GAP_TREND_WITH",
    "DetectedGap",
    "GapAnalysisResult",
    "GapDirection",
    "GapFillEventHistory",
    "TrendDirection",
    "analyze_gap",
    "compute_gap_fill_probability",
    "detect_session_gap",
]
