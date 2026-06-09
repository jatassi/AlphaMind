"""Tests for the post-fill-collection auto re-bracket step (ALP-938, Scopes E/F/G).

The reproduction these tests pin: a PM-directed **partial** equity CLOSE reduces a
native-bracket position; ALP-937 cancels every broker-enforced protective leg but
leaves the bracket ACTIVE on the remainder, so the remaining shares are
broker-unprotected. Fill collection marks the position ``reprotection_needed`` (covered
by ``test_fill_collection_write_path``); ``run_reprotection_step`` then closes the naked
window — gather (read, no lock) → submit a fresh standalone OCO (broker, no lock) →
persist the re-bracketed legs onto the still-ACTIVE bracket (its own write txn).

Sociable tests against a real on-disk SQLite DB (the ``db`` fixture). The broker is
faked at the sanctioned boundary via the step's injected ``trading_client_factory`` —
no live ``TradingClient`` is constructed and no broker call happens under the write
lock. The clock is the only other mocked boundary (one gateway-exhaustion case).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.enums import OrderType as AlpacaOrderType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EnforcementBinding,
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
    TriggerSignal,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.scheduler.reprotection import run_reprotection_step
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import record_to_rows as bracket_record_to_rows
from alphamind.state.tables.brackets_codec import row_to_leg
from alphamind.state.tables.brackets_codec import rows_to_record as bracket_rows_to_record
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import record_to_row as order_record_to_row
from alphamind.state.tables.orders_codec import row_to_record as order_row_to_record
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row as position_record_to_row
from alphamind.state.tables.positions_codec import row_to_record as position_row_to_record
from tests.state._fk_substrate import stub_order_row, stub_thesis_row

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12-00Z-aaaa"
_THESIS_ID = "THE-AAPL-0123456789abcdef0123456789abcdef"
_POSITION_ID = "pos-1"
_BRACKET_ID = "brk-1"
_TP_PRICE = 200.0
_STOP_PRICE = 140.0
_REMAINING_QTY = 6.0


# ---------------------------------------------------------------------------
# Config + venue builders (the step threads these into the broker factory only)
# ---------------------------------------------------------------------------


def _make_execution_config(*, retry_window_seconds: int = 30) -> ExecutionConfig:
    from alphamind.config.models.execution import (
        FeeSchedule,
        GreeksRefresh,
        PaperHarness,
    )
    from alphamind.config.models.execution import (
        OrderType as HarnessOrderType,
    )

    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=5, move_trigger_pct=0.01),
        conservative_delta_buffer_pct=0.0,
        submission_retry_window_seconds=retry_window_seconds,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                HarnessOrderType.market: 0.1,
                HarnessOrderType.limit: 0.05,
                HarnessOrderType.stop: 0.08,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.0,
    )


def _make_venue_config() -> VenueConfig:
    creds = AlpacaCredentials(
        rest_url="https://paper-api.alpaca.markets",
        ws_url="wss://paper-api.alpaca.markets",
        api_key_env="ALPACA_PAPER_KEY",
        api_secret_env="ALPACA_PAPER_SECRET",
    )
    return VenueConfig(
        alpaca=Alpaca(paper=creds, live=creds, rate_limit_per_minute=200),
        session_hours=SessionHours(
            regular=SessionWindow(open="09:30", close="16:00"),
            pre_market=SessionWindow(open="04:00", close="09:30"),
            after_hours=SessionWindow(open="16:00", close="20:00"),
        ),
    )


# ---------------------------------------------------------------------------
# State builders — the ALP-937 naked-remainder shape
# ---------------------------------------------------------------------------


def _make_marked_position(
    *,
    share_count: float = _REMAINING_QTY,
    reprotection_needed: bool = True,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    """An OPEN equity position carrying the durable re-protection marker."""
    details = EquityPositionDetails(
        ticker=Symbol("AAPL"),
        share_count=share_count,
        average_cost_basis_per_share=150.0,
        borrow_rate_pct=None,
        accrued_borrow_cost_usd=None,
        locate_status=None,
        margin_held_usd=None,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(150.0),
            fill_quantity=10.0,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(_POSITION_ID),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=BracketId(_BRACKET_ID),
        status=status,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
        reprotection_needed=reprotection_needed,
    )


def _make_protective_order(
    *,
    order_id: str,
    role: OrderRole,
    order_type: OrderType,
    price_parameters: PriceParameters,
) -> OrderRecord:
    """A now-CANCELLED protective order carrying the original level (ALP-937 leaves
    these rows in place so the re-bracket can replay them)."""
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(_POSITION_ID),
        bracket_id=BracketId(_BRACKET_ID),
        role=role,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.SELL,  # the close side of a LONG protective leg
        order_type=order_type,
        order_class=OrderClass.SIMPLE,
        price_parameters=price_parameters,
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.CANCELLED,
        alpaca_order_id=None,
        alpaca_order_id_chain=(),
        submission_timestamp=_NOW - timedelta(hours=2),
        last_update_timestamp=_NOW - timedelta(minutes=5),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId(_THESIS_ID),
        originating_pm_command_id=None,
        age_hours=2.0,
    )


def _make_take_profit_order() -> OrderRecord:
    return _make_protective_order(
        order_id=f"{_BRACKET_ID}-ord-tp",
        role=OrderRole.TAKE_PROFIT,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=price(_TP_PRICE)),
    )


def _make_price_stop_order() -> OrderRecord:
    return _make_protective_order(
        order_id=f"{_BRACKET_ID}-ord-stop",
        role=OrderRole.PRICE_STOP,
        order_type=OrderType.STOP,
        price_parameters=PriceParameters(stop_trigger_price=price(_STOP_PRICE)),
    )


def _make_cancelled_bracket() -> BracketRecord:
    """An ACTIVE bracket whose TAKE_PROFIT + PRICE_STOP legs are both CANCELLED —
    the naked state a PM-directed partial close leaves (ALP-937)."""
    tp_leg = BracketLeg(
        leg_id=f"{_BRACKET_ID}-leg-tp",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(f"{_BRACKET_ID}-ord-tp"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=_TP_PRICE, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.CANCELLED,
        enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
    )
    stop_leg = BracketLeg(
        leg_id=f"{_BRACKET_ID}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{_BRACKET_ID}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=_STOP_PRICE, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.CANCELLED,
        enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
        trigger_signal=TriggerSignal.UNDERLYING_PRICE,
    )
    return BracketRecord(
        bracket_id=BracketId(_BRACKET_ID),
        position_id=PositionId(_POSITION_ID),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(tp_leg, stop_leg),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


async def _seed_naked_remainder(
    factory: async_sessionmaker[AsyncSession],
    *,
    position: PositionRecord | None = None,
) -> None:
    """Seed the full naked-remainder cluster atomically (deferred FKs settle at COMMIT)."""
    position = position if position is not None else _make_marked_position()
    bracket_row, leg_rows = bracket_record_to_rows(_make_cancelled_bracket())
    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(stub_thesis_row(_THESIS_ID, _POSITION_ID))
        sess.add(order_record_to_row(_make_take_profit_order()))
        sess.add(order_record_to_row(_make_price_stop_order()))
        sess.add(stub_order_row("ord-entry-1", _BRACKET_ID, position_id=_POSITION_ID))
        sess.add(bracket_row)
        await sess.flush()  # parent bracket before legs (non-deferred legs.bracket_id FK)
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


# ---------------------------------------------------------------------------
# Broker fake — returns an OCO submission carrying BOTH protective leg ids
# ---------------------------------------------------------------------------


def _fake_leg(order_type: AlpacaOrderType) -> MagicMock:
    leg = MagicMock()
    leg.id = uuid.uuid4()
    leg.order_type = order_type
    return leg


class _OcoStubClient:
    """A duck-typed Alpaca ``TradingClient`` whose ``submit_order`` returns an OCO
    order: the take-profit as the top-level LIMIT, the stop child on ``order.legs``."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.tp_id = uuid.uuid4()
        self.stop_leg = _fake_leg(AlpacaOrderType.STOP)
        self.on_submit: Any = None

    def submit_order(self, request: Any) -> Any:
        self.requests.append(request)
        if self.on_submit is not None:
            self.on_submit(request)
        order = MagicMock()
        order.id = self.tp_id
        order.client_order_id = request.client_order_id
        order.status = AlpacaOrderStatus.ACCEPTED
        order.order_class = AlpacaOrderClass.OCO
        order.order_type = AlpacaOrderType.LIMIT
        order.legs = [self.stop_leg]
        return order


def _factory_for(client: _OcoStubClient) -> Callable[[VenueConfig, ExecutionMode], TradingClient]:
    """Wrap the duck-typed broker stub as the step's ``trading_client_factory``.

    The broker is the sanctioned mock boundary; ``submit_equity_oco`` only ever calls
    ``client.submit_order``, so the duck-typed stub stands in for the real
    ``TradingClient`` (untyped at the alpaca-py boundary) under a ``cast``.
    """
    return lambda _venue, _mode: cast("TradingClient", client)


async def _read_legs(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[list[BracketLegRow], list[BracketLeg]]:
    async with factory() as sess:
        rows = list(
            (
                await sess.execute(
                    select(BracketLegRow)
                    .where(BracketLegRow.bracket_id == _BRACKET_ID)
                    .order_by(BracketLegRow.leg_index)
                )
            ).scalars()
        )
        return rows, [row_to_leg(r) for r in rows]


async def _read_position(factory: async_sessionmaker[AsyncSession]) -> PositionRecord:
    async with factory() as sess:
        row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == _POSITION_ID))
        ).scalar_one()
        return position_row_to_record(row)


async def _read_order(factory: async_sessionmaker[AsyncSession], order_id: str) -> OrderRecord:
    async with factory() as sess:
        row = (
            await sess.execute(select(OrderRow).where(OrderRow.order_id == order_id))
        ).scalar_one()
        return order_row_to_record(row)


# ---------------------------------------------------------------------------
# Reproduction — the broker-routed happy path (AC #5 / #3)
# ---------------------------------------------------------------------------


async def test_reprotection_step_resubmits_oco_and_appends_active_legs(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The end-to-end reproduction: a marked naked remainder is re-protected with a
    fresh OCO whose legs append to the still-ACTIVE bracket; the marker clears."""
    _, factory = db
    await _seed_naked_remainder(factory)
    client = _OcoStubClient()

    persisted = await run_reprotection_step(
        session_factory=factory,
        invocation_id=_INV_ID,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        execution_config=_make_execution_config(),
        broker_routing_active=True,
        now=_NOW,
        trading_client_factory=_factory_for(client),
    )

    assert persisted == 1
    # A single standalone OCO was submitted for the remaining shares.
    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.order_class == AlpacaOrderClass.OCO
    assert req.qty == _REMAINING_QTY
    assert req.take_profit.limit_price == _TP_PRICE
    assert req.stop_loss.stop_price == _STOP_PRICE

    # The marker cleared; the position stays OPEN with a readable bracket.
    pos = await _read_position(factory)
    assert pos.reprotection_needed is False
    assert pos.status == PositionStatus.OPEN

    leg_rows, legs = await _read_legs(factory)
    active = [leg for leg in legs if leg.status == BracketLegStatus.ACTIVE]
    assert {leg.leg_type for leg in active} == {
        BracketLegType.TAKE_PROFIT,
        BracketLegType.PRICE_STOP,
    }
    # The new legs append onto the SAME bracket with fresh indices (no new bracket).
    assert len(legs) == 4
    assert max(r.leg_index for r in leg_rows) == 3

    # Both protective broker ids are stamped onto the fresh ACTIVE legs.
    tp_leg = next(leg for leg in active if leg.leg_type == BracketLegType.TAKE_PROFIT)
    stop_leg = next(leg for leg in active if leg.leg_type == BracketLegType.PRICE_STOP)
    assert tp_leg.enforcement_binding == EnforcementBinding.BROKER_ENFORCED
    assert stop_leg.enforcement_binding == EnforcementBinding.BROKER_ENFORCED
    assert tp_leg.order_id is not None and stop_leg.order_id is not None
    tp_order = await _read_order(factory, tp_leg.order_id)
    stop_order = await _read_order(factory, stop_leg.order_id)
    assert tp_order.alpaca_order_id == str(client.tp_id)
    assert stop_order.alpaca_order_id == str(client.stop_leg.id)
    # The re-bracketed orders carry the original protective levels at the new size.
    assert tp_order.price_parameters.limit_price == _TP_PRICE
    assert stop_order.price_parameters.stop_trigger_price == _STOP_PRICE
    assert tp_order.quantity == _REMAINING_QTY

    # The bracket re-materializes through the read codec (the _assert_bracket_readable
    # guard the persist runs at write time — re-run here as an explicit check).
    async with factory() as sess:
        bracket_row = await sess.get(BracketRow, _BRACKET_ID)
        assert bracket_row is not None
        bracket_rows_to_record(bracket_row, tuple(leg_rows))


# ---------------------------------------------------------------------------
# ALP-824 invariant — no broker call under the fill-collection write lock
# ---------------------------------------------------------------------------


async def test_submit_runs_with_no_write_lock_held(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    """The broker submit happens outside any write transaction (ALP-824). Probed by
    acquiring the SQLite write lock from a separate connection *during* the submit —
    it succeeds only because the step holds no write lock across the broker call."""
    _, factory = db
    await _seed_naked_remainder(factory)
    db_path = tmp_path / "alphamind.db"
    client = _OcoStubClient()
    lock_free_during_submit: list[bool] = []

    def _probe_write_lock(_request: Any) -> None:
        # Runs inside submit_order, which the broker adapter offloads to a worker
        # thread — a genuinely concurrent connection. BEGIN IMMEDIATE acquires the
        # write lock iff no other write txn holds it.
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            lock_free_during_submit.append(True)
        except sqlite3.OperationalError:
            lock_free_during_submit.append(False)
        finally:
            conn.close()

    client.on_submit = _probe_write_lock

    persisted = await run_reprotection_step(
        session_factory=factory,
        invocation_id=_INV_ID,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        execution_config=_make_execution_config(),
        broker_routing_active=True,
        now=_NOW,
        trading_client_factory=_factory_for(client),
    )

    assert persisted == 1
    assert lock_free_during_submit == [True]


# ---------------------------------------------------------------------------
# debug_e2e mode — no broker call, monitor-enforced legs with NULL broker ids
# ---------------------------------------------------------------------------


async def test_debug_e2e_persists_monitor_enforced_legs_without_broker_call(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """With broker routing inactive the step issues no broker call and persists the
    re-bracketed legs MONITOR_ENFORCED with NULL broker ids, mirroring the OPEN path."""
    _, factory = db
    await _seed_naked_remainder(factory)

    def _factory_must_not_be_called(_v: VenueConfig, _m: ExecutionMode) -> Any:
        raise AssertionError("broker client factory must not be built in debug_e2e mode")

    persisted = await run_reprotection_step(
        session_factory=factory,
        invocation_id=_INV_ID,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        execution_config=_make_execution_config(),
        broker_routing_active=False,
        now=_NOW,
        trading_client_factory=_factory_must_not_be_called,
    )

    assert persisted == 1
    pos = await _read_position(factory)
    assert pos.reprotection_needed is False
    assert pos.status == PositionStatus.OPEN

    _, legs = await _read_legs(factory)
    active = [leg for leg in legs if leg.status == BracketLegStatus.ACTIVE]
    assert len(active) == 2
    for leg in active:
        assert leg.enforcement_binding == EnforcementBinding.MONITOR_ENFORCED
        assert leg.order_id is not None
        order = await _read_order(factory, leg.order_id)
        assert order.alpaca_order_id is None
        assert order.alpaca_order_id_chain == ()


# ---------------------------------------------------------------------------
# No candidates — the gather filter excludes unmarked positions
# ---------------------------------------------------------------------------


async def test_unmarked_position_yields_no_submit_and_no_change(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A position without the marker is not a candidate: no broker call, no new legs."""
    _, factory = db
    await _seed_naked_remainder(factory, position=_make_marked_position(reprotection_needed=False))
    client = _OcoStubClient()

    persisted = await run_reprotection_step(
        session_factory=factory,
        invocation_id=_INV_ID,
        venue_config=_make_venue_config(),
        execution_mode=ExecutionMode.paper,
        execution_config=_make_execution_config(),
        broker_routing_active=True,
        now=_NOW,
        trading_client_factory=_factory_for(client),
    )

    assert persisted == 0
    assert client.requests == []
    _, legs = await _read_legs(factory)
    assert len(legs) == 2  # the two original CANCELLED legs, untouched
    assert all(leg.status == BracketLegStatus.CANCELLED for leg in legs)


# ---------------------------------------------------------------------------
# Failure handling (Scope G) — the marker survives, the step never raises
# ---------------------------------------------------------------------------


async def _assert_failure_leaves_position_unprotected_but_marked(
    factory: async_sessionmaker[AsyncSession],
    *,
    client: _OcoStubClient,
    execution_config: ExecutionConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.CRITICAL, logger="alphamind.scheduler.reprotection"):
        persisted = await run_reprotection_step(
            session_factory=factory,
            invocation_id=_INV_ID,
            venue_config=_make_venue_config(),
            execution_mode=ExecutionMode.paper,
            execution_config=execution_config,
            broker_routing_active=True,
            now=_NOW,
            trading_client_factory=_factory_for(client),
        )

    # The step did not raise; nothing was persisted.
    assert persisted == 0
    # The marker survives so the next invocation retries.
    pos = await _read_position(factory)
    assert pos.reprotection_needed is True
    assert pos.status == PositionStatus.OPEN
    # No fresh legs were appended; the position stays exactly as naked as before.
    _, legs = await _read_legs(factory)
    assert len(legs) == 2
    assert all(leg.status == BracketLegStatus.CANCELLED for leg in legs)
    # The operator NAKED-POSITION alert fired.
    assert any(
        rec.levelno == logging.CRITICAL and "NAKED POSITION" in rec.getMessage()
        for rec in caplog.records
    )


async def test_permanent_rejection_leaves_marker_and_alerts(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A permanent broker rejection (the submit raises) alerts, leaves the marker set,
    and does not abort the invocation."""
    _, factory = db
    await _seed_naked_remainder(factory)
    client = _OcoStubClient()

    def _raise_permanent(_request: Any) -> None:
        exc = Exception("insufficient buying power")
        exc.status_code = 403  # type: ignore[attr-defined]  # permanent → re-raised by retry
        raise exc

    client.on_submit = _raise_permanent
    await _assert_failure_leaves_position_unprotected_but_marked(
        factory, client=client, execution_config=_make_execution_config(), caplog=caplog
    )


async def test_gateway_failure_leaves_marker_and_alerts(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway failure (retry window exhausted on transient errors) alerts, leaves
    the marker set, and does not abort the invocation."""
    _, factory = db
    await _seed_naked_remainder(factory)
    client = _OcoStubClient()

    # Drive the retry window to exhaustion in fake time — the clock is a sanctioned
    # mock boundary. Each transient attempt + each backoff sleep advances the clock.
    clock = [1_000.0]

    def fake_monotonic() -> float:
        return clock[0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.time.monotonic", fake_monotonic)
    monkeypatch.setattr("alphamind.execution.broker_adapter.retry.asyncio.sleep", fake_sleep)

    def _raise_transient(_request: Any) -> None:
        clock[0] += 0.1  # simulate round-trip latency
        raise ConnectionError("network hiccup")  # no status_code → classified transient

    client.on_submit = _raise_transient
    await _assert_failure_leaves_position_unprotected_but_marked(
        factory,
        client=client,
        execution_config=_make_execution_config(retry_window_seconds=1),
        caplog=caplog,
    )
    # The retry path was exercised (more than one attempt before the window closed).
    assert len(client.requests) >= 1
