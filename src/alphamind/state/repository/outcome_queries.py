"""Window-scoped read helpers for resolved theses (ALP-885 / story 06c).

The feedback-loop outcome-tier metrics calibrate the system's predictors against
the *realized* outcomes recorded on RESOLVED theses. This module exposes the one
window-scoped read the analytics loader composes for that surface:

* :func:`read_resolved_theses_in_window` — every RESOLVED thesis whose
  ``resolution_timestamp`` falls in ``[start, end)``, decoded (with its component
  rows) into the typed :class:`~alphamind.portfolio_state.records.theses.ThesisRecord`.

Kept out of ``sql_repository.py`` so the concurrent feedback-loop wave does not
collide on that god-module, mirroring ``validation_queries`` / ``agent_calls_queries``.

The helper filters on ``resolution_timestamp`` via a second-precision prefix
comparison — the same chronologically-faithful technique
:func:`alphamind.state.repository.agent_calls_queries.read_agent_calls_in_window`
uses — because resolution timestamps are a Text column written by paths with
differing sub-second precision.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.portfolio_state.records.theses import ThesisRecord, ThesisRecordStatus
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import rows_to_record
from alphamind.state.tables.thesis_components import ThesisComponentRow

# Length of the ``YYYY-MM-DDTHH:MM:SS`` second-precision prefix shared by every
# ISO-8601 timestamp the codebase writes, regardless of its sub-second suffix.
_SECOND_PREFIX_LEN = 19


def _second_prefix(value: datetime) -> str:
    """Render a window bound as its ``YYYY-MM-DDTHH:MM:SS`` second prefix (UTC).

    ``theses.resolution_timestamp`` is a Text column; comparing the bound's
    second prefix against ``substr(resolution_timestamp, 1, 19)`` is faithful at
    second granularity for every stored value regardless of its suffix
    (``Z`` / ``+00:00`` / microseconds), which is the resolution callers need.
    """
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


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
    resolution_prefix = func.substr(ThesisRow.resolution_timestamp, 1, _SECOND_PREFIX_LEN)
    thesis_stmt = (
        select(ThesisRow)
        .where(
            ThesisRow.status == ThesisRecordStatus.RESOLVED.value,
            resolution_prefix >= _second_prefix(start),
            resolution_prefix < _second_prefix(end),
        )
        .order_by(ThesisRow.resolution_timestamp.asc(), ThesisRow.thesis_id.asc())
    )
    thesis_rows = tuple((await session.execute(thesis_stmt)).scalars())
    if not thesis_rows:
        return ()

    thesis_ids = [row.thesis_id for row in thesis_rows]
    comp_stmt = select(ThesisComponentRow).where(ThesisComponentRow.thesis_id.in_(thesis_ids))
    components_by_thesis: dict[str, list[ThesisComponentRow]] = {tid: [] for tid in thesis_ids}
    for comp in (await session.execute(comp_stmt)).scalars():
        components_by_thesis[comp.thesis_id].append(comp)

    return tuple(
        rows_to_record(row, tuple(components_by_thesis[row.thesis_id])) for row in thesis_rows
    )


__all__ = ["read_resolved_theses_in_window"]
