"""Tests for ``BrokerEntryWindowRepricer`` (ALP-740).

The repricer decides reprice-vs-cancel from the entry's fill state, whether it
is a repriceable equity limit, and how many times it has already been repriced
(``modification_count`` vs the budget). These tests drive each branch with
fakes — recorded fills, an un-routed synthetic id, a spent budget, a non-equity
entry, a missing quote, and the three broker-replace classifications — asserting
the returned outcome and which seams fired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import Price, price
from alphamind.execution.broker_adapter.entry_pricing import (
    TouchQuote,
    marketable_limit_price,
)
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    EntryWindowDeadlineOutcome,
)
from alphamind.execution.continuous_monitor.entry_window.repricer import (
    BrokerEntryWindowRepricer,
    BrokerReplaceClassification,
    BrokerReplaceResult,
    RepriceTarget,
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
_BPS = 5.0
_QUOTE = TouchQuote(bid=price("100.00"), ask=price("100.10"))


def _bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id="BRK-1-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("BRK-1-ord-stop"),
        trigger=PriceTrigger(underlying_ticker=Symbol("ZS"), threshold_usd=140.0, direction="GTE"),
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


@dataclass
class _Recorder:
    target: RepriceTarget | None
    replace_result: BrokerReplaceResult = field(
        default_factory=lambda: BrokerReplaceResult(
            classification=BrokerReplaceClassification.REPLACED,
            new_alpaca_order_id=AlpacaOrderId("alpaca-new-uuid"),
        )
    )
    quote: TouchQuote | None = _QUOTE
    cancel_outcome: EntryWindowDeadlineOutcome = EntryWindowDeadlineOutcome.CANCELLED
    resolved: list[str] = field(default_factory=list)
    replaced: list[tuple[str, Price]] = field(default_factory=list)
    written_back: list[tuple[str, Price, str, str]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)

    async def resolve_target(self, entry_order_id: str) -> RepriceTarget | None:
        self.resolved.append(entry_order_id)
        return self.target

    async def latest_quote(self, symbol: str) -> TouchQuote | None:
        self.quoted.append(symbol)
        return self.quote

    async def broker_replace(
        self, alpaca_order_id: AlpacaOrderId, new_limit: Price
    ) -> BrokerReplaceResult:
        self.replaced.append((alpaca_order_id, new_limit))
        return self.replace_result

    async def reprice_writeback(
        self, entry_order_id: str, new_limit: Price, new_alpaca_order_id: str, reason: str
    ) -> None:
        self.written_back.append((entry_order_id, new_limit, new_alpaca_order_id, reason))

    async def cancel(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowDeadlineOutcome:
        del now
        self.cancelled.append(bracket.bracket_id)
        return self.cancel_outcome


def _repricer(rec: _Recorder, *, max_reprice_count: int = 2) -> BrokerEntryWindowRepricer:
    return BrokerEntryWindowRepricer(
        resolve_target=rec.resolve_target,
        quote_source=rec,
        broker_replace=rec.broker_replace,
        reprice_writeback=rec.reprice_writeback,
        canceller=rec,
        max_reprice_count=max_reprice_count,
        bps_through_touch=_BPS,
    )


def _target(
    *,
    alpaca_order_id: str = "alpaca-uuid-xyz",
    has_recorded_fills: bool = False,
    is_equity_limit: bool = True,
    side: str = "sell",
    modification_count: int = 0,
) -> RepriceTarget:
    return RepriceTarget(
        alpaca_order_id=AlpacaOrderId(alpaca_order_id),
        has_recorded_fills=has_recorded_fills,
        is_equity_limit=is_equity_limit,
        ticker="ZS",
        side=side,  # type: ignore[arg-type]
        modification_count=modification_count,
    )


async def test_successful_reprice_writes_back_and_returns_repriced() -> None:
    """A repriceable equity limit under budget is escalated to a marketable
    limit through the touch, the new broker id is handed to the writeback, and
    the outcome is the non-terminal REPRICED — no cancel."""
    rec = _Recorder(target=_target(side="sell", modification_count=0))
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.REPRICED
    # A short (SELL) entry is priced off the bid (marketable_limit_price short).
    expected_limit = marketable_limit_price(direction="short", quote=_QUOTE, bps_through_touch=_BPS)
    assert rec.replaced == [(AlpacaOrderId("alpaca-uuid-xyz"), expected_limit)]
    assert rec.written_back == [
        ("ORD-entry-1", expected_limit, "alpaca-new-uuid", "entry_window_reprice")
    ]
    assert rec.cancelled == []


async def test_long_entry_prices_through_the_ask() -> None:
    """A long (BUY) entry is escalated above the ask, not the bid."""
    rec = _Recorder(target=_target(side="buy", modification_count=0))
    await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    expected_limit = marketable_limit_price(direction="long", quote=_QUOTE, bps_through_touch=_BPS)
    assert rec.replaced == [(AlpacaOrderId("alpaca-uuid-xyz"), expected_limit)]


async def test_recorded_fill_skips_reprice_and_cancel() -> None:
    rec = _Recorder(target=_target(has_recorded_fills=True))
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.SKIPPED_FILLED
    assert rec.replaced == []
    assert rec.written_back == []
    assert rec.cancelled == []


async def test_synthetic_broker_id_is_retried() -> None:
    rec = _Recorder(target=_target(alpaca_order_id="alp-ORD-entry-1"))
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.replaced == []
    assert rec.cancelled == []


async def test_missing_target_returns_failed() -> None:
    rec = _Recorder(target=None)
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.replaced == []
    assert rec.cancelled == []


async def test_non_equity_limit_delegates_to_cancel() -> None:
    """A market / stop_limit / non-equity entry is not repriced — the terminal
    cancel handles it (ALP-738 equity-limit-only scope)."""
    rec = _Recorder(target=_target(is_equity_limit=False))
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["BRK-1"]
    assert rec.replaced == []
    assert rec.written_back == []


async def test_budget_exhausted_delegates_to_cancel() -> None:
    """Once modification_count reaches the reprice budget the loop stops chasing
    and cancels (with the ALP-739 no-fill alert)."""
    rec = _Recorder(target=_target(modification_count=2))
    outcome = await _repricer(rec, max_reprice_count=2).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["BRK-1"]
    assert rec.replaced == []


async def test_under_budget_still_reprices() -> None:
    """modification_count strictly below the budget still escalates."""
    rec = _Recorder(target=_target(modification_count=1))
    outcome = await _repricer(rec, max_reprice_count=2).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.REPRICED
    assert rec.cancelled == []
    assert len(rec.written_back) == 1


async def test_zero_budget_never_reprices() -> None:
    """max_reprice_count=0 disables repricing — restores ALP-737 cancel."""
    rec = _Recorder(target=_target(modification_count=0))
    outcome = await _repricer(rec, max_reprice_count=0).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["BRK-1"]
    assert rec.replaced == []


async def test_missing_quote_retries_without_cancelling() -> None:
    rec = _Recorder(target=_target(), quote=None)
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.replaced == []
    assert rec.written_back == []
    assert rec.cancelled == []


async def test_broker_replace_retryable_is_retried() -> None:
    rec = _Recorder(
        target=_target(),
        replace_result=BrokerReplaceResult(classification=BrokerReplaceClassification.RETRYABLE),
    )
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.FAILED
    assert rec.written_back == []
    assert rec.cancelled == []


async def test_broker_replace_rejected_falls_back_to_cancel() -> None:
    """A structurally-invalid replace (permanent 4xx) cannot escalate — cancel
    instead so the loop terminates with the no-fill alert."""
    rec = _Recorder(
        target=_target(),
        replace_result=BrokerReplaceResult(classification=BrokerReplaceClassification.REJECTED),
    )
    outcome = await _repricer(rec).handle(bracket=_bracket(), now=_NOW)

    assert outcome is EntryWindowDeadlineOutcome.CANCELLED
    assert rec.cancelled == ["BRK-1"]
    assert rec.written_back == []


@dataclass
class _SequentialRecorder:
    """Simulates the watcher loop across cycles: each successful reprice bumps
    ``modification_count`` (as the real writeback does); ``filled`` can flip to
    model the marketable limit filling between cycles."""

    modification_count: int = 0
    filled: bool = False
    outcomes: list[EntryWindowDeadlineOutcome] = field(default_factory=list)

    async def resolve_target(self, entry_order_id: str) -> RepriceTarget:
        del entry_order_id
        return _target(has_recorded_fills=self.filled, modification_count=self.modification_count)

    async def latest_quote(self, symbol: str) -> TouchQuote:
        del symbol
        return _QUOTE

    async def broker_replace(
        self, alpaca_order_id: AlpacaOrderId, new_limit: Price
    ) -> BrokerReplaceResult:
        del alpaca_order_id, new_limit
        return BrokerReplaceResult(
            classification=BrokerReplaceClassification.REPLACED,
            new_alpaca_order_id=AlpacaOrderId("alpaca-new-uuid"),
        )

    async def reprice_writeback(
        self, entry_order_id: str, new_limit: Price, new_alpaca_order_id: str, reason: str
    ) -> None:
        del entry_order_id, new_limit, new_alpaca_order_id, reason
        self.modification_count += 1

    async def cancel(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowDeadlineOutcome:
        del bracket, now
        return EntryWindowDeadlineOutcome.CANCELLED


def _sequential_repricer(rec: _SequentialRecorder) -> BrokerEntryWindowRepricer:
    return BrokerEntryWindowRepricer(
        resolve_target=rec.resolve_target,
        quote_source=rec,
        broker_replace=rec.broker_replace,
        reprice_writeback=rec.reprice_writeback,
        canceller=rec,
        max_reprice_count=2,
        bps_through_touch=_BPS,
    )


async def test_bounded_loop_reprices_then_cancels_when_budget_spent() -> None:
    """ALP-740 AC3: a patient limit past its deadline is repriced toward the
    market on each cycle, then — after the bounded retries — cancels (the
    no-fill alert fires off that terminal cancel)."""
    rec = _SequentialRecorder()
    repricer = _sequential_repricer(rec)
    bracket = _bracket()

    first = await repricer.handle(bracket=bracket, now=_NOW)  # mods 0 -> 1
    second = await repricer.handle(bracket=bracket, now=_NOW)  # mods 1 -> 2
    third = await repricer.handle(bracket=bracket, now=_NOW)  # mods 2 == budget -> cancel

    assert first is EntryWindowDeadlineOutcome.REPRICED
    assert second is EntryWindowDeadlineOutcome.REPRICED
    assert third is EntryWindowDeadlineOutcome.CANCELLED


async def test_bounded_loop_stops_when_marketable_limit_fills() -> None:
    """ALP-740 AC3: once the re-pegged marketable limit fills, the next cycle
    sees the recorded fill and stops (reconciliation activates the bracket) —
    no further reprice, no cancel."""
    rec = _SequentialRecorder()
    repricer = _sequential_repricer(rec)
    bracket = _bracket()

    first = await repricer.handle(bracket=bracket, now=_NOW)  # reprices to marketable
    rec.filled = True  # the marketable limit fills before the next cycle
    second = await repricer.handle(bracket=bracket, now=_NOW)

    assert first is EntryWindowDeadlineOutcome.REPRICED
    assert second is EntryWindowDeadlineOutcome.SKIPPED_FILLED


def test_broker_replace_result_requires_id_iff_replaced() -> None:
    import pytest

    with pytest.raises(ValueError, match="iff classification is REPLACED"):
        BrokerReplaceResult(classification=BrokerReplaceClassification.REPLACED)
    with pytest.raises(ValueError, match="iff classification is REPLACED"):
        BrokerReplaceResult(
            classification=BrokerReplaceClassification.RETRYABLE,
            new_alpaca_order_id=AlpacaOrderId("x"),
        )
