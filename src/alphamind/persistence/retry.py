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

# Transient SQLite lock signatures. SQLITE_BUSY and SQLITE_BUSY_SNAPSHOT (code
# 517) both surface as the message "database is locked"; pysqlite also exposes the
# reliable extended name on the DBAPI error's ``sqlite_errorname`` attribute. We
# match the error name first (robust to message-text drift across SQLite builds),
# then fall back to the message for wrappers that don't carry the name.
_TRANSIENT_SQLITE_ERRORNAMES = frozenset({"SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT"})
_TRANSIENT_LOCK_MESSAGE_MARKERS = ("database is locked", "database is busy")

_DEFAULT_ATTEMPTS = 5
_DEFAULT_BASE_BACKOFF_S = 0.05


def _is_transient_lock_error(exc: OperationalError) -> bool:
    """Whether an ``OperationalError`` is a transient SQLite lock worth retrying.

    Checks the wrapped DBAPI error's ``sqlite_errorname`` (the reliable
    discriminator for SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT) first, then falls back to
    substring-matching the lowercased message for DBAPI wrappers that don't expose
    the name (and to the SQLAlchemy-level message when ``orig`` is absent).
    """
    orig = exc.orig
    if getattr(orig, "sqlite_errorname", None) in _TRANSIENT_SQLITE_ERRORNAMES:
        return True
    message = (str(orig) if orig is not None else str(exc)).lower()
    return any(marker in message for marker in _TRANSIENT_LOCK_MESSAGE_MARKERS)


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
    re-raises immediately; the final attempt's result (success or exception)
    propagates directly once the retry budget is spent (ALP-824).

    This is a *backstop*, not the primary cross-writer wait. With ``BEGIN
    IMMEDIATE`` (see :func:`alphamind.persistence.session.begin_write_immediate`)
    an ordinary lock conflict is already absorbed by ``PRAGMA busy_timeout``
    *inside* the BEGIN, so the short backoff here is not meant to outwait a long
    lock hold — it covers an immediate ``SQLITE_BUSY_SNAPSHOT`` (which
    ``busy_timeout`` does NOT cover) and the rare case of a fully-exhausted
    ``busy_timeout``.
    """
    if attempts < 1:
        msg = f"attempts must be >= 1, got {attempts}"
        raise ValueError(msg)
    # Attempts 1..N-1 retry with backoff; the final attempt runs outside the loop
    # so its outcome propagates directly — no sentinel / post-loop re-raise.
    for attempt in range(attempts - 1):
        try:
            return await operation()
        except OperationalError as exc:
            if not _is_transient_lock_error(exc):
                raise
            backoff_s = base_backoff_s * (2**attempt)
            log.warning(
                "sqlite transient lock on attempt %d/%d (%s); retrying in %.3fs",
                attempt + 1,
                attempts,
                exc.orig if exc.orig is not None else exc,
                backoff_s,
            )
            await asyncio.sleep(backoff_s)
    return await operation()
