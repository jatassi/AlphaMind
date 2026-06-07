"""Typed Intent records: per-thesis PnL/cost-basis ledger + capital reservations.

Intent (CONTEXT.md) is a fact AlphaMind authors that the broker never owns —
the thesis, the order→thesis link, a capital reservation, per-thesis cost basis,
and per-thesis realized PnL with provenance. Sole authority: the pipeline; never
overwritten by a broker snapshot (ADR-0005: pipeline is the single writer).

* :class:`ThesisPnlLedgerRecord` — per-thesis realized PnL + cost basis with
  provenance. Story 03c *derives* these values from the broker-event log; this
  story defines the durable shape only.
* :class:`CapitalReservationRecord` — a per-thesis capital reservation written
  by the command-execution OPEN path when capital is reserved for a thesis, read by the
  projection / PnL derivations.

The order→thesis link itself rides the broker-carried link (story 01b) and the
existing ``orders.originating_thesis_id`` — there is no new link table here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from alphamind._kernel.ids import InvocationId, ThesisId
from alphamind._kernel.money import Money


class ThesisPnlLedgerRecord(BaseModel):
    """Frozen per-thesis realized-PnL / cost-basis ledger entry.

    ``realized_pnl_usd`` and ``cost_basis_usd`` are :class:`Money`
    (Decimal-backed) and round-trip exactly through the ``DecimalText`` column.
    ``provenance_json`` carries the derivation trail (which event-log rows the
    figures were folded from) so a value is always traceable to its source
    events. ``derived_from_invocation_id`` records the pipeline invocation that
    last (re)derived the entry. ``last_derived_event_seq`` (ALP-865) is the max
    ``broker_event_log.event_seq`` folded into the figures — the per-thesis
    re-derivation watermark; ``None`` means never derived (always dirty).
    """

    model_config = ConfigDict(frozen=True)

    thesis_id: ThesisId
    realized_pnl_usd: Money
    cost_basis_usd: Money
    provenance_json: str
    derived_from_invocation_id: InvocationId | None
    updated_at: datetime
    last_derived_event_seq: int | None = None


class CapitalReservationRecord(BaseModel):
    """Frozen per-thesis capital-reservation entry.

    Written by the command-execution OPEN path when capital is reserved for a thesis;
    read by the projection / PnL derivations. ``released_at`` is null while the
    reservation is live and set when the capital is released.
    """

    model_config = ConfigDict(frozen=True)

    reservation_id: str
    thesis_id: ThesisId
    reserved_capital_usd: Money
    reserved_by_invocation_id: InvocationId | None
    reserved_at: datetime
    released_at: datetime | None


__all__ = [
    "CapitalReservationRecord",
    "ThesisPnlLedgerRecord",
]
