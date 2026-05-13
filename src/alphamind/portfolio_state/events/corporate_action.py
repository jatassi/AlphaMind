"""Corporate-action event details — splits, dividends, mergers, spin-offs."""

from __future__ import annotations

from dataclasses import dataclass

from alphamind._kernel.money import Money
from alphamind.portfolio_state.events.types import (
    CorporateActionType,
    EventGroup,
    EventType,
)


@dataclass(frozen=True, slots=True)
class CorporateActionAppliedDetail:
    """Detail payload for CORPORATE_ACTION_APPLIED events.

    Monetary fields (per the audit hot-list):
    * ``pre_action_cost_basis`` / ``post_action_cost_basis`` — total cost
      basis values, non-negative ``Money``.
    * ``signed_cash_impact_usd`` — may be negative (a debit from a short
      cash dividend) — uses signed ``Money``.

    ``ratio_or_amount`` is intentionally a plain ``float`` — it is *not* a
    money field. For splits it is the share-multiplication ratio (``2.0`` for
    a 2-for-1 split); for stock dividends it is the bonus-share fraction; for
    cash dividends it is the per-share dollar amount (``0.0`` when not
    applicable). Modeling it as ``Price`` would mis-fire on the legitimate
    zero / non-monetary cases.
    """

    action_type: CorporateActionType
    alpaca_activity_id: str
    ticker: str
    new_ticker: str | None
    ratio_or_amount: float
    pre_action_quantity: float
    post_action_quantity: float
    pre_action_cost_basis: Money
    post_action_cost_basis: Money
    signed_cash_impact_usd: Money
    parent_position_id: str | None
    resulting_position_status: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.CORPORATE_ACTION_APPLIED,
        CorporateActionAppliedDetail,
        EventGroup.CORPORATE_ACTION,
    ),
]


__all__ = ["CorporateActionAppliedDetail"]
