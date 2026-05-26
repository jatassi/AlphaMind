"""Tests for the monitor's halt-mode state repository (ALP-665).

The repository wraps the ``monitor_halt_mode`` singleton row that persists the
operator-set halt-mode flag across monitor restarts — the ``halt_mode_engaged``
portfolio state field per ``docs/design/monitor-control-and-events-schema.md``
§ Notes on cross-field invariants.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker as _async_sessionmaker

from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRecord,
    HaltModeRepository,
)
from alphamind.persistence.models import Base
from alphamind.state import tables as _tables  # noqa: F401 — ensures Base.metadata sees all tables


@pytest.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return eng


@pytest.fixture
async def repository(
    engine: AsyncEngine,
) -> HaltModeRepository:
    factory = _async_sessionmaker(engine, expire_on_commit=False)
    return HaltModeRepository(session_factory=factory)


@pytest.mark.asyncio
class TestHaltModeRepository:
    async def test_read_on_empty_table_returns_disengaged(
        self, repository: HaltModeRepository
    ) -> None:
        record = await repository.read()
        assert record.enabled is False
        # ``applied_at`` is None on the disengaged default — never set.
        assert record.applied_at is None
        assert record.reason is None

    async def test_write_persists_engaged_state(self, repository: HaltModeRepository) -> None:
        ts = datetime.now(UTC)
        await repository.write(
            HaltModeRecord(enabled=True, reason="circuit breaker", applied_at=ts)
        )
        record = await repository.read()
        assert record.enabled is True
        assert record.reason == "circuit breaker"
        assert record.applied_at is not None

    async def test_write_overwrites_singleton(self, repository: HaltModeRepository) -> None:
        await repository.write(
            HaltModeRecord(enabled=True, reason="first", applied_at=datetime.now(UTC))
        )
        await repository.write(
            HaltModeRecord(enabled=False, reason="lifted", applied_at=datetime.now(UTC))
        )
        record = await repository.read()
        assert record.enabled is False
        assert record.reason == "lifted"
