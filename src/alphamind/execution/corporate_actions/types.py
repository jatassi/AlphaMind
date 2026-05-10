"""Types for the corporate-actions integration package (ALP-409 / ALP-410).

``CorporateActionActivity`` is the typed handle for a single Alpaca CA activity
awaiting integration, moved from ``state_persistence.write_paths.phase1`` so the
corporate-actions package owns its own input model.

``AlpacaPositionLookup`` is a Protocol-shaped callable that non-equity handlers
use to read post-adjustment Alpaca position state.  Story 04 wires the real
client; until then callers pass ``None``.

``PositionLookup`` is the per-symbol view the fetcher (ALP-410) consumes to
match v1beta1 CA events against local positions.  Carries ``position_id``,
``direction`` (so cash dividends split into LONG / SHORT), and ``quantity``
(so the fetcher can multiply per-share rates into an absolute USD cash
impact).  Story 04 wires the real lookup from the snapshot assembler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from alphamind.execution.broker_adapter.queries import PositionSnapshot
from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.portfolio_state.records.positions import Direction


class CorporateActionActivity(BaseModel):
    """Typed handle for a single Alpaca CA activity awaiting integration.

    Phase 1 integrates one ``CorporateActionActivity`` per row drained by
    the v1beta1 fetcher (:func:`fetch_unprocessed_ca_activities` in the
    ``fetcher`` module) from Alpaca's ``GET /v1beta1/corporate-actions``
    endpoint.

    Field semantics:

    * ``ratio_or_amount`` — for splits / stock dividends, the per-share
      multiplier component (e.g., ``new_rate / old_rate`` for splits, the raw
      rate for stock dividends; handlers translate stock-dividend rate into
      ``(1 + rate)``).  For spin-offs, the child-per-parent allocation.  Zero
      otherwise.
    * ``signed_cash_impact_usd`` — absolute USD impact for this CA on the
      matching local position; positive credit, negative debit, zero where
      the event itself produces no cash leg.  The fetcher multiplies any
      per-share rate by the matched position's quantity once at
      construction time.
    * ``transaction_time`` — for v1beta1 events, anchored at midnight UTC on
      the event's ``ex_date`` where present, else ``process_date`` /
      ``effective_date``.  The Phase 1 chronological merge compares this
      against fill ``fill_timestamp`` (precise datetime); fills on the
      ex-date resolve before the CA after midnight-UTC anchoring.
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


@dataclass(frozen=True)
class PositionLookup:
    """Per-symbol local position view consumed by the fetcher.

    Story 04 wires the real lookup from the snapshot assembler; until then
    callers pass a dict-backed callable.  ``direction`` lets the fetcher
    discriminate ``CashDividend`` into ``CASH_DIVIDEND_LONG`` / ``CASH_DIVIDEND_SHORT``;
    ``quantity`` lets the fetcher multiply per-share rates from cash
    dividends and cash mergers into an absolute USD ``signed_cash_impact_usd``.
    """

    position_id: str
    direction: Direction
    quantity: float


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
    "PositionLookup",
]
