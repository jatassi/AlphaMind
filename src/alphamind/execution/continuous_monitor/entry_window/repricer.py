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
3. broker id still absent (un-routed — ``alpaca_order_id is None``, ALP-847) →
   ``FAILED`` (retry once acked).
4. not a repriceable equity limit, **or** the per-session reprice count has
   reached the reprice budget → delegate to the terminal
   :class:`EntryWindowCanceller` (cancel + ALP-739 no-fill alert). The loop bound
   is the count held in :class:`EntryWindowSessionMemory` (ALP-867): seeded once
   from the order row's durable ``modification_count`` so a process restart
   respects the budget already spent + projected, then incremented in-session per
   confirmed replace — because the order row's ``modification_count`` now lags
   until the pipeline projects the ``ENTRY_REPRICED`` events. So the escalation
   cannot chase the market indefinitely.
5. otherwise → fetch the live touch and price a marketable limit. If that price
   would invert the bracket geometry (the market moved past the analyst's
   take-profit / stop) → delegate to the terminal cancel. Otherwise
   cancel-and-replace at the broker, record the new id + bumped count in session
   memory, and append the append-only ``ENTRY_REPRICED`` event (no order-row
   writeback — ALP-867) → ``REPRICED`` (re-evaluated next cycle).

The loop always terminates: a successful reprice increments the session count
toward the budget (then step 4 cancels), a geometry-inverting market cancels, and
a successful marketable fill resolves to ``SKIPPED_FILLED`` next cycle. A *broker
error* on the replace (transient gateway failure, or a 404/422 that most likely
means the entry just filled during the round trip) returns ``FAILED`` so the entry
keeps resting and the next cycle retries — we never cancel off a broker error, so
a freshly-filled entry is resolved by the fill check rather than dissolved. A
missing quote likewise returns ``FAILED``.

Per parent issue ALP-123 § Pre-resolved decision (I) the continuous monitor
talks to the broker adapter directly rather than through an engine envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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
    EntryWindowSessionMemory,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    BracketRecord,
    PriceTrigger,
)

log = logging.getLogger(__name__)

# Provenance recorded on the order_modified activity-log entry per reprice.
_REPRICE_REASON = "entry_window_reprice"


@dataclass(frozen=True, slots=True)
class RepriceTarget:
    """What the repricer needs to decide an expired entry's fate.

    ``is_equity_limit`` gates the reprice path — only an equity ``LIMIT`` entry
    with a known long/short ``direction`` is escalated through the touch; anything
    else (a market / stop_limit entry, a non-equity instrument, or a directionless
    MLEG envelope) falls back to the terminal cancel, mirroring ALP-738's
    equity-limit-only scope. ``direction`` is the position direction the marketable
    pricing keys off (short → price the bid, long → the ask) — only meaningful when
    ``is_equity_limit``. ``has_recorded_fills`` reflects ``fill_records`` written by
    the fill-stream consumer ahead of reconciliation. ``alpaca_order_id`` is
    ``None`` for a not-yet-routed entry (ALP-847 deleted the synthetic ``alp-``
    placeholder) — the repricer retries rather than acting on a missing broker id.
    ``modification_count`` is the order row's durable reprice count; since ALP-867
    relocated the reprice writeback to the pipeline it lags between projections, so
    it is read **only to seed** :class:`EntryWindowSessionMemory` on first encounter
    — the in-session count is the live loop bound thereafter.
    """

    alpaca_order_id: AlpacaOrderId | None
    has_recorded_fills: bool
    is_equity_limit: bool
    ticker: str
    direction: Direction
    modification_count: int


# entry_order_id → the reprice target projection (None when the order is missing).
type RepriceTargetResolver = Callable[[str], Awaitable[RepriceTarget | None]]
# (broker order id, new marketable limit) → the new alpaca_order_id on a confirmed
# cancel-and-replace, or None on any failure (transient gateway / classified 4xx).
# We never distinguish "rejected" from "retryable": a 404/422 most likely means the
# marketable limit just filled, so the repricer retries and the next cycle's fill
# check resolves it — rather than cancelling off a broker error and risking a
# dissolve of a freshly-filled bracket (ALP-740 review). Geometry inversions are
# caught pre-flight, so they never reach the broker.
type BrokerReplace = Callable[[AlpacaOrderId, Price], Awaitable[AlpacaOrderId | None]]
# (entry_order_id, new_limit, new_alpaca_order_id, reason) → append the append-only
# ``ENTRY_REPRICED`` event (idempotent on its event_key). The monitor no longer writes
# the order row (ALP-867); the pipeline projects the event onto limit/id/mod-count +
# reservation. This is the only DB touch the reprice path makes, and it is an append —
# never an RMW on a shared row — so the SQLITE_BUSY_SNAPSHOT race stays unreachable.
type EntryRepriceEventAppend = Callable[[str, Price, str, str], Awaitable[None]]


def _leg_threshold(legs: tuple[BracketLeg, ...], leg_type: BracketLegType) -> float | None:
    """Return the price threshold of the first *leg_type* leg with a price trigger.

    ``None`` when the bracket carries no such leg, or its leg is a time/event
    trigger (no price level) — the geometry check then skips that side.
    """
    for leg in legs:
        if leg.leg_type is leg_type and isinstance(leg.trigger, PriceTrigger):
            return leg.trigger.threshold_usd
    return None


def _marketable_preserves_geometry(
    *, direction: Direction, new_limit: Price, protective_legs: tuple[BracketLeg, ...]
) -> bool:
    """True if *new_limit* keeps the bracket's entry/target/stop ordering valid.

    The repricer counterpart of ALP-738's ``entry_pricing._preserves_bracket_geometry``
    (which works off the ``OpenCommand``; this reads the persisted bracket's
    protective-leg thresholds). Repricing moves a short entry down toward the bid /
    a long entry up toward the ask; if the market has moved past the analyst's
    take-profit (or stop), the marketable price crosses it and inverts the bracket —
    a sign the thesis' edge is gone. The watcher then cancels rather than escalating
    into an inverted bracket (which the broker may reject, or worse accept and
    instantly round-trip). A missing take-profit / stop leg skips that side's check.
    """
    target = _leg_threshold(protective_legs, BracketLegType.TAKE_PROFIT)
    stop = _leg_threshold(protective_legs, BracketLegType.PRICE_STOP)
    limit = float(new_limit)
    if direction == "short":
        if target is not None and limit <= target:
            return False
        return stop is None or limit < stop
    if target is not None and limit >= target:
        return False
    return stop is None or limit > stop


@runtime_checkable
class EntryWindowDeadlineHandler(Protocol):
    """The seam the watcher cycle fires on an expired ``PENDING_ENTRY`` bracket."""

    async def handle(
        self, *, bracket: BracketRecord, now: datetime, reprice_memory: EntryWindowSessionMemory
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
    append_event: EntryRepriceEventAppend
    canceller: EntryWindowCanceller
    max_reprice_count: int
    bps_through_touch: float

    async def handle(
        self, *, bracket: BracketRecord, now: datetime, reprice_memory: EntryWindowSessionMemory
    ) -> EntryWindowDeadlineOutcome:
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
        if target.alpaca_order_id is None:
            log.warning(
                "entry_window: bracket %s entry not yet broker-routed (no broker id); retrying",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        # Seed the per-session reprice count from the order row's durable
        # ``modification_count`` on first encounter; the in-session count is the
        # loop bound thereafter (the row lags until the pipeline projects the
        # ``ENTRY_REPRICED`` events, ALP-867).
        reprice_memory.seed_if_absent(
            bracket.bracket_id, durable_reprice_count=target.modification_count
        )
        if (
            not target.is_equity_limit
            or reprice_memory.reprice_count(bracket.bracket_id) >= self.max_reprice_count
        ):
            # Not a repriceable equity limit, or the escalation budget is spent —
            # fall back to the terminal cancel (fires ALP-739's no-fill alert).
            return await self.canceller.cancel(
                bracket=bracket, now=now, reprice_memory=reprice_memory
            )
        return await self._reprice(
            bracket=bracket, target=target, now=now, reprice_memory=reprice_memory
        )

    async def _reprice(
        self,
        *,
        bracket: BracketRecord,
        target: RepriceTarget,
        now: datetime,
        reprice_memory: EntryWindowSessionMemory,
    ) -> EntryWindowDeadlineOutcome:
        # _reprice is reached only past the un-routed guard in ``handle`` — a
        # repriceable equity limit is, by construction, already broker-routed.
        assert target.alpaca_order_id is not None
        quote = await self.quote_source.latest_quote(target.ticker)
        if quote is None:
            log.warning(
                "entry_window: no quote for %s; cannot reprice bracket %s, retrying",
                target.ticker,
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        # marketable_limit_price is a pure Decimal computation over a validated
        # touch (AlpacaQuoteSource guarantees bid/ask > 0); a degenerate-input
        # raise is isolated + retried by the watcher cycle's per-bracket catch
        # (task._run_entry_window_cycle), so no local guard is needed here.
        new_limit = marketable_limit_price(
            direction=target.direction, quote=quote, bps_through_touch=self.bps_through_touch
        )
        if not _marketable_preserves_geometry(
            direction=target.direction,
            new_limit=new_limit,
            protective_legs=bracket.protective_legs,
        ):
            # The market moved past the analyst's target/stop — a marketable
            # escalation would invert the bracket. Cancel rather than chase an
            # inverted entry (fires ALP-739's no-fill alert).
            log.warning(
                "entry_window: marketable %s would invert bracket %s (market moved past "
                "target/stop); cancelling instead of repricing",
                new_limit,
                bracket.bracket_id,
            )
            return await self.canceller.cancel(
                bracket=bracket, now=now, reprice_memory=reprice_memory
            )
        new_alpaca_id = await self.broker_replace(target.alpaca_order_id, new_limit)
        if new_alpaca_id is None:
            # Transient gateway failure, or a 404/422 that most likely means the
            # entry just filled during the round trip. Retry: next cycle's fill
            # check resolves a fill to SKIPPED_FILLED rather than cancelling off a
            # broker error and risking a dissolve of a freshly-filled bracket.
            log.warning(
                "entry_window: broker replace not confirmed for bracket %s; retrying next cycle",
                bracket.bracket_id,
            )
            return EntryWindowDeadlineOutcome.FAILED
        # Record the new broker id + bumped count in session memory BEFORE the
        # durable append: the cancel-and-replace already happened at the broker, so
        # a terminal cancel this session must target the *new* id (the canceller
        # reads ``current_alpaca_order_id``) even if the append below fails. Then
        # append the append-only ``ENTRY_REPRICED`` event — the monitor writes NO
        # order row (ALP-867); the pipeline projects limit / id / modification_count
        # + the reservation from the event. This is the reprice path's only DB
        # touch, and it is an append, never an RMW on a shared row.
        new_count = reprice_memory.record_reprice(
            bracket.bracket_id, new_alpaca_order_id=new_alpaca_id
        )
        await self.append_event(bracket.entry_order_id, new_limit, new_alpaca_id, _REPRICE_REASON)
        log.info(
            "entry_window: repriced bracket %s entry to marketable %s (reprice #%d)",
            bracket.bracket_id,
            new_limit,
            new_count,
        )
        return EntryWindowDeadlineOutcome.REPRICED


__all__ = [
    "BrokerEntryWindowRepricer",
    "BrokerReplace",
    "EntryRepriceEventAppend",
    "EntryWindowDeadlineHandler",
    "RepriceTarget",
    "RepriceTargetResolver",
]
