"""Auto re-protection of a partial-close remainder (ALP-938).

After a PM-directed **partial** equity CLOSE, ALP-937 cancels every broker-enforced
protective leg but leaves the bracket ACTIVE on the remainder, so the remaining
shares are broker-unprotected until the PM re-evaluates. Fill collection marks such
a position ``reprotection_needed=1`` (see
``write_paths/fill_collection._exit_fill_left_naked_remainder``); this module is the
DB half of the post-fill-collection re-bracket step that closes that window:

* :func:`gather_reprotection_candidates` — the **read** phase (no write lock):
  resolve each flagged position's remaining size, protective side, ticker, the
  original protective levels (off the cancelled TAKE_PROFIT / PRICE_STOP order rows)
  and the original legs' trigger geometry to replay.
* :func:`persist_reprotection` — the **persist** phase (inside a write transaction):
  append a fresh TAKE_PROFIT + PRICE_STOP order + leg pair (carrying the new OCO
  broker ids when broker-routed) onto the still-ACTIVE bracket, clear the marker, and
  re-materialize the bracket through the read codec as a write-time guard.

The broker **submit** between the two lives in ``scheduler/reprotection.py`` — it
holds the ``TradingClient`` and must not run under the SQLite write lock (the ALP-824
invariant), so the orchestration is split across the layer boundary.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Literal

from sqlalchemy import select

from alphamind._kernel.ids import AlpacaOrderId, OrderId, ThesisId
from alphamind.execution.broker_adapter import EquityLegAck, EquityOcoLevels
from alphamind.execution.oms.command_ids import synthesize_id_suffix
from alphamind.execution.write_paths.command_execution._shared import (
    _assert_bracket_readable,
    _build_pending_order,
    _order_direction_for_close,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegStatus,
    BracketLegType,
    EnforcementBinding,
    OrderClass,
    OrderRecord,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    PositionStatus,
    position_direction,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets_codec import leg_to_row, row_to_leg
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import record_to_row as order_record_to_row
from alphamind.state.tables.orders_codec import row_to_record as order_row_to_record
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record as position_row_to_record

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ReprotectionCandidate:
    """One naked partial-close remainder to re-protect with a fresh OCO (ALP-938)."""

    position_id: str
    bracket_id: str
    ticker: str
    remaining_qty: float
    position_side: Literal["long", "short"]
    thesis_id: ThesisId
    # The original (now CANCELLED) protective order + leg pair, carried so the
    # persist can replay the exact levels (off the orders) and trigger geometry
    # (off the legs) onto fresh ACTIVE legs.
    take_profit_order: OrderRecord
    take_profit_leg: BracketLeg
    price_stop_order: OrderRecord
    price_stop_leg: BracketLeg

    @property
    def levels(self) -> EquityOcoLevels:
        """The OCO protective levels read off the original protective order rows."""
        tp_price = self.take_profit_order.price_parameters.limit_price
        stop_price = self.price_stop_order.price_parameters.stop_trigger_price
        # gather only emits a candidate when both are present (see _build_candidate);
        # raise (not assert — survives ``python -O``) so a malformed candidate fails
        # loudly here rather than as a float(None) crash deeper in submit_equity_oco.
        if tp_price is None or stop_price is None:
            msg = (
                f"reprotection levels require both a take-profit limit and a stop "
                f"trigger; got tp={tp_price}, stop={stop_price}"
            )
            raise ValueError(msg)
        return EquityOcoLevels(
            take_profit_price=tp_price,
            stop_price=stop_price,
            stop_limit_price=self.price_stop_order.price_parameters.limit_price,
        )


async def gather_reprotection_candidates(
    handle: InvocationHandle,
) -> tuple[ReprotectionCandidate, ...]:
    """Resolve every ``reprotection_needed=1`` OPEN equity position to a candidate.

    Read-only — runs in its own read session with no write lock held (ALP-824).
    A position is skipped (and logged) when it lacks a thesis, a bracket, or a
    well-formed cancelled TAKE_PROFIT + PRICE_STOP pair to replay; the marker
    stays set so a later invocation can retry once the precondition holds.
    """
    stmt = select(PositionRow).where(
        PositionRow.reprotection_needed == 1,
        PositionRow.status == PositionStatus.OPEN.value,
        PositionRow.instrument_type == InstrumentType.EQUITY.value,
        PositionRow.bracket_id.is_not(None),
    )
    rows = list((await handle.session.execute(stmt)).scalars())
    candidates: list[ReprotectionCandidate] = []
    for pos_row in rows:
        candidate = await _build_candidate(handle, pos_row)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


async def _build_candidate(
    handle: InvocationHandle, pos_row: PositionRow
) -> ReprotectionCandidate | None:
    position = position_row_to_record(pos_row)
    if not isinstance(position.details, EquityPositionDetails):
        return None
    direction = position_direction(position)
    if direction is None or position.thesis_id is None or position.bracket_id is None:
        return None
    remaining_qty = position.details.share_count
    if remaining_qty <= 0:
        return None

    tp_pair = await _latest_protective_pair(handle, position.bracket_id, BracketLegType.TAKE_PROFIT)
    stop_pair = await _latest_protective_pair(
        handle, position.bracket_id, BracketLegType.PRICE_STOP
    )
    if tp_pair is None or stop_pair is None:
        logger.warning(
            "reprotection: position %s flagged but missing a TAKE_PROFIT/PRICE_STOP pair "
            "to replay (take_profit=%s, price_stop=%s) — leaving the marker set",
            position.position_id,
            tp_pair is not None,
            stop_pair is not None,
        )
        return None
    tp_order, tp_leg = tp_pair
    stop_order, stop_leg = stop_pair
    if (
        tp_order.price_parameters.limit_price is None
        or stop_order.price_parameters.stop_trigger_price is None
    ):
        logger.warning(
            "reprotection: position %s protective levels are incomplete "
            "(tp_limit=%s, stop_trigger=%s) — cannot build an OCO; leaving the marker set",
            position.position_id,
            tp_order.price_parameters.limit_price,
            stop_order.price_parameters.stop_trigger_price,
        )
        return None

    return ReprotectionCandidate(
        position_id=position.position_id,
        bracket_id=position.bracket_id,
        ticker=position.details.ticker,
        remaining_qty=remaining_qty,
        position_side="long" if direction == Direction.LONG else "short",
        thesis_id=position.thesis_id,
        take_profit_order=tp_order,
        take_profit_leg=tp_leg,
        price_stop_order=stop_order,
        price_stop_leg=stop_leg,
    )


async def _latest_protective_pair(
    handle: InvocationHandle, bracket_id: str, leg_type: BracketLegType
) -> tuple[OrderRecord, BracketLeg] | None:
    """The highest-``leg_index`` CANCELLED leg of *leg_type* on *bracket_id* + its order.

    Restricted to CANCELLED legs: re-protection replays the levels of a *naked*
    (cancelled) protective leg, never a currently-ACTIVE one — gather is only reached
    when the marker fired, which requires every leg CANCELLED, so this is the latest
    cancelled geometry. Filtering on status keeps a stale read or a future weakening
    of that invariant from replaying a live leg's levels onto a duplicate OCO.
    Highest index = the most recently cancelled protective geometry (a re-protected
    remainder appends fresh legs), so a position re-protected more than once replays
    its latest levels rather than a stale original. Returns ``None`` when no such leg
    or its backing order is present.
    """
    leg_stmt = (
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id == bracket_id)
        .where(BracketLegRow.leg_type == leg_type.value)
        .where(BracketLegRow.leg_status == BracketLegStatus.CANCELLED.value)
        .order_by(BracketLegRow.leg_index.desc())
        .limit(1)
    )
    leg_row = (await handle.session.execute(leg_stmt)).scalars().first()
    if leg_row is None or leg_row.order_id is None:
        return None
    order_row = await handle.session.get(OrderRow, leg_row.order_id)
    if order_row is None:
        return None
    return order_row_to_record(order_row), row_to_leg(leg_row)


async def persist_reprotection(
    handle: InvocationHandle,
    *,
    candidate: ReprotectionCandidate,
    client_order_id: str,
    leg_acks: tuple[EquityLegAck, ...],
    broker_enforced: bool,
    timestamp: datetime,
) -> None:
    """Append the re-bracketed remainder onto the still-ACTIVE bracket (ALP-938).

    Builds a fresh TAKE_PROFIT + PRICE_STOP order + leg pair from the candidate's
    original levels / trigger geometry, stamping the new OCO broker ids from
    *leg_acks* (``broker_enforced=True``); in broker-inactive (debug_e2e) mode
    *leg_acks* is empty and the legs persist MONITOR_ENFORCED with NULL broker ids,
    mirroring the OPEN path. Clears ``reprotection_needed`` and re-materializes the
    bracket through the read codec (``_assert_bracket_readable``).
    """
    acks_by_role: dict[str, AlpacaOrderId] = {ack.role: ack.alpaca_order_id for ack in leg_acks}
    binding = (
        EnforcementBinding.BROKER_ENFORCED
        if broker_enforced
        else EnforcementBinding.MONITOR_ENFORCED
    )
    close_direction = _order_direction_for_close(
        Direction.LONG if candidate.position_side == "long" else Direction.SHORT
    )
    suffix = synthesize_id_suffix(client_order_id)
    # The leg_index is woven into the persisted order_id / leg_id PKs (below): the
    # client_order_id suffix is invocation-invariant (synthesize_id_suffix strips the
    # ~inv- link, so re-protecting the SAME position again mints the same suffix), and
    # only the monotonic leg_index makes the second re-protection's rows distinct —
    # without it the re-INSERT would collide on the primary key.
    tp_index = await _next_leg_index(handle, candidate.bracket_id)
    stop_index = tp_index + 1

    def _build(
        *, original_order: OrderRecord, original_leg: BracketLeg, order_id: str, leg_id: str
    ) -> tuple[OrderRecord, BracketLeg]:
        # Replay the original's role / order_type / price_parameters at the
        # remaining quantity (OPEN-path ``_build_pending_order``); the leg replays
        # the original's leg_type / trigger / enforcement / trigger_signal /
        # pl_anchor, swapping only identity, order id, ACTIVE status, and the
        # broker-vs-monitor binding. The OCO ack role is keyed off the leg type:
        # TAKE_PROFIT → ``take_profit``, PRICE_STOP → ``stop_loss``.
        oco_role = (
            "take_profit" if original_leg.leg_type == BracketLegType.TAKE_PROFIT else "stop_loss"
        )
        broker_id = acks_by_role.get(oco_role)
        order = _build_pending_order(
            order_id=order_id,
            position_id=candidate.position_id,
            bracket_id=candidate.bracket_id,
            role=original_order.role,
            order_class=OrderClass.OCO,
            direction=close_direction,
            order_type=original_order.order_type,
            price_parameters=original_order.price_parameters,
            quantity=candidate.remaining_qty,
            ticker=candidate.ticker,
            pm_command_id=client_order_id,
            thesis_id=candidate.thesis_id,
            timestamp=timestamp,
            alpaca_order_id_override=str(broker_id) if broker_id is not None else None,
        )
        leg = dataclasses.replace(
            original_leg,
            leg_id=leg_id,
            order_id=OrderId(order_id),
            status=BracketLegStatus.ACTIVE,
            enforcement_binding=binding,
        )
        return order, leg

    tp_order, tp_leg = _build(
        original_order=candidate.take_profit_order,
        original_leg=candidate.take_profit_leg,
        order_id=f"ORD-RBR-tp-{suffix}-{tp_index}",
        leg_id=f"{candidate.bracket_id}-leg-rbr-tp-{suffix}-{tp_index}",
    )
    stop_order, stop_leg = _build(
        original_order=candidate.price_stop_order,
        original_leg=candidate.price_stop_leg,
        order_id=f"ORD-RBR-stop-{suffix}-{stop_index}",
        leg_id=f"{candidate.bracket_id}-leg-rbr-stop-{suffix}-{stop_index}",
    )

    handle.session.add(order_record_to_row(tp_order))
    handle.session.add(order_record_to_row(stop_order))
    handle.session.add(leg_to_row(tp_leg, bracket_id=candidate.bracket_id, leg_index=tp_index))
    handle.session.add(leg_to_row(stop_leg, bracket_id=candidate.bracket_id, leg_index=stop_index))

    pos_row = await handle.session.get(PositionRow, candidate.position_id)
    if pos_row is not None:
        pos_row.reprotection_needed = 0

    await _assert_bracket_readable(handle, bracket_id=candidate.bracket_id)


async def _next_leg_index(handle: InvocationHandle, bracket_id: str) -> int:
    stmt = select(BracketLegRow.leg_index).where(BracketLegRow.bracket_id == bracket_id)
    indices = [int(i) for i in (await handle.session.execute(stmt)).scalars()]
    return (max(indices) + 1) if indices else 0


__all__ = [
    "ReprotectionCandidate",
    "gather_reprotection_candidates",
    "persist_reprotection",
]
