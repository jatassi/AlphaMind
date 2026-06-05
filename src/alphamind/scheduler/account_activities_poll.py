"""Scheduler-layer registration of the account-activities poll (ALP-846 / W1b).

The option-lifecycle activity poll is **scheduled / pipeline-cadence** work
(ADR-0004 evicts it from the always-on monitor): it runs inside the pipeline's
Phase-1 write transaction, once per invocation, alongside fill integration and
corporate-actions. This module is the composition-root seam that builds the
broker activities source and drives
:func:`alphamind.execution.account_activities.poll.poll_account_activities`.

``activities_source_factory`` mirrors the ``account_queries_factory`` seam in
``phase1_inputs``: ``None`` on the production daemon path builds the inline
Alpaca-backed ``AccountStateQueries``; the test suite and the debug-e2e harness
pass their own factory to substitute the broker without monkey-patching.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.account_activities.handlers import event_key_for
from alphamind.execution.account_activities.poll import (
    AccountActivitiesSource,
    PollResult,
    poll_account_activities,
)
from alphamind.execution.account_activities.records import LifecycleActivityType
from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
)
from alphamind.execution.broker_adapter.client_factory import (
    ExecutionMode as ClientFactoryExecutionMode,
)
from alphamind.execution.broker_adapter.queries import AccountStateQueries
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.broker_event_log import BrokerEventLogRow

ActivitiesSourceFactory = Callable[[VenueConfig | None, ExecutionMode], AccountActivitiesSource]

# The ``event_key`` prefix the lifecycle handlers stamp on every account-activity
# row (``event_key_for`` = ``activity:{id}``). Stripping it recovers the raw
# Alpaca activity id the poll resumes from as ``page_token``.
_EVENT_KEY_PREFIX = event_key_for("")
_LIFECYCLE_EVENT_TYPES = tuple(t.value for t in LifecycleActivityType)


def _default_activities_source_factory(
    venue_config: VenueConfig | None, execution_mode: ExecutionMode
) -> AccountActivitiesSource:
    """Default Alpaca-backed ``AccountActivitiesSource`` (the production path).

    Builds an :class:`AccountStateQueries` over the venue's trading client — the
    same wrapper ``phase1_inputs`` uses for the other read endpoints. Its
    ``get_account_activities`` generator is the previously-dead endpoint this
    story wires up.
    """
    if venue_config is None:
        msg = "venue_config is required to build the default Alpaca activities source"
        raise ValueError(msg)
    mode_literal: ClientFactoryExecutionMode = (
        "live" if execution_mode is ExecutionMode.live else "paper"
    )
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return AccountStateQueries(factory.build_trading_client())


async def _resolve_resume_cursor(handle: InvocationHandle) -> str | None:
    """Derive the resume cursor from the durable ``broker_event_log``.

    The cursor home is the append-only event log itself — every booked
    account-activity row carries ``event_key = activity:{id}`` (ADR-0005), so the
    lexicographically-max account-activity ``event_key`` recovers the last
    activity id seen across ALL prior pipeline runs (Alpaca activity ids are
    timestamp-prefixed and sort by recency). Resuming the broker fetch from this
    id as ``page_token`` advances the cursor without re-fetching the full
    activity history every run — and needs no extra cursor table (no schema
    change). ``event_key`` PK idempotency still backstops any overlap.

    Returns ``None`` on a fresh DB (no prior account-activity rows) so the first
    run fetches from the broker's default window.
    """
    stmt = select(func.max(BrokerEventLogRow.event_key)).where(
        BrokerEventLogRow.event_type.in_(_LIFECYCLE_EVENT_TYPES)
    )
    max_event_key = (await handle.session.execute(stmt)).scalar_one_or_none()
    if max_event_key is None:
        return None
    return max_event_key.removeprefix(_EVENT_KEY_PREFIX)


async def run_account_activities_poll(
    handle: InvocationHandle,
    *,
    venue_config: VenueConfig | None,
    execution_mode: ExecutionMode,
    activities_source_factory: ActivitiesSourceFactory | None = None,
    after: str | None = None,
) -> PollResult:
    """Run the option-lifecycle activity poll inside the Phase-1 write transaction.

    Resolves the activities source (inline Alpaca default when
    *activities_source_factory* is ``None``) and drives
    :func:`poll_account_activities`. When *after* is not supplied, the resume
    cursor is derived from the durable ``broker_event_log`` (see
    :func:`_resolve_resume_cursor`) so each run resumes after the last booked
    activity rather than re-fetching the full history. Returns the poll result so
    the orchestrator can log the count booked.
    """
    source_factory = activities_source_factory or _default_activities_source_factory
    source = source_factory(venue_config, execution_mode)
    resume_after = after if after is not None else await _resolve_resume_cursor(handle)
    return await poll_account_activities(handle, queries=source, after=resume_after)


__all__ = ["ActivitiesSourceFactory", "run_account_activities_poll"]
