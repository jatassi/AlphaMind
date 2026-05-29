"""The fire-action for an expired entry window: broker-cancel + Phase-2 writeback.

When the watcher cycle (``task.py``) finds a ``PENDING_ENTRY`` bracket past its
``entry_window_deadline``, it delegates to an :class:`EntryWindowCanceller`. The
production implementation (:class:`BrokerEntryWindowCanceller`):

1. Resolves the entry order's broker id and asks the broker to cancel it.
2. Uses the **broker's** answer as the race-safe "did it fill?" oracle — a
   ``PENDING_ENTRY`` bracket read from the DB reflects only *reconciled* state,
   so a fill that happened at the broker but has not yet been reconciled would
   otherwise look unfilled. The broker rejects a cancel of an already-filled
   order (422); that rejection means the entry filled and the bracket must NOT
   be dissolved — the next scheduled reconciliation flips it to ``ACTIVE``.
3. Only when the broker *accepts* the cancel does it run the Phase-2 CANCEL
   writeback (``_writeback_cancel`` via :func:`persist_entry_window_cancel`),
   which marks the entry ``CANCELLED``, dissolves the bracket, releases the
   reserved capital, and resolves the thesis ``CANCELLED_NEVER_ENTERED``.

Per parent issue ALP-123 § Pre-resolved decision (I) the continuous monitor
talks to the broker adapter directly rather than through an engine envelope —
the engine-envelope path is CLOSE-and-guardrail-specific and does not model a
deadline-driven entry cancel.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from alphamind._kernel.ids import AlpacaOrderId
from alphamind.portfolio_state.records.orders import BracketRecord

log = logging.getLogger(__name__)

_CANCEL_REASON = "entry_window_expired"


class BrokerCancelClassification(Enum):
    """How the broker answered a cancel request for the resting entry order."""

    ACCEPTED = "accepted"  # broker accepted the cancel → the entry had not filled
    ALREADY_TERMINAL = "already_terminal"  # 404/422 → order already filled or gone
    FAILED = "failed"  # transient gateway failure → retry on the next cycle


class EntryWindowCancelOutcome(Enum):
    """Outcome of attempting to cancel one expired entry window."""

    CANCELLED = "cancelled"  # broker accepted + state written back; do not re-fire
    SKIPPED_FILLED = "skipped_filled"  # entry already filled; reconciliation handles it
    FAILED = "failed"  # transient failure; retry on a later cycle


@runtime_checkable
class EntryWindowCanceller(Protocol):
    """The seam the watcher cycle fires on an expired ``PENDING_ENTRY`` bracket."""

    async def cancel(
        self, *, bracket: BracketRecord, now: datetime
    ) -> EntryWindowCancelOutcome: ...


# entry_order_id → the broker order id to cancel (None when unresolved).
type AlpacaOrderIdResolver = Callable[[str], Awaitable[AlpacaOrderId | None]]
# broker order id → how the broker answered the cancel.
type BrokerCancel = Callable[[AlpacaOrderId], Awaitable[BrokerCancelClassification]]
# entry_order_id + cancel_reason → run the Phase-2 CANCEL writeback under a fresh handle.
type CancelWriteback = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BrokerEntryWindowCanceller:
    """Production :class:`EntryWindowCanceller`.

    Holds three injected seams so the broker round-trip and the DB writeback
    are both fakeable in tests without a live Alpaca client:

    * ``resolve_alpaca_id`` — entry_order_id → broker order id (a DB read).
    * ``broker_cancel`` — broker order id → :class:`BrokerCancelClassification`.
    * ``writeback`` — runs ``persist_entry_window_cancel`` under a fresh
      session + :class:`InvocationHandle` and commits.
    """

    resolve_alpaca_id: AlpacaOrderIdResolver
    broker_cancel: BrokerCancel
    writeback: CancelWriteback

    async def cancel(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowCancelOutcome:
        del now  # the deadline check already fired; provenance lives in the reason
        alpaca_id = await self.resolve_alpaca_id(bracket.entry_order_id)
        if alpaca_id is None:
            log.error(
                "entry_window: cannot cancel bracket %s — entry order %s has no broker id",
                bracket.bracket_id,
                bracket.entry_order_id,
            )
            return EntryWindowCancelOutcome.FAILED
        classification = await self.broker_cancel(alpaca_id)
        if classification is BrokerCancelClassification.FAILED:
            log.warning(
                "entry_window: broker cancel failed for bracket %s; retrying next cycle",
                bracket.bracket_id,
            )
            return EntryWindowCancelOutcome.FAILED
        if classification is BrokerCancelClassification.ALREADY_TERMINAL:
            # The entry filled (or the order is gone) — do NOT dissolve the
            # bracket; the next scheduled reconciliation activates it.
            log.info(
                "entry_window: bracket %s entry already terminal at broker; "
                "leaving reconciliation to activate it",
                bracket.bracket_id,
            )
            return EntryWindowCancelOutcome.SKIPPED_FILLED
        await self.writeback(bracket.entry_order_id, _CANCEL_REASON)
        log.info(
            "entry_window: cancelled never-filled entry for bracket %s (window elapsed)",
            bracket.bracket_id,
        )
        return EntryWindowCancelOutcome.CANCELLED


__all__ = [
    "BrokerCancelClassification",
    "BrokerEntryWindowCanceller",
    "EntryWindowCancelOutcome",
    "EntryWindowCanceller",
]
