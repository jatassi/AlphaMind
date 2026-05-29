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

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol
from alphamind.config.models.execution import (
    ExecutionConfig,
    FeeSchedule,
    GreeksRefresh,
    PaperHarness,
)
from alphamind.config.models.execution import OrderType as ExecOrderType
from alphamind.execution.continuous_monitor.entry_window.wiring import (
    SqlPendingEntryBracketReader,
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
