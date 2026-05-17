"""Shared fixtures for the continuous-monitor test tree."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
async def sync_session_factory(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[sessionmaker[Session]]:
    """Sync sessionmaker bound to the same SQLite file as ``db_session_factory``.

    The regime resolver runs against a sync ``Session`` (per ALP-454 (C));
    SQLite WAL mode handles concurrent sync + async access on one file.
    Async-defined so pytest-asyncio resolves the upstream async fixture's
    schema-create coroutine before this one runs. Yields so the sync engine
    is disposed when the test completes — otherwise the engine outlives the
    test and leaks the SQLite connection pool.
    """
    del db_session_factory  # ordering dependency only — schema-create must have run.
    sync_engine = make_engine(str(tmp_path / "alphamind.db"))
    try:
        yield make_session_factory(sync_engine)
    finally:
        sync_engine.dispose()
