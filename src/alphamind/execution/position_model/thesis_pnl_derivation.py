"""Pure per-thesis realized-PnL / cost-basis derivation from the event log (ALP-851).

Invariant 3 (ADR-0001 / ADR-0002): per-thesis realized PnL and cost basis are
**Intent**, derivable from a thesis's :data:`broker_event_log` events **alone**
(fills ∪ activities), never a join to a lose-able ``orders`` table and never
overwritten by a broker snapshot. :func:`derive_thesis_pnl` is the functional
core — a pure, deterministic fold over the event records (plain values, no I/O):
the same event set always yields the same figures. The imperative shell
(:mod:`alphamind.execution.write_paths.thesis_pnl_ledger`) reads the events for a
thesis and writes the derived figures to ``thesis_pnl_ledger`` as the single
writer.

Two event families contribute (CONTEXT.md, ADR-0002):

* **Fills** (``FILL``) — the ``trade_updates`` executions. Realized PnL and cost
  basis fold out of the fill cashflows via an average-cost lot model: an opening
  fill adds capital to the open lot's cost basis; a closing fill realizes PnL
  against the running average cost and releases basis proportionally. The
  open/close direction is the broker order ``side`` (``buy`` / ``sell``) read off
  the captured ``FillReport`` payload against the running net quantity — no
  ``position_intent`` is required (it is unset for equity entries).
* **Activities** (``OPEXP`` / ``OPASN`` / ``OPEXC``) — option-lifecycle events
  that change a position with **no fill** (an OTM expiry, an assignment). They
  carry their realized-PnL contribution on the event payload
  (``realized_pnl_delta_usd``), booked by the activity capturer
  (:mod:`alphamind.execution.account_activities.handlers`) at capture time so the
  contribution lives in the log and the fold reproduces it. The paired ``OPTRD``
  leg carries the opened-equity cost basis (``cost_basis_delta_usd``); the option
  PnL is the ``-premium`` on the ``OPASN`` / ``OPEXC`` row, so the strike
  economics are not double-counted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from alphamind._kernel.ids import ThesisId
from alphamind._kernel.money import DECIMAL_ZERO, Money, money, signed_money
from alphamind.state.records_broker_event_log import BrokerEventRecord, BrokerEventType


@dataclass(frozen=True, slots=True)
class ThesisPnlDerivation:
    """The figures a thesis's event log folds to — realized PnL + cost basis.

    ``realized_pnl_usd`` is signed (a loss is negative). ``cost_basis_usd`` is
    the capital tied up in the thesis's still-open lots — non-negative.
    ``provenance_event_keys`` is the ordered tuple of ``event_key`` values the
    figures were folded from, so a value is always traceable to its source
    events.
    """

    realized_pnl_usd: Money
    cost_basis_usd: Money
    provenance_event_keys: tuple[str, ...]


def derive_thesis_pnl(
    thesis_id: ThesisId,
    events: tuple[BrokerEventRecord, ...],
) -> ThesisPnlDerivation:
    """Fold a thesis's ``broker_event_log`` events into realized PnL + cost basis.

    Pure and deterministic: the same event set yields the same figures. Events
    are folded in ``broker_timestamp`` order (``captured_at`` breaks ties) so the
    average-cost lot model sees opening fills before the closes that realize
    against them. Events that do not belong to *thesis_id* are ignored, so a
    caller may pass an over-broad slice.
    """
    realized = DECIMAL_ZERO
    lot = _Lot()
    provenance: list[str] = []
    for event in _ordered_for_thesis(thesis_id, events):
        provenance.append(event.event_key)
        realized += _realized_delta(event, lot)

    return ThesisPnlDerivation(
        realized_pnl_usd=signed_money(realized),
        cost_basis_usd=money(lot.cost_basis()),
        provenance_event_keys=tuple(provenance),
    )


def _ordered_for_thesis(
    thesis_id: ThesisId, events: tuple[BrokerEventRecord, ...]
) -> list[BrokerEventRecord]:
    """The thesis's events, ordered by ``(broker_timestamp, captured_at)``."""
    mine = [e for e in events if e.thesis_id == thesis_id]
    mine.sort(key=lambda e: (e.broker_timestamp or e.captured_at, e.captured_at))
    return mine


def _realized_delta(event: BrokerEventRecord, lot: _Lot) -> Decimal:
    """The signed realized-PnL contribution of one event; mutates *lot*."""
    if event.event_type is BrokerEventType.FILL:
        return _fill_realized_delta(event, lot)
    return _activity_realized_delta(event, lot)


def _fill_realized_delta(event: BrokerEventRecord, lot: _Lot) -> Decimal:
    """Apply a FILL to the lot model; return the realized PnL it produced."""
    payload = json.loads(event.raw_payload_json)
    price_d = Decimal(str(payload["fill_price"]))
    qty = Decimal(str(payload["fill_quantity"]))
    side = _fill_side(payload)
    signed_qty = qty if side == "buy" else -qty
    return lot.apply(signed_qty, price_d)


def _activity_realized_delta(event: BrokerEventRecord, lot: _Lot) -> Decimal:
    """Apply an option-lifecycle activity; return its realized-PnL delta.

    The activity carries its realized-PnL contribution
    (``realized_pnl_delta_usd``) and any opened-equity cost basis
    (``cost_basis_delta_usd``) on the payload, booked at capture time — so the
    fold reproduces the figure from the log without re-deriving option math it
    cannot see (the option's premium lives on the position, not the event).
    """
    payload = json.loads(event.raw_payload_json)
    cost_basis_delta = payload.get("cost_basis_delta_usd")
    if cost_basis_delta is not None:
        lot.add_external_basis(Decimal(str(cost_basis_delta)))
    realized = payload.get("realized_pnl_delta_usd")
    return Decimal(str(realized)) if realized is not None else DECIMAL_ZERO


def _fill_side(payload: dict[str, object]) -> str:
    """The broker order side (``buy`` / ``sell``) off a captured FillReport payload."""
    raw = payload.get("raw_event_payload")
    order = raw.get("order") if isinstance(raw, dict) else None
    side = order.get("side") if isinstance(order, dict) else None
    if side not in ("buy", "sell"):
        msg = f"FILL event payload carries no buy/sell order side; got {side!r}"
        raise ValueError(msg)
    return str(side)


class _Lot:
    """Running average-cost lot for one thesis across its fills.

    Tracks signed ``net_qty`` (positive long, negative short) and the average
    cost of the open lot. An opening fill (same sign, or from flat) adds to the
    lot; a closing fill (opposite sign) realizes PnL against the average cost and
    releases basis proportionally. A fill that flips the lot through zero closes
    the old lot entirely and opens a new one with the residual at the fill price.
    """

    __slots__ = ("_avg_cost", "_external_basis", "_net_qty")

    def __init__(self) -> None:
        self._net_qty = DECIMAL_ZERO
        self._avg_cost = DECIMAL_ZERO
        self._external_basis = DECIMAL_ZERO

    def apply(self, signed_qty: Decimal, price: Decimal) -> Decimal:
        """Apply a signed fill quantity at *price*; return realized PnL."""
        if signed_qty == 0:
            return DECIMAL_ZERO
        opening = self._net_qty == 0 or _same_sign(self._net_qty, signed_qty)
        if opening:
            self._open(signed_qty, price)
            return DECIMAL_ZERO
        return self._close(signed_qty, price)

    def _open(self, signed_qty: Decimal, price: Decimal) -> None:
        new_qty = self._net_qty + signed_qty
        old_abs = abs(self._net_qty)
        add_abs = abs(signed_qty)
        self._avg_cost = (self._avg_cost * old_abs + price * add_abs) / (old_abs + add_abs)
        self._net_qty = new_qty

    def _close(self, signed_qty: Decimal, price: Decimal) -> Decimal:
        closing_abs = min(abs(signed_qty), abs(self._net_qty))
        direction = Decimal(1) if self._net_qty > 0 else Decimal(-1)
        # Long close: (exit - avg) * qty; short close: (avg - exit) * qty.
        realized = (price - self._avg_cost) * direction * closing_abs
        self._net_qty += signed_qty
        if self._net_qty == 0:
            self._avg_cost = DECIMAL_ZERO
        elif not _same_sign(self._net_qty, direction):
            # Flipped through zero: residual opens a fresh lot at the fill price.
            self._avg_cost = price
        return realized

    def add_external_basis(self, basis: Decimal) -> None:
        """Add cost basis sourced outside the fill stream (an OPTRD equity leg)."""
        self._external_basis += basis

    def cost_basis(self) -> Decimal:
        """Capital tied up in the still-open lot, plus any external basis."""
        return abs(self._net_qty) * self._avg_cost + self._external_basis


def _same_sign(a: Decimal, b: Decimal) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


__all__ = ["ThesisPnlDerivation", "derive_thesis_pnl"]
