"""Durable pre-broker ``orders`` row for a monitor-fired bracket close (FS4 / ALP-836).

A monitor-fired bracket close submits to the broker directly (no engine envelope,
no ``CloseCommand`` writeback), so historically it left NO local ``orders`` row.
When the close fill returned it self-attributed to ``broker_event_log`` via the
broker-carried link, but ``persist_fill_report`` only appends a ``fill_records``
row when an ``oms_order_id`` resolves — and with no ``orders`` row none did. Phase 1
closes positions by integrating UNPROCESSED ``fill_records``, so the closing fill
was never integrated and the position stayed OPEN after the broker filled the exit
(the phantom-open class).

This module mirrors the entry-side ALP-836 atomicity-first pattern: persist a
durable close ``orders`` row keyed by the broker ``client_order_id`` BEFORE the
broker submit, carrying NO broker id (``alpaca_order_id`` NULL, ALP-847) and
status ``PENDING_SUBMIT``. The returning close fill then resolves an
``oms_order_id`` through the existing ``_resolve_oms_order_id``
client_order_id-keyed path, ``persist_fill_report`` appends the ``fill_records``
row, and Phase 1 integrates it and closes the position.

The write is idempotent on the ``client_order_id``: a retried fire (the leg
re-prepares after a transient pre-submit failure) finds the existing row and is a
no-op, so it never PK-collides or doubles the durable intent.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import ClientOrderId
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    CloseOrderPrecommitter,
    FloorAlpacaIdResolver,
)
from alphamind.execution.write_paths.command_execution._shared import (
    _build_pending_order,
    _close_order_direction_for_position,
    _instrument_spec_for_position,
    _position_quantity,
)
from alphamind.execution.write_paths.command_execution.close import _close_order_id
from alphamind.persistence.retry import run_with_sqlite_busy_retry
from alphamind.persistence.session import begin_write_immediate
from alphamind.portfolio_state.records.orders import (
    OrderClass,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    StrategyPositionDetails,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import record_to_row

logger = logging.getLogger(__name__)


async def precommit_monitor_close_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    position: PositionRecord,
    client_order_id: str,
) -> None:
    """Commit the durable close ``orders`` row for a monitor-fired close (FS4).

    Builds a minimal market CLOSE order against *position* keyed by
    *client_order_id* (= the engine client_order_id the closer wove the
    broker-carried link into), in ``PENDING_SUBMIT`` with NO broker id, and
    commits it in its own transaction BEFORE the broker submit. Idempotent on the
    ``client_order_id`` — an existing row means a prior (retried) fire already
    pre-committed it, so this is a no-op.
    """
    order_id = _close_order_id(position.position_id, client_order_id)

    async def _write() -> None:
        async with session_factory() as session:
            await begin_write_immediate(session)
            existing = (
                await session.execute(
                    select(OrderRow.order_id).where(OrderRow.client_order_id == client_order_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            session.add(_build_monitor_close_order_row(position, order_id, client_order_id))
            await session.commit()

    await run_with_sqlite_busy_retry(_write)
    logger.debug(
        "bracket_stops: pre-committed durable close order %s (client_order_id=%s) for position %s",
        order_id,
        client_order_id,
        position.position_id,
    )


def _build_monitor_close_order_row(
    position: PositionRecord, order_id: str, client_order_id: str
) -> OrderRow:
    """Build the durable PENDING_SUBMIT close ``OrderRow`` keyed by client_order_id."""
    timestamp = datetime.now(UTC)
    order_class = (
        OrderClass.MLEG
        if isinstance(position.details, StrategyPositionDetails)
        else OrderClass.SIMPLE
    )
    if position.bracket_id is None:
        msg = (
            f"Cannot build monitor close order for position {position.position_id!r}: "
            "bracket_id is None.  A monitor-fired close always fires because a "
            "protective bracket leg fired, so every live monitor-close position must "
            "carry a real bracket_id.  Inserting an empty string would cause a "
            "deferred FK violation at commit (brackets.bracket_id='')."
        )
        raise ValueError(msg)
    record = _build_pending_order(
        order_id=order_id,
        position_id=position.position_id,
        bracket_id=position.bracket_id,
        role=OrderRole.CLOSE,
        order_class=order_class,
        direction=_close_order_direction_for_position(position),
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(),
        instrument_spec=_instrument_spec_for_position(position),
        pm_command_id=client_order_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        quantity=_position_quantity(position),
    )
    # ALP-836 — durable Intent: keyed by client_order_id, no broker id yet
    # (alpaca_order_id NULL, ALP-847), PENDING_SUBMIT until the fill / reconcile
    # confirms it reached the broker.
    record = dataclasses.replace(
        record,
        status=OrderStatus.PENDING_SUBMIT,
        client_order_id=ClientOrderId(client_order_id),
    )
    return record_to_row(record)


def make_close_order_precommitter(
    session_factory: async_sessionmaker[AsyncSession],
) -> CloseOrderPrecommitter:
    """Bind :func:`precommit_monitor_close_order` to *session_factory*.

    Returns the ``(position, client_order_id)`` callable the closer's pre-submit
    step invokes; production wiring passes it into
    :func:`register_options_bracket_watcher_task`.
    """

    async def _precommit(position: PositionRecord, client_order_id: str) -> None:
        await precommit_monitor_close_order(
            session_factory, position=position, client_order_id=client_order_id
        )

    return _precommit


def make_floor_alpaca_id_resolver(
    session_factory: async_sessionmaker[AsyncSession],
) -> FloorAlpacaIdResolver:
    """Bind an ``order_id`` → floor ``alpaca_order_id`` resolver to *session_factory*.

    The capital floor is a tracked broker order whose durable OrderRow carries the
    broker id (ALP-856 / FS4); the floor bracket leg's ``order_id`` points at that
    OrderRow (the FK target), NOT the alpaca id. Cancel-on-monitor-fire resolves
    the resting Alpaca order's id through this lookup. Returns ``None`` when no
    OrderRow resolves or its broker-id backfill hasn't landed yet (the closer then
    skips the cancel). Reads on a fresh session so it can outlive any one
    invocation context (the monitor runs across invocations).
    """

    async def _resolve(order_id: str) -> str | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(OrderRow.alpaca_order_id).where(OrderRow.order_id == order_id)
                )
            ).scalar_one_or_none()

    return _resolve


__all__ = [
    "make_close_order_precommitter",
    "make_floor_alpaca_id_resolver",
    "precommit_monitor_close_order",
]
