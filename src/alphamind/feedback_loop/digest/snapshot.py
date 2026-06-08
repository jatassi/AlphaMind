"""Weekly-digest snapshot writer (ALP-891 story 08a).

Serializes the generated :class:`~alphamind.feedback_loop.digest.generator.WeeklyDigest`
for a week into a ``weekly_digest_snapshots`` row, idempotently, so historical
week-to-week comparison is queryable. The pure core
(:func:`build_snapshot_record`) assembles the typed
:class:`~alphamind.state.tables.weekly_digest_snapshots.WeeklyDigestSnapshotRecord`
from an already-generated digest; the async shell (:func:`snapshot_week`) owns the
I/O — the idempotency read, the trailing-window load, generation, the insert, and the
commit.

The shell takes an :class:`~sqlalchemy.ext.asyncio.AsyncSession` (not a ``db_path``):
the caller owns the engine lifecycle, so the same seam serves the CLI producer, a
later scheduler step, or ALP-686 without a rewrite (the documented seam contract).
The clock and the minted ``snapshot_id`` are injected into the shell so the core stays
a pure function of its inputs.

Idempotency rests on the ``week_start`` UNIQUE constraint: the shell reads
``read_weekly_digest_snapshot`` first and reports ``written=False`` without inserting
when a snapshot for that boundary already exists. Should a concurrent run insert the
row between that read and this commit, the UNIQUE fires as an ``IntegrityError``; the
shell rolls back, re-reads the winning row, and reports the same ``written=False``
no-op — so the reported-no-op contract holds under concurrency, not just single-cron.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from alphamind.feedback_loop.digest.codec import DIGEST_SCHEMA_VERSION, serialize_digest
from alphamind.feedback_loop.digest.generator import generate_digest
from alphamind.feedback_loop.digest.windows import load_week_inputs, trailing_weeks
from alphamind.state.repository.digest_queries import (
    insert_weekly_digest_snapshot,
    read_weekly_digest_snapshot,
)
from alphamind.state.tables.weekly_digest_snapshots import WeeklyDigestSnapshotRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind.config.models.digest import DigestConfig
    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.digest.generator import WeeklyDigest

#: Trailing weeks loaded for the snapshotted digest's trajectory + shift baselines
#: (mirrors the read CLI's default: 12 covers the longest baseline span).
DEFAULT_TRAJECTORY_WEEKS = 12

#: Days from the week's Monday to its inclusive last day (Sunday).
_LAST_WEEKDAY_OFFSET = 6

__all__ = [
    "DEFAULT_TRAJECTORY_WEEKS",
    "SnapshotOutcome",
    "build_snapshot_record",
    "snapshot_week",
]


@dataclass(frozen=True, slots=True)
class SnapshotOutcome:
    """The result of one :func:`snapshot_week` call.

    ``written`` is ``True`` when a new row was inserted, ``False`` when a snapshot
    for ``week_start`` already existed (the reported idempotent no-op). ``snapshot_id``
    is the id of the row that now represents the week — the freshly minted one when
    written, the pre-existing one when skipped.
    """

    week_start: date
    written: bool
    snapshot_id: str


def build_snapshot_record(
    digest: WeeklyDigest,
    *,
    week_start: date,
    week_end: date,
    snapshot_id: str,
    snapshotted_at: datetime,
) -> WeeklyDigestSnapshotRecord:
    """Assemble the snapshot record from an already-generated *digest* (pure).

    Serializes *digest* via the codec, stamping :data:`DIGEST_SCHEMA_VERSION`. No I/O
    and no identity / clock generation — the shell injects ``snapshot_id`` and
    ``snapshotted_at`` so this stays a pure function of its inputs.
    """
    return WeeklyDigestSnapshotRecord(
        snapshot_id=snapshot_id,
        week_start=week_start,
        week_end=week_end,
        snapshotted_at=snapshotted_at.isoformat(),
        digest_schema_version=DIGEST_SCHEMA_VERSION,
        digest_json=serialize_digest(digest),
    )


async def snapshot_week(
    session: AsyncSession,
    week_start: date,
    *,
    digest_config: DigestConfig,
    feedback_config: FeedbackLoopConfig,
    trajectory_weeks: int = DEFAULT_TRAJECTORY_WEEKS,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    snapshot_id: str | None = None,
) -> SnapshotOutcome:
    """Snapshot the week beginning *week_start* into ``weekly_digest_snapshots``.

    Idempotency check first: if a snapshot for *week_start* exists, report
    ``written=False`` without loading or inserting (no duplicate, no error). Otherwise
    load the trailing *trajectory_weeks* windows, generate the digest, build the
    record, insert it, and commit. The clock and ``snapshot_id`` are injected so the
    record assembly stays pure; ``snapshot_id`` defaults to a fresh UUID.

    The caller owns the engine lifecycle (this takes an ``AsyncSession``), so the seam
    serves the CLI producer and a later scheduler / ALP-686 caller unchanged.
    """
    existing = await session.run_sync(
        lambda sync_session: read_weekly_digest_snapshot(sync_session, week_start)
    )
    if existing is not None:
        return SnapshotOutcome(
            week_start=week_start,
            written=False,
            snapshot_id=existing.snapshot_id,
        )

    mondays = trailing_weeks(week_start, trajectory_weeks)
    weeks = await load_week_inputs(session, mondays, feedback_config)
    digest = generate_digest(weeks, digest_config)

    # The inclusive last day of the snapshotted week (Sunday): week_start + 6 days.
    week_end = week_start + timedelta(days=_LAST_WEEKDAY_OFFSET)
    minted_id = snapshot_id or str(uuid.uuid4())
    record = build_snapshot_record(
        digest,
        week_start=week_start,
        week_end=week_end,
        snapshot_id=minted_id,
        snapshotted_at=clock(),
    )
    try:
        await session.run_sync(
            lambda sync_session: insert_weekly_digest_snapshot(sync_session, record)
        )
        await session.commit()
    except IntegrityError:
        # A concurrent run inserted the week's snapshot between the idempotency read
        # and this commit (the week_start UNIQUE fired). Roll back, re-read, and report
        # the same idempotent no-op the read-first path would have — so the
        # reported-no-op contract holds under concurrency, not just single-cron.
        await session.rollback()
        winner = await session.run_sync(
            lambda sync_session: read_weekly_digest_snapshot(sync_session, week_start)
        )
        if winner is None:  # pragma: no cover — IntegrityError without a surviving row
            raise
        return SnapshotOutcome(week_start=week_start, written=False, snapshot_id=winner.snapshot_id)
    return SnapshotOutcome(week_start=week_start, written=True, snapshot_id=minted_id)
