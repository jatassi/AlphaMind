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

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.tables.agent_calls import AgentCallRecord, AgentCallsRow
from alphamind.state.tables.agent_calls_codec import decode_agent_call, encode_agent_call
from alphamind.state.tables.invocations import InvocationRow


async def insert_agent_call(session: AsyncSession, record: AgentCallRecord) -> None:
    """Encode and add one ``AgentCallRecord`` to the session.

    The row is queued for INSERT; the caller controls when the unit of work
    flushes (``flush()`` or ``commit()``). Immutable once written — no UPDATE
    path is provided.
    """
    row = AgentCallsRow(**encode_agent_call(record))
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
    return tuple(decode_agent_call(row) for row in result.scalars())


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
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    stmt = (
        select(AgentCallsRow)
        .join(InvocationRow, AgentCallsRow.invocation_id == InvocationRow.invocation_id)
        .where(
            InvocationRow.start_at >= start_iso,
            InvocationRow.start_at < end_iso,
        )
        .order_by(InvocationRow.start_at.asc(), AgentCallsRow.attempt_number.asc())
    )
    result = await session.execute(stmt)
    return tuple(decode_agent_call(row) for row in result.scalars())


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
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    stmt = (
        select(AgentCallsRow)
        .join(InvocationRow, AgentCallsRow.invocation_id == InvocationRow.invocation_id)
        .where(
            AgentCallsRow.agent_name == agent_name,
            InvocationRow.start_at >= start_iso,
            InvocationRow.start_at < end_iso,
        )
        .order_by(InvocationRow.start_at.asc(), AgentCallsRow.attempt_number.asc())
    )
    result = await session.execute(stmt)
    return tuple(decode_agent_call(row) for row in result.scalars())


__all__ = [
    "insert_agent_call",
    "read_agent_calls_for_agent",
    "read_agent_calls_for_invocation",
    "read_agent_calls_in_window",
]
