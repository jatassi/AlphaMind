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
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    PositionRecord,
    PositionStatus,
)
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


def _equity_position_snapshot(
    *, symbol: str = "AAPL", qty: float = 10.0, avg_entry_price: float = 150.0
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=price(avg_entry_price),
        market_value=money(qty * avg_entry_price),
        cost_basis=money(qty * avg_entry_price),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(avg_entry_price),
        side="long",
    )


def _make_pending_equity_position(
    *, position_id: str = "pos-1", ticker: str = "AAPL"
) -> PositionRecord:
    """Build a PENDING equity ``PositionRecord`` (share_count=0, no fills).

    Mirrors the ALP-763 wedged state: the OPEN writeback laid down the thesis +
    bracket + order scaffolding, the position row is PENDING with
    ``share_count=0`` and empty ``execution_history`` (the status-rule
    invariant), but the entry fill that should have flipped it to OPEN was
    dropped — while Alpaca already holds the shares.
    """
    from dataclasses import replace as dc_replace

    template = make_open_equity_position(position_id=position_id, ticker=ticker, share_count=0.0)
    assert isinstance(template.details, EquityPositionDetails)
    return dc_replace(
        template,
        status=PositionStatus.PENDING,
        entry_timestamp=None,
        execution_history=(),
    )


# OCC contract symbol Alpaca returns for the default ``make_open_options_position``
# (AAPL call, strike $150, expiry 2026-09-18). Derived from the substrate
# defaults at module-import time so a strike/expiry tweak in the substrate
# auto-retags every test that plants an Alpaca-side snapshot — no scattered
# string edits, no risk of the constant going stale silently.
def _default_options_occ_symbol() -> str:
    from alphamind.portfolio_state.records.positions import (
        OptionsPositionDetails,
        alpaca_occ_symbol,
    )

    details = make_open_options_position().details
    assert isinstance(details, OptionsPositionDetails)
    return alpaca_occ_symbol(details)


_DEFAULT_OPTIONS_OCC_SYMBOL = _default_options_occ_symbol()


def _options_position_snapshot(
    *, symbol: str = _DEFAULT_OPTIONS_OCC_SYMBOL, qty: float = 5.0
) -> PositionSnapshot:
    """Build a PositionSnapshot shaped like Alpaca returns for an options position.

    Alpaca's ``GET /v2/positions`` keys options positions by the bare OCC
    contract symbol (no Polygon ``O:`` prefix), not the underlying ticker.
    """
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_option",
        qty=qty,
        avg_entry_price=price(2.50),
        market_value=money(qty * 250.0),
        cost_basis=money(qty * 250.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(2.50),
        side="long",
    )


def test_alpaca_occ_symbol_strips_dot_from_share_class_ticker() -> None:
    """ALP-637 — share-class tickers (BRK.B, BF.B) are encoded WITHOUT the
    dot in the OCC convention; Alpaca's `get_all_positions` returns the
    dot-stripped form, and `build_occ_symbol` in the order-submission path
    likewise strips the dot. The reconciler's helper must mirror that —
    otherwise the lookup misses for every share-class options position,
    autocorrect fires, and the local row gets zeroed."""
    from dataclasses import replace as dc_replace

    from alphamind._kernel.ids import Symbol
    from alphamind.portfolio_state.records.positions import (
        OptionsPositionDetails,
        alpaca_occ_symbol,
    )

    details = make_open_options_position().details
    assert isinstance(details, OptionsPositionDetails)
    brk_b_details = dc_replace(details, underlying_ticker=Symbol("BRK.B"))

    occ = alpaca_occ_symbol(brk_b_details)
    assert occ.startswith("BRKB"), f"expected dot-stripped root, got {occ!r}"
    assert "." not in occ


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
    alert with domain='position', field_name='contract_count'. Alpaca keys the
    snapshot by the bare OCC contract symbol (ALP-637)."""
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

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_options_position_snapshot(qty=4.0),),
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

        # Cash row UNTOUCHED — equity drift must not bleed into cash state.
        from alphamind.state.tables.cash_ledger import (
            CASH_LEDGER_SINGLETON_ID,
            CashLedgerRow,
        )

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0)


async def test_reconcile_writes_back_options_drift_and_emits_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-637 — options contract_count drift now mirrors the equity
    auto-correct path: reconcile updates local contract_count to Alpaca's
    qty AND emits a paired RECONCILIATION_CORRECTION row alongside the
    existing RECONCILIATION_ALERT. Match is by bare OCC contract symbol."""
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

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(_options_position_snapshot(qty=4.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        # contract_count now matches Alpaca's authoritative value.
        assert pos.details.contract_count == pytest.approx(4.0)

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
        assert '"field_name":"contract_count"' in alert_rows[0].detail_json
        # Alert carries the OCC contract symbol so operators can find the
        # specific contract that drifted.
        assert _DEFAULT_OPTIONS_OCC_SYMBOL in alert_rows[0].detail_json

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
        assert '"field_name":"contract_count"' in correction_rows[0].detail_json
        assert '"prior_local_value":5.0' in correction_rows[0].detail_json
        assert '"applied_alpaca_value":4.0' in correction_rows[0].detail_json


async def test_reconcile_no_spurious_alert_for_correctly_synced_options(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-637 bug reproducer — a correctly-synced local options position
    (contract_count == Alpaca qty, matched by bare OCC contract symbol)
    must produce NO alert and NO correction. Pre-fix, matching-by-underlying
    always missed and the spurious-alert path fired on every invocation."""
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

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(_options_position_snapshot(qty=5.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.event_group == "RECONCILIATION")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_reconcile_multiple_options_same_underlying_reconcile_independently(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-637 — two held options on the same underlying (different strikes
    /expiries) must reconcile against their OWN OCC snapshots, not collide
    on the shared underlying ticker. Drift on one contract auto-corrects in
    isolation; the other contract stays untouched."""
    from dataclasses import replace as dc_replace
    from datetime import date

    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import (
        OptionsPositionDetails,
        alpaca_occ_symbol,
    )
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )
    from tests.state._fk_substrate import stub_thesis_row

    _, factory = db
    await seed_invocation_substrate(factory)

    # Two contracts on AAPL: strike 150 / Sep 2026 (default) and strike 160
    # / Dec 2026. Same underlying, different OCC symbols.
    pos_a = make_open_options_position(
        position_id="pos-1",
        thesis_id="thesis-1",
        bracket_id="brk-1",
        strike=150.0,
        contract_count=5.0,
    )
    pos_b_template = make_open_options_position(
        position_id="pos-2",
        thesis_id="thesis-2",
        bracket_id=None,
        contract_count=3.0,
    )
    assert isinstance(pos_b_template.details, OptionsPositionDetails)
    pos_b_details = dc_replace(
        pos_b_template.details,
        strike_price=160.0,
        expiration_date=date(2026, 12, 18),
    )
    pos_b = dc_replace(pos_b_template, details=pos_b_details)

    await seed_position_cluster(
        factory,
        pos_a,
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    # Seed pos-2 standalone (its own position row + minimal thesis stub for
    # the FK). No bracket/order — the reconciler reads the position table only.
    from alphamind.state.tables.positions_codec import (
        record_to_row as position_record_to_row,
    )

    async with factory() as sess:
        sess.add(stub_thesis_row("thesis-2", "pos-2"))
        await sess.flush()
        sess.add(position_record_to_row(pos_b))
        await sess.commit()

    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    assert isinstance(pos_a.details, OptionsPositionDetails)
    occ_a = alpaca_occ_symbol(pos_a.details)
    occ_b = alpaca_occ_symbol(pos_b_details)
    # Pre-condition: distinct OCC keys despite shared underlying.
    assert occ_a != occ_b

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(
            # pos_a drifted from 5 → 4; pos_b correctly synced at 3.
            _options_position_snapshot(symbol=occ_a, qty=4.0),
            _options_position_snapshot(symbol=occ_b, qty=3.0),
        ),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        rows = {
            r.position_id: position_row_to_record(r)
            for r in (await sess.execute(select(PositionRow))).scalars().all()
        }
        pos_a_after = rows["pos-1"]
        pos_b_after = rows["pos-2"]
        assert isinstance(pos_a_after.details, OptionsPositionDetails)
        assert isinstance(pos_b_after.details, OptionsPositionDetails)
        # pos_a auto-corrected; pos_b untouched.
        assert pos_a_after.details.contract_count == pytest.approx(4.0)
        assert pos_b_after.details.contract_count == pytest.approx(3.0)

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
        # Exactly one alert + one correction, both tied to pos-1.
        assert len(alerts) == 1
        assert len(corrections) == 1
        assert alerts[0].position_id == "pos-1"
        assert corrections[0].position_id == "pos-1"


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
    we still hold AAPL; Alpaca's snapshot reports a DIFFERENT held symbol
    (MSFT) and is missing AAPL. The Alpaca-side non-empty snapshot provides
    positive evidence the broker is responding, so auto-correct collapses
    the AAPL share_count to 0 alongside the alert + correction trail. (An
    empty Alpaca snapshot would be ambiguous — broker-degraded vs zero-
    holdings — and is intentionally NOT auto-corrected by the writeback
    gate at reconciliation.py.)"""
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
        # Positive Alpaca evidence: a real held position (MSFT) confirms the
        # broker is up; AAPL's absence is the vanished-position signal.
        alpaca_positions=(_equity_position_snapshot(symbol="MSFT", qty=3.0),),
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

        # Two alerts: vanished AAPL position + Alpaca-only MSFT orphan.
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
        assert len(alerts) == 2
        # One correction for the AAPL drift-to-zero; orphan path stays alert-only.
        assert len(corrections) == 1
        assert '"applied_alpaca_value":0.0' in corrections[0].detail_json


async def test_reconcile_short_equity_drift_uses_unsigned_magnitude(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — Alpaca's PositionSnapshot.qty is signed (negative for
    shorts); local share_count is unsigned with PositionRecord.direction
    tracking sign. A correctly-synced SHORT (local share_count=5,
    direction=SHORT, Alpaca qty=-5, side='short') has zero unsigned drift
    and must NOT alert or auto-correct. A SHORT drift (local=5 vs Alpaca
    qty=-4) writes back share_count=4 (the unsigned magnitude), preserving
    the unsigned-by-convention invariant."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import EquityPositionDetails
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    short_pos = make_open_equity_position(share_count=5.0)
    # Substrate defaults to LONG; coerce to SHORT for this test (the short
    # invariants need borrow_rate/locate/margin to be non-None per
    # PositionRecord._check_equity_direction_fields).
    from dataclasses import replace as dc_replace

    from alphamind.portfolio_state.records.positions import (
        Direction,
        LocateStatus,
    )

    assert isinstance(short_pos.details, EquityPositionDetails)
    short_details = dc_replace(
        short_pos.details,
        borrow_rate_pct=0.5,
        accrued_borrow_cost_usd=0.0,
        locate_status=LocateStatus.LOCATED,
        margin_held_usd=750.0,
    )
    short_pos = dc_replace(short_pos, direction=Direction.SHORT, details=short_details)
    await seed_position_cluster(
        factory,
        short_pos,
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # Alpaca reports qty=-4 (signed) with side='short' — drifted by 1 unit
    # in the unsigned-magnitude sense.
    short_snapshot = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_equity",
        qty=-4.0,
        avg_entry_price=price(150.0),
        market_value=money(4.0 * 150.0),
        cost_basis=money(4.0 * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="short",
    )

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(short_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # share_count auto-corrected to abs(-4) = 4 (unsigned magnitude
        # preserved; the local row stays SHORT-direction).
        assert pos.details.share_count == pytest.approx(4.0)
        assert pos.direction == Direction.SHORT


async def test_reconcile_short_no_drift_no_alert(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — a correctly-synced SHORT (local share_count=5, Alpaca
    qty=-5, side='short') has zero unsigned drift; reconcile emits no
    alerts and no corrections."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile

    _, factory = db
    await seed_invocation_substrate(factory)
    short_pos = make_open_equity_position(share_count=5.0)
    from dataclasses import replace as dc_replace

    from alphamind.portfolio_state.records.positions import (
        Direction,
        EquityPositionDetails,
        LocateStatus,
    )

    assert isinstance(short_pos.details, EquityPositionDetails)
    short_details = dc_replace(
        short_pos.details,
        borrow_rate_pct=0.5,
        accrued_borrow_cost_usd=0.0,
        locate_status=LocateStatus.LOCATED,
        margin_held_usd=750.0,
    )
    short_pos = dc_replace(short_pos, direction=Direction.SHORT, details=short_details)
    await seed_position_cluster(
        factory,
        short_pos,
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    short_snapshot = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_equity",
        qty=-5.0,
        avg_entry_price=price(150.0),
        market_value=money(5.0 * 150.0),
        cost_basis=money(5.0 * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="short",
    )

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        alpaca_positions=(short_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0
    async with factory() as sess:
        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.event_group == "RECONCILIATION")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_reconcile_direction_flip_alerts_but_does_not_autocorrect(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — direction mismatch (local LONG, Alpaca short) is
    semantically distinct from a quantity drift (stop-out, assignment, or
    broker-side error). Emit a `direction` field-name alert and skip
    auto-correct so the operator triages manually."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import EquityPositionDetails
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    # Local LONG AAPL qty=10.
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # Alpaca says SHORT AAPL qty=-10 — same magnitude, flipped direction.
    flipped_snapshot = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_equity",
        qty=-10.0,
        avg_entry_price=price(150.0),
        market_value=money(10.0 * 150.0),
        cost_basis=money(10.0 * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="short",
    )

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(flipped_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Local position UNCHANGED — direction flip skips auto-correct.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)

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
        # Direction-flip alert fires (quantity matches in magnitude so no
        # secondary share_count alert; the unsigned magnitudes 10 == 10).
        assert len(alerts) == 1
        assert '"field_name":"direction"' in alerts[0].detail_json

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


async def test_reconcile_dual_degraded_broker_skips_reconcile_entirely(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — when both alpaca_positions=() AND alpaca_account=None
    (broker fully degraded, the staleness_flag=True path), reconcile
    short-circuits: zero alerts, zero corrections, local state unchanged.
    This is the safety gate that prevents a transient broker failure from
    wiping the local book to zero."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import EquityPositionDetails
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )
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
    count = await reconcile(
        handle,
        alpaca_positions=(),
        alpaca_account=None,
    )
    await ctx.__aexit__(None, None, None)

    assert count == 0
    async with factory() as sess:
        # Local state preserved end-to-end.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0)

        rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.event_group == "RECONCILIATION")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_reconcile_half_degraded_positions_empty_skips_writeback(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-619 — half-degraded path: positions fetch failed (alpaca_positions=())
    but account fetch succeeded (alpaca_account is non-None). The positive-
    evidence gate must suppress position writeback to avoid wiping the book
    on a transient positions-endpoint failure, but cash reconcile + alerts
    still run. Local position share_count is preserved; no correction row
    for the position; cash drift still emits alert + correction."""
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
        # Positions fetch "failed" — gathered as empty tuple.
        alpaca_positions=(),
        # Account fetch succeeded with drift.
        alpaca_account=_trade_account(cash=99_500.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Position share_count PRESERVED (no writeback under positive-
        # evidence gate).
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)

        # Position alert still fires for visibility (vanished local AAPL).
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
        assert len(alerts) == 2  # position vanished + cash drift
        assert any('"field_name":"share_count"' in a.detail_json for a in alerts)
        assert any('"field_name":"current_cash_usd"' in a.detail_json for a in alerts)

        # Exactly one correction — the cash drift. Position writeback gated off.
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
        assert len(corrections) == 1
        assert '"domain":"cash"' in corrections[0].detail_json


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


async def test_reconcile_options_only_alpaca_response_does_not_wipe_local_equity(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-662 — cross-asset-class isolation. When Alpaca returns ONLY
    ``us_option`` snapshots (e.g., the equity-fetch sub-call silently
    degraded to empty while options-fetch succeeded), the equity branch
    must NOT auto-correct local equity rows to zero. The vanished-equity
    alert still fires so the operator gets visibility, but the destructive
    writeback is suppressed.
    """
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
        # Options-only positive evidence — equity-fetch silently degraded.
        alpaca_positions=(_options_position_snapshot(qty=5.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Local equity share_count PRESERVED — equity-asset-class positive
        # evidence is absent, so the writeback gate is closed for equity.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)

        # Equity-side alert still fires (AAPL vanished from Alpaca's view)
        # AND the orphan options snapshot fires an ``alpaca_only_position``
        # alert — pins the symmetric isolation: the opposite-class
        # snapshot is still surfaced for operator triage, just not
        # auto-materialized.
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
        assert any('"field_name":"share_count"' in a.detail_json for a in alerts)
        assert any('"field_name":"alpaca_only_position"' in a.detail_json for a in alerts)

        # No correction emitted for the equity position.
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
        assert all('"field_name":"share_count"' not in c.detail_json for c in corrections)


async def test_reconcile_equity_only_alpaca_response_does_not_wipe_local_options(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-662 — cross-asset-class isolation. When Alpaca returns ONLY
    ``us_equity`` snapshots (e.g., the options-fetch sub-call silently
    degraded to empty while equity-fetch succeeded), the options branch
    must NOT auto-correct local options rows to zero. The vanished-options
    alert still fires for visibility, but the destructive writeback is
    suppressed.
    """
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

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        # Equity-only positive evidence — options-fetch silently degraded.
        alpaca_positions=(_equity_position_snapshot(symbol="MSFT", qty=3.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Local options contract_count PRESERVED — options-asset-class
        # positive evidence is absent, so the writeback gate is closed for
        # options.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(5.0)

        # Options-side alert still fires (local OCC contract vanished from
        # Alpaca's view) AND the orphan MSFT equity snapshot fires an
        # ``alpaca_only_position`` alert — pins the symmetric isolation.
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
        assert any('"field_name":"contract_count"' in a.detail_json for a in alerts)
        assert any('"field_name":"alpaca_only_position"' in a.detail_json for a in alerts)

        # No correction emitted for the options position.
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
        assert all('"field_name":"contract_count"' not in c.detail_json for c in corrections)


async def test_reconcile_pending_local_with_nonzero_alpaca_escalates_only(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-763 — a PENDING local equity position (share_count=0, no fills)
    whose Alpaca holding is nonzero is ESCALATED, not auto-opened. The
    reconciler emits ONE distinct ``pending_with_broker_holding`` alert so the
    operator (and the honest recovery path — the fill drain/backfill that
    writes a real ``fill_records`` row → Phase 1 integrates it → the position
    flips PENDING → OPEN with the true basis) can act. The reconciler does NOT
    flip status, write share_count, synthesize a fill, or emit a correction —
    authoring portfolio state is not its job."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        _make_pending_equity_position(),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    count = await reconcile(
        handle,
        # Alpaca holds 10 shares at $152.50 — the dropped entry fill.
        alpaca_positions=(
            _equity_position_snapshot(symbol="AAPL", qty=10.0, avg_entry_price=152.5),
        ),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    # Exactly one escalation alert.
    assert count == 1

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # Row UNTOUCHED — still PENDING, still zero shares, no synthesized fill.
        assert pos.status == PositionStatus.PENDING
        assert pos.details.share_count == pytest.approx(0.0)
        assert pos.entry_timestamp is None
        assert pos.execution_history == ()

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
        assert '"field_name":"pending_with_broker_holding"' in alerts[0].detail_json
        assert alerts[0].position_id == "pos-1"

        # No correction — the reconciler does not mutate state here.
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


async def test_reconcile_pending_short_with_nonzero_alpaca_does_not_crash(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-763 regression — a PENDING SHORT equity position with a (negative-
    qty) Alpaca short holding must escalate WITHOUT raising. The prior auto-open
    path rebuilt the record via ``dataclasses.replace(status=OPEN, ...)`` while
    preserving ``direction=SHORT`` BUT dropping the short-only basis fields it
    didn't repopulate — ``abs(alpaca.qty)`` fired the branch on the negative
    short qty, then ``PositionRecord.__post_init__`` /
    ``_check_equity_direction_fields`` raised ValueError and aborted the whole
    reconcile pass. Escalate-only never rebuilds the record, so the pass
    completes and emits the escalation alert; the row is untouched."""
    from dataclasses import replace as dc_replace

    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.portfolio_state.records.positions import Direction, LocateStatus
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    # PENDING SHORT: share_count=0, direction=SHORT, short-only fields set so
    # the local record is constructible (the direction-field invariant binds
    # regardless of status). Alpaca's signed negative qty is what made the
    # auto-open rebuild crash.
    pending_template = _make_pending_equity_position()
    assert isinstance(pending_template.details, EquityPositionDetails)
    short_details = dc_replace(
        pending_template.details,
        borrow_rate_pct=0.5,
        accrued_borrow_cost_usd=0.0,
        locate_status=LocateStatus.LOCATED,
        margin_held_usd=750.0,
    )
    pending_short = dc_replace(pending_template, direction=Direction.SHORT, details=short_details)
    await seed_position_cluster(
        factory,
        pending_short,
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # Alpaca reports a short holding: qty=-10 (signed), side='short'.
    short_snapshot = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_equity",
        qty=-10.0,
        avg_entry_price=price(150.0),
        market_value=money(10.0 * 150.0),
        cost_basis=money(10.0 * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="short",
    )

    ctx, handle = await open_handle(factory)
    # Must NOT raise.
    count = await reconcile(
        handle,
        alpaca_positions=(short_snapshot,),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    assert count == 1

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # Row untouched — still PENDING SHORT, zero shares, no fills.
        assert pos.status == PositionStatus.PENDING
        assert pos.direction == Direction.SHORT
        assert pos.details.share_count == pytest.approx(0.0)
        assert pos.execution_history == ()

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
        assert '"field_name":"pending_with_broker_holding"' in alerts[0].detail_json

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


async def test_reconcile_pending_local_no_equity_evidence_left_untouched(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-763 — the positive-evidence gate guards the PENDING escalation. When
    Alpaca returns NO ``us_equity`` snapshot (only options evidence), a PENDING
    local equity position must stay PENDING — no escalation alert, no
    correction. The escalation only fires when Alpaca affirmatively reports a
    held equity position for that ticker."""
    from alphamind.execution.corporate_actions.reconciliation import reconcile
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        _make_pending_equity_position(),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        # Options-only evidence — no us_equity snapshot, so the equity
        # escalation gate is closed and the PENDING row must stay untouched.
        alpaca_positions=(_options_position_snapshot(qty=5.0),),
        alpaca_account=_trade_account(cash=100_000.0),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # Untouched: still PENDING, still zero shares, no fills.
        assert pos.status == PositionStatus.PENDING
        assert pos.details.share_count == pytest.approx(0.0)
        assert pos.entry_timestamp is None
        assert pos.execution_history == ()

        # No escalation alert fired (only the orphan options snapshot alert).
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
        assert all(
            '"field_name":"pending_with_broker_holding"' not in a.detail_json for a in alerts
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
        assert corrections == []


async def test_reconcile_corrects_settled_cash_alongside_current_cash(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-778 — when cash drift triggers a current_cash_usd correction,
    settled_cash_usd must be updated to the same Alpaca value.

    Prior to the fix, _reconcile_cash only wrote current_cash_usd, leaving
    settled_cash_usd frozen at the seed value and overstating deployable
    capital for position sizing.
    """
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
    # Seed with both current and settled at 100_000 (normal post-seed state).
    await seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # Alpaca reports a lower balance — simulates fills the pipeline processed
    # before this reconciliation run, leaving settled stale at 100_000.
    ctx, handle = await open_handle(factory)
    await reconcile(
        handle,
        alpaca_positions=(_equity_position_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(cash=93_251.67),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert float(cash_row.current_cash_usd) == pytest.approx(93_251.67)
        # ALP-778: settled must track current after reconciliation correction.
        assert float(cash_row.settled_cash_usd) == pytest.approx(93_251.67)
