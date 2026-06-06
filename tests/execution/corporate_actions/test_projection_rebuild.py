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
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol, ThesisId
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
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.invocation_context.context import InvocationContext, InvocationHandle
from alphamind.state.records import FillProcessingStatus
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.broker_event_log_codec import record_to_row as event_record_to_row
from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID, CashLedgerRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.projection_rebuild_watermark import ProjectionRebuildWatermarkRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import record_to_rows as thesis_record_to_rows
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.state._fk_substrate import stub_order_row

from ._handler_substrate import (
    NOW,
    build_async_db,
    make_active_bracket,
    make_active_thesis,
    make_invocation_record,
    make_open_equity_position,
    make_pending_entry_order,
    make_pending_equity_position,
    open_handle,
    seed_cash_ledger,
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
# CR1-cleanup — the rebuild no longer rederives the thesis ledgers; the
# post-poll ``rederive_thesis_ledgers`` is the SOLE rederive (no double-write).
# ---------------------------------------------------------------------------


async def test_rebuild_does_not_rederive_thesis_ledgers(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CR1-cleanup — ``rebuild_projection`` no longer re-derives the thesis PnL
    ledgers, so a thesis carrying FILL events stays DARK after the rebuild. The
    sole rederive is the orchestrator's post-poll ``rederive_thesis_ledgers``,
    which folds the *complete* log (post account-activities poll) — re-deriving in
    the rebuild too would be a redundant pre-poll double-write."""
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

    ctx, handle = await open_handle(factory)
    await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    # The rebuild wrote NO ledger row — the thesis stays dark until the post-poll
    # rederive runs (the rebuild's redundant rederive is removed).
    async with factory() as sess:
        after_rebuild = (await sess.execute(select(ThesisPnlLedgerRow))).scalars().all()
    assert after_rebuild == []


async def test_post_poll_rederive_produces_the_complete_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CR1-cleanup — the post-poll ``rederive_thesis_ledgers`` (the sole rederive)
    folds the complete log into the ledger. A thesis carrying FILL events gets a
    ``thesis_pnl_ledger`` row derived from the log (the populating path moved out
    of ``rebuild_projection`` into this single post-poll pass)."""
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

    ctx, handle = await open_handle(factory)
    rederived = await rederive_thesis_ledgers(handle)
    await ctx.__aexit__(None, None, None)

    assert rederived == 1
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


# ---------------------------------------------------------------------------
# ALP-865 — the two O(all-history) scans are bounded by an event_seq watermark.
# ---------------------------------------------------------------------------


async def _open_handle_with(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> tuple[InvocationContext, InvocationHandle]:
    """Open a write handle bound to *invocation_id* (a distinct invocation per call).

    ``open_handle`` always uses one fixed invocation id; the watermark/dirty-set
    tests need several invocations in one DB (``insert_invocation_row`` is a plain
    insert, so a repeated id would collide on the PK).
    """
    ctx = InvocationContext(
        session_factory=factory,
        record=make_invocation_record(invocation_id=invocation_id),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


async def test_terminal_status_scan_is_bounded_by_the_watermark(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC (part-1) — the terminal-status scan reads only events with
    ``event_seq > last_projected_event_seq``. After a first rebuild advances the
    watermark, a later rebuild does **not** re-scan (and so cannot re-project) an
    event at or below the watermark — only genuinely new events are folded.

    Order A is cancelled by event ``tevt-A`` in rebuild #1 (watermark → seqA). Then
    A is reset to PENDING and a new order B with a newer terminal event ``tevt-B``
    (seqB > seqA) is appended. Rebuild #2 projects only B: A stays PENDING because
    its event is at/below the watermark and is never re-read — the unbounded scan
    would have re-folded ``tevt-A`` and re-cancelled A.
    """
    _, factory = db
    await seed_invocation_substrate(factory)
    await _entry_order(factory, alpaca_order_id="broker-uuid-A")
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-A",
            alpaca_order_id="broker-uuid-A",
            client_order_id="cli-A",
            terminal_status=OrderStatus.CANCELLED,
        ),
    )

    ctx1, handle1 = await _open_handle_with(factory, "inv-865-r1")
    summary1 = await rebuild_projection(handle1, alpaca_positions=(), alpaca_account=None)
    await ctx1.__aexit__(None, None, None)
    assert summary1.order_statuses_projected == 1

    # The singleton watermark advanced to the scanned event's seq.
    async with factory() as sess:
        watermark = await sess.get(ProjectionRebuildWatermarkRow, "current")
        assert watermark is not None
        assert watermark.last_projected_event_seq > 0

    # Reset A to PENDING (the unbounded scan would re-cancel it from tevt-A), and
    # append a NEW pending order B with a terminal event past the watermark.
    async with factory() as sess:
        row_a = await sess.get(OrderRow, "ord-entry-1")
        assert row_a is not None
        row_a.status = OrderStatus.PENDING.value
        sess.add(
            stub_order_row(
                "ord-extra-B",
                "brk-1",
                position_id="pos-1",
                status=OrderStatus.PENDING.value,
                alpaca_order_id="broker-uuid-B",
            )
        )
        await sess.commit()
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-B",
            alpaca_order_id="broker-uuid-B",
            client_order_id="cli-B",
            terminal_status=OrderStatus.CANCELLED,
        ),
    )

    ctx2, handle2 = await _open_handle_with(factory, "inv-865-r2")
    summary2 = await rebuild_projection(handle2, alpaca_positions=(), alpaca_account=None)
    await ctx2.__aexit__(None, None, None)

    # Only B's event was scanned (seqB > watermark); A's older event was not re-read.
    assert summary2.order_statuses_projected == 1
    async with factory() as sess:
        row_a = await sess.get(OrderRow, "ord-entry-1")
        row_b = await sess.get(OrderRow, "ord-extra-B")
        assert row_a is not None and row_b is not None
        assert row_a.status == OrderStatus.PENDING.value  # NOT re-cancelled
        assert row_b.status == OrderStatus.CANCELLED.value  # the new event projected


async def test_unresolved_terminal_event_still_advances_watermark(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC (part-1) — a TERMINAL_ORDER_STATUS event that resolves to no order row
    (a genuinely out-of-band order, no local row that will ever exist) still
    advances the watermark, so it is processed exactly once and never re-scanned.

    This pins the safety contract the bounded scan depends on: advancing past an
    unresolved event is correct because (ALP-836) a durable order row precedes the
    broker submit that produces the event, so an unresolved miss is never a
    not-yet-persisted order a later run would resolve. Re-scanning it forever would
    reintroduce the O(all-history) cost the bound removes.
    """
    _, factory = db
    await seed_invocation_substrate(factory)
    # An out-of-band terminal event: no local order row, no thesis/position link.
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-orphan",
            alpaca_order_id="broker-uuid-orphan",
            client_order_id="cli-orphan",
            terminal_status=OrderStatus.CANCELLED,
            thesis_id=None,
            position_id=None,
        ),
    )

    ctx1, handle1 = await _open_handle_with(factory, "inv-orphan-1")
    summary1 = await rebuild_projection(handle1, alpaca_positions=(), alpaca_account=None)
    await ctx1.__aexit__(None, None, None)
    # Nothing projected (no row resolves), but the watermark advanced past the event.
    assert summary1.order_statuses_projected == 0
    async with factory() as sess:
        watermark = await sess.get(ProjectionRebuildWatermarkRow, "current")
        assert watermark is not None
        assert watermark.last_projected_event_seq > 0
        advanced_to = watermark.last_projected_event_seq

    # Second rebuild: the orphan event is at/below the watermark, so it is not
    # re-scanned — the watermark is unchanged (no new events past it).
    ctx2, handle2 = await _open_handle_with(factory, "inv-orphan-2")
    summary2 = await rebuild_projection(handle2, alpaca_positions=(), alpaca_account=None)
    await ctx2.__aexit__(None, None, None)
    assert summary2.order_statuses_projected == 0
    async with factory() as sess:
        watermark = await sess.get(ProjectionRebuildWatermarkRow, "current")
        assert watermark is not None
        assert watermark.last_projected_event_seq == advanced_to


async def test_clean_thesis_skipped_then_redirtied_by_new_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC (part-2) — a closed thesis with no new events is NOT re-derived on a later
    invocation (its ledger ``updated_at`` + ``derived_from_invocation_id`` are
    unchanged), and a new event re-dirties it.

    Derivation #1 (invocation A) writes the ledger and stamps
    ``last_derived_event_seq``. Derivation #2 (invocation B), no new events, finds
    the thesis clean and skips it — the row still carries A's invocation id and
    timestamp. A new OPASN event (seq past the watermark) makes it dirty again, so
    derivation #3 (invocation C) re-derives and re-stamps it.
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

    ctx_a, handle_a = await _open_handle_with(factory, "inv-865-a")
    n_a = await rederive_thesis_ledgers(handle_a)
    await ctx_a.__aexit__(None, None, None)
    assert n_a == 1
    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert row is not None
        first_updated_at = row.updated_at
        assert row.derived_from_invocation_id == "inv-865-a"
        assert row.last_derived_event_seq is not None
        first_seq = row.last_derived_event_seq

    # Derivation #2 — no new events → thesis is clean → skipped (count 0).
    ctx_b, handle_b = await _open_handle_with(factory, "inv-865-b")
    n_b = await rederive_thesis_ledgers(handle_b)
    await ctx_b.__aexit__(None, None, None)
    assert n_b == 0
    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert row is not None
        # Untouched — NOT re-stamped to invocation B.
        assert row.updated_at == first_updated_at
        assert row.derived_from_invocation_id == "inv-865-a"
        assert row.last_derived_event_seq == first_seq

    # A new realized-PnL event re-dirties the thesis (seq past its watermark).
    await _append_events(
        factory,
        _activity_event(
            event_key="aevt-1",
            event_type=BrokerEventType.OPASN,
            realized_pnl_delta_usd=125.0,
        ),
    )
    ctx_c, handle_c = await _open_handle_with(factory, "inv-865-c")
    n_c = await rederive_thesis_ledgers(handle_c)
    await ctx_c.__aexit__(None, None, None)
    assert n_c == 1
    async with factory() as sess:
        row = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert row is not None
        assert row.derived_from_invocation_id == "inv-865-c"  # re-derived
        assert row.last_derived_event_seq is not None
        assert row.last_derived_event_seq > first_seq
        assert row.realized_pnl_usd == pytest.approx(125.0)


async def _seed_extra_thesis(
    factory: async_sessionmaker[AsyncSession], thesis_id: str, position_id: str
) -> None:
    """Seed a second bare thesis row (reusing *position_id*) for multi-thesis logs."""
    thesis_row, component_rows = thesis_record_to_rows(
        make_active_thesis(thesis_id=thesis_id, position_id=position_id)
    )
    async with factory() as sess:
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        await sess.commit()


async def _snapshot_state(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, tuple[float, float, str]], dict[str, str]]:
    """Return comparable (ledger figures by thesis, order status by id).

    Ledger figures are the derivation output (realized PnL, cost basis,
    provenance) — the fields the bound path must reproduce identically.
    ``last_derived_event_seq`` / ``updated_at`` / ``derived_from_invocation_id``
    are deliberately excluded: the watermark and timestamps legitimately differ
    between an incremental run and a one-shot run; the *derived figures* must not.
    """
    async with factory() as sess:
        ledgers = (await sess.execute(select(ThesisPnlLedgerRow))).scalars().all()
        orders = (await sess.execute(select(OrderRow))).scalars().all()
    ledger_figures = {
        row.thesis_id: (
            float(row.realized_pnl_usd),
            float(row.cost_basis_usd),
            row.provenance_json,
        )
        for row in ledgers
    }
    order_status = {row.order_id: row.status for row in orders}
    return ledger_figures, order_status


async def test_bounded_incremental_matches_full_one_shot(tmp_path: Path) -> None:
    """AC (identical output) — over a multi-event, multi-thesis log, the bounded
    incremental path (a rebuild + rederive after each of two batches, so the
    watermark + dirty-set actually bound the second pass) produces ledger figures
    and projected order statuses **identical** to a single full one-shot derivation
    of the same complete log.

    Both DBs receive the same events; only the *number of bounded passes* differs.
    Identical final state proves the watermark/dirty-set change the *amount scanned*,
    never the derived result.
    """

    async def _build_and_run(
        factory: async_sessionmaker[AsyncSession], *, incremental: bool
    ) -> tuple[dict[str, tuple[float, float, str]], dict[str, str]]:
        await seed_invocation_substrate(factory)
        await seed_position_cluster(
            factory,
            make_open_equity_position(share_count=10.0),
            make_pending_entry_order(),
            make_active_thesis(),
            make_active_bracket(),
        )
        await _seed_extra_thesis(factory, thesis_id="thesis-2", position_id="pos-1")
        # A standalone pending order whose terminal event the projection folds.
        async with factory() as sess:
            sess.add(
                stub_order_row(
                    "ord-extra",
                    "brk-1",
                    position_id="pos-1",
                    status=OrderStatus.PENDING.value,
                    alpaca_order_id="broker-uuid-extra",
                )
            )
            await sess.commit()

        batch1 = (
            _fill_event(
                event_key="fevt-t1",
                alpaca_order_id="broker-uuid-1",
                fill_price=150.0,
                fill_quantity=10.0,
                side="buy",
            ),
            _fill_event(
                event_key="fevt-t2",
                alpaca_order_id="broker-uuid-2",
                fill_price=200.0,
                fill_quantity=5.0,
                side="buy",
                thesis_id="thesis-2",
            ),
            _terminal_event(
                event_key="tevt-extra",
                alpaca_order_id="broker-uuid-extra",
                client_order_id="cli-extra",
                terminal_status=OrderStatus.CANCELLED,
            ),
        )
        batch2 = (
            _activity_event(
                event_key="aevt-t1",
                event_type=BrokerEventType.OPASN,
                realized_pnl_delta_usd=80.0,
            ),
            _activity_event(
                event_key="aevt-t2",
                event_type=BrokerEventType.OPASN,
                realized_pnl_delta_usd=-30.0,
                thesis_id="thesis-2",
            ),
        )

        if incremental:
            await _append_events(factory, *batch1)
            ctx1, h1 = await _open_handle_with(factory, "inv-inc-1")
            await rebuild_projection(h1, alpaca_positions=(), alpaca_account=None)
            await rederive_thesis_ledgers(h1)
            await ctx1.__aexit__(None, None, None)
            await _append_events(factory, *batch2)
            ctx2, h2 = await _open_handle_with(factory, "inv-inc-2")
            await rebuild_projection(h2, alpaca_positions=(), alpaca_account=None)
            await rederive_thesis_ledgers(h2)
            await ctx2.__aexit__(None, None, None)
        else:
            await _append_events(factory, *batch1, *batch2)
            ctx, h = await _open_handle_with(factory, "inv-full-1")
            await rebuild_projection(h, alpaca_positions=(), alpaca_account=None)
            await rederive_thesis_ledgers(h)
            await ctx.__aexit__(None, None, None)

        return await _snapshot_state(factory)

    inc_engine, inc_factory = build_async_db(tmp_path, "incremental")
    full_engine, full_factory = build_async_db(tmp_path, "oneshot")
    try:
        incremental_state = await _build_and_run(inc_factory, incremental=True)
        one_shot_state = await _build_and_run(full_factory, incremental=False)
    finally:
        await inc_engine.dispose()
        await full_engine.dispose()

    incremental_ledgers, incremental_orders = incremental_state
    one_shot_ledgers, one_shot_orders = one_shot_state
    # Two theses derived, identically, regardless of batching.
    assert set(incremental_ledgers) == {"thesis-1", "thesis-2"}
    assert incremental_ledgers == one_shot_ledgers
    # The cancelled order projected identically too.
    assert incremental_orders == one_shot_orders
    assert incremental_orders["ord-extra"] == OrderStatus.CANCELLED.value


# ALP-863 — entry-window cancel cascade, relocated off the always-on monitor.
#
# With ``orders.status`` projected, every bracket still ``PENDING_ENTRY`` whose
# ENTRY order just reached a terminal status with no recorded fills is dissolved
# by the pipeline (not the monitor): bracket DISSOLVED, capital released, thesis
# ``CANCELLED_NEVER_ENTERED``, position PENDING→CANCELLED. The cascade internals
# are covered by the phase-2 ``persist_entry_window_cancel`` test; these prove the
# *rebuild* triggers it — and declines when the entry filled in the cancel race.
# ---------------------------------------------------------------------------


def _pending_entry_bracket() -> BracketRecord:
    """A ``PENDING_ENTRY`` bracket (still awaiting its patient limit entry to fill)
    over the standard ``brk-1`` / ``pos-1`` / ``ord-entry-1`` cluster."""
    leg = BracketLeg(
        leg_id="brk-1-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("brk-1-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId("brk-1"),
        position_id=PositionId("pos-1"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=NOW,
    )


def _pending_entry_limit_order(*, status: OrderStatus = OrderStatus.PENDING) -> OrderRecord:
    """A patient never-filled equity-bracket LIMIT entry (10 sh @ $100 → $1,000 reserved)."""
    return OrderRecord(
        order_id=OrderId("ord-entry-1"),
        position_id=PositionId("pos-1"),
        bracket_id=BracketId("brk-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("100.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=status,
        alpaca_order_id=AlpacaOrderId("broker-uuid-cancel"),
        alpaca_order_id_chain=(AlpacaOrderId("broker-uuid-cancel"),),
        submission_timestamp=NOW,
        last_update_timestamp=NOW,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-1"),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


async def _seed_unprocessed_fill(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed one UNPROCESSED ``fill_records`` row for ``ord-entry-1`` — the FILL the
    fill-stream consumer wrote in the cancel race, not yet drained into
    ``filled_quantity``. Its mere presence is the ``has_recorded_fills`` signal the
    cascade predicate excludes on (it is the load-bearing guard, not ``filled_quantity``).
    """
    async with factory() as sess:
        sess.add(
            FillRecordRow(
                fill_id="fill-race-1",
                order_id="ord-entry-1",
                fill_timestamp=NOW.isoformat(),
                fill_price=Decimal("100.0"),
                fill_quantity=4.0,
                remaining_quantity_after=6.0,
                order_status_after=OrderStatus.PARTIALLY_FILLED.value,
                slippage_usd=None,
                fees_usd=Decimal(0),
                execution_venue=None,
                gateway_reference=None,
                persistence_timestamp=NOW.isoformat(),
                processing_status=FillProcessingStatus.UNPROCESSED.value,
                processing_invocation_id=None,
                processing_timestamp=None,
                regt_attribution_json=None,
                live_execution_estimate_json=None,
            )
        )
        await sess.commit()


@pytest.mark.parametrize("terminal_status", [OrderStatus.CANCELLED, OrderStatus.EXPIRED])
async def test_terminal_entry_event_dissolves_pending_entry_bracket_via_cascade(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    terminal_status: OrderStatus,
) -> None:
    """ALP-863 AC2 — a zero-fill terminal event for a ``PENDING_ENTRY`` bracket's
    ENTRY order makes the *pipeline* (not the monitor) run the dissolve cascade:
    the order projects to its terminal status, then the bracket DISSOLVES, the
    reserved capital releases, the thesis resolves ``CANCELLED_NEVER_ENTERED``, and
    the never-filled position goes PENDING→CANCELLED.

    Driven for both CANCELLED (the monitor broker-cancel) and EXPIRED (the broker's
    own TIF lapse) — the cascade canonicalizes either disposition to ``CANCELLED`` on
    the order via the shared CANCEL writeback (matching the monitor's prior behavior).
    The order stays terminal + ``filled_quantity == 0`` so the ``entry_no_fill`` alert
    still matches (it fires off ``orders.status``, independently of the cascade).
    """
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await seed_position_cluster(
        factory,
        make_pending_equity_position(),
        _pending_entry_limit_order(),
        make_active_thesis(),
        _pending_entry_bracket(),
    )
    await _append_events(
        factory,
        _terminal_event(
            event_key="tevt-cancel",
            alpaca_order_id="broker-uuid-cancel",
            client_order_id="inv-1.ENV-1.0.0",
            terminal_status=terminal_status,
        ),
    )

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.order_statuses_projected == 1
    assert summary.entry_window_cancels_cascaded == 1
    async with factory() as sess:
        order = await sess.get(OrderRow, "ord-entry-1")
        assert order is not None
        # Canonicalized to CANCELLED by the shared CANCEL writeback, even for EXPIRED.
        assert order.status == OrderStatus.CANCELLED.value
        assert order.filled_quantity == 0  # the no-fill alert precondition still holds

        bracket = await sess.get(BracketRow, "brk-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value

        thesis = await sess.get(ThesisRow, "thesis-1")
        assert thesis is not None
        assert thesis.status == ThesisRecordStatus.CANCELLED.value
        assert thesis.resolution_category == "CANCELLED_NEVER_ENTERED"

        position = await sess.get(PositionRow, "pos-1")
        assert position is not None
        assert position.status == "CANCELLED"

        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)  # the $1,000 released


async def test_filled_entry_in_cancel_race_is_not_dissolved(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-863 AC3 — if the entry filled in the cancel race, the cascade does NOT
    fire. The order reached a terminal status (broker-cancelled), but a FILL flowed
    in the race: an UNPROCESSED ``fill_records`` row exists, not yet drained into
    ``filled_quantity``. The predicate's ``NOT EXISTS (fill_records)`` guard (the
    ``has_recorded_fills`` parity) excludes it, so the never-dissolved bracket keeps
    protecting the filled shares and the reserved capital is left untouched.

    The untouched-capital assertion is the discriminator: without the fill_records
    guard the cascade would run and ``persist_entry_window_cancel`` would release the
    unfilled-remainder capital (via its ALP-760 early-return) before declining to
    dissolve — so capital would drop below the reserved $1,000.
    """
    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await seed_position_cluster(
        factory,
        make_pending_equity_position(),
        _pending_entry_limit_order(status=OrderStatus.CANCELLED),
        make_active_thesis(),
        _pending_entry_bracket(),
    )
    await _seed_unprocessed_fill(factory)  # the FILL that flowed in the race

    ctx, handle = await open_handle(factory)
    summary = await rebuild_projection(handle, alpaca_positions=(), alpaca_account=None)
    await ctx.__aexit__(None, None, None)

    assert summary.entry_window_cancels_cascaded == 0
    async with factory() as sess:
        # Bracket NOT dissolved — the filled shares stay protected.
        bracket = await sess.get(BracketRow, "brk-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.PENDING_ENTRY.value

        # Thesis NOT resolved, position NOT cancelled — a fill landed.
        thesis = await sess.get(ThesisRow, "thesis-1")
        assert thesis is not None
        assert thesis.status == ThesisRecordStatus.ACTIVE.value

        position = await sess.get(PositionRow, "pos-1")
        assert position is not None
        assert position.status == "PENDING"

        # Reserved capital untouched — the cascade never ran (the discriminator).
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(1_000.0)
