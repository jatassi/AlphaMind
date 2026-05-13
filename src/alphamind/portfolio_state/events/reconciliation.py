"""Reconciliation event details — Alpaca vs local-state divergences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphamind.portfolio_state.events.types import EventGroup, EventType


@dataclass(frozen=True, slots=True)
class ReconciliationAlertDetail:
    """Detail payload for ``RECONCILIATION_ALERT`` events.

    Emitted by the post-Phase-1 reconciliation step when local state diverges
    from Alpaca's authoritative ``GET /v2/positions`` / ``GET /v2/account``
    snapshot beyond the documented tolerance. One entry per unexplained delta;
    local state is *not* mutated to match Alpaca — auto-correction is deferred
    to the continuous monitor's reconciliation pass (ALP-123).

    ``domain`` discriminates the source of the delta: ``"position"`` for
    equity-position quantity mismatches, ``"cash"`` for ``cash_ledger`` /
    ``TradeAccount.cash`` mismatches, and ``"buying_power"`` for the
    ``buying_power`` mapping when surfaced. ``field_name`` carries the local
    field that mismatched (``share_count`` / ``contract_count`` /
    ``current_cash_usd``). ``local_value`` and ``alpaca_value`` carry the
    compared scalars and ``delta_description`` carries the operator-facing
    summary.
    """

    domain: Literal["position", "cash", "buying_power"]
    field_name: str
    local_value: float
    alpaca_value: float
    delta_description: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.RECONCILIATION_ALERT,
        ReconciliationAlertDetail,
        EventGroup.RECONCILIATION,
    ),
]


__all__ = ["ReconciliationAlertDetail"]
