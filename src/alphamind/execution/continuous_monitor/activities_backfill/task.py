"""Periodic fill-backfill backstop (ALP-763).

A PM- or engine-originated fill self-attributes off its broker-carried link
(ADR-0002), so it no longer depends on a committed ``orders`` row — the prior
PM fast-fill race is closed. What this backstop now catches is the link-LESS
native-bracket protective child: its ``client_order_id`` is Alpaca-generated, so
it attributes only through the order-row projection cache, and a fill that
arrives before that row's position→thesis edge commits is quarantined (B1) until
the edge lands. The websocket disconnect-recovery
(:func:`recover_missed_fills_since`) only runs on a websocket RECONNECT and
keys its ``since`` off ``max(fill_records.fill_timestamp)`` — which, once a
LATER fill lands, permanently excludes the earlier quarantined fill.

This task is the backstop: a sweep that runs ON AN INTERVAL (no disconnect
needed) with an INDEPENDENT, generous lookback bound (``now - lookback``,
NOT max-fill-timestamp), feeding any Alpaca fill missing from ``fill_records``
through the normal persist path and then draining the unattributed-fills queue.

It REUSES the tested primitives rather than inventing a parallel translator:

* :func:`recover_missed_fills_since` (ALP-389) — the get_orders → OrderSnapshot
  → FillReport recovery routine;
* :func:`persist_fill_report` (the shared persist entry) — self-attribute via
  the broker-carried link → append to the event log (+ ``fill_records`` when an
  order row resolves), only quarantining a genuinely out-of-band fill; never
  drops;
* :func:`drain_unattributed_fills` — integrate any queued fill whose order row
  now exists.

The event-log + ``fill_records`` idempotency (``event_key`` / dedupe key) makes
re-feeding an already-captured fill a no-op, so the generous lookback window
costs only redundant reads, never duplicate rows.

The loop owns the run-forever lifecycle: a sweep error logs and continues to
the next interval (matching the reconnect-supervisor tolerance in the fill
consumer); ``asyncio.CancelledError`` exits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter import FillReport, recover_missed_fills_since
from alphamind.execution.broker_adapter.client_factory import ExecutionMode
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    EnrichmentCallable,
    persist_fill_report,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.unattributed_drain import (
    drain_unattributed_fills,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import SupervisedLoop

log = logging.getLogger(__name__)

# Factory aliases mirror ``fill_stream_consumer.task`` so the wiring reuses the
# same seams: a mode-keyed trading-client builder and an account-state-queries
# wrapper over it. The recovery primitive consumes only the queries' async
# ``get_orders`` (its ``_OrdersSource`` Protocol), so tests swap in a stub.
TradingClientFactory = Callable[[ExecutionMode], object]
AccountStateQueriesFactory = Callable[[object], object]
NowProvider = Callable[[], datetime]


async def run_fill_backfill(  # noqa: PLR0913 — composition root; each kw-arg is one injected seam
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    trading_client_factory: TradingClientFactory,
    account_state_queries_factory: AccountStateQueriesFactory,
    loop: SupervisedLoop,
    enrichment_callable: EnrichmentCallable | None = None,
    process_lifetime_id: str | None = None,
    now: NowProvider = lambda: datetime.now(UTC),
) -> None:
    """Run-forever periodic fill-backfill backstop.

    Lifecycle per the story spec:

    1. Build the queries surface via the same factory pattern the fill
       consumer uses (``account_state_queries_factory(trading_client_factory(mode))``).
    2. Drive *loop* (the supervisor's :meth:`supervised_loop` iterator, with the
       ``fill_backfill_interval_seconds`` cadence pre-bound): each iteration
       beats the watchdog at the top, runs one sweep, then the seam paces the
       loop — so the task is liveness-watched with no hand-wired ``beat()`` and
       no trailing ``sleep``.
    3. Each sweep computes an INDEPENDENT ``since = now - lookback`` (NOT
       max-fill-timestamp), recovers every Alpaca fill in the window through
       :func:`persist_fill_report`, then drains the unattributed-fills queue.
    4. A sweep error logs and continues to the next interval — it must not
       crash the task. ``asyncio.CancelledError`` propagates for clean
       shutdown.
    """
    queries = account_state_queries_factory(trading_client_factory(session.mode))
    lookback = timedelta(seconds=config.fill_backfill_lookback_seconds)
    escalation_ttl_seconds = config.unattributed_fill_escalation_ttl_seconds

    async for _ in loop():
        try:
            await _run_sweep(
                queries,
                lookback=lookback,
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
                process_lifetime_id=process_lifetime_id,
                escalation_ttl_seconds=escalation_ttl_seconds,
                now=now,
            )
        except asyncio.CancelledError:
            log.info("fill_backfill cancelled cleanly")
            raise
        except Exception:
            # A sweep failure (broker outage, transient DB error) must not
            # crash the backstop — log and retry on the next interval, matching
            # the fill consumer's reconnect-supervisor tolerance.
            log.exception("fill_backfill sweep failed; retrying next interval")


async def _run_sweep(
    queries: object,
    *,
    lookback: timedelta,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
    process_lifetime_id: str | None,
    escalation_ttl_seconds: int,
    now: NowProvider,
) -> None:
    """Run one backfill sweep: recover missing fills, then drain the queue.

    The ``since`` bound is independent of ``fill_records`` state — a generous
    ``now - lookback`` so a fill dropped earlier in the swing-trading horizon
    is still in-window. Each recovered ``FillReport`` flows through the shared
    :func:`persist_fill_report`, which self-attributes via the broker-carried
    link and appends to the event log (dedupe collapses re-feeds of
    already-captured fills), only quarantining a genuinely out-of-band fill that
    carries no link. The trailing :func:`drain_unattributed_fills` then
    integrates any previously-quarantined fill whose order row has since
    materialized into ``fill_records``.
    """
    until = now()
    since = until - lookback
    # ``recover_missed_fills_since`` honours the ``_OrdersSource`` Protocol
    # (async ``get_orders``); cast through ``object`` so we don't pin to
    # ``AccountStateQueries`` and can swap in a stub.
    gen: AsyncIterator[FillReport] = recover_missed_fills_since(queries, since=since, until=until)  # type: ignore[arg-type]
    async for report in gen:
        await persist_fill_report(
            report,
            session_factory=session_factory,
            enrichment_callable=enrichment_callable,
        )
    await drain_unattributed_fills(
        session_factory=session_factory,
        enrichment_callable=enrichment_callable,
        process_lifetime_id=process_lifetime_id,
        escalation_ttl_seconds=escalation_ttl_seconds,
        now=now,
    )


__all__ = [
    "AccountStateQueriesFactory",
    "EnrichmentCallable",
    "TradingClientFactory",
    "run_fill_backfill",
]
