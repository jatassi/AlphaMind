"""Drawdown aggregate (Tier 3 — raw state category 2c).

Per ``state-persistence.md`` § Tier 3, ``DrawdownState`` is a pre-computed
aggregate derived from Tier 1 + Tier 2 data, exposed in the read path so
consumers don't recompute drawdown statistics at snapshot time.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

__all__ = ["DrawdownState"]

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class DrawdownState(BaseModel):
    """Raw state 2c — drawdown tracking."""

    model_config = ConfigDict(frozen=True)

    current_drawdown_pct: Annotated[float, Field(ge=0.0)]
    equity_high_water_mark_usd: _FiniteFloat
    drawdown_duration_hours: Annotated[float, Field(ge=0.0)]
    lifetime_max_drawdown_pct: Annotated[float, Field(ge=0.0)]
    intraday_drawdown_pct: Annotated[float, Field(ge=0.0)]
    daily_zone: RiskZone
    cumulative_zone: RiskZone
    cumulative_tier: DrawdownTier | None
    drawdown_by_source_pct: dict[str, float]
