"""Run-forever entry-window expiry watcher (ALP-737).

The long-running asyncio task the supervisor registers as ``entry_window``.
Each cycle:

1. Reads every ``PENDING_ENTRY`` bracket that carries an
   ``entry_window_deadline`` (the status-keyed read in ``wiring.py``).
2. For each whose deadline is strictly before ``now`` and that this session has
   not already cancelled, delegates to the :class:`EntryWindowCanceller`
   (broker cancel + Phase-2 writeback; see ``canceller.py``).
3. Records the bracket id in a per-session ``fired`` set on a terminal outcome
   (cancelled, or already-filled-at-broker) so subsequent cycles do not re-fire
   while the DB still shows ``PENDING_ENTRY`` pending the next reconciliation.

The watcher only enforces the deadline. The reprice/escalate decision
(ALP-738) and the no-fill operator alert (ALP-739) are companion concerns that
key off the same expiry signal.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    EntryWindowCanceller,
    EntryWindowCancelOutcome,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.portfolio_state.records.orders import BracketRecord

log = logging.getLogger(__name__)

NowProvider = Callable[[], datetime]
SleepCallable = Callable[[float], Awaitable[None]]

# Outcomes that mean "stop looking at this bracket this session": the entry was
# cancelled, or it had already filled at the broker (reconciliation will flip it
# to ACTIVE). A transient FAILED is retried on the next cycle.
_TERMINAL_OUTCOMES = frozenset(
    {EntryWindowCancelOutcome.CANCELLED, EntryWindowCancelOutcome.SKIPPED_FILLED}
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
    canceller: EntryWindowCanceller,
    now: datetime,
    fired: set[str],
) -> None:
    """One inspection-cycle pass — public for testability.

    Cancels each ``PENDING_ENTRY`` bracket whose ``entry_window_deadline`` is
    strictly before ``now`` and that has not already fired this session. A
    per-bracket failure is logged and skipped so one bad bracket never stalls
    the others (the run-forever loop's catch is a backstop for programming
    bugs, mirroring ``bracket_stops``).
    """
    del config  # cadence consumed by the run-forever loop; kernel is per-cycle
    brackets = await bracket_reader.get_pending_entry_brackets()
    for bracket in brackets:
        if bracket.bracket_id in fired:
            continue
        deadline = bracket.entry_window_deadline
        if deadline is None or now <= deadline:
            continue
        try:
            outcome = await canceller.cancel(bracket=bracket, now=now)
        except Exception:
            log.exception(
                "entry_window: canceller raised for bracket %s; retrying next cycle",
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
    canceller: EntryWindowCanceller,
    now: NowProvider = lambda: datetime.now(UTC),
    sleep: SleepCallable = asyncio.sleep,
) -> None:
    """Long-running task the supervisor registers as ``entry_window``.

    The loop body delegates to :func:`_run_entry_window_cycle` so the same code
    path the tests exercise drives production. The ``fired`` set is private to
    this task — per-session memory of which bracket has already been cancelled
    so a read of the still-``PENDING_ENTRY`` row before the next reconciliation
    does NOT re-fire.
    """
    del session  # session id is not woven into the cancel reason (no trigger id)
    fired: set[str] = set()
    while True:
        try:
            await _run_entry_window_cycle(
                config=config,
                bracket_reader=bracket_reader,
                canceller=canceller,
                now=now(),
                fired=fired,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Per-cycle supervisor backstop: the cycle handles its own per-
            # bracket failures, so a raise here is a programming bug. Log and
            # continue so the loop survives transient consistency issues.
            log.exception("entry_window cycle raised; continuing after sleep")
        await sleep(float(config.entry_window_evaluation_cadence_seconds))


__all__ = [
    "PendingEntryBracketReader",
    "run_entry_window_watcher",
]
