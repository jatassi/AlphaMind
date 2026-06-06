"""The fire-action for an expired entry window: broker-cancel, no DB write (ALP-863).

When the watcher cycle (``task.py``) finds a ``PENDING_ENTRY`` bracket past its
``entry_window_deadline``, it delegates to an :class:`EntryWindowCanceller`. The
production implementation (:class:`BrokerEntryWindowCanceller`) decides whether
the entry will be dissolved from whether it **filled** — not from the broker's
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
   combined with the no-recorded-fills check above, → ``CANCELLED``.

**The cancel writes nothing to local state** (ALP-863 — single-writer invariant 1,
ADR-0005). The monitor used to RMW ``orders`` / ``brackets`` / ``positions`` /
``theses`` here via a fresh-handle ``persist_entry_window_cancel`` writeback — a
residual cross-process writer reachable by the ``SQLITE_BUSY_SNAPSHOT`` race
(ALP-824 class). That writeback is gone. The disposition now reaches local state
the same way every other terminal order status does (W1c): the broker cancel
produces a ``canceled`` / ``expired`` trade-update → the fill-stream consumer
appends a ``TERMINAL_ORDER_STATUS`` event → the single (pipeline) writer projects
``orders.status`` and runs the dissolve cascade (bracket DISSOLVED + legs
cancelled + capital released + thesis ``CANCELLED_NEVER_ENTERED`` + position
PENDING→CANCELLED) in ``write_paths/projection_rebuild.py``. This is strictly
more robust than deciding off the cancel ack: if the entry actually filled in the
cancel race a FILL event flows instead, no zero-fill terminal event is appended,
and the cascade correctly does not fire.

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


@dataclass(frozen=True, slots=True)
class BrokerEntryWindowCanceller:
    """Production :class:`EntryWindowCanceller`.

    Holds two injected seams so the broker round-trip and the DB read are both
    fakeable in tests without a live Alpaca client. The canceller performs **no**
    local-state write (ALP-863) — the dissolve cascade is the pipeline's job:

    * ``resolve_target`` — entry_order_id → :class:`EntryCancelTarget` (broker id
      + recorded-fill state), or ``None`` when the order row is missing.
    * ``broker_cancel`` — broker order id → :class:`BrokerCancelClassification`.
    """

    resolve_target: EntryCancelTargetResolver
    broker_cancel: BrokerCancel

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
        # (or already gone) without filling. The monitor writes NOTHING (ALP-863):
        # the broker's ``canceled`` trade-update flows to the fill-stream consumer
        # as a TERMINAL_ORDER_STATUS event, and the pipeline projects it and runs
        # the dissolve cascade. Latching CANCELLED here only stops this session's
        # watcher re-firing on the still-PENDING_ENTRY row before that projection.
        log.info(
            "entry_window: broker-cancelled never-filled entry for bracket %s (window "
            "elapsed); dissolve cascade deferred to the pipeline projection",
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
