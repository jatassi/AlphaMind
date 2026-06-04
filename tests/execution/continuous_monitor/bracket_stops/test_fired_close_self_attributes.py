"""A monitor-enforced leg fires a fresh self-attributing close (story 03e / ALP-853).

When a Monitor-enforced leg fires, the closer submits a fresh close whose
``client_order_id`` carries the broker-carried link (the engine-form id with the
originating thesis *why* + invocation *when*, ALP-844). The resulting close fill
then **self-attributes** end-to-end through the 02a fill→event-log path with **no
local ``orders`` row required** (ADR-0002) — and never via a synthetic id or an
engine-envelope detour (invariants 2 & 5).

This crosses the seam the story owns: the id the monitor *mints on fire* is the
id a returning fill *self-attributes through*. The closer runs for real; the fill
is then fed through the real :func:`persist_fill_report` against an on-disk SQLite
schema (the DB is a sanctioned mock boundary; here it is real).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.models import Order, TradeUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    BracketId,
    InvocationId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    CloseSubmissionResult,
    submit_options_bracket_close,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.oms.command_ids import is_engine_originated
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from tests.state._fk_substrate import (
    seed_position_cluster,
    stub_invocation_row,
    stub_process_lifetime_row,
)

# The thesis the fired leg's position carries — the *why* woven into the close's
# client_order_id. A valid THE-<ticker>-<32hex> so derive_engine_command_id accepts it.
_THESIS_ID = "THE-NVDA-0123456789abcdef0123456789abcdef"
_INVOCATION_BARE = "20260511T143000Z-aabbccdd"
_MONITOR_SESSION_ID = "mon-20260511T143000Z-aabbccdd"
_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _const_str(value: str):  # type: ignore[no-untyped-def]
    async def _inner() -> str:
        return value

    return _inner


@dataclass
class _LinkCapturingSubmitter:
    """Captures the link-carrying ``client_order_id`` the closer threads.

    Stands in for the broker-call surface (a sanctioned boundary): the closer
    builds the engine-form id and hands it here; the real fill that returns
    echoes exactly this id back.
    """

    captured_client_order_id: str | None = None

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        del position, details, trigger_reason
        self.captured_client_order_id = client_order_id
        return CloseSubmissionResult(order_ids=(client_order_id,), mode="single_leg")

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
        trigger_reason: PositionExitMethod,
    ) -> CloseSubmissionResult:
        # Unused: this end-to-end test exercises the single-leg options path.
        raise NotImplementedError


@dataclass
class _CollectingActivityLog:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


def _options_position() -> PositionRecord:
    return PositionRecord(
        position_id=PositionId("pos-1"),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=BracketId("bracket-1"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=850.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=12.0,
            greeks=OptionGreeks(
                delta=0.5, gamma=0.02, theta=-0.04, vega=0.2, as_of_timestamp=_NOW, iv_used=0.30
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(12.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=865.0, direction="LTE"
        ),
        # A Monitor-enforced leg: armed Intent, no broker order — its fire submits
        # a fresh self-attributing close (CONTEXT.md / ADR-0003).
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId("bracket-1"),
        position_id=PositionId("pos-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("order-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _fill_report_for(client_order_id: str) -> FillReport:
    """Translate a broker fill that echoes *client_order_id* back (the close fill)."""
    order = Order(
        id=uuid4(),
        client_order_id=client_order_id,
        created_at=_now_utc(),
        updated_at=_now_utc(),
        submitted_at=_now_utc(),
        symbol="NVDA250619C00850000",
        asset_class=AssetClass.US_OPTION,
        order_class=OrderClass.SIMPLE,
        order_type=OrderType.MARKET,
        type=OrderType.MARKET,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        status=AlpacaOrderStatus.FILLED,
        extended_hours=False,
        qty="1",
        filled_qty="1",
    )
    reports = translate_trade_update(
        TradeUpdate(event="fill", order=order, timestamp=_now_utc(), price=9.5, qty=1.0)
    )
    assert len(reports) == 1
    return reports[0]


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite seeded with the link's thesis/position/invocation FK targets.

    Deliberately seeds NO close ``orders`` row — the close fill must attribute via
    the broker-carried link alone (ADR-0002), proving the no-order-row guarantee.
    The thesis id matches the one the closer weaves into the close's id.
    """
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        seed_position_cluster(
            sess,
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId(_THESIS_ID),
            bracket_id=BracketId("bracket-1"),
            entry_order_id=OrderId("order-1"),
        )
        sess.add(stub_process_lifetime_row())
        sess.flush()
        sess.add(stub_invocation_row(InvocationId(f"inv-{_INVOCATION_BARE}")))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


async def _read_event_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[BrokerEventLogRow]:
    async with session_factory() as session:
        return list((await session.execute(select(BrokerEventLogRow))).scalars().all())


class TestFiredLegCloseSelfAttributes:
    async def test_fired_close_fill_self_attributes_with_no_order_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The fresh close a fired Monitor-enforced leg submits self-attributes
        end-to-end: its returning fill resolves to thesis / invocation / position
        via the broker-carried link, with NO close ``orders`` row, and never
        reaches the out-of-band quarantine (ACs 1 & 2; invariant 2)."""
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            list_unattributed_fills,
        )

        submitter = _LinkCapturingSubmitter()
        activity_log = _CollectingActivityLog()

        # The Monitor-enforced leg fires: the closer submits a fresh close.
        await submit_options_bracket_close(
            position=_options_position(),
            bracket=_bracket(),
            trigger_reason=PositionExitMethod.STOP_TRIGGERED,
            submitter=submitter,
            activity_log=activity_log.emit,
            invocation_id_provider=_const_str(f"inv-{_INVOCATION_BARE}"),
            monitor_session_id=_MONITOR_SESSION_ID,
            trigger_id=7,
            now=_NOW,
            estimated_exit_price=9.5,
            realized_pnl_usd=-250.0,
        )

        close_client_order_id = submitter.captured_client_order_id
        assert close_client_order_id is not None
        # AC 1 — the fresh close carries the engine-form broker-carried link
        # (no synthetic ``alp-…`` id; invariant 5).
        assert is_engine_originated(close_client_order_id)
        assert not close_client_order_id.startswith("alp-")

        # The broker fills the fresh close, echoing the same client_order_id back.
        report = _fill_report_for(close_client_order_id)
        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # AC 2 — the close fill self-attributes via the link with no order row.
        rows = await _read_event_log(session_factory)
        assert len(rows) == 1
        (row,) = rows
        assert row.event_type == "FILL"
        assert row.thesis_id == _THESIS_ID
        assert row.invocation_id == f"inv-{_INVOCATION_BARE}"
        assert row.position_id == "pos-1"
        # Invariant 2 — never reaches the out-of-band quarantine.
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []
