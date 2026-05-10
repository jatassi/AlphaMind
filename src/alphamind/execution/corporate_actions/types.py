"""Types for the corporate-actions integration package (ALP-409).

``CorporateActionActivity`` is the typed handle for a single Alpaca CA activity
awaiting integration, moved from ``state_persistence.write_paths.phase1`` so the
corporate-actions package owns its own input model.

``AlpacaPositionLookup`` is a Protocol-shaped callable that non-equity handlers
use to read post-adjustment Alpaca position state.  Story 04 wires the real
client; until then callers pass ``None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from alphamind.execution.broker_adapter.queries import PositionSnapshot
from alphamind.portfolio_state.events.activity_log import CorporateActionType


class CorporateActionActivity(BaseModel):
    """Typed handle for a single Alpaca CA activity awaiting integration.

    Phase 1 integrates one ``CorporateActionActivity`` per row drained from
    Alpaca's ``GET /v2/account/activities``.  The continuous-monitor work
    that produces these isn't built (:issue:`ALP-123`); tests construct
    instances directly until that lands.
    """

    model_config = ConfigDict(frozen=True)

    alpaca_activity_id: str
    action_type: CorporateActionType
    ticker: str
    new_ticker: str | None
    ratio_or_amount: float
    position_id: str
    signed_cash_impact_usd: float
    transaction_time: datetime


class AlpacaPositionLookup(Protocol):
    """Protocol for a callable that returns a live Alpaca position by symbol.

    Equity-only handlers (SPLIT, cash dividends) receive ``None``; options /
    strategy handlers call this to read post-adjustment Alpaca state.  Story 04
    wires the real :class:`~alphamind.execution.broker_adapter.queries.AlpacaDataClient`
    method; earlier stories pass ``None`` to the dispatch entry point.
    """

    def get_position(self, symbol: str) -> PositionSnapshot | None:
        """Return the live Alpaca position for *symbol*, or ``None`` if flat."""
        ...


__all__ = [
    "AlpacaPositionLookup",
    "CorporateActionActivity",
]
