"""Closing-order submission for fired bracket-stops (story 04c / ALP-440).

Per parent issue ALP-123 § Pre-resolved decision (I), bracket-stop firings
submit a closing market order via the broker adapter *directly* — not wrapped
in an engine envelope. These are thesis-driven exits, not guardrail-driven
cascade closes, so the OMS envelope envelope-state machinery is skipped and
the activity-log entry is emitted directly by the watcher.

This module is the thin orchestrator between the watcher (``task.py``) and
the broker_adapter close paths:

* Single-leg options positions: invoke ``submit_options_close`` on the
  position's OCC symbol with a fresh engine-originated client_order_id.
* Strategy positions: invoke ``submit_mleg_close`` first; on combined-close
  rejection (per ``broker-adapter.md § Known gaps``), fall back to per-leg
  ``submit_options_close`` for each leg.

The :class:`BracketCloseSubmitter` Protocol abstracts the broker-call
surface so tests can substitute a capturing fake. Production wires it to
the broker_adapter module in :mod:`alphamind.execution.continuous_monitor.bracket_stops.wiring`.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import BracketRecord
from alphamind.portfolio_state.records.positions import (
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
)

log = logging.getLogger(__name__)


ActivityLogEmitter = Callable[[ActivityLogEntry], Awaitable[None]]
# Async-native: invoked from the watcher's loop; see bracket_stops.task.
type InvocationIdProvider = Callable[[], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class CloseSubmissionResult:
    """Outcome of submitting a closing order through the broker adapter.

    ``order_ids`` carries the one-or-more closing-order IDs:

    * Single-leg options close: a single OCC submission → one ID.
    * Strategy combined close: the mleg parent order → one ID.
    * Strategy per-leg fallback: one ID per leg.

    The activity-log entry written by the closer uses
    ``order_ids[0]`` as the row's ``order_id`` field. ``mode`` lets the
    activity-log row distinguish combined-close vs. per-leg fallback for
    operator forensics; the watcher does not branch on it.
    """

    order_ids: tuple[str, ...]
    mode: str  # "single_leg" | "strategy_combined" | "strategy_per_leg"


@runtime_checkable
class BracketCloseSubmitter(Protocol):
    """Narrow broker-call surface the closer consumes.

    Two methods because the persisted shape differs between single-leg
    options positions (one OCC symbol) and multi-leg strategy positions
    (per-leg orders fanning out under one parent). Production wiring lives
    in :mod:`alphamind.execution.continuous_monitor.bracket_stops.wiring`;
    tests pass a capturing fake.
    """

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
    ) -> CloseSubmissionResult:
        """Submit a market close on a single-leg options position."""

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
    ) -> CloseSubmissionResult:
        """Submit a strategy close. The submitter is responsible for the
        Alpaca-mleg-combined-close attempt and the per-leg fallback on
        rejection. The result reflects which path succeeded."""


async def submit_options_bracket_close(  # noqa: PLR0913 — orchestrator fan-out: closer + activity-log writer
    *,
    position: PositionRecord,
    bracket: BracketRecord,
    trigger_reason: PositionExitMethod,
    submitter: BracketCloseSubmitter,
    activity_log: ActivityLogEmitter,
    invocation_id_provider: InvocationIdProvider,
    monitor_session_id: str,
    trigger_id: int,
    now: datetime,
    estimated_exit_price: float,
    realized_pnl_usd: float,
) -> CloseSubmissionResult:
    """Submit a closing market order + persist the POSITION_CLOSED activity-log entry.

    Routes single-leg options through ``submitter.submit_options_close`` and
    strategy positions through ``submitter.submit_strategy_close``. The
    activity-log entry has ``event_source=BRACKET_MANAGER`` and the supplied
    ``trigger_reason`` (``STOP_TRIGGERED`` or ``TARGET_REACHED``).

    ``trigger_id`` is the per-session monotonically-increasing identifier
    the watcher allocates; it is woven into the engine-originated
    ``client_order_id`` so the resulting Alpaca order is traceable back to
    the firing event.
    """
    del bracket  # bracket_id flows through trigger_id; leg_id is in the rationale
    client_order_id_base = _build_engine_client_order_id(
        monitor_session_id=monitor_session_id,
        trigger_id=trigger_id,
    )
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        result = await submitter.submit_options_close(
            position=position,
            details=details,
            client_order_id=client_order_id_base,
        )
    elif isinstance(details, StrategyPositionDetails):
        result = await submitter.submit_strategy_close(
            position=position,
            details=details,
            client_order_id_base=client_order_id_base,
        )
    else:
        msg = (
            f"submit_options_bracket_close requires options or strategy position; "
            f"got instrument_type={details.instrument_type!r}"
        )
        raise TypeError(msg)
    await activity_log(
        _build_position_closed_entry(
            position=position,
            order_ids=result.order_ids,
            trigger_reason=trigger_reason,
            invocation_id=await invocation_id_provider(),
            now=now,
            estimated_exit_price=estimated_exit_price,
            realized_pnl_usd=realized_pnl_usd,
        )
    )
    return result


def _build_engine_client_order_id(*, monitor_session_id: str, trigger_id: int) -> str:
    """Derive the engine-originated client_order_id for a bracket-stop fire.

    Reuses :func:`alphamind.execution.oms.command_ids.derive_engine_command_id`
    so the produced ID is structurally identical to those the engine-envelope
    cascade dispatcher emits — single canonical pattern keyed off the live
    monitor session and a per-trigger sequence.
    """
    from alphamind.execution.oms.command_ids import derive_engine_command_id

    return derive_engine_command_id(
        monitor_session_id=monitor_session_id,
        trigger_id=trigger_id,
        command_ordinal=0,
    )


def _build_position_closed_entry(
    *,
    position: PositionRecord,
    order_ids: tuple[str, ...],
    trigger_reason: PositionExitMethod,
    invocation_id: str,
    now: datetime,
    estimated_exit_price: float,
    realized_pnl_usd: float,
) -> ActivityLogEntry:
    """Compose the typed POSITION_CLOSED entry with BRACKET_MANAGER provenance.

    The activity-log row's ``order_id`` is the first closing-order ID — the
    strategist view's projection joins on this for traceability. The
    ``thesis_resolution_category`` mirrors the trigger reason so the
    feedback loop can attribute outcomes uniformly.
    """
    suffix = secrets.token_hex(4)
    entry_id = f"mon-brk-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{suffix}"
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=now,
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position.position_id,
        order_id=order_ids[0] if order_ids else None,
        thesis_id=position.thesis_id,
        source=EventSource.BRACKET_MANAGER,
        detail=PositionClosedDetail(
            exit_method=trigger_reason,
            exit_price=estimated_exit_price,
            realized_pnl_usd=realized_pnl_usd,
            thesis_resolution_category=(
                "invalidated"
                if trigger_reason is PositionExitMethod.STOP_TRIGGERED
                else "validated"
            ),
        ),
    )
