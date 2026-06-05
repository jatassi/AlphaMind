"""Tests for the projection rebuild (ALP-854 / W2a).

``rebuild_projection(handle, alpaca_positions, alpaca_account)`` replaces the
deleted ``reconcile()`` adjudication (ADR-0001). It folds the broker-event log
onto the live broker snapshot to:

* project ``orders.status`` + ``last_update_timestamp`` from
  ``TERMINAL_ORDER_STATUS`` events (critical #2);
* surface a broker position with no Intent as a first-class projection state,
  never an alert;
* re-derive the per-thesis PnL ledger from the log (critical #1).

The pure functional core (``classify_broker_facts_without_intent`` /
``project_terminal_order_statuses``) is exercised directly; the shell is
exercised over a real in-memory SQLite DB (a sanctioned mock boundary).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind._kernel.money import money, price
from alphamind.execution.broker_adapter.queries import PositionSnapshot, TradeAccountSnapshot
from alphamind.execution.write_paths.phase1 import rederive_thesis_ledgers
from alphamind.execution.write_paths.projection_rebuild import (
    BrokerFactNoIntent,
    OrderStatusProjection,
    PendingWithBrokerHolding,
    classify_broker_facts_without_intent,
    classify_pending_with_broker_holding,
    project_terminal_order_statuses,
    rebuild_projection,
)
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)
from alphamind.state.tables.broker_event_log_codec import record_to_row as event_record_to_row
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.state._fk_substrate import stub_order_row

from ._handler_substrate import (
    NOW,
    make_active_bracket,
    make_active_thesis,
    make_open_equity_position,
    make_pending_entry_order,
    make_pending_equity_position,
    open_handle,
    seed_invocation_substrate,
    seed_position_cluster,
)


def _trade_account(*, cash: float = 100_000.0) -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="alp-account-1",
        cash=money(cash),
        equity=money(cash),
        buying_power=money(cash * 2),
        regt_buying_power=money(cash * 2),
        daytrading_buying_power=money(cash * 2),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _equity_snapshot(*, symbol: str, qty: float = 10.0) -> PositionSnapshot:
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


def _terminal_event(
    *,
    event_key: str,
    alpaca_order_id: str,
    client_order_id: str,
    terminal_status: OrderStatus,
    cumulative_filled_quantity: float = 0.0,
    thesis_id: str | None = "thesis-1",
    position_id: str | None = "pos-1",
) -> BrokerEventRecord:
    """A TERMINAL_ORDER_STATUS event-log record shaped like ``order_status_sync``.

    The payload mirrors a ``FillReport.model_dump`` plus the ``terminal_status``
    key the rebuild reads — the same shape ``terminal_status_event_record``
    produces.
    """
    payload = {
        "alpaca_order_id": alpaca_order_id,
        "client_order_id": client_order_id,
        "cumulative_filled_quantity": cumulative_filled_quantity,
        "terminal_status": terminal_status.value,
    }
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.TERMINAL_ORDER_STATUS,
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        invocation_id=None,
        position_id=None if position_id is None else PositionId(position_id),
        raw_payload_json=serialize_event_payload(payload),
        broker_timestamp=NOW,
        captured_at=NOW,
    )


def _fill_event(
    *,
    event_key: str,
    alpaca_order_id: str,
    fill_price: float,
    fill_quantity: float,
    side: str,
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
) -> BrokerEventRecord:
    """A FILL event-log record the thesis-PnL fold can derive a lot from."""
    payload = {
        "alpaca_order_id": alpaca_order_id,
        "fill_price": fill_price,
        "fill_quantity": fill_quantity,
        "raw_event_payload": {"order": {"side": side}},
    }
    return BrokerEventRecord(
        event_key=event_key,
        event_type=BrokerEventType.FILL,
        thesis_id=ThesisId(thesis_id),
        invocation_id=None,
        position_id=PositionId(position_id),
        raw_payload_json=serialize_event_payload(payload),
        broker_timestamp=NOW,
        captured_at=NOW,
    )


async def _append_events(
    factory: async_sessionmaker[AsyncSession], *events: BrokerEventRecord
) -> None:
    async with factory() as sess:
        for event in events:
            sess.add(event_record_to_row(event))
        await sess.commit()


async def _entry_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    alpaca_order_id: str,
    client_order_id: str = "inv-1.ENV-1.0.0",
) -> None:
    """Seed the standard cluster, then stamp the entry order's broker ids.

    ``make_pending_entry_order`` defaults ``alpaca_order_id`` to the (deleted)
    ``alp-`` synthetic placeholder; this overrides it to a realistic broker UUID
    + ``client_order_id`` so the projection's two resolution keys are exercised.
    """
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        row.alpaca_order_id = alpaca_order_id
        row.client_order_id = client_order_id
        row.status = OrderStatus.PENDING.value
        await sess.commit()


# ---------------------------------------------------------------------------
# Functional core — pure derivations.
# ---------------------------------------------------------------------------


def test_classify_broker_facts_flags_only_positions_without_intent() -> None:
    """AC3 (pure) — a broker symbol absent from the Intent-backed set is surfaced
    as a BrokerFactNoIntent; one present is not."""
    facts = classify_broker_facts_without_intent(
        alpaca_positions=(
            _equity_snapshot(symbol="AAPL"),
            _equity_snapshot(symbol="DVN", qty=42.0),
        ),
        intent_backed_symbols=frozenset({"AAPL"}),
    )
    assert facts == (
        BrokerFactNoIntent(symbol="DVN", asset_class="us_equity", qty=42.0, side="long"),
    )


def test_project_terminal_order_statuses_folds_recognised_dispositions() -> None:
    """Pure — a recognised zero-fill terminal disposition folds to a projection;
    an unrecognised one is skipped."""
    payloads = [
        json.dumps(
            {"alpaca_order_id": "u1", "client_order_id": "c1", "terminal_status": "CANCELLED"}
        ),
        json.dumps(
            {"alpaca_order_id": "u2", "client_order_id": "c2", "terminal_status": "EXPIRED"}
        ),
        json.dumps({"alpaca_order_id": "u3", "client_order_id": "c3", "terminal_status": "FILLED"}),
    ]
    projections = project_terminal_order_statuses(payloads)
    assert projections == (
        OrderStatusProjection(
            alpaca_order_id="u1", client_order_id="c1", terminal_status=OrderStatus.CANCELLED
        ),
        OrderStatusProjection(
            alpaca_order_id="u2", client_order_id="c2", terminal_status=OrderStatus.EXPIRED
        ),
    )


# ---------------------------------------------------------------------------
# Critical #2 — orders.status projection from TERMINAL_ORDER_STATUS events.
# ---------------------------------------------------------------------------


async def test_zero_fill_terminal_event_advances_order_status_to_cancelled(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Critical #2 — a zero-fill TERMINAL_ORDER_STATUS event advances the cached
    ``orders.status`` PENDING → CANCELLED and stamps ``last_update_timestamp`` so
    the ``entry_no_fill`` condition (status IN (EXPIRED, CANCELLED)) can match."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await _entry_order(factory, alpaca_order_id="broker-uuid-1")
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-1",
            alpaca_order_id="broker-uuid-1",
            client_order_id="inv-1.ENV-1.0.0",
            terminal_status=OrderStatus.CANCELLED,
        ),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 1
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        assert row.status == OrderStatus.CANCELLED.value
        # last_update_timestamp re-stamped at observation time (not the seeded value).
        seeded_ts = make_pending_entry_order().last_update_timestamp.isoformat()
        assert row.last_update_timestamp != seeded_ts


async def test_async_rejected_event_advances_order_status_to_rejected(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CR3 — an async post-acceptance ``rejected`` event lands a
    TERMINAL_ORDER_STATUS row whose disposition the rebuild projects PENDING →
    REJECTED, so the order reaches a terminal projection (the reservation no
    longer reads against a live PENDING order) instead of stranding forever."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await _entry_order(factory, alpaca_order_id="broker-uuid-rej")
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-rej",
            alpaca_order_id="broker-uuid-rej",
            client_order_id="inv-1.ENV-1.0.0",
            terminal_status=OrderStatus.REJECTED,
        ),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 1
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        assert row.status == OrderStatus.REJECTED.value


async def test_terminal_event_resolves_order_by_client_order_id_when_uuid_null(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Critical #2 — an order whose post-submit ``alpaca_order_id`` backfill was
    lost (UUID still NULL) is resolved by its durable pre-commit
    ``client_order_id`` and still advanced to EXPIRED."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        row.alpaca_order_id = None
        row.client_order_id = "inv-1.ENV-1.0.0"
        row.status = OrderStatus.PENDING.value
        await sess.commit()
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-2",
            alpaca_order_id="broker-uuid-unmatched",
            client_order_id="inv-1.ENV-1.0.0",
            terminal_status=OrderStatus.EXPIRED,
        ),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 1
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        assert row.status == OrderStatus.EXPIRED.value


# ---------------------------------------------------------------------------
# PR1 — the terminal-status scan is bounded, not O(all-events).
# ---------------------------------------------------------------------------


async def test_terminal_status_projection_resolves_orders_in_batch(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """PR1 — the projection resolves order rows in a bounded number of SELECTs,
    not two per event. With three pending orders each carrying a terminal event,
    the orders-resolution must not scale with the event count: a per-event
    2-SELECT resolve would issue ~6 reads against ``orders``; the batched resolve
    issues a small constant regardless of event count."""
    engine, factory = db
    await seed_invocation_substrate(factory)
    await _entry_order(factory, alpaca_order_id="broker-uuid-1")
    # Two more pending orders on the same bracket, each by a distinct broker UUID.
    async with factory() as sess:
        for n in (2, 3):
            sess.add(
                stub_order_row(
                    f"ord-extra-{n}",
                    "brk-1",
                    position_id="pos-1",
                    status=OrderStatus.PENDING.value,
                    alpaca_order_id=f"broker-uuid-{n}",
                )
            )
        await sess.commit()
    await _append_events(
        factory,
        *(
            _terminal_event(
                event_key=f"tevt-batch-{n}",
                alpaca_order_id=f"broker-uuid-{n}",
                client_order_id=f"cli-{n}",
                terminal_status=OrderStatus.CANCELLED,
            )
            for n in (1, 2, 3)
        ),
    )

    orders_selects: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        normalized = " ".join(statement.split()).lower()
        if "from orders" in normalized and normalized.startswith("select"):
            orders_selects.append(statement)

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 3
    # Batched: a small constant (the batch resolve, not ~2 per event). A
    # per-event 2-SELECT resolve over 3 events would issue 6+ reads.
    assert len(orders_selects) <= 2, orders_selects


async def test_terminal_status_projection_skips_already_terminal_orders(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """PR1 — a re-delivered terminal event whose order is already in the target
    terminal status is bounded out (counts 0, no re-advance), so a rebuild does
    not re-resolve / re-stamp already-projected orders run after run."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await _entry_order(factory, alpaca_order_id="broker-uuid-1")
    # The order is ALREADY CANCELLED (a prior rebuild projected it).
    async with factory() as sess:
        row = await sess.get(OrderRow, "ord-entry-1")
        assert row is not None
        row.status = OrderStatus.CANCELLED.value
        await sess.commit()
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-dup",
            alpaca_order_id="broker-uuid-1",
            client_order_id="inv-1.ENV-1.0.0",
            terminal_status=OrderStatus.CANCELLED,
        ),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 0


# ---------------------------------------------------------------------------
# AC1 — mismatch rebuilds, no alert/correction; AC3 — broker fact, no Intent.
# ---------------------------------------------------------------------------


async def test_broker_position_without_intent_surfaces_as_projection_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC3 — a broker position with no matching local Intent (the DVN/manual-trade
    case) surfaces in the rebuild summary, NOT as a RECONCILIATION_ALERT."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(
        handle,
        alpaca_positions=(
            _equity_snapshot(symbol="AAPL"),  # Intent-backed (the seeded position)
            _equity_snapshot(symbol="DVN", qty=42.0),  # broker fact, no Intent
        ),
        alpaca_account=_trade_account(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.broker_facts_without_intent == (
        BrokerFactNoIntent(symbol="DVN", asset_class="us_equity", qty=42.0, side="long"),
    )


# ---------------------------------------------------------------------------
# RD1 — a PENDING-local position with a nonzero broker holding (dropped fill).
# ---------------------------------------------------------------------------


def test_classify_pending_with_broker_holding_flags_only_nonzero_pending() -> None:
    """Pure — a PENDING-local symbol whose broker snapshot qty is nonzero is
    surfaced; a zero-qty broker holding (or a PENDING symbol absent from the
    snapshot) is not."""
    holdings = classify_pending_with_broker_holding(
        alpaca_positions=(
            _equity_snapshot(symbol="AAPL", qty=10.0),  # PENDING + nonzero → flag
            _equity_snapshot(symbol="MSFT", qty=0.0),  # PENDING but zero → no flag
        ),
        pending_symbols=frozenset({"AAPL", "MSFT", "TSLA"}),  # TSLA absent from snapshot
    )
    assert holdings == (
        PendingWithBrokerHolding(symbol="AAPL", asset_class="us_equity", qty=10.0, side="long"),
    )


def test_classify_pending_with_broker_holding_uses_abs_qty_for_shorts() -> None:
    """Pure — a short broker holding (signed-negative qty) flags on magnitude."""
    short = PositionSnapshot(
        symbol="AAPL",
        asset_class="us_equity",
        qty=-10.0,
        avg_entry_price=price(150.0),
        market_value=money(1500.0),
        cost_basis=money(1500.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="short",
    )
    holdings = classify_pending_with_broker_holding(
        alpaca_positions=(short,),
        pending_symbols=frozenset({"AAPL"}),
    )
    assert holdings == (
        PendingWithBrokerHolding(symbol="AAPL", asset_class="us_equity", qty=10.0, side="short"),
    )


async def test_pending_local_with_nonzero_alpaca_surfaces_in_summary(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """RD1 — a PENDING local equity position (share_count=0, no fills) whose
    Alpaca holding is nonzero is a dropped/un-integrated entry fill; the rebuild
    surfaces it as a ``PendingWithBrokerHolding`` projection signal (replacing
    the deleted ``pending_with_broker_holding`` escalation) and mutates nothing
    — no status flip, no share_count write, no synthesized fill."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_pending_equity_position(),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(
        handle,
        # Alpaca holds 10 shares — the dropped entry fill.
        alpaca_positions=(_equity_snapshot(symbol="AAPL", qty=10.0),),
        alpaca_account=_trade_account(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.pending_with_broker_holding == (
        PendingWithBrokerHolding(symbol="AAPL", asset_class="us_equity", qty=10.0, side="long"),
    )
    # The PENDING symbol is Intent-backed, so it is NOT a broker-fact-no-Intent.
    assert summary.broker_facts_without_intent == ()
    # Row UNTOUCHED — still PENDING, still zero shares.
    async with factory() as sess:
        from alphamind.state.tables.positions import PositionRow

        row = await sess.get(PositionRow, "pos-1")
        assert row is not None
        assert row.status == "PENDING"


# ---------------------------------------------------------------------------
# Critical #1 — the thesis PnL ledger is populated after the rebuild.
# ---------------------------------------------------------------------------


async def test_rebuild_populates_thesis_pnl_ledger_from_the_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Critical #1 — the rebuild wires ``rederive_thesis_pnl_ledger``: a thesis
    carrying FILL events in the log gets a ``thesis_pnl_ledger`` row derived from
    the log (previously dark — no production caller)."""
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    # A single buy FILL opens a 10-share lot at $150 → cost_basis 1500, realized 0.
    await _append_events(
        factory,
        _fill_event(
            event_key="fevt-1",
            alpaca_order_id="broker-uuid-1",
            fill_price=150.0,
            fill_quantity=10.0,
            side="buy",
        ),
    )

    async with factory() as sess:
        before = (await sess.execute(select(ThesisPnlLedgerRow))).scalars().all()
    assert before == []  # dark before the rebuild

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.theses_rederived == 1
    async with factory() as sess:
        ledger = (await sess.execute(select(ThesisPnlLedgerRow))).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].thesis_id == "thesis-1"
        assert ledger[0].cost_basis_usd == pytest.approx(1500.0)
        assert ledger[0].realized_pnl_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CR1 — the thesis-ledger rederive runs AFTER the option-lifecycle activities
# poll, so a same-invocation OPASN/OPTRD activity is folded into the ledger.
# ---------------------------------------------------------------------------


def _activity_event(
    *,
    event_key: str,
    event_type: BrokerEventType,
    realized_pnl_delta_usd: float,
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
) -> BrokerEventRecord:
    """An option-lifecycle activity event the poll appends, carrying realized PnL.

    Mirrors the OPASN / OPTRD rows ``account_activities.handlers`` stamps with a
    ``realized_pnl_delta_usd`` on the payload — the thesis-PnL fold aggregates the
    delta into the ledger's realized PnL.
    """
    payload = {"realized_pnl_delta_usd": realized_pnl_delta_usd}
    return BrokerEventRecord(
        event_key=event_key,
        event_type=event_type,
        thesis_id=ThesisId(thesis_id),
        invocation_id=None,
        position_id=PositionId(position_id),
        raw_payload_json=serialize_event_payload(payload),
        broker_timestamp=NOW,
        captured_at=NOW,
    )


async def test_rederive_after_activities_poll_folds_same_invocation_lifecycle_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CR1 — ``rederive_thesis_ledgers`` runs in the orchestrator write unit AFTER
    ``run_account_activities_poll`` appends an OPASN/OPTRD event, so a thesis with a
    same-invocation option-lifecycle event has its PnL ledger reflect that event.

    On the old ordering the rederive ran inside ``process_unprocessed_fills`` —
    BEFORE the activities poll — so the activity's realized-PnL delta was missing
    from the ledger that invocation. This drives the fixed sequence: fills folded,
    then the poll appends the activity, then the rederive sees the complete log.
    """
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    # An opening buy FILL (the Phase-1 fill-fold leg) — cost_basis 1500, realized 0.
    await _append_events(
        factory,
        _fill_event(
            event_key="fevt-cr1",
            alpaca_order_id="broker-uuid-cr1",
            fill_price=150.0,
            fill_quantity=10.0,
            side="buy",
        ),
    )

    # The activities poll appends a realized-PnL OPASN event SAME invocation.
    await _append_events(
        factory,
        _activity_event(
            event_key="aevt-cr1",
            event_type=BrokerEventType.OPASN,
            realized_pnl_delta_usd=275.0,
        ),
    )

    ctx, handle = await open_handle(factory)
    rederived = await rederive_thesis_ledgers(handle)
    await ctx.__aexit__(None, None, None)

    assert rederived == 1
    async with factory() as sess:
        ledger = (await sess.execute(select(ThesisPnlLedgerRow))).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].thesis_id == "thesis-1"
        # The post-poll rederive folded the activity's realized-PnL delta into the
        # ledger — the same-invocation lifecycle event is NOT lost.
        assert ledger[0].realized_pnl_usd == pytest.approx(275.0)
