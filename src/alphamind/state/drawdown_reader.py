"""``DrawdownState`` reader for the pipeline orchestrator (ALP-472 lift).

The orchestrator computes ``halt_state`` before resolving runtime
dimensions, which requires reading the ``drawdown_state`` singleton row.
On a fresh DB the row is absent (Phase 1's write path seeds it), so this
module owns the read with a zero-drawdown fallback that keeps the
halt-detection contract intact (no drawdown → no halt).

Lives in ``alphamind.state`` because it reads from a state-layer table; the
orchestrator-side concern is purely composition.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_from_row,
)

__all__ = ["read_drawdown_state", "zero_drawdown_state"]


def zero_drawdown_state() -> DrawdownState:
    """Return a zero-drawdown ``DrawdownState``.

    Used when the singleton row is absent (fresh DB / first run) and as the
    halt-state computation input on bootstrap. The four computed read-time
    fields carry neutral defaults; only ``current_drawdown_pct`` and
    ``intraday_drawdown_pct`` (both zero) and ``cumulative_tier`` (``None``)
    matter for halt detection.
    """
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=0.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


async def read_drawdown_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> DrawdownState:
    """Read the ``drawdown_state`` singleton row; fall back to zero on absence.

    Phase 1's write path (``process_unprocessed_fills``) seeds the singleton
    row, so a fresh DB legitimately has none before the first invocation
    completes. Treating absence as zero drawdown keeps the orchestrator
    runnable from a clean state without violating the halt-detection
    contract (no drawdown → no halt).
    """
    async with session_factory() as session:
        row = await session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
        if row is None:
            return zero_drawdown_state()
        return drawdown_state_record_from_row(
            row,
            intraday_drawdown_pct=0.0,
            daily_zone=RiskZone.NORMAL,
            cumulative_zone=RiskZone.NORMAL,
            cumulative_tier=None,
        )
