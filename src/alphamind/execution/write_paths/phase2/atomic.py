"""Atomicity-first per-command persistence for the broker-active path (ALP-836).

The broker-active PM turn must commit a durable ``orders`` row **before** it
dispatches the order to Alpaca, so a lost post-submit commit can never strand a
live broker order with no local record (the GS husk class). This module hosts the
three per-command steps the dispatch loop interleaves, each in its **own**
committed transaction on a fresh session (the caller wraps each in
``begin_write_immediate`` + ``run_with_sqlite_busy_retry`` so a transient
``SQLITE_BUSY_SNAPSHOT`` waits/retries instead of silently rolling back — the
Phase-2 extension of the ALP-824 fix):

* :func:`precommit_command` — (A) durable pre-broker intent. Writes the command's
  full local graph (OPEN: position + thesis + bracket + protective legs + entry;
  CLOSE/ADD/ADJUST: the order against an existing position) with the **dispatched**
  order in ``PENDING_SUBMIT`` carrying NO broker id (``alpaca_order_id`` NULL,
  ALP-847) and ``client_order_id = command_id``, and reserves capital. Idempotent on
  ``client_order_id`` — a replay finds the existing row and re-reserves nothing.
* :func:`backfill_command_broker_ids` — (C) after a successful dispatch, stamp the
  real ``alpaca_order_id`` (+ ALP-746 native-bracket leg ids) and flip the
  dispatched order ``PENDING_SUBMIT`` → ``PENDING``.
* :func:`abandon_command` — (F) on a broker rejection, transition the
  pre-committed dispatched order to ``CANCELLED`` and release any reserved
  capital. For an OPEN this tears the whole pre-committed graph down (bracket
  dissolved, never-filled position CANCELLED, thesis resolved) by reusing the
  CANCEL writeback state machine.

CANCEL has no dispatched ``orders`` row to pre-commit (it cancels an existing
order at the broker), so it is handled by the dispatch loop on the legacy
post-dispatch path, not here.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import OrderId
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
)
from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
from alphamind.execution.write_paths.phase2 import (
    _dispatch_command_writeback,
    _emit_pm_decision,
    persist_command_abandoned,
)
from alphamind.execution.write_paths.phase2._shared import _instrument_ticker_key
from alphamind.execution.write_paths.phase2.add import _add_order_id
from alphamind.execution.write_paths.phase2.adjust import _adjust_replacement_order_id
from alphamind.execution.write_paths.phase2.cancel import _writeback_cancel
from alphamind.execution.write_paths.phase2.close import _close_order_id
from alphamind.execution.write_paths.phase2.open import _new_open_ids
from alphamind.persistence.session import begin_write_immediate
from alphamind.portfolio_state.records.orders import OrderRole, OrderStatus
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.orders import OrderRow

logger = logging.getLogger(__name__)

# Native-bracket protective-leg role → the ``leg_alpaca_order_ids`` key the broker
# dispatch returns (ALP-746). Mirrors ``open._build_protective_orders``: Alpaca's
# native bracket carries exactly one take-profit child and one stop child.
_LEG_ROLE_TO_DISPATCH_KEY: dict[str, str] = {
    OrderRole.TAKE_PROFIT.value: "take_profit",
    OrderRole.PRICE_STOP.value: "stop_loss",
}


def session_factory_from_handle(
    handle: InvocationHandle,
) -> async_sessionmaker[AsyncSession]:
    """Build a fresh ``async_sessionmaker`` bound to *handle*'s async engine.

    The atomicity-first steps each need their OWN committed transaction on a fresh
    session (so ``run_with_sqlite_busy_retry`` can re-open a clean session per
    attempt), independent of the long-lived ``handle.session`` the PM turn uses
    for broker-routing reads. ``handle.session.bind`` is the async engine the
    subprocess worker built (with the ALP-824 BEGIN-mode hooks), so a sessionmaker
    rebuilt from it supports ``begin_write_immediate``.
    """
    bind = handle.session.bind
    return async_sessionmaker(bind=bind, expire_on_commit=False)


def dispatched_order_id(command: OMSCommand, *, command_id: str) -> str | None:
    """The ``orders.order_id`` of the single order *command* dispatches to the broker.

    This is the row that carries ``client_order_id`` and receives the broker's
    real ``alpaca_order_id``. Returns ``None`` for CANCEL (no order row). Reuses
    each per-command module's own id helper so the derivation never drifts from
    the builder that mints the row.
    """
    if isinstance(command, OpenCommand):
        ticker = _instrument_ticker_key(command.instrument)
        return _new_open_ids(ticker, command_id=command_id)["entry_order_id"]
    if isinstance(command, CloseCommand):
        return _close_order_id(command.position_id, command_id)
    if isinstance(command, AddCommand):
        return _add_order_id(command.position_id, command_id)
    if isinstance(command, AdjustCommand):
        return _adjust_replacement_order_id(command.position_id, command_id)
    return None


async def precommit_command(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    command: OMSCommand,
    result: SubmissionResult,
) -> bool:
    """(A) Commit the durable pre-broker intent for *command* in its own transaction.

    Returns ``True`` if a durable row keyed by ``client_order_id`` exists on
    return (newly committed, or already present from a prior replay) — the
    dispatch guard. Returns ``False`` only when the command has no dispatched
    order (it never reaches here for CANCEL; the guard is defensive).

    Idempotent: if a row with this ``client_order_id`` already exists the whole
    writeback is skipped — no duplicate order row, no double capital reservation.
    """
    oid = dispatched_order_id(command, command_id=result.command_id)
    if oid is None:
        return False

    async with session_factory() as session:
        await begin_write_immediate(session)
        # Idempotency: an existing row for this client_order_id means a prior
        # (replayed) pre-commit already landed — re-running would PK-collide on
        # the graph and double the (non-idempotent) capital reservation.
        existing = (
            await session.execute(
                select(OrderRow.order_id).where(OrderRow.client_order_id == result.command_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return True

        handle = InvocationHandle(session=session, invocation_id=invocation_id)
        # Build the full local graph with NO broker ids yet (ALP-847 — the real
        # alpaca_order_id is backfilled on dispatch; no synthetic placeholder).
        await _dispatch_command_writeback(
            handle, command=command, result=result, dispatch_result=None
        )
        await session.flush()
        # Mark the single dispatched order as durable-but-not-yet-at-broker and
        # stamp the client_order_id the broker fill will carry.
        row = await session.get(OrderRow, oid)
        if row is None:
            msg = (
                f"precommit_command: expected dispatched order {oid!r} after writeback of "
                f"{type(command).__name__}; none found — cannot mark PENDING_SUBMIT"
            )
            raise RuntimeError(msg)
        row.status = OrderStatus.PENDING_SUBMIT.value
        row.client_order_id = result.command_id
        await session.commit()
    return True


async def backfill_command_broker_ids(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    command: OMSCommand,
    result: SubmissionResult,
    dispatch_result: BrokerDispatchResult,
) -> None:
    """(C) Stamp the real broker ids and flip the dispatched order to PENDING.

    Resolves the pre-committed row by ``client_order_id`` and writes the broker's
    real ``alpaca_order_id`` + single-element chain, flipping ``PENDING_SUBMIT`` →
    ``PENDING``. For an OPEN, also stamps the ALP-746 native-bracket protective
    children (take-profit / first stop) with their real ids. Its own transaction.
    """
    oid = dispatched_order_id(command, command_id=result.command_id)
    if oid is None:
        return
    now = datetime.now(UTC).isoformat()
    async with session_factory() as session:
        await begin_write_immediate(session)
        row = (
            await session.execute(
                select(OrderRow).where(OrderRow.client_order_id == result.command_id)
            )
        ).scalar_one_or_none()
        if row is None:
            # The pre-commit row vanished (it never landed). Nothing to backfill;
            # the reconcile-by-client_order_id pass is the recovery backstop.
            return
        real = str(dispatch_result.alpaca_order_id)
        row.alpaca_order_id = real
        row.alpaca_order_id_chain_json = json.dumps([real])
        row.status = OrderStatus.PENDING.value
        row.last_update_timestamp = now

        if isinstance(command, OpenCommand) and dispatch_result.leg_alpaca_order_ids:
            await _backfill_open_leg_ids(
                session,
                bracket_id=row.bracket_id,
                leg_alpaca_order_ids=dict(dispatch_result.leg_alpaca_order_ids),
                now=now,
            )
        await session.commit()


async def _backfill_open_leg_ids(
    session: AsyncSession,
    *,
    bracket_id: str,
    leg_alpaca_order_ids: dict[str, str],
    now: str,
) -> None:
    """Stamp native-bracket protective children with their real broker ids (ALP-746).

    Take-profit → the bracket's TAKE_PROFIT order; stop-loss → the first
    PRICE_STOP order (Alpaca's native bracket submits exactly one stop child,
    mapped from the first PriceLeg). Legs with no broker counterpart (TIME_STOP,
    advisory EVENT legs, any PRICE_STOP beyond the first) keep NO broker id
    (``alpaca_order_id`` NULL, ALP-847) — they are monitor-enforced Intent.
    """
    rows = list(
        (
            await session.execute(
                select(OrderRow)
                .where(OrderRow.bracket_id == bracket_id)
                .order_by(OrderRow.order_id.asc())
            )
        ).scalars()
    )
    stop_stamped = False
    for row in rows:
        dispatch_key = _LEG_ROLE_TO_DISPATCH_KEY.get(row.order_role)
        if dispatch_key is None:
            continue
        if row.order_role == OrderRole.PRICE_STOP.value:
            if stop_stamped:
                continue
            stop_stamped = True
        real = leg_alpaca_order_ids.get(dispatch_key)
        if real is None:
            continue
        row.alpaca_order_id = real
        row.alpaca_order_id_chain_json = json.dumps([real])
        row.last_update_timestamp = now


async def abandon_command(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    command: OMSCommand,
    result: SubmissionResult,
    reason: str,
) -> None:
    """(F) Roll back a pre-committed command whose broker dispatch was rejected.

    Transitions the dispatched order to CANCELLED and releases any reserved
    capital by reusing the CANCEL writeback state machine:

    * OPEN entry (role ENTRY) → full teardown: bracket dissolved, never-filled
      position CANCELLED, thesis resolved CANCELLED_NEVER_ENTERED, entry capital
      released.
    * ADD add-entry (role ADD_ENTRY) → order CANCELLED + add capital released
      (the existing OPEN position is untouched).
    * CLOSE (role CLOSE) → order CANCELLED (CLOSE reserves no capital).
    * ADJUST replacement (protective role) → order CANCELLED, and the protective
      leg(s) this ADJUST cancelled in its pre-commit are re-instated so local
      state matches the broker (the replace was rejected, so the original leg is
      still live).

    A no-op when the pre-committed row is absent (the pre-commit never landed —
    nothing reached the broker either, by the A-before-B guarantee).
    """
    oid = dispatched_order_id(command, command_id=result.command_id)
    if oid is None:
        return
    async with session_factory() as session:
        await begin_write_immediate(session)
        row = await session.get(OrderRow, oid)
        if row is None:
            return
        handle = InvocationHandle(session=session, invocation_id=invocation_id)
        if isinstance(command, AdjustCommand):
            await _reinstate_adjusted_legs(session, replacement_row=row)
        await _writeback_cancel(
            handle,
            command=CancelCommand(
                command_type="cancel", order_id=OrderId(oid), cancel_reason=reason
            ),
        )
        await session.commit()


async def _reinstate_adjusted_legs(session: AsyncSession, *, replacement_row: OrderRow) -> None:
    """Re-instate the protective leg(s) an ADJUST pre-commit cancelled (ALP-836).

    The ADJUST pre-commit cancelled the original protective order(s) and inserted
    the replacement, all stamped with the same timestamp. When the broker replace
    is rejected the original order is still live at Alpaca, so the local CANCELLED
    legs sharing the replacement's submission timestamp are flipped back to
    PENDING. Best-effort correlation by (bracket, CANCELLED, same-timestamp); the
    monitor / reconcile re-syncs anything this misses.
    """
    stamp = replacement_row.submission_timestamp
    rows = (
        await session.execute(
            select(OrderRow).where(
                OrderRow.bracket_id == replacement_row.bracket_id,
                OrderRow.status == OrderStatus.CANCELLED.value,
                OrderRow.last_update_timestamp == stamp,
                OrderRow.order_id != replacement_row.order_id,
            )
        )
    ).scalars()
    for row in rows:
        if (
            row.order_role in _LEG_ROLE_TO_DISPATCH_KEY
            or row.order_role == OrderRole.TIME_STOP.value
        ):
            row.status = OrderStatus.PENDING.value


async def persist_cancel_writeback(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    command: OMSCommand,
) -> None:
    """Persist a CANCEL's writeback after a successful broker cancel, own transaction.

    CANCEL creates no durable order row of its own — it cancels an existing order
    at the broker — so it has no pre-commit; its writeback (mark the target order
    CANCELLED, release reserved capital, dissolve the bracket if it targets an
    entry) runs here, after the broker confirms the cancel. A lost CANCEL
    writeback is self-healing: the broker's ``canceled`` event drives the order
    CANCELLED via terminal-status-sync.
    """
    if not isinstance(command, CancelCommand):
        return
    async with session_factory() as session:
        await begin_write_immediate(session)
        handle = InvocationHandle(session=session, invocation_id=invocation_id)
        await _writeback_cancel(handle, command=command)
        await session.commit()


async def finalize_broker_envelope(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    envelope: PMEnvelope,
    accepted_command_ids: tuple[str, ...],
    abandoned_entries: tuple[Any, ...],
    reprice_markers: tuple[Any, ...],
) -> None:
    """Emit the envelope-level audit after the per-command loop, in a fresh txn.

    The order rows + capital are already durably committed per command by
    :func:`precommit_command` / :func:`backfill_command_broker_ids`; this writes
    only the once-per-envelope ``pm_decision`` and one ``command_abandoned`` per
    broker-rejected command. The ``phase2_completed_at`` stamp is NOT emitted here
    — it is the orchestrator's single authoritative phase-2 completion stamp,
    guarded by :func:`invocation_has_pending_submit_strand` (ALP-836). Stamping
    here too would be both redundant and ineffective (the orchestrator's
    unconditional stamp would overwrite it).
    """
    async with session_factory() as session:
        await begin_write_immediate(session)
        handle = InvocationHandle(session=session, invocation_id=invocation_id)
        await _emit_pm_decision(
            handle,
            envelope=envelope,
            command_ids=accepted_command_ids,
            reprice_markers=reprice_markers,
        )
        for abandoned in abandoned_entries:
            await persist_command_abandoned(
                handle,
                envelope_id=str(envelope.envelope_id),
                command_id=str(abandoned.command_id),
                originating_agent=str(envelope.source_provenance),
                command_type=abandoned.command_type,
                failure_reason=str(abandoned.failure_reason),
                retry_attempt_count=int(abandoned.retry_attempt_count),
            )
        await session.commit()


def _command_id_prefix(invocation_id: str) -> str:
    """The ``client_order_id`` prefix for this invocation's commands.

    Mirrors :func:`alphamind.execution.oms.command_ids.derive_pm_command_id`: a
    PM command_id is ``inv-{invocation_id}.{envelope}.{ordinal}.{seq}``
    ``~the-{thesis}`` — the base id plus the ALP-844 broker-carried link suffix,
    with the ``inv-`` prefix added only when absent. This prefix check matches on
    the ``inv-{invocation_id}.`` head, which precedes the ``~the-`` suffix, so it
    is unaffected by the broker-carried link.
    """
    prefix = invocation_id if invocation_id.startswith("inv-") else f"inv-{invocation_id}"
    return f"{prefix}."


async def invocation_has_pending_submit_strand(
    session: AsyncSession, *, invocation_id: str
) -> bool:
    """Integrity guard: does this invocation have an order stuck in PENDING_SUBMIT?

    A row left in ``PENDING_SUBMIT`` is a durable order whose post-submit
    ``alpaca_order_id`` backfill was lost — a live broker order whose local row
    never got its broker id (the GS-husk signature, now made impossible to LOSE
    by atomicity but still detectable if the backfill commit itself was lost).
    The orchestrator withholds the ``phase2_completed_at`` stamp when this returns
    ``True``, leaving the invocation visibly incomplete + recoverable (the Phase-1
    reconcile order-backfill repairs it next run). Scoped to THIS invocation by
    the ``client_order_id`` prefix so a prior-invocation strand under recovery
    does not block an otherwise-clean phase 2. Logs loudly on a hit.
    """
    prefix = _command_id_prefix(invocation_id)
    rows = (
        await session.execute(
            select(OrderRow.order_id, OrderRow.client_order_id).where(
                OrderRow.status == OrderStatus.PENDING_SUBMIT.value,
                OrderRow.client_order_id.is_not(None),
            )
        )
    ).all()
    for order_id, client_order_id in rows:
        if client_order_id and client_order_id.startswith(prefix):
            logger.error(
                "phase2 integrity: order %s (client_order_id=%s) stuck in PENDING_SUBMIT — "
                "a lost post-submit backfill behind a live broker order; withholding "
                "phase2_completed_at (invocation left recoverable for the reconcile backfill)",
                order_id,
                client_order_id,
            )
            return True
    return False


__all__ = [
    "abandon_command",
    "backfill_command_broker_ids",
    "dispatched_order_id",
    "finalize_broker_envelope",
    "invocation_has_pending_submit_strand",
    "persist_cancel_writeback",
    "precommit_command",
    "session_factory_from_handle",
]
