"""Fresh-session ``BEGIN IMMEDIATE`` write units with bounded retry (ALP-942).

Composes the two ALP-824 primitives —
:func:`alphamind.persistence.session.begin_write_immediate` (write lock up
front, so ``PRAGMA busy_timeout`` governs cross-writer contention) and
:func:`alphamind.persistence.retry.run_with_sqlite_busy_retry` (bounded retry
of the residual transient lock errors) — into the one shape every write
transaction that shares the WAL database with another committer runs through.

A write transaction that reads before its first write under the async engine's
default ``BEGIN DEFERRED`` pins a WAL read snapshot on the first SELECT; once
any other connection commits, the later write-upgrade fails immediately with
``SQLITE_BUSY_SNAPSHOT`` (the busy handler is never invoked — waiting cannot
refresh a stale snapshot mid-transaction). Running the body through
:func:`run_immediate_write_unit` makes that class unreachable: the write lock
is taken at BEGIN, so concurrent committers serialize behind ``busy_timeout``
instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.persistence.retry import _DEFAULT_ATTEMPTS, run_with_sqlite_busy_retry
from alphamind.persistence.session import _SQLITE_BEGIN_MODE_OPTION, begin_write_immediate


async def run_immediate_write_unit[T](
    session_factory: async_sessionmaker[AsyncSession],
    unit: Callable[[AsyncSession], Awaitable[T]],
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
) -> T:
    """Run *unit* as one committed ``BEGIN IMMEDIATE`` transaction, with retry.

    Each attempt opens a **fresh** session from *session_factory*, issues
    ``BEGIN IMMEDIATE`` before the unit body (so the write lock is held from
    BEGIN and no deferred read snapshot can go stale under it), awaits
    ``unit(session)``, and commits. The attempt loop is
    :func:`run_with_sqlite_busy_retry`, which re-runs only transient SQLite
    lock errors (``SQLITE_BUSY`` / ``SQLITE_BUSY_SNAPSHOT``) and satisfies its
    fresh-session-per-attempt contract here by construction — a rolled-back
    attempt leaves no identity-map state behind.

    *unit* must not commit (the helper owns the transaction boundary) and must
    be safe to re-run wholesale: a retried attempt re-executes the entire body
    on a new session, so reads are re-issued and writes must be idempotent at
    the storage layer (the append helpers' ``ON CONFLICT DO NOTHING`` shape).
    The unit's return value propagates on success.
    """

    async def _attempt() -> T:
        async with session_factory() as session:
            await begin_write_immediate(session)
            result = await unit(session)
            await session.commit()
            return result

    return await run_with_sqlite_busy_retry(_attempt, attempts=attempts)


async def require_immediate_write_unit(session: AsyncSession, *, helper: str) -> None:
    """Raise unless *session*'s active connection began ``BEGIN IMMEDIATE``.

    The runtime tripwire of the write discipline (ALP-942 F): the shared
    append helpers call this first, so any future write path that reaches them
    on a deferred transaction fails loudly in its first test run instead of
    shipping a latent ``SQLITE_BUSY_SNAPSHOT`` race. *helper* names the caller
    in the error message.
    """
    conn = await session.connection()
    sync_conn = conn.sync_connection
    mode = (
        sync_conn.get_execution_options().get(_SQLITE_BEGIN_MODE_OPTION)
        if sync_conn is not None
        else None
    )
    if mode != "IMMEDIATE":
        msg = (
            f"{helper} requires a BEGIN IMMEDIATE write transaction "
            f"(got begin mode {mode!r}); run the transaction through "
            "run_immediate_write_unit, or begin_write_immediate on a fresh "
            "session (ALP-942)."
        )
        raise RuntimeError(msg)


__all__ = ["require_immediate_write_unit", "run_immediate_write_unit"]
