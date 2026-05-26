"""Tests for the monitor's halt-mode state repository (ALP-665).

The repository wraps the ``monitor_halt_mode`` singleton row that persists the
operator-set halt-mode flag across monitor restarts — the ``halt_mode_engaged``
portfolio state field per ``docs/design/monitor-control-and-events-schema.md``
§ Notes on cross-field invariants.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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

    async def test_write_idempotent_on_same_value_preserves_applied_at(
        self, repository: HaltModeRepository
    ) -> None:
        """A no-op write (same enabled + reason) leaves applied_at anchored to
        the original transition rather than overwriting with the request's
        timestamp.
        """
        first_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        await repository.write(
            HaltModeRecord(enabled=True, reason="circuit breaker", applied_at=first_at)
        )
        # Second write with same enabled + reason but a later timestamp must
        # preserve the original applied_at.
        later_at = first_at + timedelta(minutes=5)
        await repository.write(
            HaltModeRecord(enabled=True, reason="circuit breaker", applied_at=later_at)
        )
        record = await repository.read()
        assert record.enabled is True
        assert record.reason == "circuit breaker"
        assert record.applied_at == first_at

    async def test_write_state_transition_updates_applied_at(
        self, repository: HaltModeRepository
    ) -> None:
        """A real state transition (different enabled OR different reason)
        adopts the new applied_at.
        """
        first_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        await repository.write(HaltModeRecord(enabled=True, reason="r1", applied_at=first_at))
        later_at = first_at + timedelta(minutes=5)
        await repository.write(HaltModeRecord(enabled=True, reason="r2", applied_at=later_at))
        record = await repository.read()
        assert record.reason == "r2"
        assert record.applied_at == later_at

    async def test_concurrent_writes_do_not_raise_integrity_error(
        self, repository: HaltModeRepository
    ) -> None:
        """The atomic upsert eliminates the prior select-then-insert TOCTOU
        race; two concurrent writes both reach a coherent final state without
        IntegrityError (F5).
        """
        ts = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        # Concurrently write two distinct values; the upsert serializes them
        # into a single final row.
        await asyncio.gather(
            repository.write(HaltModeRecord(enabled=True, reason="a", applied_at=ts)),
            repository.write(HaltModeRecord(enabled=True, reason="b", applied_at=ts)),
        )
        record = await repository.read()
        assert record.enabled is True
        # Whichever write committed last wins; both reasons are valid finals.
        assert record.reason in {"a", "b"}
