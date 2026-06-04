"""End-to-end poll tests: broker activity stream → booked lifecycle.

The poll is the shell that drives the broker's ``get_account_activities``
generator (the one mocked boundary — the broker API), classifies the stream,
and dispatches each lifecycle event. The DB is real. No internal collaborator is
mocked.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.money import signed_money
from alphamind.execution.account_activities.poll import poll_account_activities
from alphamind.execution.broker_adapter.queries import ActivitySnapshot
from alphamind.state.invocation_context.context import InvocationContext, InvocationHandle
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from tests.execution.account_activities.test_handlers import _OCC, _TXN, _seed_open_option
from tests.execution.corporate_actions._handler_substrate import (
    make_invocation_record,
    open_handle,
)


async def _open_handle_with_id(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> tuple[InvocationContext, InvocationHandle]:
    """Open a fresh ``InvocationContext`` with a distinct invocation id.

    The poll runs once per pipeline invocation, so re-polling spans two distinct
    invocation rows; the shared ``open_handle`` helper reuses one fixed id.
    """
    ctx = InvocationContext(
        session_factory=factory,
        record=make_invocation_record(invocation_id=invocation_id),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


class FakeAccountActivitiesQueries:
    """In-memory ``get_account_activities`` seam — the broker boundary.

    Returns the registered activities filtered by the requested types, exactly
    as the real generator does, and records the ``activity_types`` / ``after``
    cursor it was called with.
    """

    def __init__(self, activities: tuple[ActivitySnapshot, ...]) -> None:
        self._activities = activities
        self.calls: list[tuple[tuple[str, ...] | None, str | None]] = []

    async def get_account_activities(
        self,
        *,
        activity_types: tuple[str, ...] | None = None,
        after: str | None = None,
        until: dt.datetime | None = None,
    ) -> AsyncIterator[ActivitySnapshot]:
        self.calls.append((activity_types, after))
        for snap in self._activities:
            if activity_types is None or snap.activity_type in activity_types:
                yield snap


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


async def test_poll_books_expiry_from_broker_stream(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The poll drives get_account_activities, classifies, and books the expiry."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)
    queries = FakeAccountActivitiesQueries((_expiry_snapshot(),))

    ctx, handle = await open_handle(factory)
    try:
        result = await poll_account_activities(handle, queries=queries)
    finally:
        await ctx.__aexit__(None, None, None)

    assert result.activities_booked == 1
    # The poll requested exactly the four lifecycle types.
    requested = queries.calls[0][0]
    assert requested is not None
    assert set(requested) == {"OPEXP", "OPEXC", "OPASN", "OPTRD"}

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        assert pos.status == "CLOSED"
        # The poll lands the OPEXP on the event log carrying its -premium delta;
        # the per-thesis ledger is then derived from the log (03c), so the poll's
        # responsibility ends at the booked event.
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1
        payload = json.loads(events[0].raw_payload_json)
        assert signed_money(payload["realized_pnl_delta_usd"]) == signed_money("-1250.00")


async def test_poll_is_idempotent_across_two_runs(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Re-polling the same broker stream does not double-book (event_key idempotency)."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)
    queries = FakeAccountActivitiesQueries((_expiry_snapshot(),))

    for run in range(2):
        ctx, handle = await _open_handle_with_id(factory, f"inv-poll-{run}")
        try:
            await poll_account_activities(handle, queries=queries)
        finally:
            await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # The event_key idempotency collapses the re-poll to one OPEXP row, so a
        # later derivation books -premium exactly once (no double-count).
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1
