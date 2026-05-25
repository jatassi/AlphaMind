"""Tests for the post-Phase-1 reconciliation step (ALP-415 / story 04; ALP-619).

``reconcile(handle, alpaca_positions, alpaca_account)`` compares local state
to caller-supplied :class:`PositionSnapshot` / :class:`TradeAccountSnapshot`
records (the post-Phase-1 view of Alpaca's authoritative state), emits one
``RECONCILIATION_ALERT`` activity-log entry per unexplained delta beyond the
documented tolerance (1e-9 for quantities, 0.01 USD for cash), and returns
the alert count.

ALP-619 — for drift on an existing OPEN equity/options position
(``share_count`` / ``contract_count``) and the singleton ``cash_ledger`` row
(``current_cash_usd``), reconcile writes Alpaca's authoritative value back to
local state in the same transaction AND emits a paired
``RECONCILIATION_CORRECTION`` row capturing the prior/applied scalars.
Alpaca-only orphans continue to alert-only.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.money import money, price
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.state.tables.activity_log import (
    ActivityLogRow,
)

from ._handler_substrate import (
    make_active_bracket,
    make_active_thesis,
    make_open_equity_position,
    make_open_options_position,
    make_pending_entry_order,
    open_handle,
    seed_cash_ledger,
    seed_invocation_substrate,
    seed_position_cluster,
)


def _trade_account(
    *, cash: float = 100_000.0, buying_power: float = 200_000.0
) -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="alp-account-1",
        cash=money(cash),
        equity=money(cash),
        buying_power=money(buying_power),
        regt_buying_power=money(buying_power),
        daytrading_buying_power=money(buying_power),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _equity_position_snapshot(*, symbol: str = "AAPL", qty: float = 10.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=price(150.0),
        market_value=money(qty * 150.0),
        cost_basis=money(qty * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="long",
    )


async def test_reconcile_emits_no_alert_when_state_matches(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When local position quantity and cash balance match Alpaca within tolerance,
    no ``RECONCILIATION_ALERT`` is emitted and the return value is zero."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_reconcile_emits_one_alert_per_position_quantity_delta(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A local equity position carrying 10 shares vs Alpaca's 9 shares emits one
    ``RECONCILIATION_ALERT`` with domain='position', field_name='share_count'."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=9.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 1

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # Activity log carries the field-level forensic payload.
        assert '"domain":"position"' in rows[0].detail_json
        assert '"field_name":"share_count"' in rows[0].detail_json


async def test_reconcile_emits_alert_for_options_contract_count_delta(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A local options position with contract_count=5 vs Alpaca's qty=4 emits one
    alert with domain='position', field_name='contract_count'."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(contract_count=5.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # Alpaca surfaces the OCC contract symbol; the reconciler matches against
    # the local options-position underlying_ticker by routing through that
    # field rather than the OCC symbol — but the comparator can also consume
    # an OCC-symbol-keyed snapshot if the test plants the matching key.
    options_snapshot = PositionSnapshot(
        symbol="AAPL",  # match the underlying_ticker the reconciler reads
        asset_class="us_option",
        qty=4.0,
        avg_entry_price=price(2.50),
        market_value=money(4.0 * 250.0),
        cost_basis=money(4.0 * 250.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(2.50),
        side="long",
    )

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(options_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 1
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert '"field_name":"contract_count"' in rows[0].detail_json


async def test_reconcile_emits_alert_for_cash_balance_delta(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A local cash balance of 100_000 vs Alpaca's 99_500 emits one alert with
    domain='cash', field_name='current_cash_usd'."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(cash=99_500.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 1

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert '"domain":"cash"' in rows[0].detail_json
        assert '"field_name":"current_cash_usd"' in rows[0].detail_json


async def test_reconcile_no_alert_when_within_cash_tolerance(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A sub-cent cash difference (0.005 USD) falls inside the 0.01 USD
    tolerance and produces no alert."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(cash=100_000.005),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0


async def test_reconcile_no_alert_when_within_quantity_tolerance(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A femto-share quantity difference (1e-10) falls inside the 1e-9 tolerance
    and produces no alert."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0 + 1e-10),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0


async def test_reconcile_emits_alert_for_alpaca_only_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Alpaca holds a symbol AlphaMind has no local OPEN/PENDING position for —
    e.g., an orphan SPIN_OFF child stranded by a prior crash, or any
    unexpected broker-side holding. The reconciler emits one alert per
    orphan with ``field_name='alpaca_only_position'``."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        # AAPL matches the seeded local position; ORPHAN_X has no local match.
        alpaca_positions=(
            _equity_position_snapshot(symbol="AAPL", qty=10.0),
            _equity_position_snapshot(symbol="ORPHAN_X", qty=7.0),
        ),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    # Exactly one orphan alert.
    assert count == 1

    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert '"field_name":"alpaca_only_position"' in rows[0].detail_json
        assert "ORPHAN_X" in rows[0].detail_json
        # Local-side value is 0 (no local row), Alpaca-side is the orphan qty.
        assert '"local_value":0.0' in rows[0].detail_json
        assert '"alpaca_value":7.0' in rows[0].detail_json


async def test_reconcile_writes_back_equity_drift_and_emits_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — equity quantity drift: reconcile updates local share_count to
    match Alpaca's qty AND emits a paired RECONCILIATION_CORRECTION row
    alongside the existing RECONCILIATION_ALERT."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import EquityPositionDetails
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=5.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # share_count now matches Alpaca's authoritative value.
        assert pos.details.share_count == pytest.approx(5.0)

        # Alert continues to fire alongside the correction.
        alert_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alert_rows) == 1
        assert '"field_name":"share_count"' in alert_rows[0].detail_json

        # Paired correction row captures prior/applied scalars.
        correction_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_CORRECTION.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(correction_rows) == 1
        assert correction_rows[0].position_id == "pos-1"
        assert '"domain":"position"' in correction_rows[0].detail_json
        assert '"field_name":"share_count"' in correction_rows[0].detail_json
        assert '"prior_local_value":10.0' in correction_rows[0].detail_json
        assert '"applied_alpaca_value":5.0' in correction_rows[0].detail_json


async def test_reconcile_writes_back_options_drift_and_emits_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — options contract_count drift writes back the new count
    AND emits a paired RECONCILIATION_CORRECTION row."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import OptionsPositionDetails
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(contract_count=5.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    options_snapshot = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_option",
        qty=4.0,
        avg_entry_price=price(2.50),
        market_value=money(4.0 * 250.0),
        cost_basis=money(4.0 * 250.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(2.50),
        side="long",
    )

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(options_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(4.0)

        correction_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_CORRECTION.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(correction_rows) == 1
        assert '"field_name":"contract_count"' in correction_rows[0].detail_json
        assert '"prior_local_value":5.0' in correction_rows[0].detail_json
        assert '"applied_alpaca_value":4.0' in correction_rows[0].detail_json


async def test_reconcile_writes_back_cash_drift_and_emits_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — manual Alpaca cash adjustment: drift in current_cash_usd
    writes back the Alpaca value AND emits a paired correction row."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(cash=99_500.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(99_500.0)

        correction_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_CORRECTION.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(correction_rows) == 1
        assert '"domain":"cash"' in correction_rows[0].detail_json
        assert '"field_name":"current_cash_usd"' in correction_rows[0].detail_json
        assert '"prior_local_value":100000.0' in correction_rows[0].detail_json
        assert '"applied_alpaca_value":99500.0' in correction_rows[0].detail_json
        assert correction_rows[0].position_id is None


async def test_reconcile_drift_to_zero_simulates_missed_close_fill(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — stream-dropped close fill or missed assignment: local says
    we still hold the position; Alpaca reports qty=0. Auto-correct collapses
    share_count to 0 alongside an alert + correction trail."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import EquityPositionDetails
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(0.0)

        alerts = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        corrections = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_CORRECTION.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert len(corrections) == 1
        assert '"applied_alpaca_value":0.0' in corrections[0].detail_json


async def test_reconcile_orphan_stays_alert_only_no_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — Alpaca-only orphans continue to ALERT-only. No
    RECONCILIATION_CORRECTION row, no synthetic local position row created."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.state.tables.positions import PositionRow

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(
            _equity_position_snapshot(symbol="AAPL", qty=10.0),
            _equity_position_snapshot(symbol="ORPHAN_X", qty=7.0),
        ),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        alerts = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert '"field_name":"alpaca_only_position"' in alerts[0].detail_json

        corrections = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.RECONCILIATION_CORRECTION.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert corrections == []

        pos_rows = (await sess.execute(select(PositionRow))).scalars().all()
        assert {row.position_id for row in pos_rows} == {"pos-1"}
