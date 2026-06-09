"""Shared broker-enforced protective-leg resolver for equity CLOSE (ALP-939).

A native equity bracket's broker-enforced OCO protective legs reserve 100% of a
position's shares (``held_for_orders``), so an equity CLOSE must cancel them at
the broker before its SIMPLE close sell or the sell sees ``available: 0``. Both
the decision side (PM-directed CLOSE — ``submit_envelope.dispatch``) and the
execution side (engine-envelope CLOSE — ``oms.submit_engine_envelope``) resolve
the same set of broker-enforced leg ids; this module is the single copy they
share, lifted out of the decision-side privates (ALP-937) so the two callers
cannot drift.

Lives in ``alphamind.state`` because it reads state-layer tables (``orders`` /
``bracket_legs``) directly — the :mod:`alphamind.state.drawdown_reader` shape,
deliberately not routed through ``state/repository`` (whose ``__init__`` eagerly
imports ``sql_repository``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.portfolio_state.records.orders import (
    PROTECTIVE_LEG_ROLE_VALUES,
    EnforcementBinding,
    OrderStatus,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.orders import OrderRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "equity_broker_enforced_protective_leg_ids",
    "leg_enforcement_binding",
]


async def equity_broker_enforced_protective_leg_ids(
    session: AsyncSession, *, bracket_id: str | None
) -> tuple[AlpacaOrderId, ...]:
    """Resolve a bracket's PENDING broker-enforced protective legs' broker ids (ALP-937).

    These are the native-bracket OCO legs that reserve 100% of the position's
    shares (``held_for_orders``); an equity CLOSE must cancel them at the broker
    before its SIMPLE sell or the sell sees ``available: 0``. Read each PENDING
    protective ``orders`` row for the bracket, then its typed
    ``enforcement_binding`` from ``bracket_legs`` (ALP-847). Only
    ``BROKER_ENFORCED`` legs carrying a real broker id are returned: a
    monitor-enforced leg (e.g. TIME_STOP) has no broker order, so it is never
    sent to ``submit_cancel`` — its state-side cancellation rides the CLOSE
    writeback instead.

    Returns an empty tuple when *bracket_id* is None (a position with no bracket).
    """
    if bracket_id is None:
        return ()
    stmt = select(OrderRow).where(
        OrderRow.bracket_id == bracket_id,
        OrderRow.status == OrderStatus.PENDING.value,
    )
    rows = (await session.execute(stmt)).scalars().all()
    leg_ids: list[AlpacaOrderId] = []
    for row in rows:
        if row.order_role not in PROTECTIVE_LEG_ROLE_VALUES or row.alpaca_order_id is None:
            continue
        binding = await leg_enforcement_binding(session, order_id=row.order_id)
        if binding is EnforcementBinding.BROKER_ENFORCED:
            leg_ids.append(AlpacaOrderId(row.alpaca_order_id))
    return tuple(leg_ids)


async def leg_enforcement_binding(session: AsyncSession, *, order_id: str) -> EnforcementBinding:
    """Read the protective leg's typed ``enforcement_binding`` by its ``order_id``.

    The binding is the Broker-Owned-Fact-vs-Intent distinction (ADR-0003) the
    dispatch router keys on. It lives on ``bracket_legs`` (populated by the OPEN
    writeback, 02c), keyed by the leg's ``order_id``. An ``order_id`` with no
    ``bracket_legs`` row (a non-protective order — e.g. a primary entry) is
    treated as ``BROKER_ENFORCED``: it always carries a real broker order, so a
    CANCEL must dispatch, never local-cancel.
    """
    stmt = select(BracketLegRow.enforcement_binding).where(BracketLegRow.order_id == order_id)
    binding = (await session.execute(stmt)).scalars().one_or_none()
    if binding is None:
        return EnforcementBinding.BROKER_ENFORCED
    return EnforcementBinding(binding)
