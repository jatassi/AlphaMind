"""Repository helpers for the ``agent_calls`` table (ALP-873).

Four async helpers expose the read/write surface consumed by the feedback-loop
harness write-hook (story 04a) and the per-agent calibration analysis (06*):

* :func:`insert_agent_call` — encode + add one record to the async session.
* :func:`read_agent_calls_for_invocation` — all calls for a single invocation,
  ordered by ``attempt_number`` ascending.
* :func:`read_agent_calls_in_window` — calls whose owning invocation's
  ``start_at`` falls in ``[start, end)``.
* :func:`read_agent_calls_for_agent` — calls for a specific ``agent_name``
  filtered to a time window via the same invocation join.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.tables.agent_calls import AgentCallRecord, AgentCallsRow
from alphamind.state.tables.agent_calls_codec import record_to_row, row_to_record
from alphamind.state.tables.invocations import InvocationRow

# Length of the ``YYYY-MM-DDTHH:MM:SS`` second-precision prefix shared by every
# ISO-8601 timestamp the codebase writes, regardless of its suffix.
_SECOND_PREFIX_LEN = 19


def _second_prefix(value: datetime) -> str:
    """Render a window bound as its ``YYYY-MM-DDTHH:MM:SS`` second prefix (UTC).

    ``invocations.start_at`` is a Text column written by two paths with
    *different* sub-second precision: the scheduler / operator paths emit
    second precision (``strftime("%Y-%m-%dT%H:%M:%SZ")``) while the
    fill-collection recovery path emits microsecond precision
    (``isoformat().replace("+00:00", "Z")``). The suffixes therefore differ
    (``Z`` 0x5A vs ``.`` 0x2E vs ``+`` 0x2B), so a naive lexicographic range
    filter over the raw column mis-sorts at sub-second boundaries.

    Comparing the bound's second prefix against the column's second prefix
    (``substr(start_at, 1, 19)``) is chronologically faithful at second
    granularity for *every* stored value regardless of which writer produced
    it, which is the resolution callers need for invocation-start windows.
    """
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


async def insert_agent_call(session: AsyncSession, record: AgentCallRecord) -> None:
    """Encode and add one ``AgentCallRecord`` to the session.

    The row is queued for INSERT; the caller controls when the unit of work
    flushes (``flush()`` or ``commit()``). Immutable once written — no UPDATE
    path is provided.
    """
    row = AgentCallsRow(**record_to_row(record))
    session.add(row)


async def read_agent_calls_for_invocation(
    session: AsyncSession,
    invocation_id: str,
) -> tuple[AgentCallRecord, ...]:
    """All agent calls for a single invocation, ordered by ``attempt_number`` ascending."""
    stmt = (
        select(AgentCallsRow)
        .where(AgentCallsRow.invocation_id == invocation_id)
        .order_by(AgentCallsRow.attempt_number.asc(), AgentCallsRow.agent_call_id.asc())
    )
    result = await session.execute(stmt)
    return tuple(row_to_record(row) for row in result.scalars())


async def read_agent_calls_in_window(
    session: AsyncSession,
    start: datetime,
    end: datetime,
) -> tuple[AgentCallRecord, ...]:
    """Agent calls whose owning invocation's ``start_at`` falls in ``[start, end)``.

    Joins ``agent_calls`` → ``invocations`` on ``invocation_id`` and filters on
    ``invocations.start_at``. The start bound is inclusive, the end bound is
    exclusive.
    """
    start_at_prefix = func.substr(InvocationRow.start_at, 1, _SECOND_PREFIX_LEN)
    stmt = (
        select(AgentCallsRow)
        .join(InvocationRow, AgentCallsRow.invocation_id == InvocationRow.invocation_id)
        .where(
            start_at_prefix >= _second_prefix(start),
            start_at_prefix < _second_prefix(end),
        )
        .order_by(InvocationRow.start_at.asc(), AgentCallsRow.attempt_number.asc())
    )
    result = await session.execute(stmt)
    return tuple(row_to_record(row) for row in result.scalars())


async def read_agent_calls_for_agent(
    session: AsyncSession,
    agent_name: str,
    start: datetime,
    end: datetime,
) -> tuple[AgentCallRecord, ...]:
    """Agent calls for a specific ``agent_name`` within a time window.

    Filters by ``agent_name`` and joins through ``invocations.start_at`` for
    the window bounds, matching the same inclusive-start / exclusive-end
    semantics as :func:`read_agent_calls_in_window`.
    """
    start_at_prefix = func.substr(InvocationRow.start_at, 1, _SECOND_PREFIX_LEN)
    stmt = (
        select(AgentCallsRow)
        .join(InvocationRow, AgentCallsRow.invocation_id == InvocationRow.invocation_id)
        .where(
            AgentCallsRow.agent_name == agent_name,
            start_at_prefix >= _second_prefix(start),
            start_at_prefix < _second_prefix(end),
        )
        .order_by(InvocationRow.start_at.asc(), AgentCallsRow.attempt_number.asc())
    )
    result = await session.execute(stmt)
    return tuple(row_to_record(row) for row in result.scalars())


__all__ = [
    "insert_agent_call",
    "read_agent_calls_for_agent",
    "read_agent_calls_for_invocation",
    "read_agent_calls_in_window",
]
