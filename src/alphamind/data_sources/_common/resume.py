"""``resume_since`` helper — compute a per-collector ``since`` watermark.

Queries ``MAX(column)`` filtered by *filters* and parses the result (an
ISO date or datetime string) into a UTC ``datetime``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func

from alphamind.data_sources._common.run_tracking import default_session_factory

__all__ = ["resume_since"]


def resume_since(
    *,
    column: Any,
    filters: tuple[Any, ...] = (),
    default_lookback: timedelta,
    overlap: timedelta = timedelta(0),
    session_factory: Any = None,
) -> datetime:
    """Return the timestamp from which a collector should resume.

    Queries ``MAX(column)`` filtered by *filters* and parses the result
    (an ISO date or datetime string) into a UTC ``datetime``.

    - When the table has no matching rows, returns ``now() - default_lookback``.
    - When matching rows exist, returns the parsed maximum minus *overlap*
      so the next pull catches late-arriving data without producing
      duplicates.
    """
    if session_factory is None:
        session_factory = default_session_factory()

    with session_factory() as sess:
        q = sess.query(func.max(column))
        for f in filters:
            q = q.filter(f)
        latest = q.scalar()

    if latest is None:
        return datetime.now(UTC) - default_lookback
    iso = latest if "T" in latest else f"{latest}T00:00:00+00:00"
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed - overlap
