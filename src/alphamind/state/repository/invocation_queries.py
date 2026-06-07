"""Window-scoped read helpers for the ``invocations`` table (ALP-911 / story 06f).

The feedback-loop conditioning surface slices the decision metrics on REGIME — the
single most important confounder (``docs/design/feedback-loop.md`` § Conditioning
surface). ``invocations.active_regime`` is the per-invocation source. This module
exposes the one window-scoped read the analytics loader composes for that surface:

* :func:`read_invocation_regimes_in_window` — the ``invocation_id ->
  active_regime`` map over invocations whose ``start_at`` falls in ``[start, end)``.

Kept out of ``sql_repository.py`` so the concurrent feedback-loop wave does not
collide on that god-module, mirroring ``outcome_queries`` / ``agent_calls_queries``.

The helper filters on ``start_at`` via a second-precision prefix comparison — the
same chronologically-faithful technique
:func:`alphamind.state.repository.agent_calls_queries.read_agent_calls_in_window`
uses — because ``invocations.start_at`` is a Text column written by paths with
differing sub-second precision.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.state.repository._window_prefix import SECOND_PREFIX_LEN, second_prefix
from alphamind.state.tables.invocations import InvocationRow


async def read_invocation_regimes_in_window(
    session: AsyncSession,
    start: datetime,
    end: datetime,
) -> dict[str, str]:
    """``invocation_id -> active_regime`` for invocations started in ``[start, end)``.

    The start bound is inclusive, the end bound exclusive — matching every other
    window-scoped read in the analytics spine. ``active_regime`` is NOT NULL, so
    every in-window invocation contributes a mapping.
    """
    # ``invocations.start_at`` is a Text column written at differing sub-second
    # precision across writers; the second-prefix comparison is faithful for all.
    start_at_prefix = func.substr(InvocationRow.start_at, 1, SECOND_PREFIX_LEN)
    stmt = select(InvocationRow.invocation_id, InvocationRow.active_regime).where(
        start_at_prefix >= second_prefix(start),
        start_at_prefix < second_prefix(end),
    )
    result = await session.execute(stmt)
    return {invocation_id: regime for invocation_id, regime in result.all()}


__all__ = ["read_invocation_regimes_in_window"]
