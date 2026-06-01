"""Supervisor wiring for the periodic fill-backfill backstop (ALP-763).

:func:`register_fill_backfill_task` is the composition root the monitor
``__main__`` calls during startup. It binds the production substrate — the
alpaca-py trading-client / account-state-queries factories, the SQLAlchemy
session factory, and the paper-mode enrichment callable — into the
``(session, config)`` closure shape the supervisor expects, then registers the
resulting coroutine factory under the name ``fill_backfill``.

Mirrors :func:`register_borrow_accrual_task` and the fill consumer's
``_register_fill_stream_consumer`` closure: each injected production-substrate
factory is a small, independently-testable constructor. The factories are the
same ones the fill consumer uses, so the backstop sweeps the identical broker
surface through the identical persist path.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.continuous_monitor.activities_backfill.task import (
    EnrichmentCallable,
    run_fill_backfill,
)
from alphamind.execution.continuous_monitor.session import MonitorMode, MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor


def register_fill_backfill_task(
    supervisor: MonitorSupervisor,
    *,
    venue_config: VenueConfig,
    db_session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
) -> None:
    """Register the ``fill_backfill`` task on *supervisor* (ALP-763).

    The supervisor's task signature is ``(session, config) -> Awaitable[None]``
    — extras flow through this closure, which pre-binds the alpaca-py factories
    (built from *venue_config* per mode, exactly like the fill consumer) and
    the SQLAlchemy session factory.

    Paper-mode wiring passes a non-None ``enrichment_callable`` so each
    recovered + integrated ``FillRecord`` is enriched with a
    ``LiveExecutionEstimate`` before persistence, matching the fill consumer.
    Live mode passes ``None``.
    """
    from alpaca.trading.client import TradingClient

    from alphamind.execution.broker_adapter import AccountStateQueries, AlpacaClientFactory

    def _trading_client_factory(monitor_mode: MonitorMode) -> TradingClient:
        return AlpacaClientFactory(venue_config, monitor_mode).build_trading_client()

    def _queries_factory(client: object) -> AccountStateQueries:
        return AccountStateQueries(cast(TradingClient, client))

    async def _fill_backfill_task(s: MonitorSession, c: ContinuousMonitorConfig) -> None:
        await run_fill_backfill(
            s,
            c,
            session_factory=db_session_factory,
            trading_client_factory=_trading_client_factory,
            account_state_queries_factory=_queries_factory,
            enrichment_callable=enrichment_callable,
        )

    supervisor.register_task(name="fill_backfill", coro_fn=_fill_backfill_task)


__all__ = ["register_fill_backfill_task"]
