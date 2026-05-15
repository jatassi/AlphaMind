"""Drawdown aggregate (Tier 3 — raw state category 2c).

Per ``state-persistence.md`` § Tier 3, ``DrawdownState`` is a pre-computed
aggregate derived from Tier 1 + Tier 2 data, exposed in the read path so
consumers don't recompute drawdown statistics at snapshot time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alphamind._kernel.regime import DrawdownTier, RiskZone

__all__ = ["DrawdownState"]


@dataclass(frozen=True, slots=True)
class DrawdownState:
    """Raw state 2c — drawdown tracking."""

    current_drawdown_pct: float
    equity_high_water_mark_usd: float
    drawdown_duration_hours: float
    lifetime_max_drawdown_pct: float
    intraday_drawdown_pct: float
    daily_zone: RiskZone
    cumulative_zone: RiskZone
    cumulative_tier: DrawdownTier | None
    drawdown_by_source_pct: dict[str, float]

    def __post_init__(self) -> None:
        if self.current_drawdown_pct < 0:
            msg = f"current_drawdown_pct must be >= 0; got {self.current_drawdown_pct}"
            raise ValueError(msg)
        if not math.isfinite(self.equity_high_water_mark_usd):
            msg = (
                f"equity_high_water_mark_usd must be finite; got {self.equity_high_water_mark_usd}"
            )
            raise ValueError(msg)
        if self.drawdown_duration_hours < 0:
            msg = f"drawdown_duration_hours must be >= 0; got {self.drawdown_duration_hours}"
            raise ValueError(msg)
        if self.lifetime_max_drawdown_pct < 0:
            msg = f"lifetime_max_drawdown_pct must be >= 0; got {self.lifetime_max_drawdown_pct}"
            raise ValueError(msg)
        if self.intraday_drawdown_pct < 0:
            msg = f"intraday_drawdown_pct must be >= 0; got {self.intraday_drawdown_pct}"
            raise ValueError(msg)
