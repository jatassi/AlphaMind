"""Shared helpers for the three feedback-loop headless CLIs (ALP-915 / story 06i).

The imperative-shell plumbing the digest, validation, and retrospective CLIs each
re-implemented: the tz-aware ISO-8601 boundary parse, the engine→session→dispose
lifecycle (sync and async), and the JSON emit. Lifted here so the three surfaces parse,
open, and emit identically.

* :func:`parse_aware_datetime` — parse one ``--start`` / ``--end`` ISO-8601 argument into
  an aware ``datetime``, rejecting invalid or naive input with
  :class:`argparse.ArgumentTypeError` (the argparse-native error, so a bad value exits 1
  without a traceback).
* :func:`open_sync_session` / :func:`open_async_session` — context managers that build the
  engine, yield a bound session, and dispose the engine in ``finally``. The yielded session
  is the deepest useful surface: the caller runs its query / commit and never touches engine
  lifecycle.
* :func:`emit_json` — write a JSON payload to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from datetime import datetime
from typing import TYPE_CHECKING

from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

__all__ = [
    "emit_json",
    "open_async_session",
    "open_sync_session",
    "parse_aware_datetime",
]


def parse_aware_datetime(value: str, arg_name: str) -> datetime:
    """Parse *value* as ISO-8601 and reject naive datetimes.

    Raises :class:`argparse.ArgumentTypeError` (not a bare ``ValueError``) on either an
    unparseable string or a naive datetime, so the CLI surfaces the failure as an
    exit-1 argument error rather than a traceback. *arg_name* names the offending flag
    in the message.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is not a valid ISO-8601 datetime. "
            "Provide a tz-aware value, e.g. 2026-01-01T00:00:00+00:00."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"{arg_name}: {value!r} is a naive datetime (no timezone offset). "
            "Provide a tz-aware value, e.g. 2026-01-01T00:00:00+00:00."
        )
    return parsed


@contextlib.contextmanager
def open_sync_session(db_path: str | None) -> Iterator[Session]:
    """Open a sync session bound to *db_path*; dispose the engine on exit.

    Builds the engine through the documented path chain (explicit → ``DATABASE_PATH``
    → ``main.yaml``), yields a bound :class:`~sqlalchemy.orm.Session`, and disposes the
    engine in ``finally``. The caller owns its own commit boundary.
    """
    engine = make_engine(db_path)
    try:
        with make_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


@contextlib.asynccontextmanager
async def open_async_session(db_path: str | None) -> AsyncIterator[AsyncSession]:
    """Open an async session bound to *db_path*; dispose the engine on exit.

    The async counterpart of :func:`open_sync_session`: builds the async engine, yields
    a bound :class:`~sqlalchemy.ext.asyncio.AsyncSession`, and disposes the engine in
    ``finally``. The caller owns its own commit boundary.
    """
    engine = make_async_engine(db_path)
    try:
        async with make_async_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


def emit_json(payload: object) -> None:
    """Write *payload* to stdout as JSON."""
    print(json.dumps(payload))
