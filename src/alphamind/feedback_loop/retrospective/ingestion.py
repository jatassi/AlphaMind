"""Retrospective Phase-1 data ingestion (ALP-890 / story 07c).

The imperative shell the ``/feedback-retrospective`` skill drives in Phase 1: one
window-bounded pull of the operating record the skill reads end-to-end before
surfacing patterns. It composes the analytics-spine loader
(:func:`~alphamind.feedback_loop.dataset.load_window`) with the retrospective-only
follow-up pull, so the skill takes a single typed value rather than orchestrating
the reads itself.

:func:`load_window` already assembles the bulk of the set into a
:class:`~alphamind.feedback_loop.dataset.WindowDataset`:

* invocation records + provenance — the in-window ``agent_calls`` (each carries its
  owning invocation's model / prompt / sampling provenance);
* thesis resolutions with component-level outcomes — the ``outcomes`` sub-bundle;
* PM decision envelopes — the ``pm_decision_log`` sliding window;
* validation contracts + the ``refs`` / ``replays`` extension sub-bundles
  (``replays`` is empty until ALP-129 lands — it ingests gracefully, never raising).

The one record :func:`load_window` does not carry is the rollback follow-up surface:
the unresolved ``optional_pending_retrospective`` validation outcomes the skill must
re-raise in the report's *Suggested follow-ups* section (``feedback-loop.md
§ Rollback evidence protocol``). :func:`ingest_window` adds that pull and returns
both halves as a frozen :class:`RetrospectiveIngestion`.

``feedback_loop`` is read-only over trading state: this module only reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alphamind.feedback_loop.dataset import load_window
from alphamind.state.repository.validation_queries import (
    read_unresolved_optional_pending_rollbacks,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from alphamind.config.models.feedback import FeedbackLoopConfig
    from alphamind.feedback_loop.dataset import WindowDataset
    from alphamind.feedback_loop.validation.records import ValidationOutcomeRecord


@dataclass(frozen=True, slots=True)
class RetrospectiveIngestion:
    """The Phase-1 retrospective data set over one window.

    ``window`` is the analytics-spine :class:`WindowDataset` — the bulk of the
    operating record (invocations + provenance, thesis resolutions, PM envelopes,
    validation contracts, citation refs, counterfactual replays). ``pending_rollbacks``
    is the retrospective-only addition: the unresolved
    ``optional_pending_retrospective`` validation outcomes awaiting a
    ``decision_type='follow_up'`` decision.
    """

    window: WindowDataset
    pending_rollbacks: tuple[ValidationOutcomeRecord, ...]


async def ingest_window(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    config: FeedbackLoopConfig | None = None,
) -> RetrospectiveIngestion:
    """Pull the retrospective Phase-1 data set over ``[start, end)``.

    Composes :func:`~alphamind.feedback_loop.dataset.load_window` (the analytics
    spine) with the unresolved ``optional_pending_retrospective`` follow-up pull.
    The follow-up read is a synchronous ``Session`` helper bridged onto this async
    session via :meth:`AsyncSession.run_sync` — the same sync bridge
    :func:`load_window` uses for the validation read.

    Counterfactual replays are carried by ``window.replays``, which is empty until
    ALP-129 lands; the ingestion never raises on their absence.
    """
    window = await load_window(session, start, end, config)
    pending_rollbacks = await session.run_sync(
        lambda sync_session: read_unresolved_optional_pending_rollbacks(sync_session)
    )
    return RetrospectiveIngestion(window=window, pending_rollbacks=pending_rollbacks)


__all__ = ["RetrospectiveIngestion", "ingest_window"]
