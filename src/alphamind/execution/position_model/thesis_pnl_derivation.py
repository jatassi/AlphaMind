"""Pure per-thesis realized-PnL / cost-basis derivation from the event log (ALP-851).

Invariant 3 (ADR-0001 / ADR-0002): per-thesis realized PnL and cost basis are
**Intent**, derivable from a thesis's :data:`broker_event_log` events **alone**
(fills + activities), never a join to a lose-able ``orders`` table and never
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
  open/close direction is the leg-authoritative ``buy`` / ``sell`` side read off
  the captured ``FillReport`` payload against the running net quantity — taken
  from the per-leg ``position_intent`` when present (mleg leg-children), else the
  order ``side`` (equity entries leave ``position_intent`` unset).
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
    """The thesis's events, ordered by ``(broker_timestamp, captured_at, event_key)``.

    The ``event_key`` tertiary key makes the order a total order independent of
    the DB row order, so the fold is deterministic even when two events share a
    timestamp (the ``broker_timestamp`` is broker-supplied and can collide).
    """
    mine = [e for e in events if e.thesis_id == thesis_id]
    mine.sort(key=lambda e: (e.broker_timestamp or e.captured_at, e.captured_at, e.event_key))
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

    Three activity shapes fold here, all reproducing their figures from the
    payload (booked at capture time) without re-deriving option math the log
    cannot see (the option's premium lives on the position, not the event):

    * **OPEXP / OPASN / OPEXC** — the option-exit legs. They carry the
      realized-PnL contribution (``realized_pnl_delta_usd``) AND the closed
      option contract quantity (``closed_contract_qty``). The quantity closes
      the option lot the buy FILL opened — without it the option would still
      show as held (phantom cost basis, and a later equity sell mis-classified
      as opening a short).
    * **OPTRD** — the paired equity leg of an assignment / exercise. It carries
      the opened-equity cost basis (``cost_basis_delta_usd``) and the equity
      share count (``equity_qty``); together they open the equity lot at the
      strike (avg cost = basis / qty) so a later equity sell closes it and the
      cost basis releases. (An OPTRD without ``equity_qty`` falls back to an
      unpriced external-basis addend — cleared on full close, FIX 3.)
    """
    payload = json.loads(event.raw_payload_json)

    closed_qty = payload.get("closed_contract_qty")
    if closed_qty is not None:
        lot.close_quantity(Decimal(str(closed_qty)))

    cost_basis_delta = payload.get("cost_basis_delta_usd")
    if cost_basis_delta is not None:
        equity_qty = payload.get("equity_qty")
        if equity_qty is not None:
            lot.open_priced_basis(Decimal(str(cost_basis_delta)), Decimal(str(equity_qty)))
        else:
            lot.add_external_basis(Decimal(str(cost_basis_delta)))

    realized = payload.get("realized_pnl_delta_usd")
    return Decimal(str(realized)) if realized is not None else DECIMAL_ZERO


_POSITION_INTENT_SIDE: dict[str, str] = {
    "buy_to_open": "buy",
    "buy_to_close": "buy",
    "sell_to_open": "sell",
    "sell_to_close": "sell",
}


def _fill_side(payload: dict[str, object]) -> str:
    """The leg-authoritative open/close side (``buy`` / ``sell``) of a FillReport.

    The side is read in priority order, leg-authoritative first:

    * **``position_intent``** (top-level on the report) is set PER LEG on every
      mleg leg-child (``buy_to_open`` / ``sell_to_close`` / …). It is the only
      leg-authoritative side: an mleg leg-child shares the PARENT ``TradeUpdate``
      dump as ``raw_event_payload``, whose ``order.side`` is the strategy NET
      direction (or ``None``) — reading that mis-signs a spread leg or aborts the
      fold on a null net side. ``position_intent`` is unset for equity entries,
      so it is only consulted when present.
    * **websocket** fills (02a ``translate_trade_update``) dump an alpaca-py
      ``TradeUpdate`` — the order is nested, so the side is at
      ``raw_event_payload['order']['side']``;
    * **REST-recovered** fills (03b ``recover_missed_fills_since``) dump an
      :class:`~alphamind.execution.broker_adapter.queries.OrderSnapshot`, which
      *is* the order — the side is top-level at ``raw_event_payload['side']``
      and there is no nested ``order`` key.

    Reading only the websocket path crashes the fold for any thesis carrying a
    recovered fill or an mleg leg-child, so all shapes are tried before surfacing.
    """
    intent = payload.get("position_intent")
    if isinstance(intent, str) and intent in _POSITION_INTENT_SIDE:
        return _POSITION_INTENT_SIDE[intent]

    raw = payload.get("raw_event_payload")
    if not isinstance(raw, dict):
        raw = {}
    nested_order = raw.get("order")
    nested_side = nested_order.get("side") if isinstance(nested_order, dict) else None
    side = nested_side if nested_side is not None else raw.get("side")
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
            self._reset_to_flat()
        elif not _same_sign(self._net_qty, direction):
            # Flipped through zero: residual opens a fresh lot at the fill price.
            self._avg_cost = price
        return realized

    def close_quantity(self, qty: Decimal) -> None:
        """Close *qty* of the open lot with no realized PnL (a lifecycle exit).

        An OPEXP / OPASN / OPEXC carries its realized PnL on the payload, so the
        lot only needs to release the closed contracts: reduce the magnitude of
        ``net_qty`` toward zero (clamped — never flipping sign), and reset the
        lot to flat when it reaches zero (FIX 2 / FIX 3). The realized figure is
        booked by the caller from the payload, not re-derived here.
        """
        if qty <= 0 or self._net_qty == 0:
            return
        close_abs = min(qty, abs(self._net_qty))
        direction = Decimal(1) if self._net_qty > 0 else Decimal(-1)
        self._net_qty -= direction * close_abs
        if self._net_qty == 0:
            self._reset_to_flat()

    def open_priced_basis(self, basis: Decimal, qty: Decimal) -> None:
        """Open a priced lot of *qty* shares whose total cost basis is *basis*.

        The OPTRD equity leg of an assignment / exercise: ``qty`` shares enter
        the lot at avg cost ``basis / qty`` (the strike), so a later equity sell
        closes against them and the basis releases proportionally. Falls back to
        an unpriced external addend only when ``qty`` is non-positive.
        """
        if qty <= 0:
            self.add_external_basis(basis)
            return
        self.apply(qty, basis / qty)

    def add_external_basis(self, basis: Decimal) -> None:
        """Add cost basis sourced outside the fill stream (an unpriced OPTRD leg)."""
        self._external_basis += basis

    def _reset_to_flat(self) -> None:
        """Return the lot to flat: no open quantity, no residual basis (FIX 3).

        Clearing ``external_basis`` on a full close is what FIX 3 repairs — left
        intact, ``cost_basis()`` would return a phantom non-zero basis for a
        fully-exited thesis (``0 * 0 + external_basis``).
        """
        self._avg_cost = DECIMAL_ZERO
        self._external_basis = DECIMAL_ZERO

    def cost_basis(self) -> Decimal:
        """Capital tied up in the still-open lot, plus any external basis."""
        return abs(self._net_qty) * self._avg_cost + self._external_basis


def _same_sign(a: Decimal, b: Decimal) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


__all__ = ["ThesisPnlDerivation", "derive_thesis_pnl"]
