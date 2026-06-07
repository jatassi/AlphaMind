"""Repository helpers for the ``weekly_digest_snapshots`` table (ALP-876).

Three helpers expose the read / write surface consumed by the snapshot CLI
producer (story 08a):

* :func:`insert_weekly_digest_snapshot` — encode + queue one record for
  insert. The ``week_start`` UNIQUE constraint raises ``IntegrityError`` at
  flush / commit (not at the ``add`` call) on a duplicate, so the producer's
  idempotency check is the caller's responsibility.

* :func:`read_weekly_digest_snapshot` — return the snapshot for a given
  ``week_start``, or ``None`` when absent.

* :func:`read_weekly_digest_snapshots_in_range` — return all snapshots
  whose ``week_start`` falls within the closed interval ``[start, end]``,
  ordered by ``week_start`` ascending.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.state.tables.weekly_digest_snapshots import (
    WeeklyDigestSnapshotRecord,
    WeeklyDigestSnapshotsRow,
    decode_weekly_digest_snapshot,
    encode_weekly_digest_snapshot,
)


def insert_weekly_digest_snapshot(
    session: Session,
    record: WeeklyDigestSnapshotRecord,
) -> None:
    """Encode and queue a ``WeeklyDigestSnapshotRecord`` for insert.

    Adds the row to *session*; the ``week_start`` UNIQUE constraint is
    enforced when the unit of work flushes, so a duplicate surfaces as
    :class:`sqlalchemy.exc.IntegrityError` at the next ``flush`` / ``commit``
    (or autoflush) — not at this call. The producer is responsible for
    idempotency checking before calling this helper.
    """
    row = WeeklyDigestSnapshotsRow(**encode_weekly_digest_snapshot(record))
    session.add(row)


def read_weekly_digest_snapshot(
    session: Session,
    week_start: date,
) -> WeeklyDigestSnapshotRecord | None:
    """Return the snapshot for *week_start*, or ``None`` when absent.

    Looks up by the ISO 8601 string representation of *week_start* (the
    storage format). Returns ``None`` when no snapshot exists for that
    week boundary.
    """
    stmt = select(WeeklyDigestSnapshotsRow).where(
        WeeklyDigestSnapshotsRow.week_start == week_start.isoformat()
    )
    row = session.execute(stmt).scalars().one_or_none()
    if row is None:
        return None
    return decode_weekly_digest_snapshot(row)


def read_weekly_digest_snapshots_in_range(
    session: Session,
    start: date,
    end: date,
) -> tuple[WeeklyDigestSnapshotRecord, ...]:
    """Return snapshots whose ``week_start`` falls in the closed interval ``[start, end]``.

    Results are ordered by ``week_start`` ascending. Returns an empty tuple
    when no snapshots exist in the range.

    Comparison is lexicographic on the ISO 8601 date string (YYYY-MM-DD),
    which preserves chronological order for same-timezone dates.
    """
    stmt = (
        select(WeeklyDigestSnapshotsRow)
        .where(
            WeeklyDigestSnapshotsRow.week_start >= start.isoformat(),
            WeeklyDigestSnapshotsRow.week_start <= end.isoformat(),
        )
        .order_by(WeeklyDigestSnapshotsRow.week_start)
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(decode_weekly_digest_snapshot(row) for row in rows)


__all__ = [
    "insert_weekly_digest_snapshot",
    "read_weekly_digest_snapshot",
    "read_weekly_digest_snapshots_in_range",
]
