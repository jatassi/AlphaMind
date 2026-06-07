"""Corporate-actions → append-only broker-event log (ALP-849 / W1c).

Every corporate-action lands as one immutable ``broker_event_log`` row
(idempotent on the ``event_key`` PK, ADR-0002 / ADR-0005), carrying the resolved
position → thesis link on its INITIAL insert. Capture is append-only; the
position/cash mutation (the per-type handler) is the separate integration step.
These tests exercise the capture through the public ``integrate_ca_activity``
entry point against a real on-disk SQLite schema (the DB is the sanctioned
boundary; here it is real).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, Symbol
from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from tests.execution.corporate_actions._handler_substrate import (
    NOW,
    make_active_bracket,
    make_active_thesis,
    make_open_equity_position,
    make_pending_entry_order,
    open_handle,
    seed_cash_ledger,
    seed_drawdown_state,
    seed_invocation_substrate,
    seed_position_cluster,
)


async def _read_event_log(
    factory: async_sessionmaker[AsyncSession],
) -> list[BrokerEventLogRow]:
    async with factory() as sess:
        return list((await sess.execute(select(BrokerEventLogRow))).scalars().all())


async def _seed_equity_cluster(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)


async def test_split_appends_ca_event_carrying_position_thesis_link(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A SPLIT integration appends one ``CA_SPLIT`` event-log row carrying the
    resolved position → thesis link on its initial insert."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_equity_cluster(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    rows = await _read_event_log(factory)
    assert len(rows) == 1
    (row,) = rows
    assert row.event_type == "CA_SPLIT"
    assert row.event_key == "ca:ca-split-1"
    assert row.position_id == "pos-1"
    assert row.thesis_id == "thesis-1"


async def test_ca_capture_is_idempotent_on_event_key(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The same CA activity integrated twice (a fill collection retry / late re-post)
    collapses to one ``broker_event_log`` row, keyed on the ``event_key`` PK."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_equity_cluster(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-dupe",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=2.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    # Re-integrate the SAME activity — the integration-ledger dedup means the
    # handler no-ops the mutation, and the event-log append collapses onto the
    # existing row.
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    rows = await _read_event_log(factory)
    assert len(rows) == 1


async def test_previously_dropped_type_is_captured_without_mutation(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A previously-dropped capture-only CA (``WorthlessRemoval``) is now
    captured onto the event log (AC: ``WorthlessRemoval`` / ``UnitSplit`` now
    captured) — and leaves the position quantity / basis UNTOUCHED (no defined
    integration math)."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import row_to_record

    _, factory = db
    await _seed_equity_cluster(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-worthless-1",
        action_type=CorporateActionType.WORTHLESS_REMOVAL,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    rows = await _read_event_log(factory)
    assert [r.event_type for r in rows] == ["CA_WORTHLESS_REMOVAL"]
    assert rows[0].position_id == "pos-1"
    # Capture-only: the position record is unchanged (share count + basis).
    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = row_to_record(pos_row)
        from alphamind.portfolio_state.records.positions import EquityPositionDetails

        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == 10.0
        assert pos.details.average_cost_basis_per_share == 150.0
        assert pos.corporate_action_adjustment_needed is False
