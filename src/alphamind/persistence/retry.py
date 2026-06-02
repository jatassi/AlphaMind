"""Bounded retry for transient SQLite lock errors (ALP-824).

AlphaMind runs two SQLite writers on one WAL database — the scheduler's Phase-1
fill reconciliation and the continuous monitor's live trade-update fills. A
``BEGIN IMMEDIATE`` write transaction (see
:func:`alphamind.persistence.session.begin_write_immediate`) takes the write lock
up front so ``PRAGMA busy_timeout`` governs contention, but a residual transient
collision can still surface (busy_timeout exhausted, or another transient lock
error). :func:`run_with_sqlite_busy_retry` is the backstop: it re-runs a write
unit a bounded number of times on a *transient* ``OperationalError``, re-raising
immediately on anything else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import OperationalError

log = logging.getLogger(__name__)

# Transient SQLite lock signatures. SQLITE_BUSY surfaces as ``database is
# locked``; SQLITE_BUSY_SNAPSHOT (code 517) carries the same message plus its own
# extended error name. Matching the lowercased ``orig`` message covers both, and
# the explicit names guard against a future SQLite build whose message differs.
_TRANSIENT_LOCK_MARKERS = (
    "database is locked",
    "database is busy",
    "sqlite_busy",
)

_DEFAULT_ATTEMPTS = 5
_DEFAULT_BASE_BACKOFF_S = 0.05


def _is_transient_lock_error(exc: OperationalError) -> bool:
    """Whether an ``OperationalError`` is a transient SQLite lock worth retrying.

    Inspects the wrapped DBAPI error (``exc.orig``) — the layer that carries the
    ``database is locked`` / ``SQLITE_BUSY`` text — falling back to the
    SQLAlchemy-level message when ``orig`` is absent.
    """
    message = str(exc.orig) if exc.orig is not None else str(exc)
    message = message.lower()
    return any(marker in message for marker in _TRANSIENT_LOCK_MARKERS)


async def run_with_sqlite_busy_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_backoff_s: float = _DEFAULT_BASE_BACKOFF_S,
) -> T:
    """Run *operation*, retrying transient SQLite lock errors with backoff.

    *operation* is a zero-arg async callable that must open its own fresh session
    per call, so SQLAlchemy identity-map state is clean on retry and a rolled-back
    attempt leaves no partial in-memory state behind. On a transient
    ``OperationalError`` (``database is locked`` / ``SQLITE_BUSY`` /
    ``SQLITE_BUSY_SNAPSHOT``) it backs off ``base_backoff_s * 2**i`` seconds and
    retries, up to ``attempts`` total tries. A non-transient ``OperationalError``
    re-raises immediately; the final transient error re-raises once the attempt
    budget is spent (ALP-824).
    """
    if attempts < 1:
        msg = f"attempts must be >= 1, got {attempts}"
        raise ValueError(msg)
    last_exc: OperationalError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except OperationalError as exc:
            if not _is_transient_lock_error(exc):
                raise
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            backoff_s = base_backoff_s * (2**attempt)
            log.warning(
                "sqlite transient lock on attempt %d/%d (%s); retrying in %.3fs",
                attempt + 1,
                attempts,
                exc.orig if exc.orig is not None else exc,
                backoff_s,
            )
            await asyncio.sleep(backoff_s)
    # Only reachable after the loop broke on the final transient failure; the
    # transient branch always assigns ``last_exc`` before breaking.
    assert last_exc is not None
    raise last_exc
