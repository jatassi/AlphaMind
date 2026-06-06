"""Shared pytest fixtures for corporate-actions handler tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ._handler_substrate import build_async_db


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, session_factory) over a fresh on-disk SQLite DB."""
    async_engine, factory = build_async_db(tmp_path)
    yield async_engine, factory
    await async_engine.dispose()
