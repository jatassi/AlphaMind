"""PM-decision replay queue iterator (ALP-564, story 08 §2).

:func:`iter_pending_replay_proposals` walks the activity log for PM decisions
that are *due* for counterfactual replay and yields the typed entry, its
``PMDecisionDetail``, and the :class:`ReplayKind` the verdict implies. The
driver (``engine.py``) consumes the iterator and replays each proposal.

A PM decision is due when:

* ``event_type == PM_DECISION``;
* ``verdict`` is ``REJECT`` (→ :attr:`ReplayKind.REJECTION`) or
  ``APPROVE_WITH_MODIFICATION`` (→ :attr:`ReplayKind.MODIFICATION_ORIGINAL_FORM`);
* the proposal's evaluation horizon — ``compute_replay_window(...).end`` using
  ``proposal_timestamp = entry.timestamp`` — has elapsed by ``as_of``;
* (when ``since`` is given) ``entry.timestamp >= since``.

The proposal timestamp is the PM-decision entry's timestamp (the originating
Recommendation / assessment carries none); the proposal body is hydrated from
the entry's ``PMDecisionDetail`` to compute its window.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.eligibility import compute_replay_window
from alphamind.execution.counterfactual_replay_engine.enums import ReplayKind
from alphamind.execution.counterfactual_replay_engine.proposal_hydration import (
    hydrate_originating_proposal,
)
from alphamind.portfolio_state.events.types import EventType, PMVerdict
from alphamind.state.invocation_context.activity_log import activity_log_entry_from_row
from alphamind.state.tables.activity_log import ActivityLogRow

if TYPE_CHECKING:
    from alphamind.config.models.replay_engine import CounterfactualReplayEngineConfig
    from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
    from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail

__all__ = ["iter_pending_replay_proposals"]

_VERDICT_TO_KIND: dict[PMVerdict, ReplayKind] = {
    PMVerdict.REJECT: ReplayKind.REJECTION,
    PMVerdict.APPROVE_WITH_MODIFICATION: ReplayKind.MODIFICATION_ORIGINAL_FORM,
}


def iter_pending_replay_proposals(
    session: Session,
    *,
    as_of: datetime,
    since: datetime | None = None,
    config: CounterfactualReplayEngineConfig,
) -> Iterator[tuple[ActivityLogEntry, PMDecisionDetail, ReplayKind]]:
    """Yield ``(entry, detail, replay_kind)`` for every PM decision due by *as_of*.

    Entries are returned in ascending ``entry_at`` order. Verdicts other than
    REJECT / APPROVE_WITH_MODIFICATION are skipped (their proposal was enacted,
    so there is nothing counterfactual to replay). The horizon gate hydrates the
    proposal to compute its replay window; a proposal whose window has not
    elapsed by *as_of* is left for a later run (the engine is idempotent, so it
    will be picked up once due).
    """
    stmt = select(ActivityLogRow).where(ActivityLogRow.event_type == EventType.PM_DECISION.value)
    if since is not None:
        stmt = stmt.where(ActivityLogRow.entry_at >= _to_storage_iso(since))
    stmt = stmt.order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())

    for row in session.execute(stmt).scalars():
        entry = activity_log_entry_from_row(row)
        detail: PMDecisionDetail = entry.detail
        replay_kind = _VERDICT_TO_KIND.get(detail.verdict)
        if replay_kind is None:
            continue
        proposal = hydrate_originating_proposal(detail)
        _, window_end = compute_replay_window(
            proposal, proposal_timestamp=entry.timestamp, config=config
        )
        if window_end > as_of:
            continue
        yield entry, detail, replay_kind


def _to_storage_iso(moment: datetime) -> str:
    """Render *moment* in the ``Z``-suffixed ISO form the activity-log writer uses.

    ``activity_log_entry_to_row`` stores ``entry_at`` as ``isoformat()`` with the
    ``+00:00`` offset rewritten to ``Z``; the ``since`` comparison is on the
    stored TEXT, whose lexicographic order matches chronological order, so the
    bound must be rendered the same way.
    """
    return moment.isoformat().replace("+00:00", "Z")
