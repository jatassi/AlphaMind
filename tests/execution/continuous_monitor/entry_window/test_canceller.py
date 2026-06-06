"""Tests for ``BrokerEntryWindowCanceller`` (ALP-737 / ALP-863).

The canceller decides cancel-vs-skip from whether the entry **filled**, not from
the broker's cancel response. These tests drive each branch with fakes: recorded
fills, an un-routed entry (no broker id — None), the two broker classifications,
and a missing order — asserting the returned outcome and which broker calls ran.

ALP-863: the canceller no longer has a DB writeback seam — a confirmed cancel
with no recorded fills returns ``CANCELLED`` and writes nothing. The dissolve
cascade is the pipeline's job (it projects the ``TERMINAL_ORDER_STATUS`` event
the broker cancel produces). The only injected seams left are ``resolve_target``
(a read) and ``broker_cancel``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
    BrokerEntryWindowCanceller,
    EntryCancelTarget,
    EntryWindowDeadlineOutcome,
    EntryWindowSessionMemory,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)

_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def _bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id="BRK-1-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("BRK-1-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol("ZS"), threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId("BRK-1"),
        position_id=PositionId("POS-1"),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ORD-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=_NOW,
    )


class _Recorder:
    def __init__(
        self,
        *,
        target: EntryCancelTarget | None,
        classification: BrokerCancelClassification = BrokerCancelClassification.CANCEL_CONFIRMED,
    ) -> None:
        self._target = target
        self._classification = classification
        self.resolved: list[str] = []
        self.cancelled: list[str] = []

    async def resolve_target(self, entry_order_id: str) -> EntryCancelTarget | None:
        self.resolved.append(entry_order_id)
        return self._target

    async def broker_cancel(self, alpaca_id: AlpacaOrderId) -> BrokerCancelClassification:
        self.cancelled.append(alpaca_id)
        return self._classification


def _canceller(rec: _Recorder) -> BrokerEntryWindowCanceller:
    return BrokerEntryWindowCanceller(
        resolve_target=rec.resolve_target,
        broker_cancel=rec.broker_cancel,
    )


async def test_confirmed_cancel_with_no_fills_returns_cancelled_and_writes_nothing() -> None:
    """ALP-863 — a confirmed broker cancel with no recorded fills latches
    ``CANCELLED`` (so the watcher stops re-firing this session) after broker-
    cancelling the resting entry, and performs NO local-state writeback. The
    canceller has only the two read/broker seams — there is no writeback to run."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=False
        ),
        classification=BrokerCancelClassification.CANCEL_CONFIRMED,
    )
    outcome = await _canceller(rec).cancel(
        bracket=_bracket(), now=_NOW, reprice_memory=EntryWindowSessionMemory()
    )

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["alpaca-uuid-xyz"]


async def test_recorded_fill_skips_cancel() -> None:
    """If the entry already has a recorded fill, never cancel/dissolve — even
    if the bracket DB row still reads PENDING_ENTRY pre-reconciliation."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=True
        ),
    )
    outcome = await _canceller(rec).cancel(
        bracket=_bracket(), now=_NOW, reprice_memory=EntryWindowSessionMemory()
    )

    assert outcome is EntryWindowDeadlineOutcome.SKIPPED_FILLED
    assert rec.cancelled == []  # never asked the broker to cancel a filled entry


async def test_unrouted_entry_is_retried_not_cancelled() -> None:
    """An entry not yet routed to the broker carries NO broker id (None, ALP-847
    — the synthetic 'alp-' placeholder is deleted); we retry rather than misread
    a missing id as terminal."""
    rec = _Recorder(
        target=EntryCancelTarget(alpaca_order_id=None, has_recorded_fills=False),
    )
    outcome = await _canceller(rec).cancel(
        bracket=_bracket(), now=_NOW, reprice_memory=EntryWindowSessionMemory()
    )

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.cancelled == []


async def test_retryable_broker_answer_does_not_latch() -> None:
    """A transient gateway failure or non-terminal 4xx (auth / rate-limit) must
    NOT latch the bracket as handled — it retries next cycle."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=False
        ),
        classification=BrokerCancelClassification.RETRYABLE,
    )
    outcome = await _canceller(rec).cancel(
        bracket=_bracket(), now=_NOW, reprice_memory=EntryWindowSessionMemory()
    )

    assert outcome is EntryWindowDeadlineOutcome.FAILED


async def test_missing_order_returns_failed_without_cancelling() -> None:
    rec = _Recorder(target=None)
    outcome = await _canceller(rec).cancel(
        bracket=_bracket(), now=_NOW, reprice_memory=EntryWindowSessionMemory()
    )

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.cancelled == []


async def test_in_session_reprice_cancels_the_current_broker_id_not_the_stale_row() -> None:
    """ALP-867 — after an in-session reprice the order row's ``alpaca_order_id`` is
    stale (the cancel-and-replace produced a new id the pipeline has not projected
    yet), so the terminal cancel must target the session-tracked **current** id."""
    rec = _Recorder(
        # The order row still carries the pre-reprice broker id.
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-stale-row-id"), has_recorded_fills=False
        ),
        classification=BrokerCancelClassification.CANCEL_CONFIRMED,
    )
    memory = EntryWindowSessionMemory()
    memory.seed_if_absent("BRK-1", durable_reprice_count=0)
    memory.record_reprice("BRK-1", new_alpaca_order_id=AlpacaOrderId("alpaca-new-replace-id"))

    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW, reprice_memory=memory)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    # The session's current id wins over the stale row id.
    assert rec.cancelled == ["alpaca-new-replace-id"]
