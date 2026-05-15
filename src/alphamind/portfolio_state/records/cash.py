"""Cash ledger records (Tier 1 — raw state category 4a).

Per ``state-persistence.md`` § Tier 1 — Core entities, the cash ledger is a
single mutable record representing the portfolio's cash state. ``CashLedger``
embeds zero-or-more ``UnsettledProceedsEntry`` value objects describing
in-flight settlements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "CashLedger",
    "UnsettledProceedsEntry",
]


def _check_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite; got {value}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UnsettledProceedsEntry:
    """A single in-flight settlement."""

    settlement_date: datetime
    amount_usd: float
    source_transaction_id: str

    def __post_init__(self) -> None:
        if self.settlement_date.tzinfo is None or self.settlement_date.utcoffset() is None:
            msg = "settlement_date must be timezone-aware"
            raise ValueError(msg)
        _check_finite(self.amount_usd, "amount_usd")


@dataclass(frozen=True, slots=True)
class CashLedger:
    """Raw state 4a — cash and buying power."""

    current_cash_usd: float
    settled_cash_usd: float
    reserved_capital_usd: float
    available_buying_power_usd: float
    margin_held_usd: float
    unsettled_proceeds: tuple[UnsettledProceedsEntry, ...]
    cash_pct_of_portfolio: float
    true_deployable_capital_usd: float
    regt_excess_trailing_30d_usd: float
    regt_excess_trailing_90d_usd: float
    regt_excess_lifetime_usd: float

    def __post_init__(self) -> None:
        for field_name in (
            "current_cash_usd",
            "settled_cash_usd",
            "reserved_capital_usd",
            "available_buying_power_usd",
            "margin_held_usd",
            "true_deployable_capital_usd",
            "regt_excess_trailing_30d_usd",
            "regt_excess_trailing_90d_usd",
            "regt_excess_lifetime_usd",
        ):
            _check_finite(getattr(self, field_name), field_name)
        if not (0.0 <= self.cash_pct_of_portfolio <= 100.0):
            msg = (
                f"cash_pct_of_portfolio must satisfy 0 <= value <= 100; "
                f"got {self.cash_pct_of_portfolio}"
            )
            raise ValueError(msg)
