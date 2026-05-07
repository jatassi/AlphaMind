"""Transactional ``InvocationContext`` (story 02b).

The substrate the configuration loader and downstream write paths opt into.
On enter: open an async transaction, INSERT the supplied ``InvocationRecord``
into the ``invocations`` table, return an ``InvocationHandle`` carrying the
session and ``invocation_id``. On exit: commit on success, rollback on
exception (and re-raise).

The handle exposes the open ``AsyncSession`` so subsequent stories' write
paths (story 03 activity-log emission, stories 07-08 Phase 1 / Phase 2
writes) can join the same transaction without re-discovering the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.state_persistence.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)


@dataclass(frozen=True)
class InvocationHandle:
    """Handle returned by ``InvocationContext.__aenter__``.

    Carries the open ``AsyncSession`` (so downstream callers can join the
    same transaction) and the ``invocation_id`` (so they can stamp child
    rows without re-reading the record).
    """

    session: AsyncSession
    invocation_id: str


class InvocationContext:
    """Async context manager that owns the per-invocation transaction.

    Usage::

        ctx = InvocationContext(session_factory=factory, record=record)
        async with ctx as handle:
            await downstream_write(handle.session, ...)
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        record: InvocationRecord,
    ) -> None:
        self._session_factory = session_factory
        self._record = record
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> InvocationHandle:
        session = self._session_factory()
        try:
            row = invocation_record_to_row(self._record)
            session.add(row)
            # Flush so a CHECK / FK violation surfaces synchronously inside
            # ``__aenter__`` rather than at commit; the caller sees the error
            # before any downstream work runs.
            await session.flush()
        except BaseException:
            await session.rollback()
            await session.close()
            raise
        self._session = session
        return InvocationHandle(session=session, invocation_id=self._record.invocation_id)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            # ``__aenter__`` failed before storing the session — the rollback
            # already happened; nothing to do.
            return
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
