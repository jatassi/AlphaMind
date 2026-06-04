"""The poll is registered as a pipeline/scheduled task, not an always-on monitor.

ADR-0004 evicts the activity poll to scheduled work — it runs in the pipeline's
Phase-1 write transaction. The scheduler-layer entry point
``run_account_activities_poll`` is the registration seam; it accepts an
activities-source factory (the broker boundary) so the production path builds an
Alpaca client while tests / debug-e2e substitute a fake without monkey-patching.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.execution.broker_adapter.queries import ActivitySnapshot
from alphamind.scheduler.account_activities_poll import run_account_activities_poll
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.execution.account_activities.test_handlers import _OCC, _TXN, _seed_open_option
from tests.execution.account_activities.test_poll import FakeAccountActivitiesQueries
from tests.execution.corporate_actions._handler_substrate import open_handle


def _expiry_snapshot() -> ActivitySnapshot:
    return ActivitySnapshot(
        id="act-exp-1",
        activity_type="OPEXP",
        transaction_time=_TXN,
        symbol=_OCC,
        qty=5.0,
        price=None,
        net_amount=None,
        side=None,
        description=None,
        raw={"id": "act-exp-1"},
    )


async def test_scheduler_poll_runs_against_factory_source(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The scheduler entry point drives the poll via the injected source factory."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    source = FakeAccountActivitiesQueries((_expiry_snapshot(),))

    ctx, handle = await open_handle(factory)
    try:
        result = await run_account_activities_poll(
            handle,
            venue_config=None,
            execution_mode=ExecutionMode.paper,
            activities_source_factory=lambda _venue, _mode: source,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    assert result.activities_booked == 1

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        assert pos.status == "CLOSED"
        ledger = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert ledger is not None
        assert ledger.realized_pnl_usd == Decimal(-1250)
