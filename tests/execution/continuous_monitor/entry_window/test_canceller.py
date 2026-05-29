"""Tests for ``BrokerEntryWindowCanceller`` (ALP-737).

The canceller's job is the race-safe sequencing: ask the broker to cancel the
resting entry first, and only run the dissolve-the-bracket writeback when the
broker *accepts* (proving the entry had not filled). These tests drive the
three broker answers + the unresolved-id guard with fakes, asserting both the
returned outcome and whether the writeback ran.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import AlpacaOrderId, BracketId, OrderId, PositionId, Symbol
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    BrokerCancelClassification,
    BrokerEntryWindowCanceller,
    EntryWindowCancelOutcome,
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
        alpaca_id: AlpacaOrderId | None,
        classification: BrokerCancelClassification,
    ) -> None:
        self._alpaca_id = alpaca_id
        self._classification = classification
        self.resolved: list[str] = []
        self.cancelled: list[str] = []
        self.written_back: list[tuple[str, str]] = []

    async def resolve(self, entry_order_id: str) -> AlpacaOrderId | None:
        self.resolved.append(entry_order_id)
        return self._alpaca_id

    async def broker_cancel(self, alpaca_id: AlpacaOrderId) -> BrokerCancelClassification:
        self.cancelled.append(alpaca_id)
        return self._classification

    async def writeback(self, entry_order_id: str, cancel_reason: str) -> None:
        self.written_back.append((entry_order_id, cancel_reason))


def _canceller(rec: _Recorder) -> BrokerEntryWindowCanceller:
    return BrokerEntryWindowCanceller(
        resolve_alpaca_id=rec.resolve,
        broker_cancel=rec.broker_cancel,
        writeback=rec.writeback,
    )


async def test_accepted_cancel_runs_writeback_and_returns_cancelled() -> None:
    rec = _Recorder(
        alpaca_id=AlpacaOrderId("alpaca-xyz"),
        classification=BrokerCancelClassification.ACCEPTED,
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowCancelOutcome.CANCELLED
    assert rec.cancelled == ["alpaca-xyz"]
    assert rec.written_back == [("ORD-entry-1", "entry_window_expired")]


async def test_already_terminal_skips_writeback() -> None:
    """Broker rejected the cancel (422 already-filled / 404) → the entry
    filled; do NOT dissolve the bracket, leave it for reconciliation."""
    rec = _Recorder(
        alpaca_id=AlpacaOrderId("alpaca-xyz"),
        classification=BrokerCancelClassification.ALREADY_TERMINAL,
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowCancelOutcome.SKIPPED_FILLED
    assert rec.cancelled == ["alpaca-xyz"]
    assert rec.written_back == []


async def test_transient_failure_skips_writeback_and_returns_failed() -> None:
    rec = _Recorder(
        alpaca_id=AlpacaOrderId("alpaca-xyz"),
        classification=BrokerCancelClassification.FAILED,
    )
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowCancelOutcome.FAILED
    assert rec.written_back == []


async def test_unresolved_broker_id_returns_failed_without_cancelling() -> None:
    """No broker id means we cannot safely cancel — never run the writeback,
    and retry on a later cycle once the order is resolvable."""
    rec = _Recorder(alpaca_id=None, classification=BrokerCancelClassification.ACCEPTED)
    outcome = await _canceller(rec).cancel(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowCancelOutcome.FAILED
    assert rec.cancelled == []
    assert rec.written_back == []
