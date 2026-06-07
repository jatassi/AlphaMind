"""Race-closing: the PM turn is the synchronous writer on the broker-active path — ALP-763.

A marketable command-execution entry is dispatched to the broker DURING the PM turn (capturing the
real ``alpaca_order_id``). A fast fill (~3s) can beat the deferred order-row commit
(~74s later in production), so the continuous monitor cannot resolve the fill and it gets
dropped/quarantined. The fix (Option A): when broker routing is active, the submit_envelope
tool handler runs the command-execution writeback IN THE PM TURN and commits on the invocation
handle's session right after dispatch — closing the race to ~0. ``dispatch_command_execution``
then skips the already-persisted envelope (see ``test_command_execution_dispatch.py``).

This test proves the entry ``OrderRow`` — carrying the broker's real ``alpaca_order_id`` —
is COMMITTED and resolvable at the end of the PM turn, before ``dispatch_command_execution`` runs.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select as _select

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId
from alphamind._kernel.money import price
from alphamind.commands.command_models import EntryOrder, OMSCommand
from tests.execution.oms.test_engine_stub_broker_routing import _default_execution_config
from tests.execution.oms.test_submit_envelope_mcp import (
    _DEFAULT_ACTIVE_SECTORS,
    _NOW,
    _make_analyst_envelope,
    _make_bundle,
    _make_pm_view,
    _make_validation_state,
    _open_command,
    _recommendation_stub,
    _retrieval_store,
    _sector_resolver,
    _state_persistence_config,
)


class _CapturingBrokerDispatch:
    """Returns a Submitted ack carrying a fresh broker ``alpaca_order_id``."""

    def __init__(self) -> None:
        self.last_order_id: AlpacaOrderId | None = None

    async def __call__(self, command: OMSCommand, *, client_order_id: str, **context: Any) -> Any:
        from alphamind.execution.broker_adapter import EquitySubmission, Submitted
        from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

        oid = AlpacaOrderId(str(uuid.uuid4()))
        self.last_order_id = oid
        return Submitted(
            payload=BrokerDispatchResult(
                alpaca_order_id=oid,
                client_order_id=ClientOrderId(client_order_id),
                status="accepted",
                order_class="simple",
                payload_kind="equity",
                raw_submission=EquitySubmission(
                    alpaca_order_id=oid,
                    client_order_id=ClientOrderId(client_order_id),
                    status="accepted",
                    order_class="simple",
                ),
            ),
            attempt_count=1,
        )


_INVOCATION_ID = "inv-2026-05-05"


async def _seed_substrate(factory: Any) -> None:
    """Seed process-lifetime + cash-ledger + invocation rows so a real
    ``persist_envelope_outcome`` (capital reservation + pm_decision) can commit."""
    from alphamind.portfolio_state.records.cash import CashLedger
    from alphamind.state.invocation_context.records import (
        InvocationRecord,
        ProcessLifetimeRecord,
        invocation_record_to_row,
        process_lifetime_record_to_row,
    )
    from alphamind.state.tables.cash_ledger_codec import cash_ledger_record_to_row

    proc = ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=123,
        hostname="host",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/p.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )
    cash = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=100_000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=100_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    inv_record = InvocationRecord(
        invocation_id=_INVOCATION_ID,
        process_lifetime_id="proc-1",
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/r.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/c.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(proc))
        await sess.flush()
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
        sess.add(invocation_record_to_row(inv_record))
        await sess.commit()


@pytest.mark.asyncio
async def test_broker_active_path_commits_order_row_in_pm_turn(tmp_path: Any) -> None:
    """A broker-routed equity OPEN persists + commits its entry ``OrderRow`` (with the
    broker ``alpaca_order_id``) in the PM turn, even though ``defer_writeback=True``."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.persistence.models import Base
    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
        make_engine,
    )
    from alphamind.portfolio_state.events.activity_log import EventType
    from alphamind.state.invocation_context.context import InvocationHandle
    from alphamind.state.tables.activity_log import ActivityLogRow
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )
    from alphamind.state.tables.orders import OrderRow

    db_path = tmp_path / "test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        await _seed_substrate(factory)

        # The handle the broker-routing reads + the in-turn writeback share — the
        # subprocess worker's normal (writable) session, NOT a context manager that
        # commits on exit. The in-turn writeback must commit it itself.
        envelope = _make_analyst_envelope(
            commands=(
                _open_command(
                    entry_order=EntryOrder(type="limit", limit_price=price(1000.0), stop_price=None)
                ),
            )
        )
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))
        fake_dispatch = _CapturingBrokerDispatch()

        async with factory() as turn_session:
            handle = InvocationHandle(session=turn_session, invocation_id="inv-2026-05-05")
            await _handle_submit_envelope(
                envelope.model_dump(mode="json"),
                state=state,
                retrieval_store=_retrieval_store(),
                pre_processor_bundle=bundle,
                pm_view=_make_pm_view(),
                active_sectors=_DEFAULT_ACTIVE_SECTORS,
                halt_mode=False,
                sector_resolver=_sector_resolver,
                state_persistence_config=_state_persistence_config(),
                invocation_handle=handle,
                client=MagicMock(),
                queries=MagicMock(),
                execution_config=_default_execution_config(),
                broker_dispatch=fake_dispatch,
                defer_writeback=True,
            )
            # No explicit commit here — the in-turn writeback owns the commit so the
            # order row is durable the instant the broker fill could arrive.

        # A FRESH session (simulating the continuous monitor) resolves the entry
        # order BEFORE dispatch_command_execution ever runs.
        async with factory() as monitor_session:
            order_rows = (await monitor_session.execute(_select(OrderRow))).scalars().all()
            log_rows = (
                (
                    await monitor_session.execute(
                        _select(ActivityLogRow).where(
                            ActivityLogRow.invocation_id == "inv-2026-05-05"
                        )
                    )
                )
                .scalars()
                .all()
            )
            cash_row = await monitor_session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)

        assert fake_dispatch.last_order_id is not None
        entry_rows = [r for r in order_rows if r.order_role == "ENTRY"]
        assert len(entry_rows) == 1
        assert entry_rows[0].alpaca_order_id == str(fake_dispatch.last_order_id)

        types = {r.event_type for r in log_rows}
        assert EventType.PM_DECISION.value in types
        assert EventType.ORDER_SUBMITTED.value in types
        assert EventType.CAPITAL_RESERVED.value in types
        assert cash_row is not None
        assert cash_row.reserved_capital_usd > 0.0
    finally:
        await async_engine.dispose()
