"""Tests for the greeks-refresh wiring providers (ALP-123 follow-up).

The /review on PR #48 identified a deadlock-fall-through in
``make_invocation_id_provider`` and ``make_risk_free_rate_provider`` (story 03a
wiring): both used ``asyncio.run()`` inside synchronous callables and caught the
resulting ``RuntimeError: cannot be called from a running event loop`` by
returning a hard-coded sentinel. Every production call returned the fallback —
``"monitor-bootstrap"`` (FK violation) and ``0.045`` (DTB3 series ignored).

These tests cover the post-fix async providers:

* ``make_invocation_id_provider`` returns the latest InvocationRow.invocation_id
  (and ``"monitor-bootstrap"`` on an empty DB).
* ``make_risk_free_rate_provider`` returns the latest DTB3 value / 100 (and the
  ``_DEFAULT_RISK_FREE_RATE`` fallback on an empty DB). Subsequent calls within
  the TTL window reuse the cached value without re-querying the DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.continuous_monitor.greeks_refresh.wiring import (
    make_invocation_id_provider,
    make_risk_free_rate_provider,
)
from alphamind.persistence.models import Base, MacroObservations
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.state.invocation_context import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow


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


def _process_lifetime_record(*, process_lifetime_id: str = "proc-1") -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=process_lifetime_id,
        process_role="monitor",
        process_start_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC).isoformat(),
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


def _dtb3(observation_date: date, value: float, *, revision: int = 1) -> MacroObservations:
    """One DTB3 observation, fully-populated to satisfy the composite primary key."""
    return MacroObservations(
        source="FRED",
        series_id="DTB3",
        observation_date=observation_date.isoformat(),
        revision_number=revision,
        release_date=observation_date.isoformat(),
        value=value,
        units="percent",
        frequency="daily",
        ingested_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC).isoformat(),
    )


def _invocation_row(*, invocation_id: str, start_at: datetime) -> InvocationRow:
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at=start_at.isoformat(),
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/calibration.json",
        data_source_freshness_json="{}",
    )


class TestMakeInvocationIdProvider:
    """Async provider — DB hit per call, fallback only on empty DB."""

    async def test_empty_db_returns_bootstrap_sentinel(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        provider = make_invocation_id_provider(async_factory)
        result = await provider()
        assert result == "monitor-bootstrap"

    async def test_returns_latest_invocation_id(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as sess:
            sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
            await sess.commit()
        async with async_factory() as sess:
            sess.add(
                _invocation_row(
                    invocation_id="inv-old",
                    start_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                )
            )
            sess.add(
                _invocation_row(
                    invocation_id="inv-latest",
                    start_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
                )
            )
            await sess.commit()

        provider = make_invocation_id_provider(async_factory)
        result = await provider()
        assert result == "inv-latest"

    async def test_runs_inside_active_event_loop(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression for the original deadlock-fall-through.

        The provider used to wrap its read in ``asyncio.run`` and catch
        ``RuntimeError`` to return ``"monitor-bootstrap"`` — which fired on
        every production call (the supervisor's loop is already running).
        Awaiting the async provider from inside the running loop must hit
        the DB and return the real value.
        """
        async with async_factory() as sess:
            sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
            await sess.commit()
        async with async_factory() as sess:
            sess.add(
                _invocation_row(
                    invocation_id="inv-loop",
                    start_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
                )
            )
            await sess.commit()

        provider = make_invocation_id_provider(async_factory)
        # ``provider`` is awaited from inside pytest-asyncio's running loop —
        # the same shape as the supervisor's task loop in production.
        result = await provider()
        assert result == "inv-loop"


class TestMakeRiskFreeRateProvider:
    """Async provider — DTB3 read with TTL cache; fallback only on empty DB."""

    async def test_empty_macro_observations_returns_default(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        provider = make_risk_free_rate_provider(async_factory)
        result = await provider()
        # _DEFAULT_RISK_FREE_RATE = 0.045 from the wiring module.
        assert result == pytest.approx(0.045)

    async def test_returns_latest_dtb3_value_divided_by_100(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with async_factory() as sess:
            sess.add(_dtb3(date(2026, 5, 1), 4.50))
            sess.add(_dtb3(date(2026, 5, 11), 5.25))
            await sess.commit()

        provider = make_risk_free_rate_provider(async_factory)
        result = await provider()
        # Latest DTB3 percentage / 100.
        assert result == pytest.approx(0.0525)

    async def test_runs_inside_active_event_loop(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression for the original deadlock-fall-through.

        The provider used to wrap its read in ``asyncio.run`` and catch
        ``RuntimeError`` to log+return ``_DEFAULT_RISK_FREE_RATE`` — which
        fired on every production call. Awaiting from inside the running
        loop must hit the DB and return the freshly-read value.
        """
        async with async_factory() as sess:
            sess.add(_dtb3(date(2026, 5, 11), 3.75))
            await sess.commit()

        provider = make_risk_free_rate_provider(async_factory)
        result = await provider()
        assert result == pytest.approx(0.0375)

    async def test_cache_within_ttl_does_not_rehit_db(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Per the wiring docstring, a TTL cache amortizes the DB read."""
        async with async_factory() as sess:
            sess.add(_dtb3(date(2026, 5, 11), 4.50))
            await sess.commit()

        provider = make_risk_free_rate_provider(async_factory, cache_ttl_seconds=60.0)
        first = await provider()
        assert first == pytest.approx(0.045)

        # Replace the DTB3 row with a new value — cached result must persist.
        async with async_factory() as sess:
            from sqlalchemy import delete

            await sess.execute(delete(MacroObservations))
            sess.add(_dtb3(date(2026, 5, 11), 10.0, revision=2))
            await sess.commit()

        second = await provider()
        assert second == pytest.approx(0.045)  # cached, not 0.10
