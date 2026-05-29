"""Reprice/escalate an expired patient-retest entry toward the market (ALP-740).

The fire-action the watcher cycle (``task.py``) runs on a ``PENDING_ENTRY``
bracket past its ``entry_window_deadline``. ALP-737 only *cancelled* such an
entry; ALP-740 first tries to *reprice* it — re-peg the resting limit through
the touch (escalate to a marketable limit via ALP-738's
:func:`marketable_limit_price`) so an accepted thesis still gets a fill —
bounded and observable, and only then cancels.

:class:`BrokerEntryWindowRepricer` is the production
:class:`EntryWindowDeadlineHandler`. It resolves the entry state once and
branches:

1. order row missing → ``FAILED`` (retry).
2. a fill is already recorded → ``SKIPPED_FILLED`` (reconciliation activates the
   bracket — never reprice or cancel a filled entry).
3. broker id still synthetic (un-routed) → ``FAILED`` (retry once acked).
4. not a repriceable equity limit, **or** ``modification_count`` has reached the
   reprice budget → delegate to the terminal :class:`EntryWindowCanceller`
   (cancel + ALP-739 no-fill alert). ``modification_count`` is the loop bound:
   each successful reprice increments it (Alpaca cancel-and-replace), so the
   escalation cannot chase the market indefinitely.
5. otherwise → fetch the live touch, price a marketable limit, cancel-and-replace
   at the broker, and run the non-terminal Phase-2 reprice writeback
   (``persist_entry_window_reprice``) → ``REPRICED`` (re-evaluated next cycle).

A broker rejection of the replace (any classified 4xx — structurally invalid /
insufficient buying power) is non-retryable, so it falls back to the terminal
cancel; that, plus the ``modification_count`` budget, guarantees the loop always
terminates (fill, or cancel with the no-fill alert). A transient gateway failure
or a missing quote returns ``FAILED`` so the entry keeps resting and the next
cycle retries.

Per parent issue ALP-123 § Pre-resolved decision (I) the continuous monitor
talks to the broker adapter directly rather than through an engine envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from alphamind._kernel.ids import AlpacaOrderId
from alphamind._kernel.money import Price
from alphamind.commands.command_models import Direction
from alphamind.execution.broker_adapter.entry_pricing import (
    QuoteSource,
    marketable_limit_price,
)
from alphamind.execution.continuous_monitor.entry_window.canceller import (
    EntryWindowCanceller,
    EntryWindowDeadlineOutcome,
    _is_synthetic,
)
from alphamind.portfolio_state.records.orders import BracketRecord

log = logging.getLogger(__name__)

# Provenance recorded on the order_modified activity-log entry per reprice.
_REPRICE_REASON = "entry_window_reprice"


class BrokerReplaceClassification(Enum):
    """How the broker answered a cancel-and-replace of the resting entry."""

    REPLACED = "replaced"  # accepted; carries the new alpaca_order_id
    RETRYABLE = "retryable"  # transient gateway failure; retry next cycle
    REJECTED = "rejected"  # permanent 4xx (invalid / no buying power); cancel instead


@dataclass(frozen=True, slots=True)
class BrokerReplaceResult:
    """Outcome of a broker replace — the new id is present iff ``REPLACED``."""

    classification: BrokerReplaceClassification
    new_alpaca_order_id: AlpacaOrderId | None = None

    def __post_init__(self) -> None:
        is_replaced = self.classification is BrokerReplaceClassification.REPLACED
        if is_replaced != (self.new_alpaca_order_id is not None):
            msg = "new_alpaca_order_id must be set iff classification is REPLACED"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RepriceTarget:
    """What the repricer needs to decide an expired entry's fate.

    ``is_equity_limit`` gates the reprice path — only an equity ``LIMIT`` entry
    is escalated through the touch; anything else (a market / stop_limit entry,
    or a non-equity instrument) falls back to the terminal cancel, mirroring
    ALP-738's equity-limit-only scope. ``side`` collapses the entry direction to
    buy/sell (only read for equity limits). ``has_recorded_fills`` reflects
    ``fill_records`` written by the fill-stream consumer ahead of reconciliation.
    """

    alpaca_order_id: AlpacaOrderId
    has_recorded_fills: bool
    is_equity_limit: bool
    ticker: str
    side: Literal["buy", "sell"]
    modification_count: int


# entry_order_id → the reprice target projection (None when the order is missing).
type RepriceTargetResolver = Callable[[str], Awaitable[RepriceTarget | None]]
# (broker order id, new marketable limit) → how the broker answered the replace.
type BrokerReplace = Callable[[AlpacaOrderId, Price], Awaitable[BrokerReplaceResult]]
# (entry_order_id, new_limit, new_alpaca_order_id, reason) → run the Phase-2 reprice writeback.
type RepriceWriteback = Callable[[str, Price, str, str], Awaitable[None]]


@runtime_checkable
class EntryWindowDeadlineHandler(Protocol):
    """The seam the watcher cycle fires on an expired ``PENDING_ENTRY`` bracket."""

    async def handle(
        self, *, bracket: BracketRecord, now: datetime
    ) -> EntryWindowDeadlineOutcome: ...


@dataclass(frozen=True, slots=True)
class BrokerEntryWindowRepricer:
    """Production :class:`EntryWindowDeadlineHandler` — reprice, else cancel.

    Holds the injected seams so the broker round-trip, quote fetch, and DB
    reads/writes are all fakeable in tests without a live Alpaca client. The
    terminal cancel is delegated to ``canceller`` so the ALP-737 path is reused
    wholesale rather than duplicated.
    """

    resolve_target: RepriceTargetResolver
    quote_source: QuoteSource
    broker_replace: BrokerReplace
    reprice_writeback: RepriceWriteback
    canceller: EntryWindowCanceller
    max_reprice_count: int
    bps_through_touch: float

    async def handle(self, *, bracket: BracketRecord, now: datetime) -> EntryWindowDeadlineOutcome:
        target = await self.resolve_target(bracket.entry_order_id)
        if target is None:
            log.error(
                "entry_window: entry order %s for bracket %s not found; retrying",
                bracket.entry_order_id,
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        if target.has_recorded_fills:
            log.info(
                "entry_window: bracket %s entry has recorded fills; leaving for reconciliation",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.SKIPPED_FILLED
        if _is_synthetic(target.alpaca_order_id):
            log.warning(
                "entry_window: bracket %s entry %s not yet broker-routed; retrying",
                bracket.bracket_id,
                target.alpaca_order_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        if not target.is_equity_limit or target.modification_count >= self.max_reprice_count:
            # Not a repriceable equity limit, or the escalation budget is spent —
            # fall back to the terminal cancel (fires ALP-739's no-fill alert).
            return await self.canceller.cancel(bracket=bracket, now=now)
        return await self._reprice(bracket=bracket, target=target, now=now)

    async def _reprice(
        self, *, bracket: BracketRecord, target: RepriceTarget, now: datetime
    ) -> EntryWindowDeadlineOutcome:
        quote = await self.quote_source.latest_quote(target.ticker)
        if quote is None:
            log.warning(
                "entry_window: no quote for %s; cannot reprice bracket %s, retrying",
                target.ticker,
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        direction: Direction = "short" if target.side == "sell" else "long"
        try:
            new_limit = marketable_limit_price(
                direction=direction, quote=quote, bps_through_touch=self.bps_through_touch
            )
        except Exception:
            log.warning(
                "entry_window: could not price a marketable limit for %s; retrying bracket %s",
                target.ticker,
                bracket.bracket_id,
                exc_info=True,
            )
            return EntryWindowDeadlineOutcome.FAILED
        result = await self.broker_replace(target.alpaca_order_id, new_limit)
        if result.classification is BrokerReplaceClassification.RETRYABLE:
            log.warning(
                "entry_window: broker replace not confirmed for bracket %s; retrying next cycle",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        if result.classification is BrokerReplaceClassification.REJECTED:
            log.warning(
                "entry_window: broker rejected the reprice for bracket %s; cancelling instead",
                bracket.bracket_id,
            )
            return await self.canceller.cancel(bracket=bracket, now=now)
        # REPLACED — the post-init invariant guarantees the new id is present.
        assert result.new_alpaca_order_id is not None
        await self.reprice_writeback(
            bracket.entry_order_id, new_limit, result.new_alpaca_order_id, _REPRICE_REASON
        )
        log.info(
            "entry_window: repriced bracket %s entry to marketable %s (reprice #%d)",
            bracket.bracket_id,
            new_limit,
            target.modification_count + 1,
        )
        return EntryWindowDeadlineOutcome.REPRICED


__all__ = [
    "BrokerEntryWindowRepricer",
    "BrokerReplace",
    "BrokerReplaceClassification",
    "BrokerReplaceResult",
    "EntryWindowDeadlineHandler",
    "RepriceTarget",
    "RepriceTargetResolver",
    "RepriceWriteback",
]
