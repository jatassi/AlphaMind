"""SQLAlchemy mapping for the ``weekly_digest_snapshots`` table (ALP-876).

One immutable row per weekly snapshot boundary. Written by the snapshot CLI
producer (story 08a) on a fixed schedule (8am ET Sunday). Holds the
serialized weekly-digest data structure so historical week-to-week comparison
is queryable.

``digest_json`` stores the serialized ``WeeklyDigest`` payload as opaque
JSON-text. The typed ``WeeklyDigest`` parse/serialize lives with the digest
structure in story 07a — this table stores it opaquely.

A UNIQUE constraint on ``week_start`` ensures one snapshot per weekly
boundary, providing the key the producer's idempotency check (story 08a)
uses to detect an already-present snapshot before inserting.

``digest_schema_version`` guards payload-shape evolution so consumers can
reject or migrate stale payloads without silent data corruption.
"""

from __future__ import annotations

import dataclasses
from datetime import date

from sqlalchemy import Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base


@dataclasses.dataclass(frozen=True)
class WeeklyDigestSnapshotRecord:
    """Typed in-memory representation of one weekly digest snapshot.

    ``digest_json`` carries the serialized weekly-digest payload as a JSON
    string. The caller is responsible for serializing / deserializing the
    payload; this record stores it opaquely so the table layer has no
    dependency on the ``WeeklyDigest`` structure (story 07a).

    All fields are immutable — the record is frozen. The ``week_start``
    column's UNIQUE constraint on the table side enforces one snapshot per
    week boundary; the record itself does not validate this.
    """

    snapshot_id: str
    week_start: date
    week_end: date
    snapshotted_at: str
    digest_schema_version: int
    digest_json: str


class WeeklyDigestSnapshotsRow(Base):
    """Append-only per-snapshot row.

    One row per weekly snapshot boundary (``week_start``). The
    ``uq_weekly_digest_snapshots_week_start`` UNIQUE constraint enforces the
    one-snapshot-per-boundary invariant at the storage level.
    """

    __tablename__ = "weekly_digest_snapshots"

    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # ISO 8601 date strings (YYYY-MM-DD). Stored as Text for SQLite
    # portability; the codec converts to/from ``datetime.date``.
    week_start: Mapped[str] = mapped_column(Text, nullable=False)
    week_end: Mapped[str] = mapped_column(Text, nullable=False)
    # ISO 8601 datetime string (tz-aware, UTC) for the wall-clock snapshot time.
    snapshotted_at: Mapped[str] = mapped_column(Text, nullable=False)
    digest_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Opaque JSON-text payload. The WeeklyDigest typed structure is
    # defined in story 07a; here we store the serialized form only.
    digest_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "week_start",
            name="uq_weekly_digest_snapshots_week_start",
        ),
    )


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


def encode_weekly_digest_snapshot(record: WeeklyDigestSnapshotRecord) -> dict[str, object]:
    """Project a ``WeeklyDigestSnapshotRecord`` to a column-keyed dict for INSERT."""
    return {
        "snapshot_id": record.snapshot_id,
        "week_start": record.week_start.isoformat(),
        "week_end": record.week_end.isoformat(),
        "snapshotted_at": record.snapshotted_at,
        "digest_schema_version": record.digest_schema_version,
        "digest_json": record.digest_json,
    }


def decode_weekly_digest_snapshot(row: WeeklyDigestSnapshotsRow) -> WeeklyDigestSnapshotRecord:
    """Rehydrate an ORM row into a ``WeeklyDigestSnapshotRecord``."""
    return WeeklyDigestSnapshotRecord(
        snapshot_id=row.snapshot_id,
        week_start=date.fromisoformat(row.week_start),
        week_end=date.fromisoformat(row.week_end),
        snapshotted_at=row.snapshotted_at,
        digest_schema_version=row.digest_schema_version,
        digest_json=row.digest_json,
    )


__all__ = [
    "WeeklyDigestSnapshotRecord",
    "WeeklyDigestSnapshotsRow",
    "decode_weekly_digest_snapshot",
    "encode_weekly_digest_snapshot",
]
