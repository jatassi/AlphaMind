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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    fill_report_to_fill_record,
    order_id_for_report,
    terminal_order_status_for,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.execution.write_paths.order_status_sync import (
    sync_terminal_order_status,
)
from alphamind.state.records import FillRecord
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow

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
# Per ALP-528 (paper-evaluation harness wedge): paper-mode wiring passes a
# callable that enriches each translated FillRecord with a
# ``live_execution_estimate`` before persistence. Live-mode passes ``None`` so
# the hot path is unchanged.
EnrichmentCallable = Callable[[FillRecord], Awaitable[FillRecord]]


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
            await _persist_one(
                report,
                session_factory=session_factory,
                enrichment_callable=enrichment_callable,
            )
    finally:
        await gen.aclose()


async def _persist_one(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None,
) -> None:
    """Translate a single ``FillReport`` and append it in its own transaction.

    Paper-mode wiring (per ALP-528) injects ``enrichment_callable`` so each
    translated :class:`FillRecord` is enriched with a
    ``live_execution_estimate`` before persistence. Live mode passes ``None``;
    the column persists as NULL and the hot path is unchanged.
    """
    record = fill_report_to_fill_record(report)
    log.debug(
        "fill report received: event_type=%s client_order_id=%s fill_timestamp=%s",
        report.event_type,
        report.client_order_id,
        report.fill_timestamp.isoformat(),
    )
    if record is None:
        await _sync_terminal_status_if_any(report, session_factory=session_factory)
        return
    async with session_factory() as db:
        # Resolve the local ``orders`` PK this fill applies to. The broker's
        # client_order_id does not round-trip the OMS order id for equity
        # entries (it carries the command id) or for native-bracket protective
        # children (Alpaca generates it), so fall back to the captured broker
        # UUID (ALP-746). Skip (with a loud log) when no local order matches —
        # the fill_records.order_id FK would otherwise reject the insert.
        oms_order_id = await _resolve_oms_order_id(db, report)
        if oms_order_id is None:
            log.warning(
                "fill references no local order: client_order_id=%s alpaca_order_id=%s "
                "event=%s — skipping (no orders row to attribute it to)",
                report.client_order_id,
                report.alpaca_order_id,
                report.event_type,
            )
            return
        if oms_order_id != record.order_id:
            # Re-derive so ``order_id`` + ``fill_id`` reflect the resolved PK.
            record = fill_report_to_fill_record(report, oms_order_id=oms_order_id)
            if record is None:  # pragma: no cover — gates are identical to the first call
                return
        if enrichment_callable is not None:
            record = await enrichment_callable(record)
        await append_fill_record(db, record)
        await db.commit()


async def _sync_terminal_status_if_any(
    report: FillReport,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reflect a broker terminal non-fill event in ``orders.status`` (ALP-739).

    ``canceled`` / ``expired`` events append no fill but must update the local
    order row — otherwise an accepted entry that expires / cancels unfilled
    stays ``PENDING`` and the ``entry_no_fill`` alert never fires. Every other
    non-fill event (``new`` / ``replaced`` / …) carries no terminal
    disposition and no-ops here. Its own short-lived transaction, mirroring
    the per-fill write.

    Scoped to **zero-fill** terminals (``cumulative_filled_quantity == 0``): a
    partially-filled-then-terminal order is left to the fill path + Phase 1,
    which own ``filled_quantity`` and integrate the partials. Stamping a
    terminal status here for a partially-filled order would (a) read
    ``filled_quantity == 0`` until Phase 1 catches up and fire a false
    no-fill alert, and (b) be reverted to ``PARTIALLY_FILLED`` by Phase 1's
    fill integration anyway. The no-fill case is the one the fill path does
    not cover, so it is the only one this sync owns.
    """
    terminal_status = terminal_order_status_for(report)
    if terminal_status is None:
        return
    if report.cumulative_filled_quantity > 0:
        return
    async with session_factory() as db:
        # Resolve by broker UUID when the client_order_id doesn't name a local
        # PK — this is the path an OCO sibling-cancel takes (the broker cancels
        # the unfired protective leg, whose client_order_id Alpaca generated;
        # only the captured leg UUID locates the local row). ALP-746.
        order_id = await _resolve_oms_order_id(db, report)
        if order_id is None:
            log.debug(
                "terminal status for unknown order: client_order_id=%s alpaca_order_id=%s "
                "status=%s — skipping",
                report.client_order_id,
                report.alpaca_order_id,
                terminal_status.value,
            )
            return
        transitioned = await sync_terminal_order_status(
            db,
            order_id=order_id,
            terminal_status=terminal_status,
            observed_at=datetime.now(UTC),
        )
        await db.commit()
    if transitioned:
        log.info(
            "synced terminal order status: order_id=%s status=%s",
            order_id,
            terminal_status.value,
        )


async def _resolve_oms_order_id(db: AsyncSession, report: FillReport) -> str | None:
    """Resolve the local ``orders`` PK a fill / terminal event applies to.

    Two-step resolution (ALP-746):

    1. Treat the report-derived id (``parent_client_order_id or client_order_id``)
       as a candidate PK — the historical / already-aligned path (and the only
       path the test substrate exercises by hand-aligning the two).
    2. Otherwise resolve by the broker UUID of the order that owns the local
       row: for an mleg per-leg child that is the parent's
       ``parent_alpaca_order_id`` (legs do not own ``orders`` rows); for an
       equity entry / close / native-bracket protective child it is the report's
       own ``alpaca_order_id``. The captured-at-submission UUID was written onto
       that row (entry / close / TAKE_PROFIT / PRICE_STOP), so the lookup hits.

    Returns ``None`` when neither resolves — the caller declines to attribute
    the event rather than violate the ``fill_records.order_id`` FK.
    """
    candidate_pk = order_id_for_report(report)
    row = await db.get(OrderRow, candidate_pk)
    if row is not None:
        return row.order_id
    uuid_key = report.parent_alpaca_order_id or report.alpaca_order_id
    stmt = select(OrderRow).where(OrderRow.alpaca_order_id == uuid_key)
    row = (await db.execute(stmt)).scalars().first()
    return row.order_id if row is not None else None


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
        await _persist_one(
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
