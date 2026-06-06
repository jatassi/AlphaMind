"""PM-decision replay queue iterator (ALP-564, story 08 §2).

:func:`iter_pending_replay_proposals` walks the activity log for PM decisions
whose verdict is counterfactually replayable and yields the typed entry, its
``PMDecisionDetail``, and the :class:`ReplayKind` the verdict implies. The
driver (``engine.py``) consumes the iterator and replays each proposal.

A PM decision is yielded when:

* ``event_type == PM_DECISION``;
* ``verdict`` is ``REJECT`` (→ :attr:`ReplayKind.REJECTION`) or
  ``APPROVE_WITH_MODIFICATION`` (→ :attr:`ReplayKind.MODIFICATION_ORIGINAL_FORM`);
* (when ``since`` is given) ``entry.timestamp >= since``.

The queue does **not** hydrate the proposal body, and so does **not** gate on the
evaluation horizon: hydration can raise :class:`ProposalHydrationError` on a
malformed body, and doing it inside this generator would let that error escape
the driver's per-proposal ``try``/``except`` and abort the whole batch. The
driver (:func:`~.engine.replay_proposal`) hydrates each yielded entry once inside
its own protected body and applies the not-yet-due gate there.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.enums import ReplayKind
from alphamind.portfolio_state.events.types import EventType, PMVerdict
from alphamind.state.invocation_context.activity_log import activity_log_entry_from_row
from alphamind.state.tables.activity_log import ActivityLogRow

if TYPE_CHECKING:
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
    since: datetime | None = None,
) -> Iterator[tuple[ActivityLogEntry, PMDecisionDetail, ReplayKind]]:
    """Yield ``(entry, detail, replay_kind)`` for every replayable-verdict PM decision.

    Entries are returned in ascending ``entry_at`` order. Verdicts other than
    REJECT / APPROVE_WITH_MODIFICATION are skipped (their proposal was enacted,
    so there is nothing counterfactual to replay). No hydration happens here —
    the not-yet-due horizon gate is applied by the driver once it has hydrated
    the proposal inside its protected body (see the module docstring).
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
        yield entry, detail, replay_kind


def _to_storage_iso(moment: datetime) -> str:
    """Render *moment* in the ``Z``-suffixed ISO form the activity-log writer uses.

    ``activity_log_entry_to_row`` (``_entry_at_to_iso``) stores ``entry_at`` as
    ``isoformat()`` with the ``+00:00`` offset rewritten to ``Z``; the ``since``
    comparison is on the stored TEXT, whose lexicographic order matches
    chronological order only when both sides share that byte-for-byte format. A
    naive or non-UTC ``since`` would render with a different (or absent) offset
    and mis-order the comparison, so coerce to UTC first: a naive bound is
    assumed UTC, an offset-aware one is converted.
    """
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")
