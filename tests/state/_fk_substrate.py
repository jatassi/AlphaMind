"""Minimal FK-satisfying parent-row stubs for state-persistence tests.

All deferrable FKs are checked at COMMIT, so cyclic references
(positions ↔ theses ↔ brackets ↔ orders) must be seeded in a single
transaction.  This module provides one helper per entity type that
supplies just the non-NULL columns, and a combined ``seed_position_cluster``
that inserts the full position/thesis/bracket/order graph atomically.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.portfolio_state.records.orders import (
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
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
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.state.tables.theses import ThesisRow

_TS = "2026-05-07T14:30:00Z"
_DEFAULT_PROCESS_LIFETIME_ID = "plt-stub-1"


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
    alpaca_order_id: str | None = None,
    client_order_id: str | None = None,
) -> OrderRow:
    # ``alpaca_order_id`` defaults to the synthetic ``alp-{order_id}`` placeholder;
    # pass a real broker UUID to exercise the alpaca_order_id-keyed resolution
    # path (ALP-746). ``client_order_id`` defaults to NULL; pass the command_id to
    # exercise the ALP-836 client_order_id-keyed (pre-backfill) resolution path.
    resolved_alpaca_id = alpaca_order_id or f"alp-{order_id}"
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
        alpaca_order_id=AlpacaOrderId(resolved_alpaca_id),
        alpaca_order_id_chain_json=f'["{resolved_alpaca_id}"]',
        submission_timestamp=_TS,
        last_update_timestamp=_TS,
        filled_quantity=0.0,
        average_fill_price=None,
        remaining_quantity=1.0,
        modification_count=0,
        metadata_json='{"originating_thesis_id":null,"originating_pm_command_id":null,"age_hours":0.0}',
        client_order_id=client_order_id,
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


def stub_bracket_leg_row(
    bracket_leg_id: str,
    bracket_id: str,
    *,
    leg_index: int,
    order_id: str | None,
    enforcement_binding: str,
    leg_type: str = BracketLegType.PRICE_STOP.value,
    leg_status: str = BracketLegStatus.ACTIVE.value,
    enforcement: str = BracketLegEnforcement.MECHANICAL.value,
) -> BracketLegRow:
    """Minimal ``bracket_legs`` row with a valid PRICE trigger payload.

    The trigger payload is a well-formed ``price`` discriminator so a CLOSE
    writeback that reads the leg back through ``brackets_codec`` decodes cleanly.
    """
    return BracketLegRow(
        bracket_leg_id=bracket_leg_id,
        bracket_id=bracket_id,
        leg_index=leg_index,
        leg_type=leg_type,
        order_id=order_id,
        trigger_kind="PRICE",
        trigger_payload_json=json.dumps(
            {
                "trigger_type": "price",
                "underlying_ticker": "STUB",
                "threshold_usd": 1.0,
                "direction": "LTE",
            }
        ),
        pl_anchor_json=None,
        enforcement=enforcement,
        enforcement_binding=enforcement_binding,
        leg_status=leg_status,
        trigger_signal=None,
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


def stub_process_lifetime_row(
    process_lifetime_id: str = _DEFAULT_PROCESS_LIFETIME_ID,
) -> ProcessLifetimeRow:
    """Minimal ``process_lifetimes`` row — the ``invocations`` FK target."""
    return ProcessLifetimeRow(
        process_lifetime_id=process_lifetime_id,
        process_role="monitor",
        process_start_at=_TS,
        process_pid=1,
        hostname="stub-host",
        git_sha="0" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.0",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="snapshot/path",
        anthropic_sdk_version="0.0.0",
        claude_agent_sdk_version="0.0.0",
        os_release="stub-os",
    )


def stub_invocation_row(
    invocation_id: str,
    *,
    process_lifetime_id: str = _DEFAULT_PROCESS_LIFETIME_ID,
) -> InvocationRow:
    """Minimal ``invocations`` row — the broker-event-log ``invocation_id`` FK
    target a broker-carried link points at.

    Requires the ``process_lifetimes`` parent (``stub_process_lifetime_row``)
    to be committed first / in the same transaction.
    """
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=_TS,
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="manual",
        trigger_source="continuous_monitor",
        trigger_reason="test-fixture",
        git_sha_at_invocation="0" * 40,
        active_profile="default",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="snapshot/path",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="snapshot/path",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=0,
        snapshot_metadata_json=None,
    )
