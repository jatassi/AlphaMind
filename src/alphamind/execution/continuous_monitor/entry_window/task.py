"""Run-forever entry-window expiry watcher (ALP-737 + ALP-740).

The long-running asyncio task the supervisor registers as ``entry_window``.
Each cycle:

1. Reads every ``PENDING_ENTRY`` bracket that carries an
   ``entry_window_deadline`` (the status-keyed read in ``wiring.py``).
2. For each whose deadline is strictly before ``now`` and that this session has
   not already finished with, delegates to the
   :class:`EntryWindowDeadlineHandler` — in production the repricer
   (``repricer.py``), which reprices/escalates the resting limit toward the
   market while it can and falls back to the terminal cancel (``canceller.py``)
   once the reprice budget is spent.
3. Records the bracket id in a per-session ``fired`` set on a *terminal* outcome
   (cancelled, or already-filled-at-broker) so subsequent cycles do not re-fire
   while the DB still shows ``PENDING_ENTRY`` pending the next reconciliation. A
   ``REPRICED`` outcome is deliberately non-terminal — the re-pegged entry is
   re-evaluated next cycle (it filled → SKIPPED_FILLED, or the budget is spent
   → CANCELLED).

The no-fill operator alert (ALP-739) fires off the terminal cancel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    EntryWindowDeadlineOutcome,
)
from alphamind.execution.continuous_monitor.entry_window.repricer import (
    EntryWindowDeadlineHandler,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import SupervisedLoop
from alphamind.portfolio_state.records.orders import BracketRecord

log = logging.getLogger(__name__)

NowProvider = Callable[[], datetime]

# Outcomes that mean "stop looking at this bracket this session": the entry was
# cancelled, or it had already filled at the broker (reconciliation will flip it
# to ACTIVE). REPRICED and FAILED are non-terminal — a re-pegged or transiently-
# failed entry is re-evaluated on the next cycle.
_TERMINAL_OUTCOMES = frozenset(
    {EntryWindowDeadlineOutcome.CANCELLED, EntryWindowDeadlineOutcome.SKIPPED_FILLED}
)


@runtime_checkable
class PendingEntryBracketReader(Protocol):
    """Narrow read surface: ``PENDING_ENTRY`` brackets carrying a deadline."""

    async def get_pending_entry_brackets(self) -> tuple[BracketRecord, ...]: ...


# ---------------------------------------------------------------------------
# Single-cycle kernel
# ---------------------------------------------------------------------------


async def _run_entry_window_cycle(
    *,
    config: ContinuousMonitorConfig,
    bracket_reader: PendingEntryBracketReader,
    handler: EntryWindowDeadlineHandler,
    now: datetime,
    fired: set[str],
) -> None:
    """One inspection-cycle pass — public for testability.

    Hands each ``PENDING_ENTRY`` bracket whose ``entry_window_deadline`` is
    strictly before ``now`` and that has not already terminally fired this
    session to the handler (reprice/escalate, else cancel). A per-bracket
    failure is logged and skipped so one bad bracket never stalls the others
    (the run-forever loop's catch is a backstop for programming bugs, mirroring
    ``bracket_stops``).
    """
    del config  # cadence consumed by the run-forever loop; kernel is per-cycle
    brackets = await bracket_reader.get_pending_entry_brackets()
    for bracket in brackets:
        if bracket.bracket_id in fired:
            continue
        try:
            deadline = bracket.entry_window_deadline
            # Strict ``>``: a bracket exactly at its deadline does NOT fire, per
            # the BracketRecord lifecycle contract (orders.py). The deadline read
            # is inside the try so a malformed deadline isolates to this bracket
            # instead of stalling the whole cycle.
            if deadline is None or now <= deadline:
                continue
            outcome = await handler.handle(bracket=bracket, now=now)
        except Exception:
            log.exception(
                "entry_window: handler raised for bracket %s; retrying next cycle",
                bracket.bracket_id,
            )
            continue
        if outcome in _TERMINAL_OUTCOMES:
            fired.add(bracket.bracket_id)


# ---------------------------------------------------------------------------
# Run-forever entry point
# ---------------------------------------------------------------------------


async def run_entry_window_watcher(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    bracket_reader: PendingEntryBracketReader,
    handler: EntryWindowDeadlineHandler,
    loop: SupervisedLoop,
    now: NowProvider = lambda: datetime.now(UTC),
) -> None:
    """Long-running task the supervisor registers as ``entry_window``.

    The loop body delegates to :func:`_run_entry_window_cycle` so the same code
    path the tests exercise drives production. *loop* is the supervisor's
    :meth:`MonitorSupervisor.supervised_loop` iterator (name + cadence pre-bound
    by the wiring): it beats the watchdog at the top of every iteration and paces
    the loop at the entry-window cadence, so this task is liveness-watched with no
    hand-wired ``beat()``. The ``fired`` set is private to this task —
    per-session memory of which bracket has already terminally fired so a read of
    the still-``PENDING_ENTRY`` row before the next reconciliation does NOT
    re-fire (a REPRICED bracket is intentionally left out so it is re-evaluated
    next cycle).
    """
    del session  # session id is not woven into the cancel reason (no trigger id)
    fired: set[str] = set()
    async for _ in loop():
        try:
            await _run_entry_window_cycle(
                config=config,
                bracket_reader=bracket_reader,
                handler=handler,
                now=now(),
                fired=fired,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Per-cycle supervisor backstop: the cycle handles its own per-
            # bracket failures, so a raise here is a programming bug. Log and
            # continue so the loop survives transient consistency issues.
            log.exception("entry_window cycle raised; continuing after pacing sleep")


__all__ = [
    "PendingEntryBracketReader",
    "run_entry_window_watcher",
]
