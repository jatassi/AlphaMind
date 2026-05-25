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
    snapshot beyond the documented tolerance. One alert per unexplained delta.

    Mutation contract (ALP-619 / ALP-637). An alert WITHOUT a paired
    :class:`ReconciliationCorrectionDetail` row for the same
    ``(invocation_id, position_id, field_name)`` means local state was NOT
    mutated — the auto-correction path skipped this drift. Skips happen for:

    * Alpaca-only orphans (no local row to mutate honestly).
    * PENDING (not OPEN) equity / options positions.
    * Direction-flip alerts (Alpaca side disagrees with local direction;
      semantically distinct from a quantity drift, demands operator review).
    * Half-degraded broker state (``alpaca_positions=()`` with positive
      account evidence — gate suppresses position writeback to avoid wiping
      the book on a transient broker failure).

    When auto-correction does run (OPEN equity ``share_count`` drift, OPEN
    options ``contract_count`` drift, and ``cash_ledger.current_cash_usd``
    drift), the paired CORRECTION row captures the prior local value and
    the applied Alpaca value.

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


@dataclass(frozen=True, slots=True)
class ReconciliationCorrectionDetail:
    """Detail payload for ``RECONCILIATION_CORRECTION`` events (ALP-619).

    Emitted alongside a :class:`ReconciliationAlertDetail` row whenever the
    post-Phase-1 reconciliation step writes Alpaca's authoritative value back
    to local state — drift on an existing OPEN equity/options position
    (``share_count`` / ``contract_count``) or the singleton ``cash_ledger``
    row (``current_cash_usd``). Carries the forensic trail the operator needs
    to reconstruct why a local field flipped between two invocations:
    ``prior_local_value`` is what the local row held before the write,
    ``applied_alpaca_value`` is what Alpaca reported and what the row carries
    after the write.

    Auto-correction is deliberately narrowed to drift on existing rows.
    Alpaca-only orphans (a symbol present in ``GET /v2/positions`` with no
    matching local OPEN/PENDING row) continue to surface as ALERT-only —
    materializing a synthetic ``PositionRecord`` requires a thesis_id, a cost
    basis, and an execution history that can't be honestly synthesized from
    the Alpaca snapshot. Operator triage handles those out of band.

    See ``docs/design/05-execution-layer/corporate-actions.md`` § Phase 1
    integration sequence step 4 and ``broker-adapter.md`` § Account state
    queries — "Alpaca's positions and account endpoints are the source of
    truth. On disagreement, Alpaca wins."
    """

    domain: Literal["position", "cash"]
    field_name: str
    prior_local_value: float
    applied_alpaca_value: float


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.RECONCILIATION_ALERT,
        ReconciliationAlertDetail,
        EventGroup.RECONCILIATION,
    ),
    (
        EventType.RECONCILIATION_CORRECTION,
        ReconciliationCorrectionDetail,
        EventGroup.RECONCILIATION,
    ),
]


__all__ = ["ReconciliationAlertDetail", "ReconciliationCorrectionDetail"]
