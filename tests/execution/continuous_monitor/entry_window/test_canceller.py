"""Tests for ``BrokerEntryWindowCanceller`` (ALP-737).

The canceller decides dissolve-vs-skip from whether the entry **filled**, not
from the broker's cancel response. These tests drive each branch with fakes:
recorded fills, an un-routed synthetic id, the two broker classifications, and a
missing order — asserting the returned outcome and whether the dissolve
writeback ran.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
    BrokerEntryWindowCanceller,
    EntryCancelTarget,
    EntryWindowDeadlineOutcome,
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
        self.written_back: list[tuple[str, str]] = []

    async def resolve_target(self, entry_order_id: str) -> EntryCancelTarget | None:
        self.resolved.append(entry_order_id)
        return self._target

    async def broker_cancel(self, alpaca_id: AlpacaOrderId) -> BrokerCancelClassification:
        self.cancelled.append(alpaca_id)
        return self._classification

    async def writeback(self, entry_order_id: str, cancel_reason: str) -> None:
        self.written_back.append((entry_order_id, cancel_reason))


def _canceller(rec: _Recorder) -> BrokerEntryWindowCanceller:
    return BrokerEntryWindowCanceller(
        resolve_target=rec.resolve_target,
        broker_cancel=rec.broker_cancel,
        writeback=rec.writeback,
    )


async def test_confirmed_cancel_with_no_fills_dissolves_and_returns_cancelled() -> None:
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=False
        ),
        classification=BrokerCancelClassification.CANCEL_CONFIRMED,
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["alpaca-uuid-xyz"]
    assert rec.written_back == [("ORD-entry-1", "entry_window_expired")]


async def test_recorded_fill_skips_cancel_and_writeback() -> None:
    """If the entry already has a recorded fill, never cancel/dissolve — even
    if the bracket DB row still reads PENDING_ENTRY pre-reconciliation."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=True
        ),
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.SKIPPED_FILLED
    assert rec.cancelled == []  # never asked the broker to cancel a filled entry
    assert rec.written_back == []


async def test_synthetic_broker_id_is_retried_not_dissolved() -> None:
    """An entry not yet routed to the broker carries a synthetic 'alp-' id; we
    retry rather than misread a placeholder 404 as terminal."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alp-ORD-entry-1"), has_recorded_fills=False
        ),
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.cancelled == []
    assert rec.written_back == []


async def test_retryable_broker_answer_skips_writeback() -> None:
    """A transient gateway failure or non-terminal 4xx (auth / rate-limit) must
    NOT dissolve and must NOT latch the bracket — it retries next cycle."""
    rec = _Recorder(
        target=EntryCancelTarget(
            alpaca_order_id=AlpacaOrderId("alpaca-uuid-xyz"), has_recorded_fills=False
        ),
        classification=BrokerCancelClassification.RETRYABLE,
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.written_back == []


async def test_missing_order_returns_failed_without_cancelling() -> None:
    rec = _Recorder(target=None)
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.cancelled == []
    assert rec.written_back == []
