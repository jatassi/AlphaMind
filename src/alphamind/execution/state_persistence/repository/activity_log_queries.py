"""Read APIs against the ``activity_log`` table (story 03 / ALP-357).

Four queries match the projections the snapshot assembler and the three
blocked distillation stories consume — see
``docs/design/05-execution-layer/state-persistence.md`` § Read paths.

Each helper returns the typed ``ActivityLogEntry`` (or a tuple thereof);
rehydration goes through ``activity_log_entry_from_row`` so reads are
isomorphic with the writes the emission helper performs.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.execution.state_persistence.invocation_context.activity_log import (
    activity_log_entry_from_row,
)
from alphamind.execution.state_persistence.tables.activity_log import ActivityLogRow
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventType,
)


async def read_intra_invocation_changelog(
    session: AsyncSession,
    invocation_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """All entries for a single invocation, ordered by ``entry_at`` ascending.

    Drives raw state category 5a — the intra-invocation changelog the
    snapshot read consumes.
    """
    stmt = (
        select(ActivityLogRow)
        .where(ActivityLogRow.invocation_id == invocation_id)
        .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
    )
    result = await session.execute(stmt)
    return tuple(activity_log_entry_from_row(row) for row in result.scalars())


async def read_recent_pm_decision_log(
    session: AsyncSession,
    sliding_window_invocations: int,
) -> tuple[ActivityLogEntry, ...]:
    """``PM_DECISION`` entries from the most recent N invocations.

    Drives raw state category 5b — the PM decision sliding window. The
    window is computed against ``invocations.start_at`` desc and intersects
    with ``activity_log.event_type == 'PM_DECISION'``.
    """
    recent_invocations_subq = (
        select(InvocationRow.invocation_id)
        .order_by(InvocationRow.start_at.desc())
        .limit(sliding_window_invocations)
        .subquery()
    )
    stmt = (
        select(ActivityLogRow)
        .where(
            ActivityLogRow.event_type == EventType.PM_DECISION.value,
            ActivityLogRow.invocation_id.in_(select(recent_invocations_subq.c.invocation_id)),
        )
        .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
    )
    result = await session.execute(stmt)
    return tuple(activity_log_entry_from_row(row) for row in result.scalars())


async def read_position_modification_trail(
    session: AsyncSession,
    position_ids: list[str],
) -> dict[str, tuple[ActivityLogEntry, ...]]:
    """Per-position activity-log entries, ordered chronologically.

    Drives raw state category 5c — the position modification trail. Returns
    one tuple per requested position id; positions with no entries map to
    an empty tuple so callers can iterate uniformly.
    """
    if not position_ids:
        return {}
    stmt = (
        select(ActivityLogRow)
        .where(ActivityLogRow.position_id.in_(position_ids))
        .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
    )
    result = await session.execute(stmt)
    grouped: dict[str, list[ActivityLogEntry]] = {pid: [] for pid in position_ids}
    for row in result.scalars():
        # row.position_id is non-None because the WHERE clause filtered to
        # the supplied list — narrow for the type checker.
        pid = row.position_id
        assert pid is not None
        grouped[pid].append(activity_log_entry_from_row(row))
    return {pid: tuple(entries) for pid, entries in grouped.items()}


async def read_most_recent_config_change_new_hash(
    session: AsyncSession,
    config_file: str,
) -> str | None:
    """The ``new_hash`` from the most recent ``DISTILLATION_CONFIG_CHANGE``.

    Filters by ``config_file`` (matched against the
    ``DistillationConfigChangeDetail.config_file`` field carried in
    ``detail_json``). Returns ``None`` when no entry matches. Consumed by
    ALP-100 — the configuration loader needs to know whether the prior
    reload's resolved hash matches the current one to suppress a no-op
    activity-log entry.
    """
    stmt = (
        select(ActivityLogRow)
        .where(ActivityLogRow.event_type == EventType.DISTILLATION_CONFIG_CHANGE.value)
        .order_by(ActivityLogRow.entry_at.desc(), ActivityLogRow.entry_id.desc())
    )
    result = await session.execute(stmt)
    for row in result.scalars():
        entry = activity_log_entry_from_row(row)
        if entry.detail.config_file == config_file:
            return str(entry.detail.new_hash)
    return None
