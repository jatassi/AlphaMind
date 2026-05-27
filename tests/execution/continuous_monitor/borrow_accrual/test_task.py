"""Shell-side tests for the borrow-accrual tick (ALP-719).

Exercises :func:`run_accrual_tick` against a real in-memory SQLite
database. Per project policy (and the parent issue's pre-resolved
decision on test substrates) we do not mock the database — the kernel +
shell co-operate atomically against an actual transaction.

The five-step recipe under one transaction:

1. Insert the per-tick ``InvocationRow`` (``trigger_type="scheduled"``,
   ``trigger_source="borrow_accrual"``).
2. Read every ``status=OPEN``, ``direction=SHORT``, ``instrument_type=EQUITY``
   position via the ``positions`` table.
3. For each in-scope ticker, read the latest closing print from
   ``ohlcv_bars`` (the canonical equity-bars table — the design doc's
   *equity_bars* shorthand maps here) and the live annualized fee via
   the injected ``borrow_cost_resolver``.
4. Call :func:`compute_tick` to produce the per-position updated records
   + activity-log entries.
5. UPDATE every position's row and INSERT every activity-log entry,
   commit once.

A miss on any closing print or any fee rate raises
:class:`ValueError`; the surrounding transaction rolls back so no
position accumulator and no activity-log entry persists.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, make_symbol
from alphamind._kernel.money import money, price, signed_money
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.borrow_accrual.task import (
    _next_tick_utc,
    run_accrual_tick,
    run_borrow_accrual_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.persistence.models import AssetUniverse, Base, OhlcvBars
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
)
from alphamind.portfolio_state.events.activity_log import (
    BorrowCostAccruedDetail,
    EventType,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
)
from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

_NOW = datetime(2026, 5, 27, 20, 0, tzinfo=UTC)  # 16:00 ET in summer
_PROCESS_LIFETIME_ID = "proc-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def async_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    engine = make_async_engine(str(db_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        process_role="monitor",
        process_start_at=datetime(2026, 5, 27, 13, 30, tzinfo=UTC).isoformat(),
        process_pid=12345,
        hostname="host",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Darwin-25.4.0",
    )


def _short_equity_record(
    *,
    position_id: str,
    ticker: str,
    share_count: float = 100.0,
    accrued: float = 0.0,
    avg_cost: float = 50.0,
) -> PositionRecord:
    # ``thesis_id`` left None so the test fixtures need not seed the
    # ``theses`` table — the borrow-accrual tick does not consult thesis state.
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.SHORT,
        entry_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        details=EquityPositionDetails(
            ticker=make_symbol(ticker),
            share_count=share_count,
            average_cost_basis_per_share=avg_cost,
            borrow_rate_pct=10.0,
            accrued_borrow_cost_usd=accrued,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=share_count * avg_cost * 0.5,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                fill_price=price(avg_cost),
                fill_quantity=share_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _long_equity_record(*, position_id: str, ticker: str) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
        details=EquityPositionDetails(
            ticker=make_symbol(ticker),
            share_count=100.0,
            average_cost_basis_per_share=50.0,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
                fill_price=price(50.0),
                fill_quantity=100.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _asset_universe_row(ticker: str) -> AssetUniverse:
    return AssetUniverse(
        asset_id=f"asset-{ticker}",
        ticker=ticker,
        full_name=ticker,
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2026-01-01",
        last_updated="2026-05-27",
    )


def _ohlcv_row(*, ticker: str, close: float, period_start: str) -> OhlcvBars:
    return OhlcvBars(
        ticker=ticker,
        timeframe="1d",
        period_start=period_start,
        period_end=period_start,
        session="regular",
        adj_open=close,
        adj_high=close,
        adj_low=close,
        adj_close=close,
        adj_volume=1_000_000,
        unadj_open=close,
        unadj_high=close,
        unadj_low=close,
        unadj_close=close,
        unadj_volume=1_000_000,
        source="polygon",
        ingested_at=datetime(2026, 5, 27, 20, 5, tzinfo=UTC).isoformat(),
    )


async def _seed_position(
    async_factory: async_sessionmaker[AsyncSession], record: PositionRecord
) -> None:
    async with async_factory() as sess:
        sess.add(position_record_to_row(record))
        await sess.commit()


async def _seed_process_lifetime(
    async_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
        await sess.commit()


async def _seed_ohlcv(
    async_factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str,
    close: float,
    period_start: str = "2026-05-27T00:00:00",
) -> None:
    async with async_factory() as sess:
        sess.add(_asset_universe_row(ticker))
        sess.add(_ohlcv_row(ticker=ticker, close=close, period_start=period_start))
        await sess.commit()


def _stub_resolver_factory(
    fees: dict[str, float | None],
) -> Any:
    """Build a stub ``borrow_cost_resolver_factory`` returning per-call lookup.

    The factory contract mirrors :func:`build_borrow_cost_resolver` — a
    sync callable that returns ``Callable[[str], float | None]``.
    """

    def _factory() -> Any:
        return lambda ticker: fees.get(ticker)

    return _factory


# ---------------------------------------------------------------------------
# Happy-path tick
# ---------------------------------------------------------------------------


class TestHappyPathTick:
    async def test_advances_one_position_accumulator(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="ABCD")
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await run_accrual_tick(
            session_factory=async_factory,
            borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0}),
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            now=_NOW,
        )

        async with async_factory() as sess:
            row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
            record = position_row_to_record(row)
        assert isinstance(record.details, EquityPositionDetails)
        expected = 100 * 50 * 10 / 100 / 252
        assert record.details.accrued_borrow_cost_usd == pytest.approx(expected)

    async def test_writes_invocation_row_with_borrow_accrual_trigger_source(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="ABCD")
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await run_accrual_tick(
            session_factory=async_factory,
            borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0}),
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            now=_NOW,
        )

        async with async_factory() as sess:
            rows = (await sess.execute(select(InvocationRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.trigger_type == "scheduled"
        assert row.trigger_source == "borrow_accrual"
        assert row.process_lifetime_id == _PROCESS_LIFETIME_ID

    async def test_emits_activity_log_entry_with_full_payload(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory,
            _short_equity_record(position_id="pos-1", ticker="ABCD", accrued=5.0),
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await run_accrual_tick(
            session_factory=async_factory,
            borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0}),
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            now=_NOW,
        )

        async with async_factory() as sess:
            row = (await sess.execute(select(ActivityLogRow))).scalar_one()
            entry = activity_log_entry_from_row(row)
        assert entry.event_type == EventType.BORROW_COST_ACCRUED
        assert entry.position_id == "pos-1"
        assert isinstance(entry.detail, BorrowCostAccruedDetail)
        expected_today = 100 * 50 * 10 / 100 / 252
        assert entry.detail.accrued_amount_usd == money(expected_today)
        assert entry.detail.cumulative_accrued_usd == money(5.0 + expected_today)
        assert entry.detail.annual_fee_pct_used == 10.0


# ---------------------------------------------------------------------------
# Filtering: LONG / intraday-closed positions skipped
# ---------------------------------------------------------------------------


class TestFiltering:
    async def test_long_equity_excluded(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(async_factory, _long_equity_record(position_id="pos-1", ticker="ABCD"))
        # No ohlcv row needed — the LONG position must be skipped.

        await run_accrual_tick(
            session_factory=async_factory,
            borrow_cost_resolver_factory=_stub_resolver_factory({}),
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            now=_NOW,
        )

        async with async_factory() as sess:
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
        assert entries == []

    async def test_intraday_closed_position_excluded(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A SHORT position that closed intraday (status CLOSED) is skipped."""
        await _seed_process_lifetime(async_factory)
        base = _short_equity_record(position_id="pos-1", ticker="ABCD")
        closed = dataclasses.replace(
            base, status=PositionStatus.CLOSED, realized_pnl_to_date_usd=-12.34
        )
        await _seed_position(async_factory, closed)
        # No ohlcv row needed.

        await run_accrual_tick(
            session_factory=async_factory,
            borrow_cost_resolver_factory=_stub_resolver_factory({}),
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            now=_NOW,
        )

        async with async_factory() as sess:
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            position_row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
        assert entries == []
        record = position_row_to_record(position_row)
        assert isinstance(record.details, EquityPositionDetails)
        # CLOSED short still carries the accumulator; it must be untouched.
        assert record.details.accrued_borrow_cost_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Atomicity: any failure rolls back the whole tick
# ---------------------------------------------------------------------------


class TestRollbackOnFailure:
    async def test_resolver_miss_rolls_back_other_position(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Two OPEN SHORTs; resolver maps ABCD → 10.0 and EFGH → None.

        The whole tick raises and rolls back: neither accumulator advances,
        no InvocationRow lands, no activity-log entries land.
        """
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="ABCD")
        )
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-2", ticker="EFGH")
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)
        await _seed_ohlcv(async_factory, ticker="EFGH", close=50.0)

        with pytest.raises(ValueError, match="EFGH"):
            await run_accrual_tick(
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0, "EFGH": None}),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                now=_NOW,
            )

        async with async_factory() as sess:
            invocations = (await sess.execute(select(InvocationRow))).scalars().all()
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            position_rows = (await sess.execute(select(PositionRow))).scalars().all()
        assert invocations == []
        assert entries == []
        for row in position_rows:
            record = position_row_to_record(row)
            assert isinstance(record.details, EquityPositionDetails)
            assert record.details.accrued_borrow_cost_usd == pytest.approx(0.0)

    async def test_missing_close_print_rolls_back(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="WXYZ")
        )
        # NO ohlcv row for WXYZ -> close lookup returns None.

        with pytest.raises(ValueError, match="WXYZ"):
            await run_accrual_tick(
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory({"WXYZ": 10.0}),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                now=_NOW,
            )

        async with async_factory() as sess:
            invocations = (await sess.execute(select(InvocationRow))).scalars().all()
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            position_row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
        assert invocations == []
        assert entries == []
        record = position_row_to_record(position_row)
        assert isinstance(record.details, EquityPositionDetails)
        assert record.details.accrued_borrow_cost_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Multi-position commits atomically in one transaction
# ---------------------------------------------------------------------------


class TestEmitFailureAtomicity:
    """If activity-log INSERT fails mid-tick, the whole tick rolls back.

    Simulated by injecting a duplicate ``entry_id`` collision via the
    primary-key constraint — the second activity_log row INSERT raises
    :class:`IntegrityError` at flush time, which propagates out of the
    session context and rolls back the whole transaction.
    """

    async def test_pre_existing_entry_id_collision_rolls_back_whole_tick(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="ABCD")
        )
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-2", ticker="EFGH")
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)
        await _seed_ohlcv(async_factory, ticker="EFGH", close=50.0)

        # Monkey-patch ``secrets.token_hex`` as seen by the recompute module
        # so both per-position entries mint the same entry_id — the second
        # INSERT collides with the first's primary key.
        monkeypatch.setattr(
            "alphamind.execution.continuous_monitor.borrow_accrual.recompute.secrets.token_hex",
            lambda _n: "deadbeef",
        )

        with pytest.raises(IntegrityError):
            await run_accrual_tick(
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0, "EFGH": 10.0}),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                now=_NOW,
            )

        async with async_factory() as sess:
            invocations = (await sess.execute(select(InvocationRow))).scalars().all()
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            position_rows = (await sess.execute(select(PositionRow))).scalars().all()
        assert invocations == []
        assert entries == []
        for row in position_rows:
            record = position_row_to_record(row)
            assert isinstance(record.details, EquityPositionDetails)
            assert record.details.accrued_borrow_cost_usd == pytest.approx(0.0)


class TestMultiPositionOneTransaction:
    async def test_three_positions_one_commit(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_process_lifetime(async_factory)
        for pid, ticker in [("pos-1", "ABCD"), ("pos-2", "EFGH"), ("pos-3", "IJKL")]:
            await _seed_position(
                async_factory, _short_equity_record(position_id=pid, ticker=ticker)
            )
            await _seed_ohlcv(async_factory, ticker=ticker, close=50.0)

        # Track commits by listening to the engine's commit event.
        engine = async_factory.kw["bind"]
        sync_engine = engine.sync_engine
        commit_count = [0]

        def _on_commit(conn: Any) -> None:
            commit_count[0] += 1

        event.listen(sync_engine, "commit", _on_commit)
        try:
            await run_accrual_tick(
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory(
                    {"ABCD": 10.0, "EFGH": 10.0, "IJKL": 10.0}
                ),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                now=_NOW,
            )
        finally:
            event.remove(sync_engine, "commit", _on_commit)

        # Exactly one commit fired by the tick (seeding commits happened
        # before the listener was attached).
        assert commit_count[0] == 1
        async with async_factory() as sess:
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            invocations = (await sess.execute(select(InvocationRow))).scalars().all()
        assert len(entries) == 3
        assert len(invocations) == 1


# ---------------------------------------------------------------------------
# _next_tick_utc — pure date arithmetic with calendar predicate
# ---------------------------------------------------------------------------


class _AlwaysOpenCalendar:
    """Stub :class:`TradingCalendar` that treats every day as a trading day."""

    def is_trading_day(self, day: date) -> bool:
        return True


class _WeekdaysOnlyCalendar:
    """Stub :class:`TradingCalendar` that skips Saturday + Sunday."""

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5  # Mon=0 .. Fri=4


class TestNextTickUtc:
    """Pure scheduler helper — picks the next fire wall clock in UTC."""

    def test_same_day_future_tick(self) -> None:
        """If today's local tick is still in the future, fire today."""
        # 13:00 ET on a Wednesday; tick at 16:00 ET → fires same day.
        current = datetime(2026, 5, 27, 17, 0, tzinfo=UTC)  # 13:00 ET (summer)
        result = _next_tick_utc(
            current=current,
            tick_local_time=time(16, 0),
            calendar=_AlwaysOpenCalendar(),
        )
        assert result == datetime(2026, 5, 27, 20, 0, tzinfo=UTC)  # 16:00 ET

    def test_post_tick_advances_to_tomorrow(self) -> None:
        """If today's local tick has passed, fire tomorrow."""
        current = datetime(2026, 5, 27, 21, 0, tzinfo=UTC)  # 17:00 ET (post-tick)
        result = _next_tick_utc(
            current=current,
            tick_local_time=time(16, 0),
            calendar=_AlwaysOpenCalendar(),
        )
        assert result == datetime(2026, 5, 28, 20, 0, tzinfo=UTC)

    def test_weekend_skips_to_monday(self) -> None:
        """Friday post-tick → Monday's tick (skips Sat + Sun)."""
        # Friday 2026-05-29 21:00 UTC = 17:00 ET. Tick at 16:00 ET fired
        # already, so the next candidate is Sat → advance to Monday.
        current = datetime(2026, 5, 29, 21, 0, tzinfo=UTC)
        result = _next_tick_utc(
            current=current,
            tick_local_time=time(16, 0),
            calendar=_WeekdaysOnlyCalendar(),
        )
        assert result == datetime(2026, 6, 1, 20, 0, tzinfo=UTC)  # Monday 16:00 ET


# ---------------------------------------------------------------------------
# run_borrow_accrual_loop — sleeps to next tick, fires, repeats; respects
# CancelledError as the supervisor's shutdown signal
# ---------------------------------------------------------------------------


class TestRunBorrowAccrualLoop:
    async def test_respects_cancellation_at_initial_sleep(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A ``CancelledError`` raised inside ``sleep`` aborts the loop.

        Mirrors the supervisor shutdown path: ``MonitorSupervisor.run``
        cancels each task on stop. The sleep callable is the supervisor's
        cooperative-yield point; raising ``CancelledError`` there is the
        canonical shape ``asyncio.sleep`` produces under cancellation.
        """
        await _seed_process_lifetime(async_factory)
        config = _default_monitor_config()
        monitor_session = MonitorSession(
            session_id="mon-test",
            started_at=datetime(2026, 5, 27, 13, 30, tzinfo=UTC),
            mode="paper",
        )

        async def _sleep_immediately_cancels(_delay: float) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_borrow_accrual_loop(
                monitor_session,
                config,
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory({}),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                calendar=_AlwaysOpenCalendar(),
                now=lambda: datetime(2026, 5, 27, 17, 0, tzinfo=UTC),
                sleep=_sleep_immediately_cancels,
            )

    async def test_fires_one_tick_then_loops(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The loop sleeps, fires :func:`run_accrual_tick`, sleeps again.

        We let the first sleep return immediately, observe one tick fired
        against the DB, then cancel on the second sleep.
        """
        await _seed_process_lifetime(async_factory)
        await _seed_position(
            async_factory, _short_equity_record(position_id="pos-1", ticker="ABCD")
        )
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        config = _default_monitor_config()
        monitor_session = MonitorSession(
            session_id="mon-test",
            started_at=datetime(2026, 5, 27, 13, 30, tzinfo=UTC),
            mode="paper",
        )

        call_count = [0]

        async def _sleep(_delay: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                # Second sleep: end the loop.
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_borrow_accrual_loop(
                monitor_session,
                config,
                session_factory=async_factory,
                borrow_cost_resolver_factory=_stub_resolver_factory({"ABCD": 10.0}),
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                calendar=_AlwaysOpenCalendar(),
                now=lambda: datetime(2026, 5, 27, 17, 0, tzinfo=UTC),
                sleep=_sleep,
            )

        async with async_factory() as sess:
            entries = (await sess.execute(select(ActivityLogRow))).scalars().all()
            invocations = (await sess.execute(select(InvocationRow))).scalars().all()
        assert len(entries) == 1
        assert len(invocations) == 1


def _default_monitor_config() -> ContinuousMonitorConfig:
    """A minimal valid :class:`ContinuousMonitorConfig` for the loop tests."""
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )
