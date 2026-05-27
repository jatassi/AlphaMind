"""Tests for the borrow-accrual supervisor wiring (ALP-719).

Three contracts:

1. :func:`register_borrow_accrual_task` adds one task named
   ``borrow_accrual`` to the supervisor.
2. The wired resolver factory builds a fresh
   :class:`alphamind.risk_guardrails.borrow_cost.BorrowCostResolver`
   instance from a sync session each call — so a per-tick resolver
   reflects the latest ``borrow_cost_daily`` rows.
3. The wired calendar adapter consults
   :class:`alphamind.execution.venue_configuration.calendar_cache.TradingCalendarCache`
   correctly: a date is a trading day iff a representative noon-ET
   datetime on that date returns ``is_market_open=True``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.borrow_accrual.wiring import (
    _calendar_adapter,
    _resolver_factory,
    register_borrow_accrual_task,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.persistence.models import AssetUniverse, Base, BorrowCostDaily
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)


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


@pytest.fixture()
async def sync_factory(
    async_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[sessionmaker[Session]]:
    del async_factory  # ordering dependency only
    sync_engine = make_engine(str(tmp_path / "alphamind.db"))
    try:
        yield make_session_factory(sync_engine)
    finally:
        sync_engine.dispose()


def _monitor_config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _monitor_session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-test",
        started_at=datetime(2026, 5, 27, 13, 30, tzinfo=UTC),
        mode="paper",
    )


class TestRegisterBorrowAccrualTask:
    async def test_registers_task_named_borrow_accrual(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        sync_factory: sessionmaker[Session],
    ) -> None:
        supervisor = MonitorSupervisor(
            session=_monitor_session(), config=_monitor_config()
        )

        class _StubCalendar:
            def is_market_open(self, _ts: datetime) -> bool:
                return True

        register_borrow_accrual_task(
            supervisor,
            session_factory=async_factory,
            sync_session_factory=sync_factory,
            calendar_cache=_StubCalendar(),
            process_lifetime_id="proc-1",
        )

        assert supervisor.task_names() == ("borrow_accrual",)


def _asset_universe(ticker: str) -> AssetUniverse:
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


class TestResolverFactory:
    async def test_returns_callable_over_live_borrow_cost_daily(
        self, sync_factory: sessionmaker[Session]
    ) -> None:
        # Seed ``borrow_cost_daily`` with one row.
        with sync_factory() as sess:
            sess.add(_asset_universe("ABCD"))
            sess.add(
                BorrowCostDaily(
                    ticker="ABCD",
                    observation_date="2026-05-27",
                    fee_pct=12.5,
                    source="test",
                    ingested_at=datetime(2026, 5, 27, 20, 0, tzinfo=UTC).isoformat(),
                )
            )
            sess.commit()

        factory = _resolver_factory(sync_factory)
        resolver = factory()
        assert resolver("ABCD") == 12.5
        assert resolver("WXYZ") is None

    async def test_each_factory_call_reads_fresh_state(
        self, sync_factory: sessionmaker[Session]
    ) -> None:
        """A new ``borrow_cost_daily`` row between two factory calls shows up."""
        factory = _resolver_factory(sync_factory)

        first = factory()
        assert first("ABCD") is None  # no row yet

        with sync_factory() as sess:
            sess.add(_asset_universe("ABCD"))
            sess.add(
                BorrowCostDaily(
                    ticker="ABCD",
                    observation_date="2026-05-27",
                    fee_pct=15.0,
                    source="test",
                    ingested_at=datetime(2026, 5, 27, 20, 0, tzinfo=UTC).isoformat(),
                )
            )
            sess.commit()

        second = factory()
        assert second("ABCD") == 15.0


class TestCalendarAdapter:
    def test_consults_is_market_open_at_noon_eastern(self) -> None:
        """Adapter probes ``is_market_open`` at 12:00 ET on the given date.

        12:00 ET avoids both the pre-open and after-hours windows so the
        predicate cleanly answers "is this a trading day".
        """
        captured: list[datetime] = []

        class _RecordingCache:
            def is_market_open(self, ts: datetime) -> bool:
                captured.append(ts)
                # Weekdays only.
                return ts.weekday() < 5

        adapter = _calendar_adapter(_RecordingCache())

        # Tuesday 2026-05-26 should be a trading day per the stub.
        assert adapter.is_trading_day(date(2026, 5, 26)) is True
        # Saturday 2026-05-30 should not.
        assert adapter.is_trading_day(date(2026, 5, 30)) is False
        assert len(captured) == 2
        for ts in captured:
            # Each probe sat at 12:00 in some timezone with a non-None tz.
            assert ts.tzinfo is not None
            assert ts.hour == 12
