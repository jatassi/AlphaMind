"""Round-trip codec between :class:`DrawdownState` and :class:`DrawdownStateRow`.

Persisted subset of the typed record: ``equity_high_water_mark_usd``,
``current_drawdown_pct``, ``drawdown_duration_hours``,
``lifetime_max_drawdown_pct``, ``drawdown_by_source_pct``. Read-time
computed fields (``intraday_drawdown_pct``, ``daily_zone``,
``cumulative_zone``, ``cumulative_tier``) must be supplied by the caller
when rehydrating a typed record from a row.
"""

from __future__ import annotations

import json
from datetime import datetime

from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates import DrawdownState
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.state.tables._singleton_codec import (
    datetime_to_iso_z,
)
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)


def drawdown_state_record_to_row(
    state: DrawdownState,
    *,
    last_updated_at: datetime,
) -> DrawdownStateRow:
    """Build a ``DrawdownStateRow`` from a typed ``DrawdownState``."""
    return DrawdownStateRow(
        id=DRAWDOWN_STATE_SINGLETON_ID,
        equity_high_water_mark_usd=state.equity_high_water_mark_usd,
        current_drawdown_pct=state.current_drawdown_pct,
        drawdown_duration_hours=state.drawdown_duration_hours,
        lifetime_max_drawdown_pct=state.lifetime_max_drawdown_pct,
        drawdown_by_source_json=json.dumps(state.drawdown_by_source_pct),
        last_updated_at=datetime_to_iso_z(last_updated_at, field_name="last_updated_at"),
    )


def drawdown_state_record_from_row(
    row: DrawdownStateRow,
    *,
    intraday_drawdown_pct: float,
    daily_zone: RiskZone,
    cumulative_zone: RiskZone,
    cumulative_tier: DrawdownTier | None,
) -> DrawdownState:
    """Rehydrate a typed ``DrawdownState`` from a row plus computed kwargs.

    The four computed fields are intentionally required: they derive from
    current snapshot context (intraday tracking, the active risk-parameter
    set), so the storage layer can't recover them from the row alone.
    """
    return DrawdownState(
        current_drawdown_pct=row.current_drawdown_pct,
        equity_high_water_mark_usd=row.equity_high_water_mark_usd,
        drawdown_duration_hours=row.drawdown_duration_hours,
        lifetime_max_drawdown_pct=row.lifetime_max_drawdown_pct,
        intraday_drawdown_pct=intraday_drawdown_pct,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct=json.loads(row.drawdown_by_source_json),
    )
