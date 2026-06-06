"""Pipeline-cadence account-activities poll (ALP-846 / W1b).

The imperative shell that drives the previously-dead ``get_account_activities``
endpoint (``broker_adapter/queries.py``) for the four option-lifecycle types,
classifies the stream into typed events
(:mod:`alphamind.execution.account_activities.classify`), and dispatches each
through the per-type handlers
(:mod:`alphamind.execution.account_activities.dispatch`).

This is a **scheduled / pipeline-cadence** task — it runs inside the pipeline's
Phase-1 write transaction (the single writer, ADR-0005), not an always-on
monitor loop (ADR-0004 evicts the activity poll to scheduled work). The
broker query is the only external dependency, behind the :class:`AccountActivitiesSource`
Protocol so the production ``AccountStateQueries`` and the test fake share one
seam.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from alphamind.execution.account_activities.classify import classify_lifecycle_activities
from alphamind.execution.account_activities.dispatch import integrate_lifecycle_event
from alphamind.execution.account_activities.records import LifecycleActivityType
from alphamind.execution.broker_adapter.queries import ActivitySnapshot
from alphamind.state.invocation_context.context import InvocationHandle

# The four option-lifecycle activity types the poll requests (ADR-0002).
_LIFECYCLE_ACTIVITY_TYPES: tuple[str, ...] = tuple(t.value for t in LifecycleActivityType)


class AccountActivitiesSource(Protocol):
    """The broker-query seam the poll drives.

    Exactly the signature of
    :meth:`alphamind.execution.broker_adapter.queries.AccountStateQueries.get_account_activities`
    — the production class satisfies it structurally; tests supply an in-memory
    fake. This is the one external boundary the poll depends on.
    """

    def get_account_activities(
        self,
        *,
        activity_types: tuple[str, ...] | None = None,
        after: str | None = None,
        until: dt.datetime | None = None,
    ) -> AsyncIterator[ActivitySnapshot]: ...


@dataclass(frozen=True, slots=True)
class PollResult:
    """Outcome of one poll run.

    ``activities_booked`` is the number of lifecycle events dispatched to a
    handler (re-polled idempotent events still count as observed but book
    nothing). ``cursor`` is the last activity id seen — the caller persists it
    to resume the next poll from where this one stopped.
    """

    activities_booked: int
    cursor: str | None


async def poll_account_activities(
    handle: InvocationHandle,
    *,
    queries: AccountActivitiesSource,
    borrow_cost_resolver: Callable[[str], float | None],
    after: str | None = None,
    until: dt.datetime | None = None,
) -> PollResult:
    """Drive the activity stream, classify, and book each lifecycle event.

    Fetches the four option-lifecycle activity types from *after* (the resume
    cursor), classifies them into typed, paired events, and dispatches each
    through its per-type handler. ``borrow_cost_resolver`` (the
    invocation-scoped resolver) is forwarded to dispatch so a SHORT
    equity-delivery assignment can stamp its short-only fields. Returns the count
    booked and the advanced cursor. All writes join *handle*'s open transaction.
    """
    snapshots: list[ActivitySnapshot] = []
    async for snap in queries.get_account_activities(
        activity_types=_LIFECYCLE_ACTIVITY_TYPES,
        after=after,
        until=until,
    ):
        snapshots.append(snap)

    cursor = snapshots[-1].id if snapshots else after
    events = classify_lifecycle_activities(tuple(snapshots))
    for event in events:
        await integrate_lifecycle_event(handle, event, borrow_cost_resolver=borrow_cost_resolver)

    return PollResult(activities_booked=len(events), cursor=cursor)


__all__ = ["AccountActivitiesSource", "PollResult", "poll_account_activities"]
