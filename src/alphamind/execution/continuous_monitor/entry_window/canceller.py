"""The fire-action for an expired entry window: broker-cancel + Phase-2 writeback.

When the watcher cycle (``task.py``) finds a ``PENDING_ENTRY`` bracket past its
``entry_window_deadline``, it delegates to an :class:`EntryWindowCanceller`. The
production implementation (:class:`BrokerEntryWindowCanceller`) decides whether
to dissolve the bracket from whether the entry **filled** — not from the broker's
answer to the cancel request, which is not a reliable fill oracle (a fire-and-
forget cancel returns "accepted" even for an order that races to FILLED):

1. Resolve the entry order's broker id **and** whether any fill has been
   recorded for it (``fill_records``, written by the fill-stream consumer ahead
   of reconciliation).
2. If a fill is already recorded → ``SKIPPED_FILLED``: the entry filled, so never
   cancel/dissolve — reconciliation will flip the bracket to ``ACTIVE``.
3. If the entry has no broker id yet (``alpaca_order_id is None`` — not yet
   routed; ALP-847 deleted the synthetic ``alp-…`` placeholder) → ``FAILED``
   (retry once it is acked); there is nothing to cancel.
4. Otherwise ask the broker to cancel. A transient gateway failure or a non-
   terminal 4xx (auth / rate-limit / malformed) → ``FAILED`` (retry, do NOT latch
   the bracket as handled). A confirmed cancel or an already-terminal 404/422,
   combined with the no-recorded-fills check above, → run the Phase-2 CANCEL
   writeback (``persist_entry_window_cancel``), which marks the entry
   ``CANCELLED``, dissolves the bracket, releases reserved capital, and resolves
   the thesis ``CANCELLED_NEVER_ENTERED``.

The residual race — a fill that lands at the broker after the no-fills check but
before the cancel processes, and has not yet been recorded — is the same async
window the PM-originated CANCEL path (``submit_cancel`` → ``_writeback_cancel``)
already accepts; the fill-record check closes the common case.

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

    # The cancel was accepted, or the order was already terminal at the broker
    # (404 not_found / 422 not-cancellable). Combined with the canceller's
    # no-recorded-fills precondition, this is safe to dissolve.
    CANCEL_CONFIRMED = "cancel_confirmed"
    # Transient gateway failure, or a non-terminal 4xx (auth / rate-limit /
    # malformed). Retry on the next cycle; do NOT latch the bracket as handled.
    RETRYABLE = "retryable"


class EntryWindowDeadlineOutcome(Enum):
    """Outcome of handling one ``PENDING_ENTRY`` bracket past its deadline.

    The terminal canceller (:class:`BrokerEntryWindowCanceller`) returns the
    cancel / skip / fail members; the repricer (ALP-740,
    :class:`...repricer.BrokerEntryWindowRepricer`) adds ``REPRICED`` — a
    non-terminal escalation that re-pegs the resting limit toward the market
    and is re-evaluated on the next cycle.
    """

    CANCELLED = "cancelled"  # cancel confirmed + no fills + state written back; do not re-fire
    SKIPPED_FILLED = "skipped_filled"  # entry already filled; reconciliation handles it
    REPRICED = "repriced"  # entry escalated toward the market; non-terminal, re-evaluate next cycle
    FAILED = "failed"  # transient / not-yet-routed; retry on a later cycle


@dataclass(frozen=True, slots=True)
class EntryCancelTarget:
    """What the canceller needs to decide an expired entry's fate.

    ``has_recorded_fills`` reflects ``fill_records`` (raw fills written by the
    fill-stream consumer before reconciliation), so a fill that has been
    observed but not yet reconciled to the bracket still blocks the cancel.

    ``alpaca_order_id`` is ``None`` for a not-yet-routed entry (ALP-847 deleted
    the synthetic ``alp-`` placeholder) — there is no broker order to cancel, so
    the canceller retries rather than misreading a missing id as terminal.
    """

    alpaca_order_id: AlpacaOrderId | None
    has_recorded_fills: bool


@runtime_checkable
class EntryWindowCanceller(Protocol):
    """The terminal cancel seam for an expired ``PENDING_ENTRY`` bracket.

    Fired directly by the watcher when repricing is disabled, and delegated to
    by the repricer (ALP-740) once its escalation budget is spent or the entry
    is not a repriceable equity limit.
    """

    async def cancel(
        self, *, bracket: BracketRecord, now: datetime
    ) -> EntryWindowDeadlineOutcome: ...


# entry_order_id → the broker order id + recorded-fill state (None when the
# entry order row is missing).
type EntryCancelTargetResolver = Callable[[str], Awaitable[EntryCancelTarget | None]]
# broker order id → how the broker answered the cancel.
type BrokerCancel = Callable[[AlpacaOrderId], Awaitable[BrokerCancelClassification]]
# entry_order_id + cancel_reason → run the Phase-2 CANCEL writeback under a fresh handle.
type CancelWriteback = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BrokerEntryWindowCanceller:
    """Production :class:`EntryWindowCanceller`.

    Holds three injected seams so the broker round-trip and the DB reads/writes
    are all fakeable in tests without a live Alpaca client:

    * ``resolve_target`` — entry_order_id → :class:`EntryCancelTarget` (broker id
      + recorded-fill state), or ``None`` when the order row is missing.
    * ``broker_cancel`` — broker order id → :class:`BrokerCancelClassification`.
    * ``writeback`` — runs ``persist_entry_window_cancel`` under a fresh
      session + :class:`InvocationHandle` and commits.
    """

    resolve_target: EntryCancelTargetResolver
    broker_cancel: BrokerCancel
    writeback: CancelWriteback

    async def cancel(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowDeadlineOutcome:
        del now  # the deadline check already fired; provenance lives in the reason
        target = await self.resolve_target(bracket.entry_order_id)
        if target is None:
            log.error(
                "entry_window: entry order %s for bracket %s not found; retrying",
                bracket.entry_order_id,
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        if target.has_recorded_fills:
            # The entry filled — never cancel/dissolve a filled entry; the next
            # scheduled reconciliation activates the bracket.
            log.info(
                "entry_window: bracket %s entry has recorded fills; leaving for reconciliation",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.SKIPPED_FILLED
        if target.alpaca_order_id is None:
            # Not yet routed to the broker (no broker id) — nothing to cancel yet;
            # retry once it is acked rather than misreading a missing id as terminal.
            log.warning(
                "entry_window: bracket %s entry not yet broker-routed (no broker id); retrying",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        classification = await self.broker_cancel(target.alpaca_order_id)
        if classification is BrokerCancelClassification.RETRYABLE:
            log.warning(
                "entry_window: broker cancel not confirmed for bracket %s; retrying next cycle",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        # CANCEL_CONFIRMED with no recorded fills → the resting entry was cancelled
        # (or already gone) without filling. Dissolve the bracket.
        await self.writeback(bracket.entry_order_id, _CANCEL_REASON)
        log.info(
            "entry_window: cancelled never-filled entry for bracket %s (window elapsed)",
            bracket.bracket_id,
        )
        return EntryWindowDeadlineOutcome.CANCELLED


__all__ = [
    "BrokerCancelClassification",
    "BrokerEntryWindowCanceller",
    "EntryCancelTarget",
    "EntryWindowCanceller",
    "EntryWindowDeadlineOutcome",
]
