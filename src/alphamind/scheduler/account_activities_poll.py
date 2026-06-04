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

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.account_activities.poll import (
    AccountActivitiesSource,
    PollResult,
    poll_account_activities,
)
from alphamind.execution.broker_adapter.client_factory import AlpacaClientFactory
from alphamind.execution.broker_adapter.queries import AccountStateQueries
from alphamind.state.invocation_context.context import InvocationHandle

ActivitiesSourceFactory = Callable[
    [VenueConfig | None, ExecutionMode], AccountActivitiesSource
]


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
    mode_literal = "live" if execution_mode is ExecutionMode.live else "paper"
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return AccountStateQueries(factory.build_trading_client())


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
    :func:`poll_account_activities`. Returns the poll result so the orchestrator
    can log the count booked and advance the resume cursor.
    """
    source_factory = activities_source_factory or _default_activities_source_factory
    source = source_factory(venue_config, execution_mode)
    return await poll_account_activities(handle, queries=source, after=after)


__all__ = ["ActivitiesSourceFactory", "run_account_activities_poll"]
