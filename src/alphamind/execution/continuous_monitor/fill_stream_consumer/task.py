"""Run-forever fill-stream consumer task (story 02c / ALP-435).

Wraps three existing primitives in a long-lived asyncio task:

* :func:`subscribe_trade_updates` (ALP-384) — websocket stream of
  :class:`FillReport` events;
* :func:`recover_missed_fills_since` (ALP-389) — GET-based recovery routine
  for the gap between the last websocket event and reconnect;
* :func:`append_fill_record` (ALP-119) — durable, idempotent append.

The task owns the lifecycle: connect, drain, persist, reconnect with
exponential backoff bounded by ``ContinuousMonitorConfig.max_reconnect_attempts``,
and clean shutdown on ``asyncio.CancelledError``. Per-fill writes use their
own short-lived transaction (one row, no batching) so a crash mid-loop never
leaves an inconsistent ledger.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter import (
    FillReport,
    recover_missed_fills_since,
    subscribe_trade_updates,
)
from alphamind.execution.broker_adapter.client_factory import ExecutionMode
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    EnrichmentCallable,
    persist_fill_report,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.unattributed_drain import (
    drain_unattributed_fills,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.state.tables.fill_records import FillRecordRow

log = logging.getLogger(__name__)

# Type aliases for the factory parameters. The trading-stream and trading-
# client factories are mode-keyed builders that mint fresh alpaca-py objects.
# The account-state-queries factory wraps a trading-client into the protocol
# the recovery routine consumes — a separate seam so tests can swap in a
# minimal stub without going through ``AccountStateQueries``' constructor
# isinstance check.
TradingStreamFactory = Callable[[ExecutionMode], object]
TradingClientFactory = Callable[[ExecutionMode], object]
AccountStateQueriesFactory = Callable[[object], object]


async def run_fill_stream_consumer(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stream_factory: TradingStreamFactory,
    trading_client_factory: TradingClientFactory,
    account_state_queries_factory: AccountStateQueriesFactory,
    enrichment_callable: EnrichmentCallable | None = None,
) -> None:
    """Run-forever fill-stream consumer.

    Lifecycle per the story spec:

    1. Build a trading stream and REST queries surface via the factories.
    2. On startup, if prior fills exist in ``fill_records``, run
       :func:`recover_missed_fills_since` with ``since=<latest fill ts>`` to
       catch up on the gap since the last shutdown.
    3. Open :func:`subscribe_trade_updates` and drain events: translate each
       to a :class:`FillRecord` and ``append_fill_record`` it. Persistence
       is idempotent on the dedupe key so live + recovery overlap collapses
       to a single row.
    4. On websocket exception, run recovery again with the latest in-DB
       timestamp, then sleep exponentially-backed-off and reconnect.
    5. On ``asyncio.CancelledError``, exit cleanly.
    """
    queries = account_state_queries_factory(trading_client_factory(session.mode))

    attempt = 0
    while True:
        # Opportunistic recovery of quarantined fills (ALP-763): a fill parked
        # because its ``orders`` row had not yet committed integrates as soon as
        # that row exists. Run it each reconnect cycle alongside REST recovery.
        # A drain error must not crash the consumer loop — matching the
        # reconnect-supervisor tolerance below.
        try:
            await drain_unattributed_fills(
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
            )
        except Exception:
            log.exception("unattributed-fill drain failed; continuing")

        # Startup + post-disconnect recovery: replay missed events from REST.
        since = await _latest_fill_timestamp(session_factory)
        if since is not None:
            await _replay_recovery(
                queries,
                since=since,
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
            )

        try:
            await _consume_stream(
                stream_factory(session.mode),
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
            )
            # ``_consume_stream`` is run-forever; a clean return is treated
            # the same as a failure (a buggy stream that closes immediately
            # must not loop without consuming reconnect budget).
        except asyncio.CancelledError:
            log.info("fill_stream_consumer cancelled cleanly")
            raise
        except Exception:
            # Reconnect-budget supervisor per runtime §G1: any websocket /
            # downstream failure counts an attempt; on exhaustion we re-raise
            # so the supervisor exits the task. ``BaseException``
            # (``CancelledError``) re-raised above for clean shutdown.
            attempt += 1
            log.exception(
                "fill_stream_consumer websocket failure (attempt %d / %d)",
                attempt,
                config.max_reconnect_attempts,
            )
            if attempt >= config.max_reconnect_attempts:
                raise
        else:
            # Clean return — count as a reconnect cycle and continue.
            attempt += 1
            if attempt >= config.max_reconnect_attempts:
                msg = (
                    "fill_stream_consumer exhausted reconnect budget "
                    f"({config.max_reconnect_attempts} attempts) on clean closes"
                )
                raise RuntimeError(msg)
        await asyncio.sleep(_backoff_seconds(attempt))


async def _consume_stream(
    stream: object,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
) -> None:
    """Drain :func:`subscribe_trade_updates` until the generator exits.

    Explicit ``try/finally`` with ``aclose()`` so a propagating exception
    (e.g., translation error) triggers the primitive's ``run_task.cancel()``
    in deterministic order, rather than relying on async-generator GC.
    """
    # ``subscribe_trade_updates`` accepts any object honouring the
    # ``_SubscribableStream`` Protocol; the cast keeps mypy quiet without
    # coupling to the alpaca-py class.
    gen = subscribe_trade_updates(stream)  # type: ignore[arg-type]
    try:
        async for report in gen:
            await persist_fill_report(
                report,
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
            )
    finally:
        await gen.aclose()


async def _replay_recovery(
    queries: object,
    *,
    since: datetime,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
) -> None:
    """Drain :func:`recover_missed_fills_since` and persist every report."""
    # The queries object honours the recovery primitive's ``_OrdersSource``
    # Protocol (``get_orders`` async-iterator); cast through ``object`` so
    # we don't pin to ``AccountStateQueries`` and can swap in a stub.
    gen: AsyncIterator[FillReport] = recover_missed_fills_since(queries, since=since)  # type: ignore[arg-type]
    async for report in gen:
        await persist_fill_report(
            report,
            session_factory=session_factory,
            enrichment_callable=enrichment_callable,
        )


async def _latest_fill_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> datetime | None:
    """Return the most-recent ``fill_timestamp`` in ``fill_records`` or ``None``.

    Used as the ``since`` bound for startup + post-disconnect recovery. On a
    fresh DB (no prior fills) returns ``None`` so the recovery routine is
    skipped — recovery requires a non-``None`` ``since`` bound and there's
    no valid lookback on first boot.
    """
    async with session_factory() as db:
        result = await db.execute(
            select(FillRecordRow.fill_timestamp)
            .order_by(FillRecordRow.fill_timestamp.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
    if latest is None:
        return None
    parsed = datetime.fromisoformat(latest)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s, 8s, … capped at 30s.

    A short cap keeps the monitor responsive on Alpaca's typical 10-30s
    websocket flap; longer outages need NSSM-level recovery anyway.
    """
    base = float(2 ** (attempt - 1))
    return min(base, 30.0)
