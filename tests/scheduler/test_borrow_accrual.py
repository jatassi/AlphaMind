"""Tests for the relocated scheduler-layer borrow accrual (ALP-855 / W4a).

ADR-0004 evicts daily borrow-cost accounting from the always-on monitor; ADR-0005
makes the pipeline the single writer. :func:`run_borrow_accrual` runs inside the
orchestrator's Phase-1 write transaction — reusing the invocation's open session
and ``invocation_id`` (no per-tick ``InvocationRow`` mint, no once-per-day timer).

Exercised against a real in-memory SQLite DB (the sanctioned DB boundary): seed
positions + OHLCV closes + the invocation/process-lifetime FK parents, run the
accrual inside one handle session, commit, and assert the position accumulator,
the activity-log row, and the once-per-trading-day idempotency guard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, make_symbol
from alphamind._kernel.money import money, price, signed_money
from alphamind.persistence.models import AssetUniverse, Base, OhlcvBars
from alphamind.persistence.session import make_async_engine, make_async_session_factory
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
from alphamind.scheduler.borrow_accrual import _already_accrued_today, run_borrow_accrual
from alphamind.state.invocation_context.activity_log import activity_log_entry_from_row
from alphamind.state.invocation_context.context import InvocationHandle
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
_INVOCATION_ID = "inv-1"
_PROCESS_LIFETIME_ID = "proc-1"


@pytest.fixture()
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
        process_role="pipeline",
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


def _invocation_row() -> InvocationRow:
    return InvocationRow(
        invocation_id=_INVOCATION_ID,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="market_open",
        trigger_reason="scheduled_run",
        git_sha_at_invocation="",
        active_profile="",
        active_regime="",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="",
        resolved_config_snapshot_path="",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _short_equity_record(
    *,
    position_id: str,
    ticker: str,
    share_count: float = 100.0,
    accrued: float = 0.0,
    avg_cost: float = 50.0,
) -> PositionRecord:
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


async def _seed_parents(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
        await sess.commit()
    async with factory() as sess:
        sess.add(_invocation_row())
        await sess.commit()
    # Every test in this module accrues the single ticker "ABCD".
    await _seed_universe(factory, ticker="ABCD")


async def _seed_position(factory: async_sessionmaker[AsyncSession], record: PositionRecord) -> None:
    async with factory() as sess:
        sess.add(position_record_to_row(record))
        await sess.commit()


async def _seed_default_short(factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed_position(factory, _short_equity_record(position_id="pos-1", ticker="ABCD"))


async def _seed_universe(factory: async_sessionmaker[AsyncSession], *, ticker: str) -> None:
    async with factory() as sess:
        sess.add(
            AssetUniverse(
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
        )
        await sess.commit()


async def _seed_ohlcv(
    factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str,
    close: float,
    period_start: str = "2026-05-27T00:00:00",
) -> None:
    async with factory() as sess:
        sess.add(_ohlcv_row(ticker=ticker, close=close, period_start=period_start))
        await sess.commit()


def _resolver(fees: dict[str, float | None]) -> object:
    return lambda ticker: fees.get(ticker)


async def _run_in_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    resolver: object,
    now: datetime = _NOW,
) -> None:
    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INVOCATION_ID)
        await run_borrow_accrual(handle, borrow_cost_resolver=resolver, now=now)  # type: ignore[arg-type]
        await session.commit()


class TestBorrowAccrualBooks:
    async def test_advances_short_equity_accumulator(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}))

        async with async_factory() as sess:
            row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
        details = position_row_to_record(row).details
        assert isinstance(details, EquityPositionDetails)
        # 100 shares * $50 = $5000 notional * 10%/yr / 252 ≈ $1.3699
        assert details.accrued_borrow_cost_usd is not None
        assert details.accrued_borrow_cost_usd == pytest.approx(5000.0 * 0.10 / 252.0)

    async def test_emits_borrow_cost_accrued_activity_row(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}))

        async with async_factory() as sess:
            rows = (await sess.execute(select(ActivityLogRow))).scalars().all()
        assert len(rows) == 1
        entry = activity_log_entry_from_row(rows[0])
        assert entry.event_type is EventType.BORROW_COST_ACCRUED
        assert entry.invocation_id == _INVOCATION_ID
        assert isinstance(entry.detail, BorrowCostAccruedDetail)
        assert entry.detail.accrual_date == date(2026, 5, 27)

    async def test_reuses_invocation_row_mints_no_new_one(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}))

        async with async_factory() as sess:
            invocation_ids = (
                (await sess.execute(select(InvocationRow.invocation_id))).scalars().all()
            )
        assert invocation_ids == [_INVOCATION_ID]


class TestUncoveredShortDoesNotAbort:
    """ALP-862: an OPEN SHORT whose ticker has no borrow rate accrues 0.0, not a rollback.

    The account-activities poll opens a SHORT equity leg from a forced assignment
    earlier in the same Phase-1 write unit; ``run_borrow_accrual`` runs right
    after, in the same transaction. If the ticker is uncovered (no
    ``borrow_cost_daily`` row), the accrual must accrue 0.0 rather than raise —
    raising would roll back the just-opened short, re-stranding the broker short.
    """

    async def test_uncovered_ticker_accrues_zero_and_does_not_raise(
        self, async_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)  # pos-1 / ABCD
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        # The resolver has NO rate for ABCD (uncovered ticker → resolver miss).
        with caplog.at_level("WARNING", logger="alphamind.scheduler.borrow_accrual"):
            await _run_in_handle(async_factory, resolver=_resolver({}))

        async with async_factory() as sess:
            row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
            details = position_row_to_record(row).details
            assert isinstance(details, EquityPositionDetails)
            # Accrued 0.0 — unchanged, no raise, no rollback of the short.
            assert details.accrued_borrow_cost_usd == pytest.approx(0.0)
            # A 0.0 BORROW_COST_ACCRUED entry is still booked (audit trail).
            rows = (await sess.execute(select(ActivityLogRow))).scalars().all()
            assert len(rows) == 1
        # The uncovered ticker is surfaced in a warning naming it.
        assert any("ABCD" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)


class TestOncePerTradingDayGuard:
    async def test_second_same_day_invocation_is_noop(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)

        # Same trading day, two invocations 4h apart.
        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}), now=_NOW)
        second = _NOW.replace(hour=23)  # later same ET day (19:00 ET)
        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}), now=second)

        async with async_factory() as sess:
            rows = (await sess.execute(select(ActivityLogRow))).scalars().all()
            pos_row = (
                await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
            ).scalar_one()
        # Exactly one accrual booked for the day; accumulator advanced once.
        assert len(rows) == 1
        details = position_row_to_record(pos_row).details
        assert isinstance(details, EquityPositionDetails)
        assert details.accrued_borrow_cost_usd == pytest.approx(5000.0 * 0.10 / 252.0)

    async def test_next_trading_day_books_again(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_parents(async_factory)
        await _seed_default_short(async_factory)
        await _seed_ohlcv(async_factory, ticker="ABCD", close=50.0)
        # A second day's bar so the kernel has a fresh close on day two.
        await _seed_ohlcv(
            async_factory, ticker="ABCD", close=50.0, period_start="2026-05-28T00:00:00"
        )

        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}), now=_NOW)
        next_day = datetime(2026, 5, 28, 20, 0, tzinfo=UTC)
        await _run_in_handle(async_factory, resolver=_resolver({"ABCD": 10.0}), now=next_day)

        async with async_factory() as sess:
            rows = (await sess.execute(select(ActivityLogRow))).scalars().all()
        assert len(rows) == 2
        details = [activity_log_entry_from_row(r).detail for r in rows]
        assert all(isinstance(d, BorrowCostAccruedDetail) for d in details)
        accrual_dates = sorted(
            d.accrual_date for d in details if isinstance(d, BorrowCostAccruedDetail)
        )
        assert accrual_dates == [date(2026, 5, 27), date(2026, 5, 28)]


class TestAlreadyAccruedTodayGuardRobustness:
    """BA2: verify the idempotency guard uses structured json_extract, not LIKE.

    The integration tests above cover the normal (Pydantic-compact) path.  This
    class tests the guard's raw SQL robustness: inserting a row with a
    whitespace-varied ``detail_json`` (``"accrual_date": "…"`` with a space
    after the colon) must still be detected.  This is impossible to exercise
    through ``run_borrow_accrual`` because Pydantic always emits compact JSON;
    we therefore call ``_already_accrued_today`` directly against a raw row.
    """

    async def test_guard_detects_standard_compact_serialization(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Guard returns True for a row whose detail_json uses compact JSON."""
        await _seed_parents(async_factory)
        async with async_factory() as sess:
            sess.add(
                ActivityLogRow(
                    entry_id="entry-compact",
                    invocation_id=_INVOCATION_ID,
                    entry_at="2026-05-27T20:00:00Z",
                    event_type=EventType.BORROW_COST_ACCRUED.value,
                    event_group="CASH_AND_MARGIN",
                    source="BORROW_ACCRUAL_MONITOR",
                    detail_json='{"accrued_amount_usd":1.37,"cumulative_accrued_usd":1.37,"annual_fee_pct_used":10.0,"notional_usd_used":5000.0,"accrual_date":"2026-05-27"}',
                )
            )
            await sess.commit()

        async with async_factory() as sess:
            result = await _already_accrued_today(sess, accrual_date_iso="2026-05-27")

        assert result is True

    async def test_guard_detects_spaced_serialization(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Guard returns True even when detail_json has a space after the colon.

        A LIKE-based guard (``'%"accrual_date":"…"%'``) would miss this row —
        the structural json_extract query must not.
        """
        await _seed_parents(async_factory)
        async with async_factory() as sess:
            sess.add(
                ActivityLogRow(
                    entry_id="entry-spaced",
                    invocation_id=_INVOCATION_ID,
                    entry_at="2026-05-27T20:00:00Z",
                    event_type=EventType.BORROW_COST_ACCRUED.value,
                    event_group="CASH_AND_MARGIN",
                    source="BORROW_ACCRUAL_MONITOR",
                    # Deliberately spaced: "accrual_date": "2026-05-27" (space after colon)
                    detail_json=(
                        '{"accrued_amount_usd": 1.37, "cumulative_accrued_usd": 1.37,'
                        ' "annual_fee_pct_used": 10.0, "notional_usd_used": 5000.0,'
                        ' "accrual_date": "2026-05-27"}'
                    ),
                )
            )
            await sess.commit()

        async with async_factory() as sess:
            result = await _already_accrued_today(sess, accrual_date_iso="2026-05-27")

        assert result is True

    async def test_guard_returns_false_when_no_matching_row(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Guard returns False when no BORROW_COST_ACCRUED row exists for the date."""
        await _seed_parents(async_factory)

        async with async_factory() as sess:
            result = await _already_accrued_today(sess, accrual_date_iso="2026-05-27")

        assert result is False
