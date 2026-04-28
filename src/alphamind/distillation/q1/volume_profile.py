"""Volume profile computation — story 02-distillation/08a.

Per ``docs/design/01-data-layer/external/quantitative.md`` § 1b the volume
profile is "not just how much traded but *where* it traded." The standard
Market Profile algorithm sorts price levels by volume descending and
accumulates until ≥ 70% of session volume is captured — the value area.
The point of control is the single most-traded price level; high- and
low-volume nodes are the upper and lower quartiles of price-level volume.

Developing vs. settled classification compares the current session's value
area to the trailing pool's value area: substantial overlap is settled, a
shift outside the pool is developing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Named constants that capture algorithmic conventions
# ---------------------------------------------------------------------------

VALUE_AREA_VOLUME_FRACTION: float = 0.70
"""Fraction of session volume captured by the value area.

The 70% convention is the settled definition from Market Profile theory —
documented in ``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
§ Volume profile as "well-established"; "Document the 70% threshold in a
named constant; it's a settled convention, not a tunable." Treated as an
algorithmic constant, not a Class A threshold.
"""


SETTLED_OVERLAP_THRESHOLD: float = 0.60
"""Minimum value-area overlap fraction classifying a session as settled.

A current-session value area whose overlap with the trailing multi-session
pool is at or above this fraction is settled; below the threshold is
developing. The 60% threshold is the quantitative definition of "shifting
outside the pool" from
``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
§ Volume profile.
"""


PROFILE_SETTLED: str = "settled"
"""Categorical label: the current session sits squarely on the trailing pool."""


PROFILE_DEVELOPING: str = "developing"
"""Categorical label: the current session has shifted outside the trailing pool."""


_HIGH_LOW_VOLUME_NODE_QUANTILE: float = 0.25
"""Quantile fraction for top/bottom volume-node extraction.

Top 25% by volume is the high-volume-node set; bottom 25% is the
low-volume-node set. Round-up convention so a small-N profile still
produces at least one node per side.
"""


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceLevelVolume:
    """Volume traded at one price level over the profile window."""

    price: float
    volume: int


@dataclass(frozen=True, slots=True)
class VolumeProfileResult:
    """Profile output — value area, POC, high/low-volume nodes."""

    value_area_prices: tuple[float, ...]
    value_area_volume_fraction: float
    value_area_low: float
    value_area_high: float
    point_of_control: float
    high_volume_nodes: tuple[float, ...]
    low_volume_nodes: tuple[float, ...]


def _quantile_count(total: int, quantile: float) -> int:
    """Round-up count for the top/bottom ``quantile`` fraction of ``total`` levels.

    A 4-level profile yields one node per side at quantile=0.25; an 8-level
    profile yields two; rounding up so any non-empty input always returns at
    least one node.
    """
    return max(1, math.ceil(total * quantile))


def compute_volume_profile(levels: Sequence[PriceLevelVolume]) -> VolumeProfileResult:
    """Compute the volume profile from per-level volume aggregations.

    Steps:

    1. Sort levels by volume descending.
    2. Walk the sorted list accumulating volume; the prefix that first
       reaches ``VALUE_AREA_VOLUME_FRACTION`` of total volume is the value
       area.
    3. POC is the highest-volume level (the head of the sorted list).
    4. High-volume nodes = top quartile by volume; low-volume nodes =
       bottom quartile.

    Raises :class:`ValueError` when ``levels`` is empty — a profile with no
    levels has no POC and no value area.
    """
    if not levels:
        raise ValueError("compute_volume_profile requires at least one PriceLevelVolume")
    total_volume = sum(level.volume for level in levels)
    if total_volume == 0:
        raise ValueError("compute_volume_profile requires positive total volume")

    sorted_desc = sorted(levels, key=lambda lv: lv.volume, reverse=True)
    accumulated = 0
    value_area: list[PriceLevelVolume] = []
    for level in sorted_desc:
        accumulated += level.volume
        value_area.append(level)
        if accumulated / total_volume >= VALUE_AREA_VOLUME_FRACTION:
            break
    value_area_prices = tuple(sorted(lv.price for lv in value_area))
    value_area_low = value_area_prices[0]
    value_area_high = value_area_prices[-1]

    poc = sorted_desc[0].price

    quartile_count = _quantile_count(len(sorted_desc), _HIGH_LOW_VOLUME_NODE_QUANTILE)
    high_nodes = tuple(lv.price for lv in sorted_desc[:quartile_count])
    low_nodes = tuple(lv.price for lv in sorted_desc[-quartile_count:])

    return VolumeProfileResult(
        value_area_prices=value_area_prices,
        value_area_volume_fraction=accumulated / total_volume,
        value_area_low=value_area_low,
        value_area_high=value_area_high,
        point_of_control=poc,
        high_volume_nodes=high_nodes,
        low_volume_nodes=low_nodes,
    )


def classify_session_profile(
    *,
    current_value_area_range: tuple[float, float],
    multi_session_value_area_range: tuple[float, float],
) -> str:
    """Classify the current session as :data:`PROFILE_SETTLED` or :data:`PROFILE_DEVELOPING`.

    Overlap fraction is the linear overlap of the current value area with
    the trailing pool's value area, divided by the pool's width.

    A pool with zero width — current and pool both sit on a single price —
    is treated as settled (degenerate case, no shift can be measured).
    """
    current_low, current_high = current_value_area_range
    pool_low, pool_high = multi_session_value_area_range
    pool_width = pool_high - pool_low
    if pool_width == 0.0:
        return PROFILE_SETTLED
    overlap_low = max(current_low, pool_low)
    overlap_high = min(current_high, pool_high)
    overlap = max(overlap_high - overlap_low, 0.0)
    overlap_fraction = overlap / pool_width
    if overlap_fraction >= SETTLED_OVERLAP_THRESHOLD:
        return PROFILE_SETTLED
    return PROFILE_DEVELOPING


__all__ = [
    "PROFILE_DEVELOPING",
    "PROFILE_SETTLED",
    "SETTLED_OVERLAP_THRESHOLD",
    "VALUE_AREA_VOLUME_FRACTION",
    "PriceLevelVolume",
    "VolumeProfileResult",
    "classify_session_profile",
    "compute_volume_profile",
]
