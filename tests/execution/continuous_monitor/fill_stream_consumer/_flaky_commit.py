"""Shared flaky-commit session factory for ALP-942 write-discipline tests.

The DB is one of the four sanctioned mock boundaries; this wrapper sits on its
seam — a real ``AsyncSession`` whose first *failures* commits raise the
transient ``database is locked`` error the 2026-06-09 production incidents
surfaced, then behave normally. ``counters["commits"]`` counts the commits
that actually went through, which pins the one-transaction (B4) invariant.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def flaky_commit_factory(
    inner: async_sessionmaker[AsyncSession], *, failures: int
) -> tuple[async_sessionmaker[AsyncSession], dict[str, int]]:
    counters = {"commits": 0, "failures_left": failures}

    class _FlakyCommitSession(AsyncSession):
        async def commit(self) -> None:
            if counters["failures_left"] > 0:
                counters["failures_left"] -= 1
                await self.rollback()
                raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))
            counters["commits"] += 1
            await super().commit()

    flaky: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=inner.kw["bind"], class_=_FlakyCommitSession, expire_on_commit=False
    )
    return flaky, counters


__all__ = ["flaky_commit_factory"]
