"""Window-scoped read helpers for resolved theses (ALP-885 / story 06c).

The feedback-loop outcome-tier metrics calibrate the system's predictors against
the *realized* outcomes recorded on RESOLVED theses. This module exposes the
window-scoped reads the analytics loader composes for that surface:

* :func:`read_resolved_theses_in_window` — every RESOLVED thesis whose
  ``resolution_timestamp`` falls in ``[start, end)``, decoded (with its component
  rows) into the typed :class:`~alphamind.portfolio_state.records.theses.ThesisRecord`.
* :func:`read_invocation_conditioning` — the ``invocation_id → InvocationConditioning``
  map for a requested set of invocation IDs (ALP-919 / story 02i), carrying the two
  invocation-level conditioning dimensions: ``regime`` (from ``active_regime``) and
  ``time_of_day`` (from ``trigger_source``).

Kept out of ``sql_repository.py`` so the concurrent feedback-loop wave does not
collide on that god-module, mirroring ``validation_queries`` / ``agent_calls_queries``.

The helper filters on ``resolution_timestamp`` via a second-precision prefix
comparison — the same chronologically-faithful technique
:func:`alphamind.state.repository.agent_calls_queries.read_agent_calls_in_window`
uses — because resolution timestamps are a Text column written by paths with
differing sub-second precision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.portfolio_state.records.theses import ThesisRecord, ThesisRecordStatus
from alphamind.state.repository._window_prefix import SECOND_PREFIX_LEN, second_prefix
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import rows_to_record
from alphamind.state.tables.thesis_components import ThesisComponentRow


@dataclass(frozen=True, slots=True)
class InvocationConditioning:
    """The invocation-level conditioning dimensions for one resolved thesis.

    Both fields are sourced from the ``invocations`` row that generated the
    thesis (ALP-919 / story 02i):

    * ``regime`` — from ``invocations.active_regime`` (the most important
      confounder in the outcomes conditioning surface).
    * ``time_of_day`` — from ``invocations.trigger_source`` (a proxy for
      trading-session slot: market-open scheduled trigger vs. continuous-monitor
      alert vs. manual console trigger).
    """

    regime: str
    time_of_day: str


async def _load_components_by_thesis_ids(
    session: AsyncSession, thesis_ids: list[str]
) -> dict[str, list[ThesisComponentRow]]:
    """``thesis_id → component rows`` for *thesis_ids*, in one follow-up query."""
    comp_stmt = select(ThesisComponentRow).where(ThesisComponentRow.thesis_id.in_(thesis_ids))
    components_by_thesis: dict[str, list[ThesisComponentRow]] = {tid: [] for tid in thesis_ids}
    for comp in (await session.execute(comp_stmt)).scalars():
        components_by_thesis[comp.thesis_id].append(comp)
    return components_by_thesis


async def read_resolved_theses_in_window(
    session: AsyncSession,
    start: datetime,
    end: datetime,
) -> tuple[ThesisRecord, ...]:
    """RESOLVED theses whose ``resolution_timestamp`` falls in ``[start, end)``.

    Reads the parent ``theses`` rows filtered to ``status = RESOLVED`` and the
    window, then their ``thesis_components`` children in one follow-up query,
    and decodes each parent+children pair via the round-trip codec. The start
    bound is inclusive, the end bound exclusive — matching every other
    window-scoped read in the analytics spine.

    Theses with a NULL ``resolution_timestamp`` cannot be RESOLVED (the typed
    record enforces it), so the prefix comparison never has to special-case NULL.
    """
    # ``theses.resolution_timestamp`` is a Text column written at differing sub-second
    # precision across writers; the second-prefix comparison is faithful for all.
    resolution_prefix = func.substr(ThesisRow.resolution_timestamp, 1, SECOND_PREFIX_LEN)
    thesis_stmt = (
        select(ThesisRow)
        .where(
            ThesisRow.status == ThesisRecordStatus.RESOLVED.value,
            resolution_prefix >= second_prefix(start),
            resolution_prefix < second_prefix(end),
        )
        .order_by(ThesisRow.resolution_timestamp.asc(), ThesisRow.thesis_id.asc())
    )
    thesis_rows = tuple((await session.execute(thesis_stmt)).scalars())
    if not thesis_rows:
        return ()

    thesis_ids = [row.thesis_id for row in thesis_rows]
    components_by_thesis = await _load_components_by_thesis_ids(session, thesis_ids)

    return tuple(
        rows_to_record(row, tuple(components_by_thesis[row.thesis_id])) for row in thesis_rows
    )


async def read_resolved_thesis_pnl_by_position(
    session: AsyncSession,
    position_ids: list[str],
) -> dict[str, float]:
    """``position_id → resolution_pnl_usd`` for RESOLVED theses on *position_ids*.

    The realized P/L of the resolved thesis on each requested position — the
    "actual approved/modified-form trade" leg of the counterfactual-replay join
    (ALP-928). A thesis is one-to-one with a position, so the map carries at most
    one entry per position. Unlike :func:`read_resolved_theses_in_window`, this is
    **not** clipped to a window: the modified-form trade behind a
    ``modification_original_form`` replay may resolve outside the replay's analytics
    window, so the join keys on position and reads the thesis's own resolution
    whenever it lands. Positions with no RESOLVED thesis (still ACTIVE, never
    opened, or unknown) are silently absent. An empty *position_ids* returns an
    empty dict without a query.
    """
    if not position_ids:
        return {}
    thesis_stmt = select(ThesisRow).where(
        ThesisRow.status == ThesisRecordStatus.RESOLVED.value,
        ThesisRow.position_id.in_(position_ids),
    )
    thesis_rows = tuple((await session.execute(thesis_stmt)).scalars())
    if not thesis_rows:
        return {}
    thesis_ids = [row.thesis_id for row in thesis_rows]
    components_by_thesis = await _load_components_by_thesis_ids(session, thesis_ids)
    pnl_by_position: dict[str, float] = {}
    for row in thesis_rows:
        record = rows_to_record(row, tuple(components_by_thesis[row.thesis_id]))
        # A RESOLVED record's resolution_pnl_usd is non-None by ThesisRecord's validators;
        # a null here is inconsistent persisted state, surfaced loudly (not a bare assert,
        # which `python -O` would strip — silently storing None as a float).
        if record.resolution_pnl_usd is None:
            msg = (
                f"thesis {record.thesis_id} has status RESOLVED but a null "
                "resolution_pnl_usd — inconsistent persisted state"
            )
            raise ValueError(msg)
        pnl_by_position[str(record.position_id)] = record.resolution_pnl_usd
    return pnl_by_position


async def read_invocation_conditioning(
    session: AsyncSession,
    invocation_ids: list[str],
) -> dict[str, InvocationConditioning]:
    """``invocation_id → InvocationConditioning`` for the requested IDs.

    Returns only the IDs present in the ``invocations`` table — unknown IDs
    are silently absent. An empty input list returns an empty dict (no query).

    ``active_regime`` and ``trigger_source`` are both NOT NULL columns on
    ``InvocationRow``, so every matched row contributes a complete mapping.
    """
    if not invocation_ids:
        return {}
    stmt = select(
        InvocationRow.invocation_id,
        InvocationRow.active_regime,
        InvocationRow.trigger_source,
    ).where(InvocationRow.invocation_id.in_(invocation_ids))
    result = await session.execute(stmt)
    return {
        inv_id: InvocationConditioning(regime=regime, time_of_day=trigger_source)
        for inv_id, regime, trigger_source in result.all()
    }


__all__ = [
    "InvocationConditioning",
    "read_invocation_conditioning",
    "read_resolved_theses_in_window",
    "read_resolved_thesis_pnl_by_position",
]
