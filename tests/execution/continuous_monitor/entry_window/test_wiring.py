"""Wiring tests for the entry-window watcher (ALP-737).

Covers the two pieces with real logic:

* :class:`SqlPendingEntryBracketReader` — the status + deadline filter, against
  a real on-disk SQLite DB seeded through the bracket codec.
* :func:`register_entry_window_watcher_task` — that it registers a task named
  ``entry_window`` on the supervisor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol
from alphamind._kernel.money import price
from alphamind.config.models.execution import (
    ExecutionConfig,
    FeeSchedule,
    GreeksRefresh,
    PaperHarness,
)
from alphamind.config.models.execution import OrderType as ExecOrderType
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
)
from alphamind.execution.continuous_monitor.entry_window.repricer import (
    BrokerReplaceClassification,
)
from alphamind.execution.continuous_monitor.entry_window.wiring import (
    AlpacaEntryCancel,
    AlpacaEntryReplace,
    SqlPendingEntryBracketReader,
    make_reprice_target_resolver,
    register_entry_window_watcher_task,
)
from alphamind.execution.continuous_monitor.session import new_session
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
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
from alphamind.state.tables.brackets_codec import record_to_rows as bracket_record_to_rows
from tests.state._fk_substrate import stub_order_row, stub_position_row

_DEADLINE = datetime(2026, 5, 29, 17, 30, tzinfo=UTC)


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401 — registers tables on Base.metadata

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def _bracket(
    *,
    bracket_id: str,
    status: BracketStatus,
    deadline: datetime | None,
) -> BracketRecord:
    leg_status = (
        BracketLegStatus.PENDING_ACTIVATION
        if status is BracketStatus.PENDING_ENTRY
        else BracketLegStatus.ACTIVE
    )
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol("ZS"), threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=leg_status,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(f"POS-{bracket_id}"),
        status=status,
        entry_order_id=OrderId(f"{bracket_id}-ord-entry"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=deadline,
    )


async def _seed(factory: async_sessionmaker[AsyncSession], bracket: BracketRecord) -> None:
    """Insert one bracket + legs plus its FK parents (position + orders).

    ``bracket_legs.bracket_id`` is a non-deferred FK, so the bracket parent is
    flushed before the legs are added; each leg's ``order_id`` also references
    ``orders`` so a stub order is seeded for it.
    """
    bracket_row, leg_rows = bracket_record_to_rows(bracket)
    async with factory() as session:
        session.add(stub_position_row(bracket.position_id, bracket_id=bracket.bracket_id))
        session.add(
            stub_order_row(
                bracket.entry_order_id, bracket.bracket_id, position_id=bracket.position_id
            )
        )
        for lrow in leg_rows:
            if lrow.order_id is not None:
                session.add(
                    stub_order_row(
                        lrow.order_id, bracket.bracket_id, position_id=bracket.position_id
                    )
                )
        session.add(bracket_row)
        await session.flush()
        for lrow in leg_rows:
            session.add(lrow)
        await session.commit()


async def test_reader_returns_only_pending_entry_brackets_with_a_deadline(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    returned = _bracket(
        bracket_id="BRK-RETURNED", status=BracketStatus.PENDING_ENTRY, deadline=_DEADLINE
    )
    no_deadline = _bracket(
        bracket_id="BRK-NODEADLINE", status=BracketStatus.PENDING_ENTRY, deadline=None
    )
    active = _bracket(bracket_id="BRK-ACTIVE", status=BracketStatus.ACTIVE, deadline=_DEADLINE)

    await _seed(factory, returned)
    await _seed(factory, no_deadline)
    await _seed(factory, active)

    brackets = await SqlPendingEntryBracketReader(factory).get_pending_entry_brackets()

    assert {b.bracket_id for b in brackets} == {"BRK-RETURNED"}
    assert brackets[0].entry_window_deadline == _DEADLINE


def _execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=15, move_trigger_pct=0.02),
        conservative_delta_buffer_pct=0.05,
        submission_retry_window_seconds=5,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.001,
            impact_coefficients={
                ExecOrderType.market: 0.001,
                ExecOrderType.limit: 0.0005,
                ExecOrderType.stop: 0.0015,
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


def test_register_entry_window_watcher_task_registers_named_task(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    supervisor = MonitorSupervisor(session=new_session(mode="live"), config=None)  # type: ignore[arg-type]

    register_entry_window_watcher_task(
        supervisor,
        session_factory=factory,
        client_factory=object(),  # never built — registration does not touch the broker
        execution_config=_execution_config(),
    )

    assert "entry_window" in supervisor.task_names()


class _FakeAPIError(Exception):
    """Minimal alpaca-py APIError surrogate carrying an HTTP status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _CancelClient:
    def __init__(self, *, raise_status: int | None = None) -> None:
        self._raise_status = raise_status
        self.calls: list[str] = []

    def cancel_order_by_id(self, order_id: str) -> None:
        self.calls.append(order_id)
        if self._raise_status is not None:
            raise _FakeAPIError(self._raise_status)


class _CancelClientFactory:
    def __init__(self, client: _CancelClient) -> None:
        self._client = client

    def build_trading_client(self) -> _CancelClient:
        return self._client


@pytest.mark.parametrize(
    ("raise_status", "expected"),
    [
        (None, BrokerCancelClassification.CANCEL_CONFIRMED),  # 2xx accept
        (404, BrokerCancelClassification.CANCEL_CONFIRMED),  # order gone
        (422, BrokerCancelClassification.CANCEL_CONFIRMED),  # not cancellable / filled
        (403, BrokerCancelClassification.RETRYABLE),  # auth — NOT terminal
        (429, BrokerCancelClassification.RETRYABLE),  # rate-limit — NOT terminal
        (400, BrokerCancelClassification.RETRYABLE),  # malformed — NOT terminal
    ],
)
async def test_alpaca_entry_cancel_classifies_broker_answers(
    raise_status: int | None, expected: BrokerCancelClassification
) -> None:
    """Only a confirmed cancel or a 404/422 is CANCEL_CONFIRMED; every other 4xx
    is RETRYABLE so a transient auth / rate-limit hiccup never abandons a still-
    resting entry past its deadline (ALP-737 review finding)."""
    canceller = AlpacaEntryCancel(
        client_factory=_CancelClientFactory(_CancelClient(raise_status=raise_status)),
        execution_config=_execution_config(),
    )
    assert await canceller(AlpacaOrderId("alpaca-uuid-xyz")) is expected


class _FakeOrder:
    """Minimal alpaca-py Order surrogate for a successful replace_order_by_id."""

    def __init__(self, order_id: str) -> None:
        self.id = order_id
        self.client_order_id = "client-xyz"
        self.status = "replaced"


class _ReplaceClient:
    def __init__(self, *, raise_status: int | None = None) -> None:
        self._raise_status = raise_status
        self.calls: list[str] = []

    def replace_order_by_id(self, order_id: str, order_data: object = None) -> _FakeOrder:
        del order_data
        self.calls.append(order_id)
        if self._raise_status is not None:
            raise _FakeAPIError(self._raise_status)
        return _FakeOrder("alpaca-new-uuid")


class _ReplaceClientFactory:
    def __init__(self, client: _ReplaceClient) -> None:
        self._client = client

    def build_trading_client(self) -> _ReplaceClient:
        return self._client


@pytest.mark.parametrize(
    ("raise_status", "expected"),
    [
        (None, BrokerReplaceClassification.REPLACED),  # 2xx accept
        (404, BrokerReplaceClassification.REJECTED),  # order gone — cannot escalate
        (422, BrokerReplaceClassification.REJECTED),  # validation / not replaceable
        (403, BrokerReplaceClassification.REJECTED),  # insufficient buying power
        (400, BrokerReplaceClassification.REJECTED),  # malformed
    ],
)
async def test_alpaca_entry_replace_classifies_broker_answers(
    raise_status: int | None, expected: BrokerReplaceClassification
) -> None:
    """A confirmed replace is REPLACED (with the new broker id); any classified
    4xx is REJECTED so the repricer falls back to the terminal cancel rather
    than chasing a doomed escalation (ALP-740 — guarantees loop termination)."""
    replace = AlpacaEntryReplace(
        client_factory=_ReplaceClientFactory(_ReplaceClient(raise_status=raise_status)),
        execution_config=_execution_config(),
    )
    result = await replace(AlpacaOrderId("alpaca-uuid-xyz"), price("99.95"))
    assert result.classification is expected
    if expected is BrokerReplaceClassification.REPLACED:
        assert result.new_alpaca_order_id == "alpaca-new-uuid"
    else:
        assert result.new_alpaca_order_id is None


async def test_make_reprice_target_resolver_projects_equity_limit_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The resolver decodes a resting equity LIMIT entry into the projection the
    repricer branches on: equity-limit flag, ticker, buy/sell side, the
    modification_count loop bound, and no recorded fills."""
    from alphamind.portfolio_state.records.orders import (
        EquityInstrumentSpec,
        OrderClass,
        OrderDirection,
        OrderDuration,
        OrderRecord,
        OrderRole,
        OrderStatus,
        OrderType,
        PriceParameters,
    )
    from alphamind.state.tables.orders_codec import (
        record_to_row as order_record_to_row,
    )

    _, factory = db
    bracket = _bracket(bracket_id="BRK-RP", status=BracketStatus.PENDING_ENTRY, deadline=_DEADLINE)
    entry = OrderRecord(
        order_id=OrderId("BRK-RP-ord-entry"),
        position_id=PositionId("POS-BRK-RP"),
        bracket_id=BracketId("BRK-RP"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("ZS")),
        direction=OrderDirection.SELL,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("100.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alpaca-real-uuid"),
        alpaca_order_id_chain=(AlpacaOrderId("alpaca-real-uuid"),),
        submission_timestamp=_DEADLINE,
        last_update_timestamp=_DEADLINE,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=1,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    bracket_row, leg_rows = bracket_record_to_rows(bracket)
    async with factory() as session:
        session.add(stub_position_row(bracket.position_id, bracket_id=bracket.bracket_id))
        session.add(order_record_to_row(entry))
        for lrow in leg_rows:
            if lrow.order_id is not None:
                session.add(
                    stub_order_row(
                        lrow.order_id, bracket.bracket_id, position_id=bracket.position_id
                    )
                )
        session.add(bracket_row)
        await session.flush()
        for lrow in leg_rows:
            session.add(lrow)
        await session.commit()

    target = await make_reprice_target_resolver(factory)("BRK-RP-ord-entry")

    assert target is not None
    assert target.is_equity_limit is True
    assert target.ticker == "ZS"
    assert target.side == "sell"
    assert target.modification_count == 1
    assert target.alpaca_order_id == "alpaca-real-uuid"
    assert target.has_recorded_fills is False


async def test_make_reprice_target_resolver_returns_none_for_missing_order(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    assert await make_reprice_target_resolver(factory)("does-not-exist") is None
