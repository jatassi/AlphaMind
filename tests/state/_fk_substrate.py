"""Minimal FK-satisfying parent-row stubs for state-persistence tests.

All deferrable FKs are checked at COMMIT, so cyclic references
(positions ↔ theses ↔ brackets ↔ orders) must be seeded in a single
transaction.  This module provides one helper per entity type that
supplies just the non-NULL columns, and a combined ``seed_position_cluster``
that inserts the full position/thesis/bracket/order graph atomically.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.portfolio_state.records.orders import (
    BracketStatus,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRole,
    OrderStatus,
    OrderType,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.theses import ThesisRow

_TS = "2026-05-07T14:30:00Z"


def stub_position_row(
    position_id: str,
    *,
    thesis_id: str | None = None,
    bracket_id: str | None = None,
    parent_position_id: str | None = None,
    status: str = PositionStatus.PENDING.value,
    direction: str = Direction.LONG.value,
) -> PositionRow:
    return PositionRow(
        position_id=position_id,
        thesis_id=thesis_id,
        bracket_id=bracket_id,
        status=status,
        direction=direction,
        entry_timestamp=None,
        instrument_type=InstrumentType.EQUITY.value,
        details_json=(
            f'{{"instrument_type":"{InstrumentType.EQUITY.value}",'
            '"ticker":"STUB","share_count":0,"average_cost_basis_per_share":0}'
        ),
        execution_history_json="[]",
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=0,
        parent_position_id=parent_position_id,
        origin=None,
    )


def stub_thesis_row(
    thesis_id: str,
    position_id: str,
    *,
    status: str = ThesisRecordStatus.ACTIVE.value,
) -> ThesisRow:
    return ThesisRow(
        thesis_id=thesis_id,
        position_id=position_id,
        status=status,
        resolution_timestamp=None,
        resolution_category=None,
        summary="stub",
        time_expectation_hours=24.0,
        position_size_rationale=None,
        generation_timestamp=_TS,
        narrative_json="{}",
    )


def stub_order_row(
    order_id: str,
    bracket_id: str,
    *,
    position_id: str | None = None,
    role: str = OrderRole.ENTRY.value,
    direction: str = OrderDirection.BUY.value,
    status: str = OrderStatus.FILLED.value,
) -> OrderRow:
    return OrderRow(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        order_role=role,
        order_class=OrderClass.SIMPLE.value,
        instrument_spec_json=f'{{"instrument_type":"{InstrumentType.EQUITY.value}","ticker":"STUB"}}',
        direction=direction,
        order_type=OrderType.MARKET.value,
        quantity=1.0,
        price_parameters_json="{}",
        duration=OrderDuration.DAY.value,
        status=status,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain_json=f'["alp-{order_id}"]',
        submission_timestamp=_TS,
        last_update_timestamp=_TS,
        filled_quantity=0.0,
        average_fill_price=None,
        remaining_quantity=1.0,
        modification_count=0,
        metadata_json='{"originating_thesis_id":null,"originating_pm_command_id":null,"age_hours":0.0}',
    )


def stub_bracket_row(
    bracket_id: str,
    position_id: str,
    entry_order_id: str,
    *,
    status: str = BracketStatus.PENDING_ENTRY.value,
) -> BracketRow:
    return BracketRow(
        bracket_id=bracket_id,
        position_id=position_id,
        status=status,
        entry_order_id=entry_order_id,
        entry_window_deadline=None,
        corporate_action_cancellation_reason=None,
        modification_history_json="[]",
    )


def seed_position_cluster(
    session: Session,
    *,
    position_id: str = "pos-1",
    thesis_id: str = "thesis-1",
    bracket_id: str = "bracket-1",
    entry_order_id: str = "order-1",
) -> None:
    """Insert position + thesis + bracket + entry-order in one transaction.

    All four rows reference each other cyclically, so they must land in a
    single deferred-FK transaction.  Call ``session.commit()`` after this
    helper if you need the rows visible outside the current transaction.
    """
    session.add(stub_position_row(position_id, thesis_id=thesis_id, bracket_id=bracket_id))
    session.add(stub_thesis_row(thesis_id, position_id))
    session.add(stub_order_row(entry_order_id, bracket_id, position_id=position_id))
    session.add(stub_bracket_row(bracket_id, position_id, entry_order_id))
