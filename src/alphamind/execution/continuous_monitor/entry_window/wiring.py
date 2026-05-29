"""Supervisor-side wiring for the entry-window expiry watcher (ALP-737).

Composes the watcher entirely from existing primitives — no new infrastructure:

* :class:`SqlPendingEntryBracketReader` — a status-keyed bracket read
  (``PENDING_ENTRY`` + non-null ``entry_window_deadline``) using the same
  row → record codec the rest of the system uses.
* :class:`AlpacaEntryCancel` — wraps the broker adapter's :func:`submit_cancel`
  and maps the three broker answers (accepted / 4xx-permanent / transient) onto
  :class:`BrokerCancelClassification`.
* :func:`make_entry_window_writeback` — opens a fresh session +
  :class:`InvocationHandle` and runs the Phase-2 ``persist_entry_window_cancel``
  writeback, mirroring the breach loop's ``make_submit_envelope`` pattern.
* :func:`register_entry_window_watcher_task` — assembles the
  :class:`BrokerEntryWindowCanceller` and registers the ``entry_window`` task.
"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.errors import classify_alpaca_error
from alphamind.execution.broker_adapter.order_modify import submit_cancel
from alphamind.execution.broker_adapter.retry import GatewaySubmissionFailed
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
    BrokerEntryWindowCanceller,
    CancelWriteback,
    EntryCancelTarget,
    EntryCancelTargetResolver,
)
from alphamind.execution.continuous_monitor.entry_window.task import (
    run_entry_window_watcher,
)
from alphamind.execution.continuous_monitor.greeks_refresh.wiring import (
    make_invocation_id_provider,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.write_paths.phase2 import persist_entry_window_cancel
from alphamind.portfolio_state.records.orders import BracketRecord, BracketStatus
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SqlPendingEntryBracketReader
# ---------------------------------------------------------------------------


class SqlPendingEntryBracketReader:
    """Reads ``PENDING_ENTRY`` brackets that carry an ``entry_window_deadline``.

    Status-keyed (``ix_brackets_status``) rather than position-keyed: a
    ``PENDING_ENTRY`` bracket's position is ``PENDING``, so the OPEN-positions
    reader the bracket-stops watcher uses never surfaces it. Opens a fresh
    session per call so it outlives any one invocation context.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_pending_entry_brackets(self) -> tuple[BracketRecord, ...]:
        async with self._session_factory() as session:
            bracket_rows = list(
                (
                    await session.execute(
                        select(BracketRow)
                        .where(BracketRow.status == BracketStatus.PENDING_ENTRY.value)
                        .where(BracketRow.entry_window_deadline.is_not(None))
                        .order_by(BracketRow.bracket_id.asc())
                    )
                ).scalars()
            )
            if not bracket_rows:
                return ()
            bracket_ids = [b.bracket_id for b in bracket_rows]
            leg_rows = list(
                (
                    await session.execute(
                        select(BracketLegRow)
                        .where(BracketLegRow.bracket_id.in_(bracket_ids))
                        .order_by(BracketLegRow.bracket_id.asc(), BracketLegRow.leg_index.asc())
                    )
                ).scalars()
            )
        legs_by_bracket: dict[str, list[BracketLegRow]] = {bid: [] for bid in bracket_ids}
        for leg in leg_rows:
            legs_by_bracket[leg.bracket_id].append(leg)
        return tuple(
            bracket_rows_to_record(b, tuple(legs_by_bracket[b.bracket_id])) for b in bracket_rows
        )


# ---------------------------------------------------------------------------
# Broker cancel adapter
# ---------------------------------------------------------------------------


# Only these 4xx codes mean the order is genuinely terminal at the broker (gone
# or not cancellable). Other 4xx — 401/403 auth, 400 malformed, 429 rate-limit —
# are NOT "already filled"; latching them as terminal would silently abandon a
# still-resting entry past its deadline, so they route to a retry instead.
_TERMINAL_CANCEL_HTTP_STATUSES = frozenset({404, 422})


class AlpacaEntryCancel:
    """Production broker-cancel callable backed by :func:`submit_cancel`.

    Maps the broker's answer onto :class:`BrokerCancelClassification`:

    * ``Submitted`` → ``CANCEL_CONFIRMED`` (the broker accepted the cancel).
    * raised 404 / 422 → ``CANCEL_CONFIRMED`` (order already terminal at the
      broker — the canceller's separate no-recorded-fills check decides whether
      that means cancelled-already vs filled).
    * raised other 4xx (auth / rate-limit / malformed) → ``RETRYABLE`` — these
      are not a fill, so do NOT latch the bracket as handled.
    * ``GatewaySubmissionFailed`` (transient retry exhaustion) → ``RETRYABLE``.
    """

    def __init__(self, *, client_factory: object, execution_config: ExecutionConfig) -> None:
        self._client_factory = client_factory
        self._execution_config = execution_config

    @property
    def _trading_client(self) -> object:
        from alpaca.trading.client import TradingClient

        return cast(TradingClient, self._client_factory.build_trading_client())  # type: ignore[attr-defined]

    async def __call__(self, alpaca_order_id: AlpacaOrderId) -> BrokerCancelClassification:
        try:
            outcome = await submit_cancel(
                client=self._trading_client,  # type: ignore[arg-type]
                execution=self._execution_config,
                target_alpaca_order_id=alpaca_order_id,
            )
        except Exception as exc:
            # submit_with_retry re-raises permanent 4xx rejections unchanged.
            # classify_alpaca_error returns a rejection for broker 4xx and None
            # for anything else (a real bug, which must propagate). Only a 404 /
            # 422 means the order is genuinely terminal; other 4xx are retried so
            # a transient auth / rate-limit hiccup does not abandon the entry.
            rejection = classify_alpaca_error(exc)
            if rejection is None:
                raise
            if rejection.http_status in _TERMINAL_CANCEL_HTTP_STATUSES:
                return BrokerCancelClassification.CANCEL_CONFIRMED
            return BrokerCancelClassification.RETRYABLE
        if isinstance(outcome, GatewaySubmissionFailed):
            return BrokerCancelClassification.RETRYABLE
        return BrokerCancelClassification.CANCEL_CONFIRMED


# ---------------------------------------------------------------------------
# Resolver + writeback callables
# ---------------------------------------------------------------------------


def make_entry_cancel_target_resolver(
    session_factory: async_sessionmaker[AsyncSession],
) -> EntryCancelTargetResolver:
    """Return a resolver mapping an entry ``order_id`` to its broker id + fill state.

    One session reads both the order's ``alpaca_order_id`` and whether any
    ``fill_records`` row exists for it (the raw fill signal the fill-stream
    consumer writes ahead of reconciliation). Returns ``None`` when the order
    row is missing.
    """

    async def _resolve(entry_order_id: str) -> EntryCancelTarget | None:
        async with session_factory() as session:
            row = await session.get(OrderRow, entry_order_id)
            if row is None:
                return None
            fill_count = (
                await session.execute(
                    select(func.count())
                    .select_from(FillRecordRow)
                    .where(FillRecordRow.order_id == entry_order_id)
                )
            ).scalar_one()
            return EntryCancelTarget(
                alpaca_order_id=AlpacaOrderId(row.alpaca_order_id),
                has_recorded_fills=fill_count > 0,
            )

    return _resolve


def make_entry_window_writeback(
    session_factory: async_sessionmaker[AsyncSession],
) -> CancelWriteback:
    """Return a writeback callable: open a fresh session + handle, run the
    Phase-2 entry-window cancel writeback, and commit.

    Mirrors the breach loop's ``make_submit_envelope`` between-invocations
    persistence pattern — the activity-log ``invocation_id`` FK references the
    most-recently-started invocation.
    """
    invocation_id_provider = make_invocation_id_provider(session_factory)

    async def _writeback(entry_order_id: str, cancel_reason: str) -> None:
        invocation_id = await invocation_id_provider()
        async with session_factory() as session:
            handle = InvocationHandle(session=session, invocation_id=invocation_id)
            await persist_entry_window_cancel(
                handle, entry_order_id=entry_order_id, cancel_reason=cancel_reason
            )
            await session.commit()

    return _writeback


# ---------------------------------------------------------------------------
# Supervisor registration
# ---------------------------------------------------------------------------


def register_entry_window_watcher_task(
    supervisor: MonitorSupervisor,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    client_factory: object,
    execution_config: ExecutionConfig,
) -> None:
    """Register the ``entry_window`` task on *supervisor*.

    Assembles the reader, the Alpaca broker-cancel callable, the entry-target
    resolver (broker id + recorded-fill state), and the Phase-2 writeback into a
    :class:`BrokerEntryWindowCanceller` — all from the shared session factory +
    client factory the other monitor tasks use.
    """
    bracket_reader = SqlPendingEntryBracketReader(session_factory)
    canceller = BrokerEntryWindowCanceller(
        resolve_target=make_entry_cancel_target_resolver(session_factory),
        broker_cancel=AlpacaEntryCancel(
            client_factory=client_factory, execution_config=execution_config
        ),
        writeback=make_entry_window_writeback(session_factory),
    )

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        await run_entry_window_watcher(
            session,
            config,
            bracket_reader=bracket_reader,
            canceller=canceller,
        )

    supervisor.register_task(name="entry_window", coro_fn=_coro)


__all__ = [
    "AlpacaEntryCancel",
    "SqlPendingEntryBracketReader",
    "make_entry_cancel_target_resolver",
    "make_entry_window_writeback",
    "register_entry_window_watcher_task",
]
