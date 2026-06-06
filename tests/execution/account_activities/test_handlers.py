"""Integration tests for the option-lifecycle handlers against a real DB.

These exercise the shell: load the open option position by OCC symbol, append
the lifecycle activity to ``broker_event_log`` **carrying its realized-PnL delta
on the payload**, and persist the position transitions — all in the open
``InvocationHandle`` transaction. The DB is real (a sanctioned boundary); no
internal collaborator is mocked.

The handler does **not** write ``thesis_pnl_ledger``: per-thesis PnL is a
derived view of the event log, written solely by the 03c derivation
(:func:`alphamind.execution.write_paths.thesis_pnl_ledger.rederive_thesis_pnl_ledger`),
so the handler books the figure onto the log and the derivation aggregates it
(one coherent derived view, no double-count). These tests assert the delta lands
on the event payload and that re-deriving reproduces it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.account_activities.dispatch import integrate_lifecycle_event
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.execution.write_paths.thesis_pnl_ledger import rederive_thesis_pnl_ledger
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.execution.corporate_actions._handler_substrate import (
    INV_ID,
    make_active_bracket,
    make_active_thesis,
    make_open_options_position,
    make_pending_entry_order,
    open_handle,
    seed_invocation_substrate,
    seed_position_cluster,
)

# The corporate-actions substrate seeds AAPL CALL, strike 150, exp 2026-09-18.
_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)
_OCC = "AAPL260918C00150000"


def _borrow_resolver(_ticker: str) -> float | None:
    """Invocation-scoped borrow-rate resolver — a flat 12%/yr for every ticker.

    The poll path threads this to the assignment handler so a SHORT equity
    delivery can stamp its short-only fields; the expiry and LONG-delivery paths
    forward it but never consult it.
    """
    return 12.0


async def _seed_open_option(
    factory: async_sessionmaker[AsyncSession],
    *,
    contract_count: float = 5.0,
    premium_paid_per_contract: float = 250.0,
) -> None:
    await seed_invocation_substrate(factory)
    position = make_open_options_position(
        position_id="pos-1",
        thesis_id="thesis-1",
        bracket_id="brk-1",
        contract_count=contract_count,
        premium_paid_per_contract=premium_paid_per_contract,
    )
    await seed_position_cluster(
        factory,
        position=position,
        order=make_pending_entry_order(),
        thesis=make_active_thesis(thesis_id="thesis-1", position_id="pos-1"),
        bracket=make_active_bracket(bracket_id="brk-1", position_id="pos-1"),
    )


def _expiry_event() -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-exp-1",
        activity_type=LifecycleActivityType.OPEXP,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )


async def test_expiry_closes_option_and_books_negative_premium_on_the_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An OTM expiry closes the option (no OPEN/0 husk) and books -premium on the event log."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        record = row_to_record(pos)
        assert record.status.value == "CLOSED"
        assert record.realized_pnl_to_date_usd == pytest.approx(-1250.0)

        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "OPEXP"
        assert events[0].position_id == "pos-1"
        # The realized-PnL delta AND the closed contract count ride the event
        # payload so the 03c derivation reproduces the figure AND releases the
        # option lot (closing the bought option) from the log alone.
        payload = json.loads(events[0].raw_payload_json)
        assert signed_money(payload["realized_pnl_delta_usd"]) == signed_money("-1250.00")
        assert payload["closed_contract_qty"] == pytest.approx(5.0)

        # The handler does NOT write the ledger — that is the derivation's job.
        assert await sess.get(ThesisPnlLedgerRow, "thesis-1") is None

    # Re-deriving from the event log reproduces the -premium realized PnL.
    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    assert ledger_record.realized_pnl_usd == signed_money("-1250.00")


async def test_re_polling_same_activity_does_not_double_book(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Idempotent on event_key: re-integrating the same activity is a no-op."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
        # Re-poll: same activity_id arrives again in the same transaction.
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1  # one row, not two

    # The event-log row is idempotent (one OPEXP), so re-deriving books -premium
    # exactly once — not -2500 — the double-count the old RMW accumulation risked.
    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    assert ledger_record.realized_pnl_usd == signed_money("-1250.00")


async def test_repoll_closes_option_when_opexp_row_exists_but_option_still_open(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC1: expiry is crash-idempotent — an OPEXP row + an OPEN option re-closes.

    If an OPEXP row ever co-exists with an OPEN option (a partial-commit / crash
    that committed the event row but not the booking), a ``newly``-gated booking
    permanently skips the close. Mirroring the assignment path, expiry must gate
    the booking on the option still being OPEN: re-resolve, and book if found.
    Simulated by committing only the OPEXP row while leaving the option OPEN — the
    re-poll must close it.
    """
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    # Pre-seed ONLY the OPEXP row — the crash committed it but not the booking.
    async with factory() as sess:
        sess.add(
            BrokerEventLogRow(
                event_key="activity:act-exp-1",
                event_type="OPEXP",
                thesis_id="thesis-1",
                invocation_id=INV_ID,
                position_id="pos-1",
                raw_payload_json=json.dumps(
                    {
                        "activity_id": "act-exp-1",
                        "occ_symbol": _OCC,
                        "realized_pnl_delta_usd": "-1250.00",
                        "closed_contract_qty": 5.0,
                    },
                    sort_keys=True,
                ),
                broker_timestamp=_TXN,
                captured_at=_TXN,
            )
        )
        await sess.commit()

    # Re-poll the same expiry: the option is still OPEN, so the booking must run.
    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        # The crash-window OPEXP no longer skips the close — the option is CLOSED.
        assert pos.status == "CLOSED"
        # Still exactly one OPEXP row (idempotent append), not two.
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1


async def test_repoll_after_clean_expiry_is_a_no_op(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC1: a re-poll after a clean expiry (option already CLOSED) is a clean no-op.

    Once expiry closed the option, ``_find_open_option_position`` returns None and
    the durable OPEXP row marks a clean no-op — no raise, no double-book.
    """
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
        # Re-poll after the option already closed: a clean no-op.
        await integrate_lifecycle_event(
            handle, _expiry_event(), borrow_cost_resolver=_borrow_resolver
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        assert pos.status == "CLOSED"
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1


def _assignment_event(activity_type: LifecycleActivityType, *, side: str = "buy") -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-asn-1",
        activity_type=activity_type,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=TradeLeg(
            activity_id="act-trd-1",
            equity_symbol="AAPL",
            qty=500.0,
            strike_price=price(150.0),
            side=side,
            net_amount=signed_money(-75_000.0),
        ),
    )


async def test_assignment_opens_equity_at_strike_with_thesis_link(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An assignment opens the equity at the strike with the option's thesis link."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle,
            _assignment_event(LifecycleActivityType.OPASN),
            borrow_cost_resolver=_borrow_resolver,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Option closed (no husk); equity opened at the strike with the thesis link.
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"

        stmt = select(PositionRow).where(PositionRow.status == "OPEN")
        equities = (await sess.execute(stmt)).scalars().all()
        assert len(equities) == 1
        equity = row_to_record(equities[0])
        assert equity.thesis_id == "thesis-1"
        assert equity.parent_position_id == "pos-1"
        details = equity.details
        assert isinstance(details, EquityPositionDetails)
        assert details.ticker == "AAPL"
        assert details.share_count == 500.0
        assert details.average_cost_basis_per_share == pytest.approx(150.0)

        # Both the OPASN and its paired OPTRD landed in the event log, and BOTH
        # carry the resolved thesis/position attribution (the OPTRD row must not
        # be left NULL — 03c's per-thesis PnL join reads it).
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: e for e in events}
        assert set(by_type) == {"OPASN", "OPTRD"}
        assert by_type["OPASN"].thesis_id == "thesis-1"
        assert by_type["OPASN"].position_id == "pos-1"
        assert by_type["OPTRD"].thesis_id == "thesis-1"
        assert by_type["OPTRD"].position_id == "pos-1"


async def test_assignment_stamps_equity_side_on_optrd_for_signed_fold(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """P4: the OPTRD payload carries ``equity_side`` so the 03c fold opens a signed lot.

    The paired OPTRD must stamp the equity delivery direction (the broker's
    buy/sell side). The 03c fold reads it to open a +N lot (long delivery) or a
    -N lot (short-call assignment); without it the fold always opened +N and a
    short cover mis-classified as opening (no realized PnL). The signed-fold math
    itself is pinned in the derivation unit tests — here we pin that the handler
    actually emits the side onto the OPTRD row, AND that a long delivery's cover
    still realizes correctly through the re-derivation.
    """
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle,
            _assignment_event(LifecycleActivityType.OPASN, side="buy"),
            borrow_cost_resolver=_borrow_resolver,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: json.loads(e.raw_payload_json) for e in events}
        # The OPTRD carries the delivery side so the fold opens a SIGNED lot.
        assert by_type["OPTRD"]["equity_side"] == "buy"
        assert by_type["OPTRD"]["equity_qty"] == pytest.approx(500.0)

        # Seed a sell FILL closing the long-delivered 500 @ 150: sell 500 @ 160
        # → realizes (160-150)*500 = +5000; cover then leaves zero basis.
        sess.add(
            BrokerEventLogRow(
                event_key="fill:sell-1",
                event_type="FILL",
                thesis_id="thesis-1",
                invocation_id=INV_ID,
                position_id="pos-1",
                raw_payload_json=json.dumps(
                    {
                        "fill_price": 160.0,
                        "fill_quantity": 500.0,
                        "raw_event_payload": {"order": {"side": "sell"}},
                    },
                    sort_keys=True,
                ),
                broker_timestamp=_TXN + dt.timedelta(hours=1),
                captured_at=_TXN + dt.timedelta(hours=1),
            )
        )
        await sess.commit()

    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    # -1250 (premium) + 5000 (long sell gain) = +3750; basis back to zero.
    assert ledger_record.realized_pnl_usd == signed_money("3750.00")
    assert ledger_record.cost_basis_usd == money("0")


async def test_short_call_assignment_opens_short_equity_then_cover_realizes_pnl(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-862: a short-call assignment books a SHORT equity leg that PERSISTS, then covers.

    A ``sell``-side delivery opens a SHORT equity position. Before this fix the
    booking built an ``EquityPositionDetails`` with the four short-only fields
    left ``None``, so ``PositionRecord.__post_init__`` rejected it and the whole
    assignment aborted — a real broker short with no local Intent. Here the leg
    persists with its borrow fields stamped from the invocation resolver, and a
    subsequent buy-to-cover realizes the OPTRD-folded PnL through the derivation.
    """
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle,
            _assignment_event(LifecycleActivityType.OPASN, side="sell"),
            borrow_cost_resolver=_borrow_resolver,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # The option closed; a SHORT equity leg opened AND persisted (the bug:
        # the record previously rejected a SHORT leg with None short-only fields).
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"
        equities = (
            (await sess.execute(select(PositionRow).where(PositionRow.status == "OPEN")))
            .scalars()
            .all()
        )
        assert len(equities) == 1
        equity = row_to_record(equities[0])
        assert equity.direction is Direction.SHORT
        details = equity.details
        assert isinstance(details, EquityPositionDetails)
        assert details.share_count == pytest.approx(500.0)
        assert details.average_cost_basis_per_share == pytest.approx(150.0)
        # The four short-only fields are stamped — the record accepted the leg.
        assert details.borrow_rate_pct == pytest.approx(12.0)
        assert details.accrued_borrow_cost_usd == pytest.approx(0.0)
        assert details.locate_status is LocateStatus.LOCATED
        assert details.margin_held_usd == pytest.approx(500.0 * 150.0 * 0.50)

        # The OPTRD stamps the short delivery side so the fold opens a -500 lot.
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: json.loads(e.raw_payload_json) for e in events}
        assert by_type["OPTRD"]["equity_side"] == "sell"

        # Seed a buy-to-cover FILL closing the short 500 @ 150: buy 500 @ 140
        # → realizes (150-140)*500 = +5000 for the short; cover leaves zero basis.
        sess.add(
            BrokerEventLogRow(
                event_key="fill:buy-cover-1",
                event_type="FILL",
                thesis_id="thesis-1",
                invocation_id=INV_ID,
                position_id="pos-1",
                raw_payload_json=json.dumps(
                    {
                        "fill_price": 140.0,
                        "fill_quantity": 500.0,
                        "raw_event_payload": {"order": {"side": "buy"}},
                    },
                    sort_keys=True,
                ),
                broker_timestamp=_TXN + dt.timedelta(hours=1),
                captured_at=_TXN + dt.timedelta(hours=1),
            )
        )
        await sess.commit()

    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    # -1250 (premium) + 5000 (short cover gain) = +3750; basis back to zero.
    assert ledger_record.realized_pnl_usd == signed_money("3750.00")
    assert ledger_record.cost_basis_usd == money("0")


async def test_exercise_books_strike_pnl_and_opens_equity_leg(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An exercise books -premium and opens the resulting equity leg with the thesis link."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle,
            _assignment_event(LifecycleActivityType.OPEXC),
            borrow_cost_resolver=_borrow_resolver,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"

        # The OPEXC carries -premium; the paired OPTRD carries the equity cost
        # basis (qty x strike). Both ride the event payloads for the derivation.
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: json.loads(e.raw_payload_json) for e in events}
        assert signed_money(by_type["OPEXC"]["realized_pnl_delta_usd"]) == signed_money("-1250.00")
        assert money(by_type["OPTRD"]["cost_basis_delta_usd"]) == money("75000.00")
        # The closed option contracts (OPEXC) and the opened equity share count
        # (OPTRD) ride the payloads so the 03c fold closes the option lot and
        # opens the equity lot at the strike — releasable by a later equity sell.
        assert by_type["OPEXC"]["closed_contract_qty"] == pytest.approx(5.0)
        assert by_type["OPTRD"]["equity_qty"] == pytest.approx(500.0)
        assert await sess.get(ThesisPnlLedgerRow, "thesis-1") is None

        stmt = select(PositionRow).where(PositionRow.status == "OPEN")
        equities = (await sess.execute(stmt)).scalars().all()
        assert len(equities) == 1
        equity = row_to_record(equities[0])
        assert equity.thesis_id == "thesis-1"
        details = equity.details
        assert isinstance(details, EquityPositionDetails)
        assert details.average_cost_basis_per_share == pytest.approx(150.0)

    # Re-deriving folds the -premium and the equity cost basis into the ledger.
    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    assert ledger_record.realized_pnl_usd == signed_money("-1250.00")
    assert ledger_record.cost_basis_usd == money("75000.00")


async def test_repoll_reappends_optrd_after_crash_window(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A crash that committed OPASN but not its paired OPTRD re-appends on re-poll.

    The narrow crash window: a prior invocation appended the OPASN row but died
    before the paired OPTRD row (which carries ``cost_basis_delta_usd``) landed.
    A naive ``_already_booked`` short-circuit on the OPASN key would skip the
    OPTRD forever → the 03c fold understates the assigned equity's cost basis.

    Simulated by committing only the OPASN row (carrying the resolved link) while
    leaving the option OPEN (the booking transaction never committed). The re-poll
    must re-append the missing OPTRD and book the equity leg.
    """
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    # Pre-seed ONLY the OPASN row — the crash committed it but not the OPTRD.
    async with factory() as sess:
        sess.add(
            BrokerEventLogRow(
                event_key="activity:act-asn-1",
                event_type="OPASN",
                thesis_id="thesis-1",
                invocation_id=INV_ID,
                position_id="pos-1",
                raw_payload_json=json.dumps(
                    {
                        "activity_id": "act-asn-1",
                        "occ_symbol": _OCC,
                        "realized_pnl_delta_usd": "-1250.00",
                    },
                    sort_keys=True,
                ),
                broker_timestamp=_TXN,
                captured_at=_TXN,
            )
        )
        await sess.commit()

    # Re-poll the same assignment: the OPTRD must re-append (idempotent OPASN).
    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(
            handle,
            _assignment_event(LifecycleActivityType.OPASN),
            borrow_cost_resolver=_borrow_resolver,
        )
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: e for e in events}
        # The OPTRD is now present — the crash window is repaired on re-poll.
        assert set(by_type) == {"OPASN", "OPTRD"}
        assert by_type["OPTRD"].thesis_id == "thesis-1"
        assert by_type["OPTRD"].position_id == "pos-1"
        optrd_payload = json.loads(by_type["OPTRD"].raw_payload_json)
        assert money(optrd_payload["cost_basis_delta_usd"]) == money("75000.00")
        # The option booked (closed) and the equity leg opened.
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"
        equities = (
            (await sess.execute(select(PositionRow).where(PositionRow.status == "OPEN")))
            .scalars()
            .all()
        )
        assert len(equities) == 1

    # The derivation now folds the equity cost basis it would have lost.
    async with factory() as sess:
        ledger_record = await rederive_thesis_pnl_ledger(sess, ThesisId("thesis-1"), None)
        await sess.commit()
    assert ledger_record.cost_basis_usd == money("75000.00")


async def test_assignment_without_paired_optrd_surfaces(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A missing paired OPTRD surfaces (raises) — the equity leg is underspecified."""
    _engine, factory = db
    await _seed_open_option(factory)

    unpaired = LifecycleEvent(
        activity_id="act-asn-2",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )

    ctx, handle = await open_handle(factory)
    try:
        with pytest.raises(ValueError, match="no paired OPTRD"):
            await integrate_lifecycle_event(handle, unpaired, borrow_cost_resolver=_borrow_resolver)
    finally:
        await ctx.__aexit__(None, None, None)
