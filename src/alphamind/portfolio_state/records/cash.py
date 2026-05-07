"""Cash ledger records (Tier 1 — raw state category 4a).

Per ``state-persistence.md`` § Tier 1 — Core entities, the cash ledger is a
single mutable record representing the portfolio's cash state. ``CashLedger``
embeds zero-or-more ``UnsettledProceedsEntry`` value objects describing
in-flight settlements.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CashLedger",
    "UnsettledProceedsEntry",
]

_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class UnsettledProceedsEntry(BaseModel):
    """A single in-flight settlement."""

    model_config = ConfigDict(frozen=True)

    settlement_date: datetime
    amount_usd: _FiniteFloat
    source_transaction_id: str

    @field_validator("settlement_date")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "settlement_date must be timezone-aware"
            raise ValueError(msg)
        return v


class CashLedger(BaseModel):
    """Raw state 4a — cash and buying power."""

    model_config = ConfigDict(frozen=True)

    current_cash_usd: _FiniteFloat
    settled_cash_usd: _FiniteFloat
    reserved_capital_usd: _FiniteFloat
    available_buying_power_usd: _FiniteFloat
    margin_held_usd: _FiniteFloat
    unsettled_proceeds: tuple[UnsettledProceedsEntry, ...]
    cash_pct_of_portfolio: Annotated[float, Field(ge=0.0, le=100.0)]
    true_deployable_capital_usd: _FiniteFloat
    regt_excess_trailing_30d_usd: _FiniteFloat
    regt_excess_trailing_90d_usd: _FiniteFloat
    regt_excess_lifetime_usd: _FiniteFloat
