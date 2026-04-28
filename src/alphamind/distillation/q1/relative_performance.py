"""Relative-performance computation — story 02-distillation/08a.

Per ``docs/design/01-data-layer/external/quantitative.md`` § 1e:

- Rolling ratio of ticker vs. its sector ETF (XLK / SMH / XLF / XLE per
  ``sector_classification.sector_etf``) and vs. SPY at 5-day and 20-day windows.
- Intra-sector ranking: position in the sector's daily performance distribution
  (percentile).
- Relative-strength regime change flag: leader ↔ laggard transition over the
  multi-day windows.

The regime quartile thresholds (top quartile = leader, bottom quartile =
laggard) are defined in
``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
§ Notes; they are algorithmic constants of the relative-strength
classification rather than Class A thresholds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Categorical labels and quartile thresholds
# ---------------------------------------------------------------------------

RS_REGIME_LEADER: str = "leader"
RS_REGIME_MID: str = "mid"
RS_REGIME_LAGGARD: str = "laggard"


_LEADER_PERCENTILE_FLOOR: float = 75.0
"""Top-quartile floor: at-or-above 75th percentile = leader."""


_LAGGARD_PERCENTILE_CEILING: float = 25.0
"""Bottom-quartile ceiling: at-or-below 25th percentile = laggard."""


# ---------------------------------------------------------------------------
# Numeric output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelativePerformanceResult:
    """Excess returns vs the sector ETF and vs SPY at 5-day and 20-day windows.

    Excess return is the simple difference of cumulative returns over the
    window. The story spec calls for a "rolling ratio" — implementing as a
    cumulative-return difference rather than a price ratio keeps the units
    aligned with the rest of the layer's percentile / z-score conventions.
    """

    vs_sector_5d: float
    vs_sector_20d: float
    vs_spy_5d: float
    vs_spy_20d: float


def compute_relative_performance(
    *,
    ticker_5d_return: float,
    ticker_20d_return: float,
    sector_5d_return: float,
    sector_20d_return: float,
    spy_5d_return: float,
    spy_20d_return: float,
) -> RelativePerformanceResult:
    """Excess returns vs sector ETF and vs SPY at 5-day and 20-day windows.

    All inputs are cumulative returns over the named window (e.g. ``0.05``
    for 5%). The returned excess values are positive when the ticker
    outperforms the benchmark.
    """
    return RelativePerformanceResult(
        vs_sector_5d=ticker_5d_return - sector_5d_return,
        vs_sector_20d=ticker_20d_return - sector_20d_return,
        vs_spy_5d=ticker_5d_return - spy_5d_return,
        vs_spy_20d=ticker_20d_return - spy_20d_return,
    )


# ---------------------------------------------------------------------------
# Intra-sector ranking
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntraSectorRankResult:
    """Percentile + leader / mid / laggard label for a ticker against its peers."""

    percentile: float
    regime_label: str


def rank_intra_sector(
    *,
    ticker: str,
    peer_returns: Mapping[str, float],
) -> IntraSectorRankResult:
    """Return the percentile and regime label for ``ticker`` vs its peers.

    Percentile uses the ``count(<= value) / n * 100`` convention so a ticker
    at the maximum is at the 100th percentile and a ticker at the minimum
    is below 100% but above zero (it is at-or-below itself).

    A singleton peer set returns a mid label — quartile classification of a
    single member is meaningless.
    """
    if ticker not in peer_returns:
        raise ValueError(f"rank_intra_sector: ticker {ticker!r} not in peer_returns mapping")
    if len(peer_returns) == 1:
        return IntraSectorRankResult(percentile=100.0, regime_label=RS_REGIME_MID)
    value = peer_returns[ticker]
    le_count = sum(1 for r in peer_returns.values() if r <= value)
    percentile = float(le_count) / float(len(peer_returns)) * 100.0
    if percentile >= _LEADER_PERCENTILE_FLOOR:
        regime: str = RS_REGIME_LEADER
    elif percentile <= _LAGGARD_PERCENTILE_CEILING:
        regime = RS_REGIME_LAGGARD
    else:
        regime = RS_REGIME_MID
    return IntraSectorRankResult(percentile=percentile, regime_label=regime)


# ---------------------------------------------------------------------------
# Regime-change detection
# ---------------------------------------------------------------------------


RegimeTransition = Literal[
    "leader_to_laggard",
    "laggard_to_leader",
    "no_change",
]


@dataclass(frozen=True, slots=True)
class RelativeStrengthRegimeChangeFlag:
    """A relative-strength leader ↔ laggard transition flag.

    Fires only on the documented quartile transition; less-extreme moves
    (mid → leader, leader → mid) do not raise the flag because they don't
    represent a regime change in the strict sense.
    """

    fired: bool
    transition: RegimeTransition


def detect_relative_strength_regime_change(
    *,
    prior_label: str,
    current_label: str,
) -> RelativeStrengthRegimeChangeFlag:
    """Return a flag describing the leader ↔ laggard transition (if any)."""
    if prior_label == RS_REGIME_LEADER and current_label == RS_REGIME_LAGGARD:
        return RelativeStrengthRegimeChangeFlag(fired=True, transition="leader_to_laggard")
    if prior_label == RS_REGIME_LAGGARD and current_label == RS_REGIME_LEADER:
        return RelativeStrengthRegimeChangeFlag(fired=True, transition="laggard_to_leader")
    return RelativeStrengthRegimeChangeFlag(fired=False, transition="no_change")


__all__ = [
    "RS_REGIME_LAGGARD",
    "RS_REGIME_LEADER",
    "RS_REGIME_MID",
    "IntraSectorRankResult",
    "RegimeTransition",
    "RelativePerformanceResult",
    "RelativeStrengthRegimeChangeFlag",
    "compute_relative_performance",
    "detect_relative_strength_regime_change",
    "rank_intra_sector",
]
