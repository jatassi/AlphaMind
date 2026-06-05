"""Shared fixtures for account-activities handler/poll tests (ALP-846).

Reuses the in-memory SQLite scaffolding and position/thesis seeders from the
corporate-actions handler substrate — the lifecycle handlers operate on the
same ``positions`` / ``theses`` / ``broker_event_log`` / ``thesis_pnl_ledger``
substrate, so duplicating the seed helpers would be pure noise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind_aa.db"

    import alphamind.state.tables  # noqa: F401 — side-effect import registers all tables

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()
